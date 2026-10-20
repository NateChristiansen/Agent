"""Dispatch runtime state: the `.agent/dispatches/<id>.json` records, the ids, paths and
branch names derived from a feature + repo, the display helpers every other module trims text
with, and the list/show views over the records."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import features as features_mod
from .. import repos as repos_mod
from .. import status
from ..core import AgentError, NotFound, agent_path, atomic_write, is_alive, json_files, reads_strict
from ..records.dispatch import denials_of, result_of, thread_of
from ..records.narrow import as_dict, as_dict_or_none, as_list, dict_list, dig, str_list
from . import holdings_view, ids

DISPATCHES_DIR = agent_path(Path(), "dispatches")
HANDOFFS_DIR = "handoffs"
WORKER_CONTRACT = Path("harness") / "worker-contract.md"

STATES = ("queued", "running", "done", "blocked", "question", "failed", "cancelled", "abandoned")
TERMINAL_STATES = ("done", "blocked", "question", "failed", "cancelled", "abandoned")
ATTENTION_STATES = ("question", "blocked", "failed")
# States that are closed the moment they are reached: the owner did it, so nothing waits on them.
CLOSED_STATES = ("cancelled", "abandoned")
DISPATCH_KINDS = ("work", "probe")
RETURN_STATUSES = ("done", "blocked", "question")
DEFAULT_NOTIFY = ["chat", "macos"]
DEFAULT_BUDGET_USD = 2.0
MAX_ATTEMPTS = 2
CANCEL_GRACE_SECONDS = 5.0
# A cold dependency install on a large repo can take several minutes. This timeout is generous on purpose. Setup failing is reported, never fatal.
WORKTREE_SETUP_TIMEOUT_S = 900.0
# How often the supervisor checks the steer inbox, the cancel flag, and its deadlines.
# How long it holds a worker open after a `question` before giving up and finalizing.
POLL_SECONDS = 0.2
QUESTION_IDLE_SECONDS = 600.0
# Bound the loop from its first result regardless of pending messages. The explicit question hold
# extends this deadline intentionally.
WATCHDOG_SECONDS = 30.0
TYPE_TAG = "handoff"

TAG_RE = ids.TAG_RE
ID_RE = ids.ID_RE
HANDOFF_FILE_RE = ids.HANDOFF_FILE_RE


# ------------------------------------------------------------------ state files


def dispatches_dir(root: Path) -> Path:
    return root / DISPATCHES_DIR


def file(root: Path, dispatch_id: str, suffix: str) -> Path:
    return dispatches_dir(root) / f"{dispatch_id}{suffix}"


def load(root: Path, dispatch_id: str) -> dict[str, Any]:
    dispatch_id = dispatch_id.strip()
    if not ID_RE.match(dispatch_id):
        raise AgentError(f"{dispatch_id!r} is not a dispatch id (expected {ids.ID_FORMS})")
    path = file(root, dispatch_id, ".json")
    if not path.exists():
        raise NotFound(f"no dispatch {dispatch_id}; see `agent dispatch list --all`")
    try:
        record = as_dict_or_none(json.loads(path.read_text(encoding="utf-8")))
        if record is None:
            raise AgentError("dispatch record must be an object")
        record.setdefault("record_revision", 0)
        record.setdefault("terminal_committed", False)
        return record
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path} is not valid JSON: {exc}") from exc


def save(root: Path, d: dict[str, Any]) -> dict[str, Any]:
    from .allocation import GUARD_TIMEOUT_S
    from .guards import area_guard

    root.mkdir(parents=True, exist_ok=True)
    path = file(root, d["id"], ".json")
    if not ID_RE.fullmatch(str(d["id"])):
        raise AgentError("invalid dispatch id")
    # Resolve the persisted identity before selecting a lock, then check it again inside.
    current = load(root, d["id"]) if path.exists() else None
    identity = (current or d).get("short")
    area = f"dispatch-records:{identity}" if identity else f"dispatch:{d['id']}"
    with area_guard(root, area, timeout=GUARD_TIMEOUT_S):
        current = load(root, d["id"]) if path.exists() else None
        if current is not None and any(current.get(k) != d.get(k) for k in ("id", "repo", "short")):
            raise AgentError("dispatch repository identity cannot change")
        if current is not None and "record_revision" not in d:
            raise AgentError("dispatch creation collision; reload before applying an update")
        revision = d.get("record_revision", 0)
        actual = current.get("record_revision", 0) if current is not None else 0
        if type(revision) is not int or revision < 0 or type(actual) is not int or actual < 0:
            raise AgentError("invalid dispatch record revision")
        if revision != actual or (current is None and revision != 0):
            raise AgentError("stale dispatch record; reload before applying this update")
        updated = {**d, "record_revision": actual + 1}
        text = json.dumps(updated, indent=2, default=str) + "\n"
        # A write that changes what this dispatch owes (`live.dispatch_obligation`: a worker's
        # question or block, an unread verdict, an unresolved branch, a cleanup) records a
        # watched snapshot intent first, as `sessions.update_record` does for a manager's:
        # a writer killed or stalled before it publishes is followed.
        from ..vault import live

        intent = None
        if live.dispatch_obligation(current or {}) != live.dispatch_obligation(updated):
            intent = live.begin_intent(root, watch=True)
        if current is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".dispatch-create-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    raise AgentError(
                        "dispatch creation collision; reload before applying an update"
                    ) from None
            finally:
                os.unlink(temporary)
        else:
            atomic_write(path, text)
        live.fact_persisted(intent)
        d["record_revision"] = actual + 1
    return d


def load_all(root: Path) -> list[dict[str, Any]]:
    base = dispatches_dir(root)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in json_files(base):
        if not ID_RE.match(path.stem):
            continue
        try:
            record = as_dict_or_none(json.loads(path.read_text(encoding="utf-8")))
            if record is not None:
                record.setdefault("record_revision", 0)
                out.append(record)
            elif reads_strict():
                raise ValueError(f"dispatch record {path.name} is not an object")
        except json.JSONDecodeError:
            if reads_strict():  # a partial view would be trusted (core.strict_reads)
                raise
            continue
    return out


def for_feature(root: Path, feature: features_mod.Feature) -> list[dict[str, Any]]:
    return [d for d in load_all(root) if d.get("feature") == feature.id]


# ------------------------------------------------------------------ ids, paths, names


def handoffs_dir(feature: features_mod.Feature) -> Path:
    return feature.dir / HANDOFFS_DIR


def allocate_seq(root: Path, feature: features_mod.Feature, tag: str | None) -> int:
    """Next sequence for this feature and machine tag (see ADR-0036): one past the highest seen in
    handoff files or dispatch json carrying the same tag (`None`: legacy untagged ids), so a
    lost `.agent/` and a lost handoff each still keep ids unique."""
    seqs = [0]
    hdir = handoffs_dir(feature)
    if hdir.exists():
        for p in hdir.glob("*.md"):
            m = HANDOFF_FILE_RE.match(p.name)
            if m and m.group("tag") == tag:
                seqs.append(int(m.group("seq")))
    for p in (
        dispatches_dir(root).glob(f"{feature.number}-*.json")
        if dispatches_dir(root).exists()
        else list[Path]()
    ):
        m = ID_RE.match(p.stem)
        if m and m.group("tag") == tag:
            seqs.append(int(m.group("seq")))
    return max(seqs) + 1


def make_id(feature: features_mod.Feature, seq: int, tag: str | None) -> str:
    return ids.format_id(feature.id, seq, tag)


def allocate(root: Path, feature: features_mod.Feature, tag: str | None) -> tuple[int, str]:
    """`(seq, id)` for the next dispatch on this feature from this machine tag."""
    seq = allocate_seq(root, feature, tag)
    return seq, make_id(feature, seq, tag)


def make_nonce(dispatch_id: str) -> str:
    return f"{dispatch_id}-{secrets.token_hex(2)}"


def _suffix(seq: int, tag: str | None) -> str:
    """`-<tag>-SS` for a tagged dispatch, the first one included, so each machine derives its
    own branch and worktree path. Legacy untagged: none for the first, `-SS` after."""
    if tag:
        return f"-{tag}-{seq:02d}"
    return "" if seq == 1 else f"-{seq:02d}"


def worktree_path(
    root: Path, repo: repos_mod.Repo, feature: features_mod.Feature, seq: int, tag: str | None
) -> Path:
    """`<worktrees_dir>/<NNN>-<slug>-<tag>-<seq>`; a null worktrees_dir falls back to
    `<clones parent>/_worktrees/<short>` (doctor warns about the null)."""
    base = repo.abs_worktrees(root)
    if base is None:
        base = repo.abs_path(root).parent / repos_mod.WORKTREES_PARENT / repo.short.lower()
    return base / f"{feature.number}-{feature.slug}{_suffix(seq, tag)}"


def branch_name(
    feature: features_mod.Feature, seq: int, repo: repos_mod.Repo | None = None, *, tag: str | None
) -> str:
    """The branch a dispatch works on, from the repo's `dispatch.branch_template`. A
    worker's branch becomes a PR in that repo, so it is named the way that repo's own branches
    are; `repos.DEFAULT_BRANCH_TEMPLATE` applies when the repo says nothing, and `repo=None`
    (a caller with no registry, such as a test) gets the same default. `{suffix}` renders
    `-<tag>-SS` for a tagged dispatch (see ADR-0036)."""
    from ..tickets import ticket_slug, tickets_of

    tickets = [s for s in (ticket_slug(t) for t in tickets_of(feature.meta)) if s]
    template = repo.branch_template if repo is not None else repos_mod.DEFAULT_BRANCH_TEMPLATE
    return template.format(
        number=feature.number,
        slug=feature.slug,
        ticket=tickets[0] if tickets else "",
        suffix=_suffix(seq, tag),
    )


def handoff_path(feature: features_mod.Feature, repo: repos_mod.Repo, seq: int, tag: str | None) -> Path:
    """`<tag>-SS-<short>.md`, or legacy `SS-<short>.md`."""
    prefix = f"{tag}-" if tag else ""
    return handoffs_dir(feature) / f"{prefix}{seq:02d}-{repo.short.lower()}.md"


def prompt_text(root: Path, d: dict[str, Any]) -> str:
    path = file(root, d["id"], ".prompt.md")
    if path.exists():
        return path.read_text(encoding="utf-8").rstrip("\n")
    first = next((e for e in thread_of(d) if e.get("from") == "manager"), None)
    return str(first.get("text") if first else "").rstrip("\n")


def one_line(text: str) -> str:
    return " ".join(str(text).split())


DISPLAY_SUMMARY_CHARS = 400


def display_summary(text: str, limit: int = DISPLAY_SUMMARY_CHARS) -> str:
    """Shorten a display copy for notifications, inboxes and list/log rows. Keep the full summary in
    the dispatch record and handoff; length never rejects a worker return.
    """
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def stored_envelope(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    """A transport envelope as the record keeps it: the result text is dropped once it parsed
    into `structured_output`, since it is the same return as a JSON string. Text that did not
    parse is kept, for whoever reads why the return failed."""
    if envelope is None or envelope.get("structured_output") is None:
        return envelope
    return {k: v for k, v in envelope.items() if k != "result"}


def store_result(d: dict[str, Any], result: dict[str, Any] | None, *, returned: bool) -> None:
    """Set `d["result"]`, the one persisted copy of the latest return. When it is the worker's
    own return (`returned`), the latest envelope gives up its `structured_output` and names
    `result` as where it went, so `evaluate` still reads it on a later collect."""
    d["result"] = result
    envelope = as_dict_or_none(d.get("last_result"))
    if returned and result is not None and envelope is not None and "structured_output" in envelope:
        kept = {k: v for k, v in envelope.items() if k not in ("structured_output", "result")}
        d["last_result"] = {**kept, "return_in": "result"}


def envelope_return(d: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any] | None:
    """The return an envelope carried: its own `structured_output`, or the record's `result`
    when `store_result` moved it there."""
    if envelope.get("return_in") == "result" and "structured_output" not in envelope:
        return as_dict_or_none(d.get("result"))
    return as_dict_or_none(envelope.get("structured_output"))


def pending_outcome_of(d: dict[str, Any]) -> dict[str, Any] | None:
    """The outcome held while cleanup is pending. New records keep its return once, in
    `d["result"]`; older ones carry their own copy, which wins."""
    pending = as_dict_or_none(d.get("pending_outcome"))
    if pending is None:
        return None
    return {"result": d.get("result"), **pending}


def permission_denials(d: dict[str, Any]) -> list[str]:
    """Every command this dispatch was refused, as one display string each.

    `d["permission_denials"]` is what the transport itself refused, accumulated across attempts by
    the supervisor loop (one envelope carries only its own attempt's). A return written before
    workers stopped reporting `commands` may also hold rows marked `DENIED`; those are still read,
    so an old record shows what it always did. `dispatch show` and the handoff both read this,
    each line through `shown_denials` or `shown`.
    """
    from ..hooks.decision_log import shown as shown_command

    out: list[str] = []
    for shown in shown_denials(denials_of(d)):
        text = shown if isinstance(shown, str) else shown.get("command") or shown.get("tool_name")
        out.append(str(text or "(unnamed tool)"))
    for cmd in as_list(d.get("legacy_denials")):
        out.append(shown_command(one_line(str(cmd))))
    for row in dict_list(result_of(d).get("commands")):
        if "DENIED" in f"{row.get('exit', '')} {row.get('status', '')}".upper():
            out.append(shown_command(one_line(str(row.get("cmd") or "(unnamed command)"))))
    return out


def shown_denials(value: object) -> list[dict[str, Any] | str]:
    """`permission_denials` as a record stores and a view prints them: a tool name through `shown_tool`,
    a Bash command through `decision_log.shown` (endpoints and credentials withheld), a Codex request
    method, nothing else. Raw or already shown, the result is the same, so old raw records render too."""
    from ..hooks.decision_log import redact_all, shown
    from ..permissions import REDACTED_MCP_TOOL, REDACTED_TOOL, shown_tool

    out: list[dict[str, Any] | str] = []
    for entry in as_list(value) or ([] if value in (None, []) else [value]):
        raw = as_dict_or_none(entry)
        if raw is None:
            text = one_line(str(entry))
            out.append(text if "/" in text and text.replace("/", "").isalpha() else shown(text))
            continue
        name, use_id = raw.get("tool_name"), raw.get("tool_use_id")
        safe = name if name in (REDACTED_TOOL, REDACTED_MCP_TOOL) else shown_tool(str(name or ""))
        kept: dict[str, Any] = {"tool_name": safe}
        if isinstance(use_id, str) and use_id and shown_tool(use_id) == use_id == redact_all(use_id):
            kept["tool_use_id"] = use_id
        if name == "Bash":
            command = dig(raw, "tool_input", "command") or raw.get("command") or ""
            kept["command"] = shown(one_line(str(command)))
        out.append({**kept, "input_omitted": True})
    return out


def permission_events(d: dict[str, Any]) -> list[dict[str, Any]]:
    """Return first-hand can_use_tool events recorded while the call is blocked. Keep these separate
    from transport and worker permission_denials; differences expose recording gaps. Events include
    the rule reason, subagent and tool-use id.
    """
    return dict_list(d.get("permission_events"))


def denied_permission_events(d: dict[str, Any]) -> list[dict[str, Any]]:
    """The subset the callback refused. Deny-only today: an allow event is a `git -C` read
    the generated rules could not reach. Worth counting, not worth a line each."""
    return [e for e in permission_events(d) if e.get("decision") == "deny"]


def first_line(text: str) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()
    return "(empty prompt)"


# A render failure must not fail the dispatch. `status.render_quietly` reports it and continues.
render = status.render_quietly


# ------------------------------------------------------------------ public: list / show


def list_(root: Path, *, feature: str | None = None, all_: bool = False) -> list[dict[str, Any]]:
    feat_id = features_mod.resolve(root, feature).id if feature else None
    out: list[dict[str, Any]] = []
    closed_features = closed_feature_ids(root)
    for d in load_all(root):
        if feat_id is not None and d.get("feature") != feat_id:
            continue
        if not all_ and not is_live(d, closed_features):
            continue
        out.append(summarize(d))
    return out


def is_actioned(d: dict[str, Any]) -> bool:
    """The owner acted on this dispatch after it last finished.

    Compared against `finished` rather than cleared on resume. A resumed worker finishes again
    later than the old `actioned_at`, so it counts as waiting again with no reset to forget."""
    at, finished = d.get("actioned_at"), d.get("finished")
    if not at:
        return False
    if not finished:
        return True
    # As instants, not strings: two stamps written in different offsets (another machine, or a
    # landing's UTC journal) ordered wrongly as text.
    try:
        return datetime.fromisoformat(str(at)) >= datetime.fromisoformat(str(finished))
    except ValueError, TypeError:
        return str(at) >= str(finished)


def is_probe(d: dict[str, Any]) -> bool:
    """A probe (`kind="probe"`): its return is the result, never an ask. A record from before
    `kind` existed is work."""
    return d.get("kind") == "probe"


def closed_feature_ids(root: Path) -> set[int]:
    """Features that are done or abandoned. A feature with no plan.md is not closed."""
    return {f.id for f in features_mod.load_all(root) if not f.is_open}


def is_live(d: dict[str, Any], closed_features: set[int] | None = None) -> bool:
    """Show in-flight work and finished work still awaiting action. Probes and finished dispatches on
    closed features owe nothing. All surfaces pass `closed_features` so their counts agree.
    """
    if d.get("verification_cleanup_pending"):
        return True
    if is_probe(d):
        return False
    if closed_features and d.get("state") not in ("queued", "running"):
        try:
            if int(d.get("feature")) in closed_features:  # type: ignore[arg-type]
                return False
        except TypeError, ValueError:
            pass
    state = d.get("state")
    if state in ("queued", "running"):
        return True
    if state in CLOSED_STATES:
        return False
    return not is_actioned(d)


def verification_summary(d: dict[str, Any]) -> dict[str, Any]:
    """Display the recorded assessment without rerunning checks or changing history.

    Worker returns, terminal state and requested exemptions cannot mint an assessment.
    This is a historical observation, not a fresh validation of evidence or the worktree.
    """
    value = as_dict(d.get("verification_result"))
    status, raw_reasons = value.get("status"), value.get("reasons")
    items, reasons = as_list(raw_reasons), str_list(raw_reasons)
    if (
        not isinstance(status, str)
        or status not in {"verified", "exempt", "unverified", "failed", "incomplete"}
        or not isinstance(raw_reasons, list)
        or len(reasons) != len(items)
    ):
        return {
            "status": "unverified",
            "reasons": [
                "supervisor verification assessment is missing or invalid; worker returns are not evidence"
            ],
        }
    return {"status": status, "reasons": reasons}


def summarize(d: dict[str, Any]) -> dict[str, Any]:
    result = result_of(d)
    return {
        "id": d["id"],
        "feature": d["feature"],
        "repo": d["short"],
        "state": d["state"],
        "attempt": d["attempt"],
        "cost_usd": round(float(d.get("cost_usd") or 0.0), 4),
        # what the worker has burned so far. The only figure there is while it runs: dollars
        # arrive with the result message.
        "tokens": int(d.get("tokens") or 0),
        "budget_usd": d["budget_usd"],
        "model": d.get("model"),
        "effort": d.get("effort"),
        "started": d.get("started"),
        "finished": d.get("finished"),
        "chat_pending": bool(d.get("chat_pending")),
        "cleanup_pending": bool(d.get("verification_cleanup_pending")),
        "cancel_unverified_pids": d.get("cancel_unverified_pids") or [],
        "cancel_inventory_error": d.get("cancel_inventory_error") or "",
        "verification_result": verification_summary(d),
        "actioned_at": d.get("actioned_at"),
        "summary": display_summary(one_line(result.get("summary") or d.get("error") or "")),
        "branch": d["branch"],
        "worktree": d["worktree"],
        "handoff": d["handoff"],
        "supervisor_alive": is_alive(d.get("supervisor_pid"))
        if d.get("state") in ("queued", "running")
        else None,
    }


def show(root: Path, *, dispatch_id: str, consume: bool = True, caller: str | None = None) -> dict[str, Any]:
    """The full record; clears `chat_pending` (the manager has now seen it). With typed notices
    only their exact recipient clears it, and consumes only the generation returned. On a record
    from before `verdict_read`, only a reader allowed to mark its verdict read clears it
    (`verdicts.may_clear_pending`). `caller` is the reading session, else the server's own.
    `consume=False` is `dispatch_read`'s pure read, which writes nothing."""
    from .verdicts import may_clear_pending

    d = load(root, dispatch_id)
    if not consume:
        pass
    elif d.get("notice_outbox"):
        from .notice_producers import show as show_notices

        d = show_notices(root, d, caller)
    elif d.get("chat_pending") and may_clear_pending(root, d, caller):
        d["chat_pending"] = False
        save(root, d)
    d.update(supervisor_alive=is_alive(d.get("supervisor_pid")), holds=holdings_view.status(root, d))
    return d


def release_repo_lease(root: Path, d: dict[str, Any]) -> None:
    """Best-effort release of the dispatch's repo slot and worktree lease, keyed by `dispatch:<id>`
    rather than pid. Manager and detached supervisor can both release them. Failures never crash
    finalization or cancellation. Keep holdings while cleanup is unconfirmed or an owned process
    may still run.
    """
    from .holdings import release

    release(root, d)
