# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..records.narrow import as_dict, as_int, dict_list
from .state import file, save


def keep_previous(root: Path, d: dict[str, Any]) -> dict[str, Any] | None:
    from . import verification_gate as gate

    attempt = int(d.get("attempt") or 1)
    if attempt <= 1:
        return None
    previous = attempt - 1
    entry: dict[str, Any] = {"attempt": previous, "path": None, "stderr": None}
    current = file(root, d["id"], ".out")
    if current.exists():
        target = _free(root, d["id"], previous, ".out")
        current.rename(target)
        entry["path"] = target.name
    offsets = as_dict(d.get("err_offsets"))
    start, end = (as_int(offsets.get(str(previous)), -1), as_int(offsets.get(str(attempt)), -1))
    err = file(root, d["id"], ".err")
    if start >= 0 and err.exists():
        with err.open("rb") as stream:
            stream.seek(start)
            data = stream.read(max(0, end - start) if end >= start else -1)
        target = _free(root, d["id"], previous, ".err")
        target.write_bytes(data)
        entry["stderr"] = target.name
    with gate.record_guard(root, d["id"]) as latest:
        latest["transcripts"] = [*dict_list(latest.get("transcripts")), entry]
        save(root, latest)
    return entry


def _free(root: Path, dispatch_id: str, attempt: int, suffix: str) -> Path:
    target = file(root, dispatch_id, f".attempt-{attempt}{suffix}")
    n = 2
    while target.exists():
        target = file(root, dispatch_id, f".attempt-{attempt}-{n}{suffix}")
        n += 1
    return target
