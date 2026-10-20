"""Bound streamed Codex agent text as deltas arrive: small RPC frames can accumulate unbounded text
without completing an item. Interrupt degenerate whitespace runs or excessive per-turn text with a
distinct stop reason, retain a bounded diagnostic sample and never auto-resume the flood.
Tool-output events are outside this text cap.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

#: Consecutive whitespace characters, across deltas, that no real answer contains.
WHITESPACE_RUN_LIMIT = 64 * 1024
#: Agent-message text in one turn. A structured return is a few KiB; prose answers stay far below.
TEXT_LIMIT = 4 * 1024 * 1024
#: Delta text written to the stream file per turn before further deltas are only counted.
FLOOD_LOG_BYTES = 256 * 1024
#: Head and tail kept for the diagnosis.
SAMPLE_BYTES = 4096


class Flood:
    """One turn's agent-message deltas."""

    def __init__(self) -> None:
        self.total = 0
        self.logged_bytes = 0
        self.whitespace_run = 0
        self.longest_run = 0
        self.head = ""
        self.tail = ""

    def add(self, delta: str) -> None:
        size = len(delta.encode("utf-8", "replace"))
        self.total += size
        self.logged_bytes += size
        # Every run counts, not only a trailing one, and a run continues across deltas: a flood
        # followed by text in one delta, or split as `"\n"*65535` then `"\nx"`, slipped past a
        # check of the final suffix alone.
        run = self.whitespace_run
        for char in delta:
            run = run + 1 if char.isspace() else 0
            self.longest_run = max(self.longest_run, run)
        self.whitespace_run = run
        if len(self.head) < SAMPLE_BYTES:
            self.head = (self.head + delta)[:SAMPLE_BYTES]
        self.tail = (self.tail + delta)[-SAMPLE_BYTES:]

    def take(self, delta: str) -> str:
        """Count one delta: `stop` when the turn is degenerate, `skip` when it is past the stream
        file's share and is only counted, else `log`."""
        self.add(delta)
        if self.degenerate():
            return "stop"
        return "skip" if self.logged_bytes > FLOOD_LOG_BYTES else "log"

    async def stop(self, rpc: Any, thread_id: str, turn_id: str, err_path: Path) -> str:
        """Interrupt the turn and keep the diagnosis; returns the stop reason."""
        with contextlib.suppress(Exception):
            await rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        with contextlib.suppress(OSError), err_path.open("a", encoding="utf-8") as err:
            err.write(self.why())
        return "degenerate_output"

    def degenerate(self) -> bool:
        return self.longest_run >= WHITESPACE_RUN_LIMIT or self.total >= TEXT_LIMIT

    def why(self) -> str:
        """One diagnosis line plus the bounded sample, for the attempt's `.err`."""
        kind = (
            f"a {self.longest_run}-character whitespace run"
            if self.longest_run >= WHITESPACE_RUN_LIMIT
            else f"{self.total} bytes of agent-message text"
        )
        return (
            f"Codex streamed {kind} in one turn; interrupted as degenerate_output.\n"
            f"head: {self.head!r}\ntail: {self.tail!r}\n"
        )
