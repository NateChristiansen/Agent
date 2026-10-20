"""Claude Code usage for one local day, from every project folder whose transcripts are in scope.

Every folder under `~/.claude/projects` is a candidate and the transcript's own `cwd` decides,
so dispatch worktrees, product worktrees and eval vaults count without knowing their slugs. The
preserved copies under `.agent/raw/*/transcripts` are read after the live tree. A request is
counted once by `requestId` across both, as `journal/tokens.py` does: one API call writes one
assistant record per content block, each repeating the call's usage.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..core import agent_path
from ..records.narrow import as_dict, as_dict_or_none, as_int, as_str
from .scope import Scope
from .tally import Tally, day_start, local_date, records, touched_since

DRAFTER = "journal-drafter"


def projects_dir(home: Path) -> Path:
    return home / ".claude" / "projects"


def transcript_files(root: Path, home: Path, since: float) -> list[Path]:
    """Live transcripts and their subagents first, then the preserved copies; each written to
    on or after `since`. Raises OSError when the projects folder exists but cannot be listed."""
    files: list[Path] = []
    projects = projects_dir(home)
    if projects.exists():
        for project in sorted(p for p in projects.iterdir() if p.is_dir()):
            files += sorted(project.glob("*.jsonl"))
            files += sorted(project.glob("*/subagents/*.jsonl"))
    raw = agent_path(root, "raw")
    if raw.is_dir():
        files += sorted(raw.glob("*/transcripts/*.jsonl"))
    return [path for path in files if touched_since(path, since)]


def _agent_type(path: Path) -> str:
    """The subagent's type from Claude Code's `<agent>.meta.json` beside its transcript."""
    try:
        meta = json.loads(path.with_name(f"{path.stem}.meta.json").read_text(encoding="utf-8"))
    except OSError, ValueError:
        return ""
    return as_str(as_dict(meta).get("agentType"))


def request_class(kind: str, dispatch: str | None, sidechain: bool, agent: str) -> str:
    if dispatch:
        return dispatch
    if kind == "eval":
        return "eval"
    if sidechain and agent == DRAFTER:
        return "drafter"
    if kind == "vault":
        return "subagent" if sidechain else "manager"
    return "other"


def claude_usage(root: Path, day: date, scope: Scope, dispatches: dict[str, str], home: Path) -> Tally:
    projects = projects_dir(home)
    tally = Tally(absent="" if projects.exists() else str(projects))
    try:
        files = transcript_files(root, home, day_start(day))
    except OSError as exc:
        return Tally(status="unavailable", reason=f"cannot list Claude transcripts: {exc}")
    seen: set[str] = set()
    for path in files:
        in_subagents = path.parent.name == "subagents"
        meta_agent: str | None = None
        kind: str | None = None
        counted = False
        for record in records(path, tally):
            if kind is None and isinstance(record.get("cwd"), str):
                kind = scope.kind(record["cwd"])
                if kind is None:
                    break
            if record.get("type") != "assistant" or kind is None:
                continue
            request_id = as_str(record.get("requestId"))
            if not request_id or request_id in seen:
                continue
            if local_date(record.get("timestamp")) != day:
                continue
            message = as_dict_or_none(record.get("message"))
            usage = as_dict_or_none(message.get("usage")) if message else None
            if message is None or usage is None:
                continue
            seen.add(request_id)
            sidechain = in_subagents or record.get("isSidechain") is True
            agent = as_str(record.get("attributionAgent"))
            if sidechain and not agent and in_subagents:
                if meta_agent is None:
                    meta_agent = _agent_type(path)
                agent = meta_agent
            cls = request_class(kind, dispatches.get(as_str(record.get("sessionId"))), sidechain, agent)
            tally.add(
                cls,
                as_str(message.get("model")) or "unknown",
                as_int(usage.get("input_tokens")),
                as_int(usage.get("output_tokens")),
                as_int(usage.get("cache_creation_input_tokens")),
                as_int(usage.get("cache_read_input_tokens")),
            )
            counted = True
        tally.files += counted
    return tally.finish()
