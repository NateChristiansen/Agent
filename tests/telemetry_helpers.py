"""Shared isolation for the telemetry tests.

Every test gets a temporary loss directory (`tempfile.tempdir` points into `tmp_path`), a clean
routing environment, a fresh process recorder and a cwd outside any repository, so nothing can
reach this repository's `.agent/telemetry` or its real loss file.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent_cli.telemetry import recorder as recorder_mod

ROUTING = ("AGENT_TELEMETRY", "AGENT_TELEMETRY_SINK", "AGENT_TELEMETRY_PARENT", "AGENT_DISPATCH_ID")


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    for name in ROUTING:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(recorder_mod, "_process", None)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    return temp


def lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def events(directory: Path) -> list[dict]:
    """Every event under `<directory>/events`, across day files."""
    out: list[dict] = []
    for path in sorted((directory / "events").glob("*.jsonl")):
        out += lines(path)
    return out
