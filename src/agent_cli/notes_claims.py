"""Id claims on the notes remote (see ADR-0040).

Feature ids and initiative ids are global sequences. A local lock serializes only clones that
share one notes repo; two synced devices could both take 027 after 026. So the notes remote
decides, as a compare-and-swap:

- A claim is one commit whose only parent is the remote head just fetched, the pinned parent.
  It appends one line, carrying an operation id, to `records/claims.jsonl` at the notes repo
  root and changes nothing else. It is built with git plumbing on a scratch index, so the work
  tree, the index and the local branch never see it, and no local auto-sync commit rides along.
- Only a plain push that fast-forwards the remote branch from that parent grants the claim. A
  rejected push means another device won. The losing commit is dropped, never rebased and never
  merged, and the claimer starts again from a fresh fetch.
- A push whose outcome went unseen (a timeout, a dropped connection) is settled by fetching
  and looking for the operation id: found means granted, absent means try again.
- No reachable remote means no id. Allocation refuses rather than guess.
- A notes repo with no git remote at all is a single device, so nothing can compete: the id is
  claimed in a local ledger under the git dir (`LOCAL_CLAIMS`), never committed. Once a remote
  exists, every claim goes to it again, and the first claim commit there also carries the local
  claims the remote lacks, so other devices count the ids taken before the remote existed.

Unlike `notes_sync._publish`, nothing here rebases on a rejection: a rebased claim would be
a merged claim. The claim holds the notes sync lock, so a Stop sync never fetches, rebases or
pushes in the middle of one, and two harness clones sharing one notes repo take turns.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from . import core, feature_tree, notes_sync
from .core import AgentError
from .records.narrow import as_dict_or_none

CLAIMS_PATH = "records/claims.jsonl"
LOCAL_CLAIMS = "local-claims.jsonl"
KINDS = {"feature": 3, "initiative": 2}
ATTEMPTS = 5
CLAIM_BUDGET_S = 60.0
LOCK_WAIT_S = notes_sync.STOP_BUDGET_S + notes_sync.CLEANUP_RESERVE_S + 5.0
REJECTED = ("[rejected]", "fetch first", "non-fast-forward")


@dataclass(frozen=True)
class Claim:
    kind: str
    id: int
    slug: str
    op: str


def claim(root: Path, kind: str, slug: str, *, op: str | None = None) -> Claim:
    """Claim the next `kind` id on the notes remote for `slug`. Raises AgentError when the notes
    repo, its upstream or the remote is missing, or no claim won within `ATTEMPTS`."""
    if kind not in KINDS:
        raise AgentError(f"cannot claim a {kind!r} id; only {', '.join(KINDS)}")
    # The claim's budget from the start: a hook's 5 s across opening the repo and
    # reading its upstream ran out under load, `feature new` refused, and the tests that went on
    # to use the feature failed with "no feature with id 1".
    opened = notes_sync.open_repo(root, CLAIM_BUDGET_S)
    if isinstance(opened, notes_sync.Result):
        raise AgentError(
            "a new id is claimed on the notes remote, and this clone has no notes repo with git "
            "linked; `agent setup --notes <path>` links one"
        )
    repo, _, common = opened
    up = notes_sync.upstream_of(repo)
    if isinstance(up, str) and not no_remote(repo):
        raise AgentError(f"a new id is claimed on the notes remote, and the notes repo cannot reach it: {up}")
    try:
        with notes_sync.repo_lock(common, LOCK_WAIT_S):
            if isinstance(up, str):
                return _claim_local(root, common, kind, slug, op or uuid.uuid4().hex)
            repo.end = time.monotonic() + CLAIM_BUDGET_S
            return _claim(root, repo, common, up, kind, slug, op or uuid.uuid4().hex)
    except TimeoutError:
        raise AgentError("the notes sync lock stayed busy, so no id was claimed; try again") from None


def no_remote(repo: notes_sync.Repo) -> bool:
    """True only when git answers that the notes repo has no remote at all. A remote that exists,
    tracked or not, or a git call that did not answer, is not this case."""
    r = repo.git("remote")
    return r is not None and r.returncode == 0 and not (r.stdout or "").strip()


def _local_ledger(common: Path) -> Path:
    return common / notes_sync.SYNC_DIR / LOCAL_CLAIMS


def _local_records(common: Path) -> list[dict[str, Any]]:
    path = _local_ledger(common)
    return _records(path.read_text(encoding="utf-8"), "local ledger") if path.is_file() else []


def _claim_local(root: Path, common: Path, kind: str, slug: str, op: str) -> Claim:
    records = _local_records(common)
    for rec in records:
        if rec.get("op") == op:
            if rec["kind"] != kind or rec.get("slug") != slug:
                raise AgentError(f"operation {op} already claimed {rec['kind']} {rec.get('slug')!r}")
            return Claim(kind, int(rec["id"]), slug, op)
    number = _next_id(root, [], records, kind)
    path = _local_ledger(common)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(_record(op, kind, number, slug), sort_keys=True) + "\n")
    return Claim(kind, number, slug, op)


def _claim(
    root: Path, repo: notes_sync.Repo, common: Path, up: tuple[str, str], kind: str, slug: str, op: str
) -> Claim:
    remote, branch = up
    local = _local_records(common)
    unseen = ""
    maybe_landed = False
    for _ in range(ATTEMPTS):
        head, why = _fetch(repo, remote, branch)
        if head is None:
            if maybe_landed:
                raise AgentError(
                    f"claim {op} was pushed without an answer and the notes remote then stopped "
                    f"answering ({why}). It may have landed; at worst one id goes unused, never twice."
                )
            raise AgentError(f"the notes remote is unreachable ({why}); no id is allocated without it")
        text = _claims_text(repo, head)
        records = _records(text, head)
        for rec in records:
            if rec.get("op") == op:
                if rec["kind"] != kind or rec.get("slug") != slug:
                    raise AgentError(
                        f"operation {op} already claimed {rec['kind']} {rec.get('slug')!r}, not {kind} "
                        f"{slug!r}; an operation id names one request"
                    )
                return Claim(kind, int(rec["id"]), slug, op)
        folders = _remote_folders(repo, head)
        if kind == "feature":
            _check_slug(slug, folders, records)
        number = _next_id(root, folders, records + local, kind)
        commit = _commit(repo, common, head, _carry(text, records, local), _record(op, kind, number, slug))
        push = _push(repo, remote, commit, branch)
        if push is not None and push.returncode == 0:
            return Claim(kind, number, slug, op)
        said = "" if push is None else f"{push.stdout}\n{push.stderr}"
        unseen = "" if any(w in said for w in REJECTED) else notes_sync.reason(push)
        maybe_landed = maybe_landed or bool(unseen)
    raise AgentError(
        f"no {kind} id was claimed in {ATTEMPTS} attempts"
        + (f"; the last push failed: {unseen}" if unseen else "; another device won each time")
    )


def _carry(text: str, records: list[dict[str, Any]], local: list[dict[str, Any]]) -> str:
    """`text` with each local claim the remote does not hold yet, so the claim commit publishes
    the ids taken before the remote existed and other devices count them."""
    ops = {r.get("op") for r in records}
    held = {(r["kind"], int(r["id"])) for r in records}
    missing = [r for r in local if r.get("op") not in ops and (r["kind"], int(r["id"])) not in held]
    if not missing:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "".join(json.dumps(r, sort_keys=True) + "\n" for r in missing)


def _fetch(repo: notes_sync.Repo, remote: str, branch: str) -> tuple[str | None, str]:
    r = repo.git("fetch", "--quiet", "--no-tags", remote, branch, cap=notes_sync.FETCH_CAP_S)
    if r is None or r.returncode:
        return None, f"fetch failed: {notes_sync.reason(r)}"
    head = repo.out("rev-parse", "--verify", "FETCH_HEAD^{commit}")
    return (head, "") if head else (None, "fetch wrote no FETCH_HEAD")


def _push(
    repo: notes_sync.Repo, remote: str, commit: str, branch: str
) -> subprocess.CompletedProcess[str] | None:
    """A plain push of the claim commit: no `+`, no `--force`, so the remote takes it only as a
    fast-forward from the pinned parent."""
    return repo.git("push", "--quiet", "--porcelain", remote, f"{commit}:{branch}", cap=notes_sync.PUSH_CAP_S)


def _git(repo: notes_sync.Repo, *args: str) -> str:
    r = repo.git(*args)
    if r is None or r.returncode:
        raise AgentError(
            f"git {args[0]} failed in the notes repo while claiming an id: {notes_sync.reason(r)}"
        )
    out = cast("object", r.stdout)  # a reader thread that failed to decode leaves None
    if not isinstance(out, str):
        raise AgentError(f"git {args[0]} in the notes repo while claiming an id returned no output")
    return out


def remote_fetch(repo: notes_sync.Repo, remote: str, branch: str) -> tuple[str | None, str]:
    """Shared pinned-parent fetch for numeric ids and installation labels."""
    return _fetch(repo, remote, branch)


def remote_push(
    repo: notes_sync.Repo, remote: str, commit: str, branch: str
) -> subprocess.CompletedProcess[str] | None:
    """Shared plain fast-forward push; its caller settles any unknown outcome."""
    return _push(repo, remote, commit, branch)


def ledger_git(repo: notes_sync.Repo, *args: str) -> str:
    """Checked Git plumbing for shared remote ledgers."""
    return _git(repo, *args)


def _claims_text(repo: notes_sync.Repo, head: str) -> str:
    return ledger_text(repo, head, CLAIMS_PATH)


def ledger_text(repo: notes_sync.Repo, head: str, path: str) -> str:
    """Read a remote ledger without touching the checkout or its index."""
    if not _git(repo, "ls-tree", head, "--", path).strip():
        return ""
    return _git(repo, "cat-file", "blob", f"{head}:{path}")


def _records(text: str, head: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = as_dict_or_none(json.loads(line))
        except ValueError:
            rec = None
        if rec is None or rec.get("kind") not in KINDS or type(rec.get("id")) is not int:
            raise AgentError(
                f"{CLAIMS_PATH} line {n} at {head[:12]} is not a claim; only the claim tool writes "
                "that file, so fix it on the notes remote before any id is allocated"
            )
        out.append(rec)
    return out


def _remote_folders(repo: notes_sync.Repo, head: str) -> list[str]:
    listing = _git(
        repo,
        "ls-tree",
        "-r",
        "-d",
        "-z",
        "--name-only",
        head,
        "--",
        feature_tree.FEATURES_DIR,
        feature_tree.INITIATIVES_DIR,
    )
    return [p for p in listing.split("\0") if p]


def _check_slug(slug: str, folders: list[str], records: list[dict[str, Any]]) -> None:
    """Refuse a feature slug the fetched head already holds, as a folder or as a competing claim."""
    held = feature_tree.feature_slugs_in_paths(folders)
    if slug in held:
        raise AgentError(f"slug {slug!r} already used by {held[slug]} on the notes remote")
    for rec in records:
        if rec["kind"] == "feature" and rec.get("slug") == slug:
            raise AgentError(
                f"slug {slug!r} already claimed on the notes remote as feature {int(rec['id']):03d}"
            )


def _next_id(root: Path, folders: list[str], records: list[dict[str, Any]], kind: str) -> int:
    """One past the highest id of `kind` anywhere: in the claims, in the folders at the fetched
    head (features from before claims existed), and in this vault (folders not synced yet)."""
    remote_features, remote_initiatives = feature_tree.ids_in_paths(folders)
    local = feature_tree.walk(root)
    if kind == "feature":
        taken = remote_features | local.feature_ids
    else:
        taken = remote_initiatives | {i.id for i in local.initiatives}
    taken |= {int(r["id"]) for r in records if r["kind"] == kind}
    return max(taken, default=0) + 1


def _record(op: str, kind: str, number: int, slug: str) -> dict[str, Any]:
    return {
        "op": op,
        "kind": kind,
        "id": number,
        "slug": slug,
        "device": socket.gethostname(),
        "at": core.iso(core.now()),
    }


def _commit(repo: notes_sync.Repo, common: Path, head: str, text: str, record: dict[str, Any]) -> str:
    """The claim commit: `head`'s tree with one line appended to the claims, `head` its only
    parent. A scratch index keeps the real index and the work tree out of it."""
    number = f"{record['id']:0{KINDS[record['kind']]}d}"
    subject = f"notes: claim {record['kind']} {number} {record['slug']}"
    body = f"Operation {record['op']} on {record['device']}. Written by the claim tool only (ADR-0040)."
    return ledger_commit(repo, common, head, text, record, CLAIMS_PATH, subject, body)


def ledger_commit(
    repo: notes_sync.Repo,
    common: Path,
    head: str,
    text: str,
    record: dict[str, Any],
    path: str,
    subject: str,
    body: str,
) -> str:
    """Append to one ledger on the pinned parent, leaving local work and index alone."""
    if text and not text.endswith("\n"):
        text += "\n"
    text += json.dumps(record, sort_keys=True) + "\n"
    scratch = common / notes_sync.SYNC_DIR
    scratch.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    blob_file, index = scratch / f"claim-{token}.jsonl", scratch / f"claim-{token}.index"
    saved = repo.env
    try:
        blob_file.write_text(text, encoding="utf-8", newline="\n")
        blob = _git(repo, "hash-object", "-w", "--no-filters", str(blob_file)).strip()
        repo.env = {**saved, "GIT_INDEX_FILE": str(index)}
        _git(repo, "read-tree", head)
        _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        tree = _git(repo, "write-tree").strip()
        return _git(repo, "commit-tree", tree, "-p", head, "-m", subject, "-m", body).strip()
    finally:
        repo.env = saved
        blob_file.unlink(missing_ok=True)
        index.unlink(missing_ok=True)
