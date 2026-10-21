"""Discover native Codex descendants by the same parent metadata used for usage."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..records.narrow import as_dict
from ..usage.codex import FOLDERS, codex_home


def index(deadline: float) -> dict[str, list[tuple[Path, str]]]:
    threads: dict[str, list[tuple[Path, str]]] = {}
    for folder in FOLDERS:
        for path in (codex_home(Path.home()) / folder).glob("**/rollout-*.jsonl"):
            if time.monotonic() >= deadline:
                raise TimeoutError("native transcript discovery deadline")
            try:
                with path.open("rb") as inp:
                    line = inp.readline(1024 * 1024)
                record = as_dict(json.loads(line))
            except OSError, ValueError, UnicodeError:
                continue
            if record.get("type") != "session_meta":
                continue
            meta = as_dict(record.get("payload"))
            ident = str(meta.get("id") or "")
            spawn = as_dict(as_dict(as_dict(meta.get("source")).get("subagent")).get("thread_spawn"))
            parent = str(meta.get("parent_thread_id") or spawn.get("parent_thread_id") or "")
            if ident:
                threads.setdefault(ident, []).append((path, parent))
    return threads


def related(record: dict[str, Any], threads: dict[str, list[tuple[Path, str]]]) -> list[dict[str, Any]]:
    sid = str(record.get("session_id") or "")
    if not sid or not threads:
        return []
    selected = {sid}
    while True:
        children = {
            ident for ident, paths in threads.items() if any(parent in selected for _, parent in paths)
        }
        if children <= selected:
            break
        selected.update(children)
    return [
        {**record, "transcript_path": str(path), "extra": {}, "native_session_id": ident}
        for ident in selected
        for path, _ in threads.get(ident, [])
    ]
