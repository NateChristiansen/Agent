"""Confine test telemetry to the run's temp root. Fixture discovery can otherwise reach an enclosing
live checkout. AGENT_TEST_SINK_FENCE propagates to child processes; refuse outside sinks and record
a violation so the test fails without raising into recorder callers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .sink import locked

FENCE_ENV = "AGENT_TEST_SINK_FENCE"
VIOLATIONS = "telemetry-fence-violations.jsonl"
# How long a violation waits for the sink lock before it appends without it. Far above the
# recorder's own budget, because a lost refusal lets a test that leaked telemetry pass.
NOTE_LOCK_TIMEOUT = 10.0


def _resolved(path: Path) -> Path:
    r"""`path.resolve()` as a plain path. Windows `realpath` keeps the `\\?\` prefix when another
    process creates the path while it resolves, which would put a sink outside every plain root."""
    text = str(path.resolve())
    if os.name == "nt":
        if text.startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[len("\\\\?\\UNC\\") :]
        elif text.startswith("\\\\?\\"):
            text = text[len("\\\\?\\") :]
    return Path(text)


def allows(directory: Path, env: dict[str, str] | None = None) -> bool:
    """True when no fence is set or `directory` is inside it."""
    fence = (os.environ if env is None else env).get(FENCE_ENV, "")
    if not fence:
        return True
    try:
        target = _resolved(directory)
        root = _resolved(Path(fence))
    except OSError:
        return False
    return target == root or root in target.parents


def note(directory: Path, kind: str) -> None:
    """One line in the fence's violation file, written under the sink's lock because Windows
    append mode is not atomic across processes. A refusal must stay visible, so contention never
    drops it: past `NOTE_LOCK_TIMEOUT` the line goes in by one unlocked append. Only an OS failure
    to write it is silent."""
    fence = os.environ.get(FENCE_ENV, "")
    if not fence:
        return
    line = (json.dumps({"sink": str(directory), "kind": kind, "pid": os.getpid()}) + "\n").encode()
    path = Path(fence) / VIOLATIONS
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with locked(path, timeout=NOTE_LOCK_TIMEOUT) as fd:
                os.write(fd, line)
            return
        except TimeoutError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 — the fence never raises into the recorder
        return
