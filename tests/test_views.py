"""Views (G2): a dispatch's lifecycle spans with per-attempt cost by source."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_cli.dispatch import spans, telemetry_events


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    monkeypatch.delenv(telemetry_events.SINK_ENV, raising=False)
    return tmp_path


def test_spans_keep_each_attempt_s_waits_and_cost_by_source(root: Path):
    d: dict[str, Any] = {"id": "001-mac-01", "feature": 40, "attempt": 1, "start_sha": "b" * 40}
    for phase in ("model-active", "awaiting-verification"):
        telemetry_events.transition(root, d, phase)
    telemetry_events.transition(root, d, "verifying", wait_s=12.5)
    telemetry_events.transition(root, d, "awaiting-answer")
    d.update(attempt=2)  # resumed: the record moves on, the events stay
    telemetry_events.transition(root, d, "model-active")
    telemetry_events.transition(root, d, "ended", state="failed")
    d.update(
        cost_attempts={"1": {"added": 0.4}},
        cost_evidence={"1": "provider"},
        thread=[{"from": "worker", "attempt": 2, "cost_usd": 0.1}],
    )
    data = spans.spans(root, d)
    phases = [(r["attempt"], r["phase"]) for r in data["spans"]]
    assert phases == [
        (1, "model-active"),
        (1, "awaiting-verification"),
        (1, "verifying"),
        (1, "awaiting-answer"),
        (2, "model-active"),
        (2, "ended"),
    ]
    assert data["spans"][2]["detail"] == {"wait_s": 12.5}
    assert data["spans"][0]["base"] == "b" * 40
    assert data["spans"][-1]["seconds"] is None and data["spans"][0]["seconds"] is not None
    assert data["costs"] == [
        {"attempt": 1, "usd": 0.4, "source": "provider"},
        {"attempt": 2, "usd": 0.1, "source": "unknown"},
    ]
    assert "attempt 2 cost $0.10 (unknown)" in spans.text(data)


def test_a_transition_names_the_harness_revision_running_it():
    assert telemetry_events.harness_revision() is not None


def test_the_idle_time_after_an_attempt_ends_is_not_lifecycle_time(root: Path):
    d: dict[str, Any] = {"id": "001-mac-02", "feature": 40, "attempt": 1}
    telemetry_events.transition(root, d, "model-active")
    telemetry_events.transition(root, d, "ended", state="question")
    d.update(attempt=2)
    telemetry_events.transition(root, d, "model-active")
    data = spans.spans(root, d)
    assert "ended" not in data["by_phase"] and data["spans"][1]["seconds"] is not None
