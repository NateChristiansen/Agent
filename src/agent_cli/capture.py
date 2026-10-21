"""Raw session capture: hook payloads → .agent/raw/NNN-slug/<date>.jsonl or .agent/raw/unassigned/."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

try:
    import fcntl
except ImportError:  # Windows: the CLI runs there, but advisory file locks are unavailable
    fcntl = None  # type: ignore[assignment]

from . import features as features_mod
from . import sessions
from .core import AgentError, agent_path, append_line, iso, now
from .records.narrow import as_dict, as_dict_or_none

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
    """Keep capture JSON readable by strict UTF-8 readers, including nested object keys."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            part = match.group()
            return (
                chr(0x10000 + (ord(part[0]) - 0xD800) * 0x400 + ord(part[1]) - 0xDC00)
                if len(part) == 2
                else "\ufffd"
            )

        return re.sub(r"[\ud800-\udbff][\udc00-\udfff]|[\ud800-\udfff]", replace, value)
    if isinstance(value, dict):
        return {_utf8_strings(k): _utf8_strings(v) for k, v in cast(dict[str, Any], value).items()}
    if isinstance(value, list):
        return [_utf8_strings(v) for v in cast(list[Any], value)]
    return value


@contextmanager
def raw_lock(root: Path) -> Generator[None]:
    """Serialize hook appends with capture rehomes and adoption on this vault."""
    path = agent_path(root, "raw", ".lock")
    if fcntl is None:
        from . import win_lock

        fd = win_lock.open_lock_file(root, (*agent_path(Path(), "raw").parts, ".lock"))
        try:
            with win_lock.exclusive(fd):
                yield
        finally:
            os.close(fd)
        return
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
    # A worker's hooks run with AGENT_DISPATCH_ID exported by the supervisor. The session
    # record (pre-mapped by `dispatch new`) is the primary source, the env the backstop.
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
    from .hooks import retention, retention_job

    record_out["retention_reports"] = retention.reports(record_out)
    day = ts.strftime("%Y-%m-%d")
    if feature_info:
        try:
            feature = features_mod.resolve(root, str(feature_info["id"]))
            target = feature.raw_dir / f"{day}.jsonl"
        except AgentError:
            target = root / UNASSIGNED / f"{day}.jsonl"
    else:
        target = root / UNASSIGNED / f"{day}.jsonl"
    # raw captures hold full prompts and replies. Owner-only, unlike ordinary vault files.
    with raw_lock(root):
        # Mapping may have changed while this append waited behind adoption.
        current = sessions.get(root, str(sid)) if sid else None
        mapped = as_dict(as_dict(current).get("feature"))
        if mapped:
            try:
                target = features_mod.resolve(root, str(mapped["id"])).raw_dir / f"{day}.jsonl"
                record_out["feature"] = mapped["id"]
            except AgentError:
                pass
        # Under the lock, so two first captures of the day cannot both see the file absent.
        new_unassigned = target.parent == root / UNASSIGNED and not target.exists()
        append_line(target, json.dumps(_utf8_strings(record_out), ensure_ascii=False) + "\n", mode=0o600)
    try:
        retention_job.enqueue(root, record_out)
    except OSError:
        # The raw event remains the durable recovery reference if launch fails.
        pass
    if new_unassigned:
        # STATUS.md lists the unassigned files and hashes their names, so a new one made the
        # render stale and doctor failed minutes after a fresh setup, before any session was
        # mapped (fresh Windows clone). A new file appears once a day at most.
        from .status import render_quietly

        render_quietly(root)
    return {"written": target.relative_to(root).as_posix(), "feature": record_out["feature"], "kind": kind}


def adopt(root: Path, feature: features_mod.Feature, session_id: str) -> dict[str, Any]:
    """Re-home every .agent/raw/unassigned record for a session into the feature's
    .agent/raw/<slug>/ directory, for captures made before `feature use` ran. Records for
    other sessions stay put."""
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
                append_line(
                    feature.raw_dir / path.name, json.dumps(rec, ensure_ascii=False) + "\n", mode=0o600
                )
                moved_here += 1
                from .hooks import retention_job

                try:
                    retention_job.enqueue(root, rec)
                except OSError:
                    pass
            else:
                keep.append(line)
        if moved_here:
            from .core import atomic_write

            if keep:
                atomic_write(path, "\n".join(keep) + "\n")
                os.chmod(path, 0o600)
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

REDACTED_RECORD_TYPES = frozenset({"atis-latch", "bridge-session"})
"""Claude Code writes its own session-auth records into a transcript, and a preserved copy keeps
them forever. `atis-latch` carries a bearer-shaped session token; `bridge-session` carries the account and organization
UUIDs. Neither is ours, neither is any use to a retro, and a credential does not belong
in an artifact handed to an AI tool. Dropped on preserve, and `raw redact` clears the ones already
on disk."""


def redact_jsonl(path: Path) -> int:
    """Drop every record whose `type` is in `REDACTED_RECORD_TYPES`, in place. Returns the
    number of records dropped.

    Whole records only. A line that is not JSON, or is JSON that is not an object, is written
    through untouched — a transcript is evidence, and the redaction is a credential filter, not a
    rewrite. The file's mode and mtime survive, which matters because `preserve_transcripts`
    skips a copy whose mtime already matches its source and a redaction would otherwise make every
    preserved transcript look stale."""
    dropped = 0
    st = path.stat()
    tmp = path.with_name(path.name + ".redact")
    try:
        with path.open(encoding="utf-8") as fh, tmp.open("w", encoding="utf-8") as out:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    try:
                        record: Any = json.loads(stripped)
                    except json.JSONDecodeError:
                        record = None
                    if as_dict(record).get("type") in REDACTED_RECORD_TYPES:
                        dropped += 1
                        continue
                out.write(line)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    if not dropped:
        tmp.unlink(missing_ok=True)
        return 0
    tmp.replace(path)
    os.chmod(path, st.st_mode & 0o7777)
    os.utime(path, (st.st_atime, st.st_mtime))
    return dropped


def redact_preserved(root: Path) -> dict[str, Any]:
    """Redact every preserved transcript under `.agent/raw/*/transcripts/`.

    Idempotent: a second run finds nothing and reports zeros. This is the catch-up for
    transcripts preserved before the redaction existed; new ones are handled at preserve time."""
    raw = agent_path(root, "raw")
    files: list[dict[str, Any]] = []
    dropped = 0
    scanned = 0
    for path in sorted(raw.glob(f"*/{TRANSCRIPTS_DIR}/*.jsonl")):
        scanned += 1
        count = redact_jsonl(path)
        if count:
            dropped += count
            files.append({"path": path.relative_to(root).as_posix(), "dropped": count})
    return {"scanned": scanned, "changed": len(files), "dropped": dropped, "files": files}


def preserve_transcripts(root: Path, feature: features_mod.Feature) -> list[Path]:
    """Refresh a feature's evidence. Full copies have stable identities across adoption.

    The return stays a list of newly copied paths; missing-source outcomes live in the
    reports linked by raw captures. Stop and worker capture also enqueue preservation.
    """
    import shutil
    import time

    from .hooks import retention, retention_job

    copied: list[Path] = []
    with retention_job.lock(root, "writer.lock", retention_job.JOB_MAX):
        records = [record for path in sorted(feature.raw_dir.glob("*.jsonl")) for record in read_jsonl(path)]
        names: dict[str, set[str]] = {}
        for record in records:
            for source in retention.references(record):
                names.setdefault(Path(source).name, set()).add(retention.key(record, source))
        for record in records:
            refreshed = retention.retain(root, record, deadline=time.monotonic() + retention_job.JOB_MAX)
            for source in retention.references(record):
                ident = retention.key(record, source)
                canonical = root / retention.BASE / "transcripts" / f"{ident}.jsonl"
                name = Path(source).name
                if len(names[name]) > 1:
                    name = f"{ident}-{name}"
                dest = feature.raw_dir / TRANSCRIPTS_DIR / name
                if canonical.exists() and (
                    canonical in refreshed
                    or not dest.exists()
                    or canonical.stat().st_mtime_ns > dest.stat().st_mtime_ns
                ):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    temporary = dest.with_name(f".{dest.name}.copy")
                    shutil.copyfile(canonical, temporary)
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, dest)
                    copied.append(dest)
    return copied
