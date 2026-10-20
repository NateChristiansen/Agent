# ruff: noqa
from __future__ import annotations
import json
import os
import re
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

try:
    import fcntl
except ImportError:
    fcntl = None
from . import features as features_mod
from . import sessions
from .core import AgentError, agent_path, append_line, iso, now
from .records.narrow import as_dict, as_dict_or_none, as_str, dig

KINDS = ("prompt", "stop", "subagent-stop", "session-start", "session-end")
EVENT_TO_KIND = {
    "UserPromptSubmit": "prompt",
    "Stop": "stop",
    "SubagentStop": "subagent-stop",
    "SessionStart": "session-start",
    "SessionEnd": "session-end",
}
LIFTED = (
    "session_id",
    "hook_event_name",
    "cwd",
    "transcript_path",
    "prompt",
    "last_assistant_message",
    "agent_id",
    "agent_type",
)
UNASSIGNED = agent_path(Path(), "raw", "unassigned")


def _utf8_strings(value: Any) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            part = match.group()
            return (
                chr(65536 + (ord(part[0]) - 55296) * 1024 + ord(part[1]) - 56320) if len(part) == 2 else "�"
            )

        return re.sub("[\\ud800-\\udbff][\\udc00-\\udfff]|[\\ud800-\\udfff]", replace, value)
    if isinstance(value, dict):
        return {_utf8_strings(k): _utf8_strings(v) for k, v in cast(dict[str, Any], value).items()}
    if isinstance(value, list):
        return [_utf8_strings(v) for v in cast(list[Any], value)]
    return value


@contextmanager
def raw_lock(root: Path) -> Generator[None]:
    path = agent_path(root, "raw", ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)


def parse_payload(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        data = as_dict_or_none(json.loads(text))
    except json.JSONDecodeError as exc:
        raise AgentError(f"stdin is not valid JSON: {exc}") from exc
    if data is None:
        raise AgentError("stdin JSON must be an object")
    return data


def capture(root: Path, kind: str, payload: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise AgentError(f"kind must be one of {', '.join(KINDS)}; got {kind!r}")
    sid = session_id or payload.get("session_id")
    feature_info = None
    dispatch_id = None
    if sid:
        record = sessions.get(root, sid)
        if record and record.get("feature"):
            feature_info = record["feature"]
        if record and record.get("dispatch_id"):
            dispatch_id = str(record["dispatch_id"])
    dispatch_id = dispatch_id or os.environ.get("AGENT_DISPATCH_ID") or None
    ts = now()
    lifted = {key: payload.get(key) for key in LIFTED}
    extra = {k: v for k, v in payload.items() if k not in LIFTED}
    record_out = {
        "ts": iso(ts),
        "kind": kind,
        "session_id": sid,
        "feature": feature_info["id"] if feature_info else None,
        **{k: lifted[k] for k in LIFTED if k != "session_id"},
        "dispatch_id": dispatch_id,
        "extra": extra,
    }
    day = ts.strftime("%Y-%m-%d")
    if feature_info:
        try:
            feature = features_mod.resolve(root, str(feature_info["id"]))
            target = feature.raw_dir / f"{day}.jsonl"
        except AgentError:
            target = root / UNASSIGNED / f"{day}.jsonl"
    else:
        target = root / UNASSIGNED / f"{day}.jsonl"
    with raw_lock(root):
        new_unassigned = target.parent == root / UNASSIGNED and (not target.exists())
        append_line(target, json.dumps(_utf8_strings(record_out), ensure_ascii=False) + "\n", mode=384)
    if new_unassigned:
        from .status import render_quietly

        render_quietly(root)
    return {"written": target.relative_to(root).as_posix(), "feature": record_out["feature"], "kind": kind}


def adopt(root: Path, feature: features_mod.Feature, session_id: str) -> dict[str, Any]:
    with raw_lock(root):
        return _adopt_locked(root, feature, session_id)


def _adopt_locked(root: Path, feature: features_mod.Feature, session_id: str) -> dict[str, Any]:
    base = root / UNASSIGNED
    moved = 0
    touched: list[str] = []
    for path in sorted(base.glob("*.jsonl")) if base.exists() else []:
        keep: list[str] = []
        moved_here = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                keep.append(line)
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                keep.append(line)
                continue
            if rec.get("session_id") == session_id:
                rec["feature"] = feature.id
                append_line(feature.raw_dir / path.name, json.dumps(rec, ensure_ascii=False) + "\n", mode=384)
                moved_here += 1
            else:
                keep.append(line)
        if moved_here:
            from .core import atomic_write

            if keep:
                atomic_write(path, "\n".join(keep) + "\n")
                os.chmod(path, 384)
            else:
                path.unlink()
            touched.append(path.name)
            moved += moved_here
    return {"moved": moved, "files": touched, "feature": feature.id}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


TRANSCRIPTS_DIR = "transcripts"


def preserve_transcripts(root: Path, feature: features_mod.Feature) -> list[Path]:
    dest_dir = feature.raw_dir / TRANSCRIPTS_DIR
    seen: set[str] = set()
    copied: list[Path] = []
    if not feature.raw_dir.exists():
        return copied
    for path in sorted(feature.raw_dir.glob("*.jsonl")):
        for record in read_jsonl(path):
            for tp in (record.get("transcript_path"), as_str(dig(record, "extra", "agent_transcript_path"))):
                if not tp or tp in seen:
                    continue
                seen.add(tp)
                src = Path(tp)
                if not src.exists():
                    continue
                dest = dest_dir / src.name
                if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
                    continue
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                copied.append(dest)
    return copied
