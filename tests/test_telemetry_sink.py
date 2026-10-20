"""Telemetry sink: concurrent appends, bounded failure, refusals and isolation.

The concurrency test also runs in the windows-setup CI job; ADR-0038 is accepted only after it
passes there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_cli import telemetry
from agent_cli.telemetry import schema, sink
from telemetry_helpers import events, isolated, lines  # noqa: F401 — `isolated` is an autouse fixture

REAL_TEMP = tempfile.gettempdir()
REPO = Path(__file__).resolve().parents[1]

PROCESSES = 8
EACH = 500
CHILD = r"""
import json, sys, time
from pathlib import Path
from agent_cli import telemetry
main, ready, go, count = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
ready.write_text("")
deadline = time.monotonic() + 60
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.001)
for i in range(count):
    telemetry.record(main, "dispatch.terminal", outcome="passed", data={"i": i, "flags": ["-x"]})
print(json.dumps(telemetry.process_recorder().counts))
"""


def test_eight_processes_append_concurrently_without_loss_or_tearing(tmp_path, isolated):
    main = tmp_path / "main"
    gate = tmp_path / "gate"
    gate.mkdir()
    env = {**os.environ, "TMPDIR": str(isolated), "TEMP": str(isolated), "TMP": str(isolated)}
    children = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD, str(main), str(gate / f"ready-{n}"), str(gate / "go"), str(EACH)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(PROCESSES)
    ]
    deadline = time.monotonic() + 60
    while len(list(gate.glob("ready-*"))) < PROCESSES and time.monotonic() < deadline:
        time.sleep(0.01)
    (gate / "go").write_text("")
    reports = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err
        reports.append(json.loads(out))
    assert all(r == {"attempted": EACH, "written": EACH, "lost": 0, "skipped": 0} for r in reports), reports
    raw = b"".join(p.read_bytes() for p in sorted((main / ".agent/telemetry/events").glob("*.jsonl")))
    assert raw.endswith(b"\n")
    rows = [json.loads(line) for line in raw.decode().splitlines()]
    assert len(rows) == PROCESSES * EACH
    assert len({row["event_id"] for row in rows}) == PROCESSES * EACH
    by_instance: dict[str, list[int]] = {}
    for row in rows:
        assert schema.validate(row) is None
        by_instance.setdefault(row["writer"]["instance"], []).append(row["writer"]["seq"])
    assert len(by_instance) == PROCESSES
    assert all(sorted(seqs) == list(range(1, EACH + 1)) for seqs in by_instance.values())
    assert not telemetry.Sink.for_main(main).loss.exists()


def test_an_event_is_one_versioned_line_in_the_utc_day_file(tmp_path):
    main = tmp_path / "main"
    event_id = telemetry.record(
        main, "dispatch.terminal", dispatch="001-03", cost_usd=1.5, cost_source="provider"
    )
    [row] = events(main / ".agent/telemetry")
    assert row["event_id"] == event_id
    assert tuple(row) == schema.FIELDS
    assert row["schema"] == 1 and row["source"] == "live" and row["writer"]["seq"] == 1
    day = row["ts"][:10]
    assert (main / ".agent/telemetry/events" / f"{day}.jsonl").exists()
    assert row["ts"].endswith("+00:00")


def _loss_lines(main: Path) -> list[dict]:
    return lines(telemetry.Sink.for_main(main).loss)


def test_a_blocked_sink_never_raises_and_leaves_a_loss_line(tmp_path):
    main = tmp_path / "main"
    (main / ".agent").mkdir(parents=True)
    (main / ".agent/telemetry").write_text("not a directory")
    rec = telemetry.Recorder()
    assert rec.record(telemetry.Sink.for_main(main), "dispatch.launch") is None
    [loss] = _loss_lines(main)
    assert loss["kind_lost"] == "dispatch.launch"
    assert loss["reason"] in {"FileExistsError", "NotADirectoryError"}
    assert loss["writer"]["instance"] == rec.instance and loss["writer"]["seq"] == 1
    assert rec.counts == {"attempted": 1, "written": 0, "lost": 1, "skipped": 0}


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions, not root")
def test_an_unwritable_directory_is_a_loss(tmp_path):
    main = tmp_path / "main"
    events_dir = main / ".agent/telemetry/events"
    events_dir.mkdir(parents=True)
    events_dir.chmod(0o500)
    try:
        assert telemetry.record(main, "dispatch.launch") is None
    finally:
        events_dir.chmod(0o700)
    assert [loss["reason"] for loss in _loss_lines(main)] == ["PermissionError"]


def test_a_held_lock_times_out_within_its_deadline(tmp_path):
    main = tmp_path / "main"
    target = telemetry.Sink.for_main(main)
    target.events.mkdir(parents=True)
    rec = telemetry.Recorder()
    with sink.locked(target.day(datetime.now(UTC).date())):
        started = time.monotonic()
        assert rec.record(target, "dispatch.launch") is None
        waited = time.monotonic() - started
    assert sink.LOCK_TIMEOUT <= waited < 1
    assert [loss["reason"] for loss in _loss_lines(main)] == ["TimeoutError"]
    assert rec.record(target, "dispatch.launch") is not None


def test_a_short_write_is_a_loss_and_leaves_a_malformed_line(tmp_path, monkeypatch):
    main = tmp_path / "main"
    real = os.write

    def short(fd: int, data: bytes) -> int:
        return real(fd, data[: len(data) // 2] if data.startswith(b'{"schema"') else data)

    monkeypatch.setattr(os, "write", short)
    assert telemetry.record(main, "dispatch.launch") is None
    monkeypatch.setattr(os, "write", real)
    assert [loss["reason"] for loss in _loss_lines(main)] == ["short_write"]
    report = telemetry.health(telemetry.Sink.for_main(main))
    assert report["counts"]["malformed"] == 1 and report["counts"]["loss_lines"] == 1


def test_three_consecutive_failures_disable_the_instance(tmp_path):
    main = tmp_path / "main"
    (main / ".agent").mkdir(parents=True)
    (main / ".agent/telemetry").write_text("")
    target = telemetry.Sink.for_main(main)
    rec = telemetry.Recorder()
    for _ in range(5):
        assert rec.record(target, "dispatch.terminal") is None
    kinds = [loss["kind_lost"] for loss in _loss_lines(main)]
    assert kinds == ["dispatch.terminal"] * 3 + ["disabled"]
    assert rec.disabled and rec.counts == {"attempted": 5, "written": 0, "lost": 3, "skipped": 2}
    (main / ".agent/telemetry").unlink()
    assert rec.record(target, "dispatch.terminal") is None, "disabled for the instance's lifetime"
    assert len(_loss_lines(main)) == 4
    assert telemetry.Recorder().record(target, "dispatch.terminal") is not None, "a new instance starts fresh"


def test_a_success_resets_the_strike_count(tmp_path, monkeypatch):
    target = telemetry.Sink.for_main(tmp_path / "main")
    rec = telemetry.Recorder()
    real = os.write
    failing = [False]

    def write(fd: int, data: bytes) -> int:
        return real(fd, data[:1] if failing[0] and data.startswith(b'{"schema"') else data)

    monkeypatch.setattr(os, "write", write)
    for _ in range(3):
        failing[0] = True
        assert rec.record(target, "dispatch.terminal") is None
        assert rec.record(target, "dispatch.terminal") is None
        failing[0] = False
        assert rec.record(target, "dispatch.terminal") is not None
    assert not rec.disabled and rec.counts["lost"] == 6


@pytest.mark.parametrize("key", sorted(schema.REFUSED_KEYS) + ["Prompt"])
def test_refused_data_keys_are_counted(tmp_path, key):
    main = tmp_path / "main"
    assert telemetry.record(main, "dispatch.terminal", data={key: "x"}) is None
    assert [(loss["kind_lost"], loss["reason"]) for loss in _loss_lines(main)] == [
        ("dispatch.terminal", "refused")
    ]
    assert events(main / ".agent/telemetry") == []


@pytest.mark.parametrize(
    "fields",
    [
        {"data": {"note": "x" * 513}},
        {"session": "x" * 513},
        {"data": {"flags": ["x"] * 33}},
        {"data": {"flags": ["x" * 129]}},
        {"data": {"nested": {"a": 1}}},
        {"data": {"ratio": float("nan")}},
        {"commit": "abc"},
        {"cost_source": "guess"},
        {"cost_source": "unknown", "cost_usd": 1.0},
        {"source": "replay"},
    ],
)
def test_invalid_events_are_refused(tmp_path, fields):
    main = tmp_path / "main"
    rec = telemetry.Recorder()
    assert rec.record(telemetry.Sink.for_main(main), "dispatch.terminal", **fields) is None
    assert [loss["reason"] for loss in _loss_lines(main)] == ["refused"]
    assert rec.counts["lost"] == 1 and not rec.disabled


def _written(tmp_path: Path) -> dict:
    main = tmp_path / "main"
    telemetry.record(main, "dispatch.terminal", data={"i": 1})
    [row] = events(main / ".agent/telemetry")
    return row


def test_a_valid_event_with_reordered_fields_stays_valid(tmp_path):
    row = _written(tmp_path)
    assert schema.validate(row) is None
    assert schema.validate(dict(reversed(list(row.items())))) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", None),
        ("event_id", "not-a-uuid"),
        ("parent_id", "not-a-uuid"),
        ("ts", "invalid"),
        ("ts", "2026-10-04T12:00:00"),
        ("ts", None),
        ("writer", {}),
        ("writer", {"host": "h", "pid": 1, "instance": "x", "seq": 1}),
        ("writer", {"host": "h", "pid": True, "instance": "6f1c0000-0000-4000-8000-000000000006", "seq": 1}),
        ("writer", {"host": "h", "pid": 1, "instance": "6f1c0000-0000-4000-8000-000000000006", "seq": 0}),
        ("writer", {"host": 1, "pid": 1, "instance": "6f1c0000-0000-4000-8000-000000000006", "seq": 1}),
        ("schema", True),
        ("schema", 1.0),
        ("schema", 2),
    ],
)
def test_the_validator_rejects_each_invalid_v1_field(tmp_path, field, value):
    row = _written(tmp_path)
    assert schema.validate({**row, field: value}) is not None


def test_the_validator_rejects_missing_and_extra_fields(tmp_path):
    row = _written(tmp_path)
    assert schema.validate({k: v for k, v in row.items() if k != "parent_id"}) is not None
    assert schema.validate({**row, "extra": 1}) is not None


def test_unknown_kinds_are_refused_and_workflow_kinds_pass(tmp_path):
    main = tmp_path / "main"
    assert telemetry.record(main, "pytest.unknown") is None
    assert telemetry.record(main, "workflow.") is None
    assert telemetry.record(main, "workflow.review") is not None
    assert len(_loss_lines(main)) == 2


def test_limits_are_inclusive(tmp_path):
    main = tmp_path / "main"
    fields = {"session": "x" * 512, "data": {"note": "y" * 512, "flags": ["z" * 128] * 32}}
    assert telemetry.record(main, "dispatch.terminal", **fields) is not None


def test_an_oversize_line_is_refused_but_is_not_a_strike(tmp_path):
    main = tmp_path / "main"
    rec = telemetry.Recorder()
    big = {f"k{n}": "x" * 500 for n in range(140)}
    for _ in range(4):
        assert rec.record(telemetry.Sink.for_main(main), "dispatch.terminal", data=big) is None
    assert [loss["reason"] for loss in _loss_lines(main)] == ["oversize"] * 4
    assert not rec.disabled


def test_misuse_never_raises(tmp_path):
    main = tmp_path / "main"
    assert telemetry.record(main, "dispatch.terminal", nonsense=1) is None
    assert telemetry.record(main, "dispatch.terminal", data=["not", "a", "mapping"]) is None  # type: ignore[arg-type]
    assert telemetry.record_implicit("dispatch.terminal", nonsense=1) is None


def test_this_repository_gains_no_line(tmp_path):
    """Every path a test uses stays in tmp: no line from this process reaches this repository's
    sink or the real temp loss file for it."""
    real_main = telemetry.sink_root(REPO)
    real_sink = telemetry.Sink.for_main(real_main)
    real_loss = Path(REAL_TEMP) / sink.loss_path(real_main).relative_to(tempfile.gettempdir())
    instance = telemetry.process_recorder().instance
    main = tmp_path / "main"
    telemetry.record(main, "dispatch.launch")
    telemetry.record(main, "dispatch.terminal", data={"prompt": "x"})
    telemetry.record_implicit("dispatch.launch")  # cwd is outside any repository: no_root
    telemetry.record_implicit("dispatch.launch", env={"AGENT_TELEMETRY_SINK": str(main / ".agent/telemetry")})
    telemetry.health(telemetry.Sink.for_main(main))
    assert telemetry.process_recorder().instance == instance
    for path in [*sorted(real_sink.events.glob("*.jsonl")), real_loss]:
        if path.exists():
            assert instance not in path.read_text(encoding="utf-8", errors="replace"), path
    assert telemetry.Sink.for_main(main).loss.is_relative_to(tmp_path)
