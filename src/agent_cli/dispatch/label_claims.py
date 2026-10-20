"""Reserve dispatch labels on the notes remote before allocating dispatch records.

The ledger is separate from feature/initiative id claims. Cached successful ownership allows
later allocations offline; first use must fetch and win a pinned-parent fast-forward push. A
notes repo with no remote at all reserves the label locally, and the first allocation after a
remote is added claims it there.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .. import features, frontmatter, notes_claims, notes_sync
from ..core import AgentError, atomic_write, iso, now
from ..records.narrow import as_dict_or_none
from . import machine
from .ids import TAG_RE

LABELS_PATH = "records/labels.jsonl"


def records(text: str, head: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    owners: dict[str, str] = {}
    operations: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = as_dict_or_none(json.loads(line))
        except ValueError:
            record = None
        label = record.get("label") if record is not None else None
        token = record.get("install") if record is not None else None
        op = record.get("op") if record is not None else None
        if (
            record is None
            or record.get("kind") != "label"
            or not isinstance(label, str)
            or not TAG_RE.fullmatch(label)
            or not isinstance(token, str)
            or len(token) < 16
            or not isinstance(op, str)
            or not op
        ):
            raise AgentError(f"{LABELS_PATH} line {number} at {head[:12]} is not a label claim")
        if op in operations or (label in owners and owners[label] != token):
            raise AgentError(f"{LABELS_PATH} has duplicate operations or conflicting owners for {label!r}")
        owners[label] = token
        operations.add(op)
        result.append(record)
    return result


def cached(root: Path, label: str) -> dict[str, Any] | None:
    install = machine.read_install(root)
    if install is None:
        return None
    claim = as_dict_or_none(install.get("label_claim"))
    if (
        claim is not None
        and claim.get("label") == label
        and claim.get("install") == install["token"]
        and isinstance(claim.get("op"), str)
        and claim["op"]
        and isinstance(claim.get("head"), str)
        and claim["head"]
    ):
        return claim
    return None


def _foreign(label: str) -> AgentError:
    return AgentError(
        f"dispatch label {label!r} belongs to another installation; choose a new `machine:` "
        "in agent.local.yaml. Existing ids and handoffs are unchanged"
    )


def _check_meta(meta: dict[str, Any], label: str, token: str, path: str) -> None:
    from .ids import parse_id

    parsed = parse_id(str(meta.get("id") or ""))
    if meta.get("machine") == label or (parsed is not None and parsed.tag == label):
        if meta.get("install") != token:
            raise AgentError(
                f"{_foreign(label).message}; legacy handoff {path} has a foreign or missing install"
            )


def check_local_handoffs(root: Path, label: str, token: str) -> None:
    for feature in features.load_all(root):
        for path in sorted((feature.dir / "handoffs").glob("*.md")):
            meta, _ = frontmatter.load(path)
            _check_meta(meta, label, token, str(path))


def _check_remote_handoffs(repo: notes_sync.Repo, head: str, label: str, token: str) -> None:
    listing = notes_claims.ledger_git(repo, "ls-tree", "-r", "-z", "--name-only", head)
    for path in listing.split("\0"):
        if "/handoffs/" not in path or not path.endswith(".md"):
            continue
        meta, _ = frontmatter.split(notes_claims.ledger_git(repo, "cat-file", "blob", f"{head}:{path}"))
        _check_meta(meta, label, token, path)


def require(root: Path, label: str) -> dict[str, Any]:
    """Verify cached ownership or claim the label before any dispatch record is allocated."""
    if not TAG_RE.fullmatch(label):
        raise AgentError("a label claim needs a valid machine label")
    install = machine.install(root)
    check_local_handoffs(root, label, install["token"])
    receipt = cached(root, label)
    if receipt is not None and not receipt.get("local"):
        return receipt
    # The claim's own budget from the start, not a hook's 5 s: opening the repo and reading its
    # upstream is about seven git calls, and under load (load average 25-40) they
    # spent 5 s in total, so a configured upstream read as "has no upstream" and about 15 tests
    # failed at setup. Each call keeps its own `LOCAL_CAP_S` cap.
    opened = notes_sync.open_repo(root, notes_claims.CLAIM_BUDGET_S)
    if isinstance(opened, notes_sync.Result):
        raise AgentError(
            "first dispatch needs a linked notes repo and its reachable remote to reserve the machine label"
        )
    repo, _, common = opened
    upstream = notes_sync.upstream_of(repo)
    if isinstance(upstream, str) and not notes_claims.no_remote(repo):
        raise AgentError(f"first dispatch cannot reserve its machine label: {upstream}")
    try:
        with notes_sync.repo_lock(common, notes_claims.LOCK_WAIT_S):
            # Another allocation in this installation may have completed while we waited. A
            # receipt taken with no remote is claimed on the remote once there is one.
            receipt = cached(root, label)
            if receipt is not None and (isinstance(upstream, str) or not receipt.get("local")):
                return receipt
            if isinstance(upstream, str):
                receipt = {
                    "op": uuid.uuid4().hex,
                    "kind": "label",
                    "label": label,
                    "install": install["token"],
                    "at": iso(now()),
                    "head": "local",
                    "local": True,
                }
            else:
                repo.end = time.monotonic() + notes_claims.CLAIM_BUDGET_S
                receipt = _reserve(repo, common, upstream, label, install["token"], uuid.uuid4().hex)
            install["label_claim"] = receipt
            atomic_write(root / machine.INSTALL_FILE, json.dumps(install, sort_keys=True) + "\n")
            return receipt
    except TimeoutError:
        raise AgentError("the notes sync lock stayed busy, so no machine label was reserved; retry") from None


def _reserve(
    repo: notes_sync.Repo,
    common: Path,
    upstream: tuple[str, str],
    label: str,
    token: str,
    op: str,
) -> dict[str, Any]:
    remote, branch = upstream
    uncertain = False
    for _ in range(notes_claims.ATTEMPTS):
        head, why = notes_claims.remote_fetch(repo, remote, branch)
        if head is None:
            suffix = (
                " The previous push may have landed; retry uses the same install ownership."
                if uncertain
                else ""
            )
            raise AgentError(f"notes remote unreachable ({why}); no dispatch is allocated.{suffix}")
        text = notes_claims.ledger_text(repo, head, LABELS_PATH)
        claims = records(text, head)
        for claim in claims:
            if claim["op"] == op and (claim["label"] != label or claim["install"] != token):
                raise AgentError(f"operation {op} already reserved another label request")
        held = next((c for c in claims if c["label"] == label), None)
        _check_remote_handoffs(repo, head, label, token)
        if held:
            if held["install"] != token:
                raise _foreign(label)
            return {**held, "head": head}
        record = {"op": op, "kind": "label", "label": label, "install": token, "at": iso(now())}
        commit = notes_claims.ledger_commit(
            repo,
            common,
            head,
            text,
            record,
            LABELS_PATH,
            f"notes: reserve dispatch label {label}",
            f"Operation {op}. A label belongs to one installation; existing ids never change.",
        )
        pushed = notes_claims.remote_push(repo, remote, commit, branch)
        if pushed is not None and pushed.returncode == 0:
            return {**record, "head": commit}
        said = "" if pushed is None else f"{pushed.stdout}\n{pushed.stderr}"
        uncertain |= not any(reason in said for reason in notes_claims.REJECTED)
    raise AgentError("no machine label was reserved after competing or unanswered pushes; retry")


def inspect(root: Path, label: str) -> dict[str, Any]:
    """Read-only local claim/cache diagnostics. Does not fetch, mint a token or write a cache."""
    install = machine.read_install(root)
    receipt = cached(root, label)
    result: dict[str, Any] = {
        "label": label,
        "verified": receipt is not None,
        "source": ("local" if receipt.get("local") else "cache") if receipt else "unverified",
    }
    if install:
        try:
            check_local_handoffs(root, label, install["token"])
        except AgentError as exc:
            result.update(verified=False, problem=exc.message)
    opened = notes_sync.open_repo(root, notes_sync.LOCAL_CAP_S)
    if isinstance(opened, notes_sync.Result):
        return result
    repo, _, _ = opened
    head = repo.out("rev-parse", "--verify", "HEAD^{commit}")
    if not head:
        return {**result, "problem": "cannot inspect the notes label ledger"}
    try:
        claims = records(notes_claims.ledger_text(repo, head, LABELS_PATH), head)
    except AgentError as exc:
        return {**result, "verified": False, "problem": exc.message}
    held = next((c for c in claims if c["label"] == label), None)
    if held and (not install or held["install"] != install["token"]):
        result.update(verified=False, problem=_foreign(label).message)
    elif held and receipt is None:
        result.update(
            source="local-ledger",
            problem="claim exists but cache is unverified; next real allocation rechecks remote ownership",
        )
    return result
