# ruff: noqa
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
CLOSED_STATES = ("cancelled", "abandoned")
DISPATCH_KINDS = ("work", "probe")
RETURN_STATUSES = ("done", "blocked", "question")
DEFAULT_NOTIFY = ["chat", "macos"]
DEFAULT_BUDGET_USD = 2.0
MAX_ATTEMPTS = 2
CANCEL_GRACE_SECONDS = 5.0
WORKTREE_SETUP_TIMEOUT_S = 900.0
POLL_SECONDS = 0.2
QUESTION_IDLE_SECONDS = 600.0
WATCHDOG_SECONDS = 30.0
TYPE_TAG = "handoff"
TAG_RE = ids.TAG_RE
ID_RE = ids.ID_RE
HANDOFF_FILE_RE = ids.HANDOFF_FILE_RE


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
    current = load(root, d["id"]) if path.exists() else None
    identity = (current or d).get("short")
    area = f"dispatch-records:{identity}" if identity else f"dispatch:{d['id']}"
    with area_guard(root, area, timeout=GUARD_TIMEOUT_S):
        current = load(root, d["id"]) if path.exists() else None
        if current is not None and any((current.get(k) != d.get(k) for k in ("id", "repo", "short"))):
            raise AgentError("dispatch repository identity cannot change")
        if current is not None and "record_revision" not in d:
            raise AgentError("dispatch creation collision; reload before applying an update")
        revision = d.get("record_revision", 0)
        actual = current.get("record_revision", 0) if current is not None else 0
        if type(revision) is not int or revision < 0 or type(actual) is not int or (actual < 0):
            raise AgentError("invalid dispatch record revision")
        if revision != actual or (current is None and revision != 0):
            raise AgentError("stale dispatch record; reload before applying this update")
        updated = {**d, "record_revision": actual + 1}
        text = json.dumps(updated, indent=2, default=str) + "\n"
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
            if reads_strict():
                raise
            continue
    return out


def for_feature(root: Path, feature: features_mod.Feature) -> list[dict[str, Any]]:
    return [d for d in load_all(root) if d.get("feature") == feature.id]


def handoffs_dir(feature: features_mod.Feature) -> Path:
    return feature.dir / HANDOFFS_DIR


def allocate_seq(root: Path, feature: features_mod.Feature, tag: str | None) -> int:
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
    seq = allocate_seq(root, feature, tag)
    return (seq, make_id(feature, seq, tag))


def make_nonce(dispatch_id: str) -> str:
    return f"{dispatch_id}-{secrets.token_hex(2)}"


def _suffix(seq: int, tag: str | None) -> str:
    if tag:
        return f"-{tag}-{seq:02d}"
    return "" if seq == 1 else f"-{seq:02d}"


def worktree_path(
    root: Path, repo: repos_mod.Repo, feature: features_mod.Feature, seq: int, tag: str | None
) -> Path:
    base = repo.abs_worktrees(root)
    if base is None:
        base = repo.abs_path(root).parent / repos_mod.WORKTREES_PARENT / repo.short.lower()
    return base / f"{feature.number}-{feature.slug}{_suffix(seq, tag)}"


def branch_name(
    feature: features_mod.Feature, seq: int, repo: repos_mod.Repo | None = None, *, tag: str | None
) -> str:
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
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def stored_envelope(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    if envelope is None or envelope.get("structured_output") is None:
        return envelope
    return {k: v for k, v in envelope.items() if k != "result"}


def store_result(d: dict[str, Any], result: dict[str, Any] | None, *, returned: bool) -> None:
    d["result"] = result
    envelope = as_dict_or_none(d.get("last_result"))
    if returned and result is not None and (envelope is not None) and ("structured_output" in envelope):
        kept = {k: v for k, v in envelope.items() if k not in ("structured_output", "result")}
        d["last_result"] = {**kept, "return_in": "result"}


def envelope_return(d: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any] | None:
    if envelope.get("return_in") == "result" and "structured_output" not in envelope:
        return as_dict_or_none(d.get("result"))
    return as_dict_or_none(envelope.get("structured_output"))


def pending_outcome_of(d: dict[str, Any]) -> dict[str, Any] | None:
    pending = as_dict_or_none(d.get("pending_outcome"))
    if pending is None:
        return None
    return {"result": d.get("result"), **pending}


def permission_denials(d: dict[str, Any]) -> list[str]:
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
    from ..hooks.decision_log import redact_all, shown
    from ..permissions import REDACTED_MCP_TOOL, REDACTED_TOOL, shown_tool

    out: list[dict[str, Any] | str] = []
    for entry in as_list(value) or ([] if value in (None, []) else [value]):
        raw = as_dict_or_none(entry)
        if raw is None:
            text = one_line(str(entry))
            out.append(text if "/" in text and text.replace("/", "").isalpha() else shown(text))
            continue
        name, use_id = (raw.get("tool_name"), raw.get("tool_use_id"))
        safe = name if name in (REDACTED_TOOL, REDACTED_MCP_TOOL) else shown_tool(str(name or ""))
        kept: dict[str, Any] = {"tool_name": safe}
        if isinstance(use_id, str) and use_id and (shown_tool(use_id) == use_id == redact_all(use_id)):
            kept["tool_use_id"] = use_id
        if name == "Bash":
            command = dig(raw, "tool_input", "command") or raw.get("command") or ""
            kept["command"] = shown(one_line(str(command)))
        out.append({**kept, "input_omitted": True})
    return out


def permission_events(d: dict[str, Any]) -> list[dict[str, Any]]:
    return dict_list(d.get("permission_events"))


def denied_permission_events(d: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in permission_events(d) if e.get("decision") == "deny"]


def first_line(text: str) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()
    return "(empty prompt)"


render = status.render_quietly


def list_(root: Path, *, feature: str | None = None, all_: bool = False) -> list[dict[str, Any]]:
    feat_id = features_mod.resolve(root, feature).id if feature else None
    out: list[dict[str, Any]] = []
    closed_features = closed_feature_ids(root)
    for d in load_all(root):
        if feat_id is not None and d.get("feature") != feat_id:
            continue
        if not all_ and (not is_live(d, closed_features)):
            continue
        out.append(summarize(d))
    return out


def is_actioned(d: dict[str, Any]) -> bool:
    at, finished = (d.get("actioned_at"), d.get("finished"))
    if not at:
        return False
    if not finished:
        return True
    try:
        return datetime.fromisoformat(str(at)) >= datetime.fromisoformat(str(finished))
    except ValueError, TypeError:
        return str(at) >= str(finished)


def is_probe(d: dict[str, Any]) -> bool:
    return d.get("kind") == "probe"


def closed_feature_ids(root: Path) -> set[int]:
    return {f.id for f in features_mod.load_all(root) if not f.is_open}


def is_live(d: dict[str, Any], closed_features: set[int] | None = None) -> bool:
    if d.get("verification_cleanup_pending"):
        return True
    if is_probe(d):
        return False
    if closed_features and d.get("state") not in ("queued", "running"):
        try:
            if int(d.get("feature")) in closed_features:
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
    value = as_dict(d.get("verification_result"))
    status, raw_reasons = (value.get("status"), value.get("reasons"))
    items, reasons = (as_list(raw_reasons), str_list(raw_reasons))
    if (
        not isinstance(status, str)
        or status not in {"verified", "exempt", "unverified", "failed", "incomplete"}
        or (not isinstance(raw_reasons, list))
        or (len(reasons) != len(items))
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
    from .holdings import release

    release(root, d)
