"""Incremental native rollout linkage for Stop pricing.

Cache directory listings by mtime; immutable session_meta headers are read once. A warm
discovery stats directories, not every rollout, and opens only newly discovered headers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import core
from .hooks.transcript_retention import load, save
from .records.narrow import as_dict, as_str, str_list
from .usage.codex import FOLDERS

DISCOVERY_SECONDS = 0.5


def _directory(path: Path, cache: dict[str, Any], threads: dict[str, Any], deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("rollout discovery deadline")
    stamp = path.stat().st_mtime_ns
    previous = as_dict(cache.get(str(path)))
    if previous.get("mtime") == stamp:
        folders = [Path(p) for p in str_list(previous.get("folders"))]
    else:
        entries = list(path.iterdir())
        folders = [p for p in entries if p.is_dir() and not p.is_symlink()]
        cache[str(path)] = {"mtime": -1, "folders": [str(p) for p in folders]}
        for rollout in entries:
            if time.monotonic() >= deadline:
                raise TimeoutError("rollout discovery deadline")
            if not rollout.name.startswith("rollout-") or rollout.suffix != ".jsonl":
                continue
            if str(rollout) in threads:
                continue
            try:
                with rollout.open("rb") as stream:
                    row = as_dict(json.loads(stream.readline(1024 * 1024)))
                meta = as_dict(row.get("payload")) if row.get("type") == "session_meta" else {}
                spawn = as_dict(as_dict(as_dict(meta.get("source")).get("subagent")).get("thread_spawn"))
                if meta.get("id"):
                    threads[str(rollout)] = {
                        "id": as_str(meta.get("id")),
                        "parent": as_str(meta.get("parent_thread_id") or spawn.get("parent_thread_id")),
                    }
                else:
                    # Retry an incomplete header on the next Stop even if the folder is unchanged.
                    stamp = -1
            except OSError, ValueError, UnicodeError:
                stamp = -1
        cache[str(path)] = {"mtime": stamp, "folders": [str(p) for p in folders]}
    for folder in folders:
        if folder.exists():
            _directory(folder, cache, threads, deadline)


def session_rollouts(root: Path, transcript: Path, session_id: str) -> list[Path]:
    """Find descendants across native date folders, following retention's parent linkage."""
    native = next((p for p in transcript.parents if p.name in FOLDERS), None)
    if native is None:
        return []
    index = core.agent_path(root, "usage", "codex-cost-index.json")
    state = load(index)
    directories = as_dict(state.get("directories"))
    threads = as_dict(state.get("threads"))
    before = json.dumps(state, sort_keys=True)
    deadline = time.monotonic() + DISCOVERY_SECONDS
    try:
        for name in FOLDERS:
            folder = native.parent / name
            if folder.exists():
                _directory(folder, directories, threads, deadline)
    finally:
        state = {"directories": directories, "threads": threads}
        if json.dumps(state, sort_keys=True) != before:
            save(index, state)
    selected = {session_id}
    while True:
        children = {
            as_str(as_dict(meta).get("id"))
            for meta in threads.values()
            if as_dict(meta).get("parent") in selected
        }
        if children <= selected:
            break
        selected.update(children)
    return sorted(
        Path(path)
        for path, meta in threads.items()
        if as_dict(meta).get("id") in selected and Path(path) != transcript and Path(path).is_file()
    )
