# ruff: noqa
from __future__ import annotations
import json
import os
from pathlib import Path

FENCE_ENV = "AGENT_TEST_SINK_FENCE"
VIOLATIONS = "telemetry-fence-violations.jsonl"


def allows(directory: Path, env: dict[str, str] | None = None) -> bool:
    fence = (os.environ if env is None else env).get(FENCE_ENV, "")
    if not fence:
        return True
    try:
        target = directory.resolve()
        root = Path(fence).resolve()
    except OSError:
        return False
    return target == root or root in target.parents


def note(directory: Path, kind: str) -> None:
    fence = os.environ.get(FENCE_ENV, "")
    if not fence:
        return
    line = json.dumps({"sink": str(directory), "kind": kind, "pid": os.getpid()}) + "\n"
    try:
        with open(Path(fence) / VIOLATIONS, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        return
