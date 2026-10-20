"""Fixed summary counts and machine completeness, with temporary sinks and notes roots."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from agent_cli import cli, core, telemetry
from agent_cli.mcp.specs_harness import harness
from agent_cli.mcp.tools import Ctx
from agent_cli.telemetry import schema
from agent_cli.telemetry.dashboard import capture, live_health
from agent_cli.telemetry.summary import machine_days, queries, summary, summary_rel
from agent_cli.usage import job, publish
from usage_helpers import sample_record

START = datetime(2026, 9, 3, tzinfo=UTC)
END = START + timedelta(days=1)


def _emit(root: Path, kind: str, at: datetime = START, **fields):
    rec = telemetry.process_recorder()
    assert rec.record(telemetry.Sink.for_main(root), kind, ts=at, **fields)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def test_fixture_day_exact_counts_and_real_paints(tmp_root: Path):
    _emit(tmp_root, "workflow.owner_question")
    _emit(tmp_root, "workflow.owner_question_cleared")
    _emit(tmp_root, "permission.request", provider="codex")
    _emit(tmp_root, "permission.request", provider="claude")
    _emit(tmp_root, "activity.transition", provider="codex", data={"to": "waiting_on_you"})
    _emit(tmp_root, "dispatch.launch")
    _emit(
        tmp_root, "dispatch.transition", dispatch="001-mac-01", data={"attempt": 1, "phase": "model-active"}
    )
    _emit(
        tmp_root,
        "dispatch.transition",
        START + timedelta(seconds=12),
        dispatch="001-mac-01",
        data={"attempt": 1, "phase": "ended"},
    )
    _emit(tmp_root, "dispatch.terminal")
    _emit(
        tmp_root,
        "verify.record",
        outcome="retry-passed",
        data={"retry_nodes": ["ordinary-one", "ordinary-two"]},
    )
    _emit(tmp_root, "hook.error")
    _emit(tmp_root, "status.render", data={"error": "ordinary render error"})
    _write(
        core.agent_path(tmp_root, "tabs", "metrics.jsonl"),
        {"at": START.timestamp(), "painted": [{"label": "✋ 001 · cx abcd"}]},
    )
    _write(
        core.agent_path(tmp_root, "tabs", "plugin-metrics.jsonl"),
        {"at": START.timestamp(), "failed": 1, "painted_labels": [{"label": "✋ 001 · cl abcd"}]},
    )
    result = queries(tmp_root, START, END, END)
    assert result["complete"] is True
    assert result["owner_questions"] == {"raised": 1, "cleared": 1}
    assert result["permissions"] == {
        "requests": {"codex": 1, "claude": 1},
        "raw_waiting_transitions": {"codex": 1},
        "visible_hand_paints": {"codex": 1, "claude": 1},
        "hand_episodes": {
            "permission": {},
            "question": {},
            "stale": {},
            "unknown": {"codex": 1, "claude": 1},
        },
        "episodes_complete": False,
        "paints_complete": True,
        "approver": "unknown",
    }
    assert result["dispatch"] == {
        "launches": 1,
        "terminal": 1,
        "spans": 1,
        "seconds_by_phase": {"model-active": 12.0},
        "seconds_unit": "summed dispatch-seconds; concurrent spans add, each clipped to the window",
        "open_spans": 0,
        "open_spans_by_phase": {},
    }
    assert result["verify"] == {"verify.record": {"retry-passed": 1}}
    assert result["flakes"] == {"retried_nodes": 2}
    assert (
        result["health"]["hook_errors"]
        == result["health"]["render_failures"]
        == result["health"]["tab_paint_failures"]
        == 1
    )


def test_fixture_day_counts_waits_by_cause_and_requests_by_provider(tmp_root: Path):
    def emit(kind, seconds=0, sid="cx", **fields):
        _emit(tmp_root, kind, START + timedelta(seconds=seconds), session=sid, **fields)

    for provider, sid in (("codex", "cx"), ("claude", "cl"), (None, "legacy")):
        emit("permission.request", sid=sid, provider=provider)
        emit("activity.transition", sid=sid, data={"to": "waiting_on_you", "cause": "PermissionRequest:Bash"})
    emit("activity.transition", 10, data={"to": "working"})
    emit("permission.request", 30, provider="codex")
    emit("activity.transition", 30, data={"to": "waiting_on_you", "cause": "PermissionRequest:Bash"})
    emit("workflow.owner_question", 35, data={"question_id": "q1"})
    emit("workflow.owner_question_cleared", 70, data={"question_id": "q1"})
    emit("workflow.owner_question", 80, data={"question_id": "q2"})
    emit("workflow.owner_question_cleared", 90, data={"question_id": "q2"})
    emit("activity.transition", 95, data={"to": "working"})
    emit("permission.request", 100, provider="codex")

    def paint(seconds, sid="cx", glyph="✋", suffix="cx abcd"):
        return {
            "at": (START + timedelta(seconds=seconds)).timestamp(),
            "painted": [{"key": f"manager-{sid}", "label": f"{glyph} 001 · {suffix}"}],
        }

    rows = [
        paint(1, "cl", suffix="cl abcd"),
        paint(2, "cl", suffix="cl abcd"),
        paint(3, "legacy", suffix="old"),
        paint(20, glyph="⚙️"),
        paint(31),
        paint(32),
        paint(36),
        paint(37, glyph="✋?"),
        paint(38),
        paint(39, glyph="✋?"),
        paint(81),
        paint(121),
    ]
    path = core.agent_path(tmp_root, "tabs", "metrics.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    # The plugin paints the same wait too. Both writes still make only one episode.
    _write(core.agent_path(tmp_root, "tabs", "plugin-metrics.jsonl"), paint(33))
    result = summary(tmp_root, since=START.isoformat(), until=END.isoformat(), now=END)["local"][
        "permissions"
    ]
    assert result["requests"] == {"codex": 3, "claude": 1, "unknown": 1}
    assert result["hand_episodes"] == {
        "permission": {"claude": 1, "unknown": 1, "codex": 2},
        "question": {"codex": 2},
        "stale": {"codex": 1},
        "unknown": {},
    }
    assert result["episodes_complete"] is True


def test_cross_day_wait_repaint_does_not_start_an_episode(tmp_root: Path):
    _emit(tmp_root, "permission.request", START - timedelta(seconds=30), session="cx", provider="codex")
    path = core.agent_path(tmp_root, "tabs", "metrics.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(
                {
                    "at": (START + timedelta(seconds=s)).timestamp(),
                    "painted": [{"key": "manager-cx", "label": "✋ 001 · cx abcd"}],
                }
            )
            + "\n"
            for s in (-5, 5, 10)
        ),
        encoding="utf-8",
    )
    _write(core.agent_path(tmp_root, "tabs", "plugin-metrics.jsonl"), {"at": START.timestamp()})
    result = queries(tmp_root, START, END, END)["permissions"]
    assert result["hand_episodes"]["permission"] == {}
    assert result["visible_hand_paints"] == {"codex": 2}


def test_legacy_request_without_provider_counts_as_unknown(tmp_root: Path):
    _emit(tmp_root, "permission.request")
    path = telemetry.Sink.for_main(tmp_root).events / "2026-09-03.jsonl"
    row = json.loads(path.read_text(encoding="utf-8"))
    row.pop("provider")
    assert schema.validate(row) is None
    _write(path, row)
    result = queries(tmp_root, START, END, END)
    assert result["permissions"]["requests"] == {"unknown": 1}
    assert result["health"]["losses"]["malformed"] == 0


def test_concurrent_spans_are_clipped_dispatch_seconds_with_open_counts(tmp_root: Path):
    for i in range(3):
        _emit(
            tmp_root,
            "dispatch.transition",
            START - timedelta(days=3),
            dispatch=f"001-mac-{i:02}",
            data={"attempt": 1, "phase": "awaiting-landing"},
        )
    stop = START + timedelta(hours=12)
    result = queries(tmp_root, START, stop, stop)["dispatch"]
    assert result["seconds_by_phase"] == {"awaiting-landing": 3 * 43200.0}
    assert result["open_spans"] == 3
    assert result["open_spans_by_phase"] == {"awaiting-landing": 3}


def test_missing_and_partial_machine_days_are_incomplete(tmp_root: Path):
    (tmp_root / "agent.local.yaml").write_text("machine: mac\n", encoding="utf-8")
    checkpoint = tmp_root / "usage/machines/windows.md"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("", encoding="utf-8")
    _write(
        tmp_root / summary_rel(START.date(), "mac"),
        {"machine": "mac", "date": START.date().isoformat(), "updated": START.isoformat(), "complete": False},
    )
    rows = machine_days(tmp_root, START.date(), START + timedelta(seconds=5))
    assert [(r["machine"], r["complete"], r["missing"]) for r in rows] == [
        ("mac", False, False),
        ("windows", False, True),
    ]
    assert rows[0]["age_s"] == 5
    assert summary(tmp_root, since=START.isoformat(), until=END.isoformat(), now=END)["complete"] is False


def test_cli_and_mcp_return_identical_json(tmp_root: Path, capsys):
    args = cli.parse(
        ["--json", "telemetry", "summary", "--since", START.isoformat(), "--until", END.isoformat()]
    )
    assert cli.dispatch(tmp_root, args, core.Output(as_json=True)) == 0
    data = json.loads(capsys.readouterr().out)
    assert (
        harness(
            Ctx(root=tmp_root, profile="manager"),
            "telemetry_summary",
            since=START.isoformat(),
            until=END.isoformat(),
        ).data
        == data
    )


def test_missing_sink_is_incomplete_and_unused_kinds_are_refused(tmp_root: Path):
    assert queries(tmp_root, START, END, END)["complete"] is False
    for kind in ("hook.call", "manager.turn", "pytest.session", "pytest.test"):
        assert not schema.known_kind(kind)


def test_fresh_partial_spend_is_warn_and_remote_age_is_retained(tmp_root: Path):
    (tmp_root / "agent.local.yaml").write_text("machine: mac\n", encoding="utf-8")
    value = {
        "machine": "windows",
        "date": core.today().isoformat(),
        "updated": core.iso(core.now()),
        "complete": False,
        "health": {
            "spend_today": {
                "status": "ok",
                "complete": False,
                "updated": core.iso(core.now()),
                "detail": "$2.00",
            }
        },
    }
    _write(tmp_root / summary_rel(core.today(), "windows"), value)
    health = live_health(tmp_root)
    row = next(r for r in health["spend_today"] if r["machine"] == "windows")
    assert row["status"] == "warn" and row["complete"] is False
    assert row["updated"] == value["updated"]


def test_missing_notes_sync_evidence_is_unknown_and_real_failure_stays(tmp_root: Path):
    day = core.today()
    for machine, detail in (("windows", "no sync recorded"), ("mac", "push failed")):
        _write(
            tmp_root / summary_rel(day, machine),
            {
                "machine": machine,
                "date": day.isoformat(),
                "updated": core.iso(core.now()),
                "complete": True,
                "health": {"notes_sync": {"status": "fail", "detail": detail}},
            },
        )
    rows = {row["machine"]: row for row in live_health(tmp_root)["notes_sync"]}
    assert rows["windows"]["status"] == "unknown"
    assert rows["mac"]["status"] == "fail"


def test_every_stop_collects_locally_but_publication_is_on_cadence(tmp_root: Path, monkeypatch):
    from agent_cli.usage import collect

    (tmp_root / "agent.local.yaml").write_text("machine: mac\n", encoding="utf-8")
    collected = []
    published = []
    monkeypatch.setattr(collect, "collect_day", lambda *a, **k: collected.append(1) or sample_record())
    monkeypatch.setattr(publish, "publish", lambda r: published.append(1) or publish.Outcome("written"))
    monkeypatch.setattr(job.time, "time", lambda: 10000.0)
    assert job.work(tmp_root)["status"] == "written"
    assert job.work(tmp_root)["status"] == "written"
    assert len(collected) == 2 and len(published) == 1
    assert list(core.agent_path(tmp_root, "usage").glob("*/*/*.mac.md"))
    assert list(core.agent_path(tmp_root, "usage", "telemetry").glob("*.mac.json"))
    monkeypatch.setattr(job.time, "time", lambda: 10901.0)
    job.work(tmp_root)
    assert len(published) == 2
    monkeypatch.setattr(
        job, "queue", lambda r: {"requests": [{"seq": 1, "reason": "session-end"}], "done_seq": 0}
    )
    monkeypatch.setattr(job.time, "time", lambda: 11000.0)
    job.work(tmp_root)
    assert len(published) == 3
    monkeypatch.setattr(job, "queue", lambda r: {})
    monkeypatch.setattr(job.time, "time", lambda: 11850.0)
    job.work(tmp_root)
    assert len(published) == 3, "a final flush renews the cadence"


def test_day_capture_never_calls_network(tmp_root: Path, monkeypatch):
    monkeypatch.setattr("agent_cli.notes_sync.sync", lambda *a: pytest.fail("health must not sync"))
    result = capture(tmp_root, date(2026, 9, 3), sample_record())
    assert result["complete"] is False
    assert result["health"]["peer_link"]["status"] == "unknown"


def test_session_end_requests_a_flush_on_the_existing_hook(tmp_root: Path, monkeypatch):
    from agent_cli.hooks.session import capture_hook

    seen = []
    monkeypatch.setattr(job, "trigger_quietly", lambda root, reason: seen.append(reason))
    capture_hook(tmp_root, {"hook_event_name": "SessionEnd", "session_id": "ordinary"})
    assert seen == ["session-end"]


def test_notes_sync_exception_remains_reported_and_emitted(tmp_root: Path, notes_remote: Path, monkeypatch):
    from agent_cli import notes_sync
    from agent_cli.hooks.notes_sync import notes_sync_hook
    from agent_cli.telemetry.summary import event_rows

    def broken(*args):
        raise OSError("ordinary disk error")

    monkeypatch.setattr(notes_sync, "sync", broken)
    notes_sync_hook(tmp_root, {"hook_event_name": "Stop"})
    assert notes_sync.last_outcomes(tmp_root)["last_error"]["status"] == "error"
    moment = datetime.now(UTC)
    events, _ = event_rows(
        telemetry.Sink.for_main(tmp_root),
        moment - timedelta(seconds=10),
        moment + timedelta(seconds=1),
    )
    assert any(e["kind"] == "notes.sync" and e["data"]["status"] == "error" for e in events)


def test_window_spans_clip_waits_that_cross_days(tmp_root: Path):
    _emit(
        tmp_root,
        "dispatch.transition",
        START - timedelta(days=2),
        dispatch="001-mac-01",
        data={"attempt": 1, "phase": "awaiting-verification"},
    )
    _emit(
        tmp_root,
        "dispatch.transition",
        START + timedelta(seconds=10),
        dispatch="001-mac-01",
        data={"attempt": 1, "phase": "verifying"},
    )
    _emit(
        tmp_root,
        "dispatch.transition",
        START + timedelta(seconds=15),
        dispatch="001-mac-01",
        data={"attempt": 1, "phase": "ended"},
    )
    result = queries(tmp_root, START, START + timedelta(seconds=20), END)
    assert result["dispatch"]["seconds_by_phase"] == {"awaiting-verification": 10.0, "verifying": 5.0}
    assert result["dispatch"]["spans"] == 2


def test_duplicate_and_malformed_lines_do_not_inflate_counts(tmp_root: Path):
    _emit(tmp_root, "workflow.owner_question")
    path = core.agent_path(tmp_root, "telemetry", "events", "2026-09-03.jsonl")
    original = path.read_text(encoding="utf-8")
    path.write_text(original + original + "ordinary broken line\n", encoding="utf-8")
    result = queries(tmp_root, START, END, END)
    assert result["owner_questions"]["raised"] == 1
    assert result["complete"] is False


def test_health_counts_a_live_verify_wait_and_missing_spend(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: pid == 4242)
    _write(
        core.agent_path(tmp_root, "dispatches", "001-mac-01.json"),
        {
            "state": "running",
            "attempt": 1,
            "slot_released": {"attempt": 1},
            "worker_cleanup_confirmed": True,
            "supervisor_pid": 4242,
        },
    )
    result = capture(tmp_root, START.date(), {"machine": "mac"})
    assert result["health"]["verify_queue"]["count"] == 1
    assert result["health"]["verify_queue"]["status"] == "warn"
    assert result["health"]["spend_today"]["status"] == "unknown"
    assert result["health"]["spend_today"]["detail"] == "unavailable"


def test_final_flush_child_has_no_owner_reap_tag(tmp_root: Path, monkeypatch):
    from types import SimpleNamespace

    from agent_cli import launcher
    from agent_cli.proc_tags import TAG_DISPATCH, TAG_SESSION

    child_env = {}

    def spawn(*args, **kwargs):
        child_env.update(kwargs["env"])
        return SimpleNamespace(pid=4242), None

    monkeypatch.delenv(job.OFF_ENV)
    monkeypatch.setenv(TAG_SESSION, "ordinary-manager")
    monkeypatch.setenv(TAG_DISPATCH, "001-mac-01")
    monkeypatch.setenv("USAGE_TEST_MARK", "kept")
    monkeypatch.setattr(launcher, "spawn_supervisor", spawn)
    assert job.spawn(["ordinary-child"], tmp_root).pid == 4242
    assert TAG_SESSION not in child_env and TAG_DISPATCH not in child_env
    assert child_env["USAGE_TEST_MARK"] == "kept"
