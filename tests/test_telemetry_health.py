"""Telemetry health and its doctor row."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_cli import telemetry
from agent_cli.doctor import checks_telemetry
from agent_cli.telemetry.health import COUNTS
from telemetry_helpers import events, isolated  # noqa: F401 — `isolated` is an autouse fixture
from verification_fixtures import fixture  # noqa: F401 — `fixture` is a pytest fixture


def _sink(tmp_path: Path) -> telemetry.Sink:
    return telemetry.Sink.for_main(tmp_path / "main")


def _append(path: Path, raw: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(raw)


def _today(target: telemetry.Sink) -> Path:
    return target.day(datetime.now(UTC).date())


def test_a_clean_sink_is_ok_with_counts_newest_and_the_caveat(tmp_path):
    target = _sink(tmp_path)
    telemetry.record(tmp_path / "main", "dispatch.launch")
    last = telemetry.record(tmp_path / "main", "dispatch.terminal")
    report = telemetry.health(target)
    assert report["status"] == "ok"
    assert report["counts"] == dict.fromkeys(COUNTS, 0)
    rows = events(target.directory)
    assert rows[-1]["kind"] == "health.probe", "the probe is a real append"
    assert report["newest"] == next(r["ts"] for r in rows if r["event_id"] == last), "probes are not news"
    assert "left no trace are not counted" in report["note"]
    assert report["process"]["written"] == 3


def test_an_empty_sink_is_ok_and_has_no_newest_event(tmp_path):
    report = telemetry.health(_sink(tmp_path))
    assert report["status"] == "ok" and report["newest"] is None


def test_a_failed_probe_is_failed(tmp_path):
    target = _sink(tmp_path)
    target.directory.parent.mkdir(parents=True)
    target.directory.write_text("")
    report = telemetry.health(target)
    assert report["status"] == "failed"
    assert report["counts"]["loss_lines"] == 1, "the failed probe leaves its own loss line"


def test_a_disabled_process_reports_failed(tmp_path):
    rec = telemetry.Recorder()
    rec.disabled = True
    assert telemetry.health(_sink(tmp_path), recorder=rec)["status"] == "failed"


def _refused(target: telemetry.Sink) -> None:
    telemetry.process_recorder().record(target, "dispatch.terminal", data={"prompt": "x"})


def _gap(target: telemetry.Sink) -> None:
    rec = telemetry.process_recorder()
    rec.record(target, "dispatch.terminal")
    rec.seq += 3


def _malformed(target: telemetry.Sink) -> None:
    _append(_today(target), '{"schema": 1, "ki\n')


def _unknown(target: telemetry.Sink) -> None:
    _append(_today(target), json.dumps({"schema": 2}) + "\n")


def _loss(target: telemetry.Sink, kind_lost: str, reason: str) -> None:
    line = {"ts": datetime.now(UTC).isoformat(), "writer": {}, "kind_lost": kind_lost, "reason": reason}
    _append(target.loss, json.dumps(line) + "\n")


@pytest.mark.parametrize(
    ("damage", "count", "value"),
    [
        (_refused, "refused", 1),
        (_gap, "gaps", 3),
        (_malformed, "malformed", 1),
        (_unknown, "unknown_schema", 1),
        (lambda t: _loss(t, "disabled", "TimeoutError"), "disabled", 1),
        (lambda t: _loss(t, "dispatch.terminal", "short_write"), "loss_lines", 1),
    ],
)
def test_observed_damage_is_degraded(tmp_path, damage, count, value):
    target = _sink(tmp_path)
    damage(target)
    report = telemetry.health(target)
    assert report["status"] == "degraded"
    assert report["counts"][count] == value
    assert sum(report["counts"].values()) == value


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "pytest.unknown", "data": {"prompt": "x"}},
        {"event_id": None},
        {"ts": "invalid"},
        {"writer": {}},
        {"schema": True},
    ],
)
def test_invalid_v1_lines_are_malformed_and_degraded(tmp_path, change):
    target = _sink(tmp_path)
    telemetry.record(tmp_path / "main", "dispatch.terminal")
    [row] = events(target.directory)
    _append(_today(target), json.dumps({**row, "event_id": str(uuid.uuid4()), **change}) + "\n")
    report = telemetry.health(target)
    assert report["status"] == "degraded"
    assert report["counts"]["malformed"] == 1
    assert sum(report["counts"].values()) == 1


def test_degraded_recovers_once_the_window_passes(tmp_path):
    target = _sink(tmp_path)
    _refused(target)
    _gap(target)
    _malformed(target)
    assert telemetry.health(target)["status"] == "degraded"
    later = datetime.now(UTC) + timedelta(hours=25)
    report = telemetry.health(target, now=later)
    assert report["counts"]["refused"] == report["counts"]["gaps"] == 0, "timed damage ages out"
    assert telemetry.health(target, now=datetime.now(UTC) + timedelta(days=3))["status"] == "ok"
    assert telemetry.health(target, window=timedelta(seconds=0))["status"] == "degraded"


def test_the_doctor_row_reports_each_state(fixture):
    _, worktree, _ = fixture
    main = worktree.parent / "repository"
    [row] = checks_telemetry.rows(worktree)
    assert row["check"] == "telemetry" and row["level"] == "ok"
    assert row["detail"].startswith("ok: loss_lines 0, refused 0")
    assert "left no trace are not counted" in row["detail"]
    _refused(telemetry.Sink.for_main(telemetry.sink_root(worktree)))
    [row] = checks_telemetry.rows(worktree)
    assert row["level"] == "warn" and row["detail"].startswith("degraded:") and "refused 1" in row["detail"]
    for path in (main / ".agent/telemetry/events").iterdir():
        path.unlink()
    (main / ".agent/telemetry/events").rmdir()
    (main / ".agent/telemetry/events").write_text("")
    [row] = checks_telemetry.rows(worktree)
    assert row["level"] == "warn" and row["detail"].startswith("failed:")
    assert "could not append" in row["detail"]
    assert not (worktree / ".agent").exists()


def test_the_doctor_row_is_absent_outside_a_repository(tmp_path):
    assert checks_telemetry.rows(tmp_path) == []
