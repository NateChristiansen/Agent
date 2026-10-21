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
from agent_cli.telemetry.dashboard import capture, lesson_row, live_health, machine_rows, peer_link
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


def _facts(**changes: dict) -> dict:
    """A healthy machine's captured facts, with `changes` merged into the named signals."""
    at = core.iso(core.now() - timedelta(minutes=2))
    facts = {
        "notes_sync": {
            "mode": "on",
            "attempt_at": at,
            "attempt_status": "current",
            "last_ok_at": at,
            "error_current": False,
        },
        "peer_link": {"enrolled": False},
        "hook_errors": {"count": 0, "last": None},
        "vault_display": {"tab_paint_failures": 0, "render_failures": 0, "heartbeat_at": at},
        "coverage": {"events": True, "plugin_metrics": True, "pane_metrics": True, "losses": {}},
    }
    for key, value in changes.items():
        facts[key] = {**facts[key], **value}
    return facts


def _record(machine: str, facts: dict, *, age: timedelta = timedelta(0)) -> dict:
    return {
        "schema": 2,
        "machine": machine,
        "date": core.today().isoformat(),
        "updated": core.iso(core.now() - age),
        "complete": False,
        "health": facts,
    }


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _hhmm() -> str:
    return core.now().astimezone().strftime("%H:%M")


def _rows(record: dict) -> dict[str, dict]:
    return {row["signal"]: row for row in machine_rows(record, core.now())}


def test_a_healthy_machine_is_ok_and_its_rows_expire_from_the_observation(tmp_root: Path):
    record = _record("mac", _facts(), age=timedelta(minutes=3))
    rows = _rows(record)
    assert {k: r["state"] for k, r in rows.items()} == {
        "notes_sync": "ok",
        "hook_errors": "ok",
        "vault_display": "ok",
        "machine": "unknown",
    }
    assert "peer_link" not in rows, "an install that never enrolled has no pairing row"
    expected = core.now() + timedelta(minutes=12)
    assert {_at(r["valid_until"]) for k, r in rows.items() if k != "machine"} == {expected}
    assert all(r["expired"] is None for k, r in rows.items() if k != "machine")
    assert _at(rows["machine"]["show_after"]) == expected
    assert rows["machine"]["note"] == "last report had no problems"
    assert rows["notes_sync"]["ages"][0]["prefix"] == " · last sync "
    assert all(r["meaning"] for r in rows.values())
    assert all(r["action"] is None for r in rows.values() if r["state"] == "ok")


def test_this_machines_rows_expire_one_by_one_without_a_machine_line(tmp_root: Path):
    rows = {r["signal"]: r for r in machine_rows(_record("mac", _facts()), core.now(), local=True)}
    assert "machine" not in rows
    hooks = rows["hook_errors"]["expired"]
    assert hooks["state"] == "unknown" and hooks["headline"] == "last report: no errors today"
    assert hooks["ages"][-1]["prefix"] == " · observed "
    missing = machine_rows({"machine": "mac"}, core.now(), local=True)
    assert [(r["signal"], r["headline"]) for r in missing] == [("machine", "no report today")]


def test_a_recovered_sync_error_is_history_and_offline_never_asks_the_owner(tmp_root: Path):
    error = {"last_error_at": core.iso(core.now()), "last_error": "fetch failed: no route"}
    recovered = _rows(_record("mac", _facts(notes_sync={**error, "last_error_status": "conflict"})))
    assert recovered["notes_sync"]["state"] == "ok"
    assert "fetch failed" not in str(recovered["notes_sync"])
    offline = _rows(
        _record("mac", _facts(notes_sync={**error, "last_error_status": "offline", "error_current": True}))
    )["notes_sync"]
    assert offline["state"] == "unknown"
    assert offline["headline"].startswith("offline since ") and "retries by itself" in offline["headline"]
    assert offline["action"].startswith("Nothing")
    conflict = _rows(
        _record("mac", _facts(notes_sync={**error, "last_error_status": "conflict", "error_current": True}))
    )["notes_sync"]
    assert conflict["state"] == "action"
    assert conflict["headline"] == f"conflict since {_hhmm()}"
    assert conflict["note"] == "fetch failed: no route"
    assert "resolve" in conflict["action"]
    nothing = _rows(_record("mac", _facts(notes_sync={"attempt_at": None})))["notes_sync"]
    assert nothing["state"] == "unknown" and nothing["headline"] == "no sync recorded"
    assert "notes_sync" not in _rows(_record("mac", _facts(notes_sync={"mode": "off"})))


def test_an_old_accepted_send_is_delivery_history_not_reachability(tmp_root: Path):
    old = core.iso(core.now() - timedelta(hours=3))
    accepted = _rows(
        _record(
            "mac",
            _facts(
                peer_link={"enrolled": True, "readable": True, "state": "accepted", "at": old, "alias": "win"}
            ),
        )
    )["peer_link"]
    assert accepted["state"] == "ok"
    assert accepted["headline"] == "last message to win accepted"
    assert accepted["ages"] == [{"prefix": " ", "at": old, "suffix": " ago"}]
    assert "reachable" not in accepted["headline"]
    assert "does not show that the other machine is reachable" in accepted["meaning"]

    def pending(minutes: int) -> dict:
        at = core.iso(core.now() - timedelta(minutes=minutes))
        fact = {"enrolled": True, "readable": True, "state": "pending", "at": at, "alias": "win"}
        return _rows(_record("mac", _facts(peer_link=fact)))["peer_link"]

    assert pending(2)["state"] == "ok"
    assert pending(11)["state"] == "action"
    assert pending(11)["headline"] == "message to win not delivered"
    refused = {"enrolled": True, "readable": True, "state": "refused:auth", "at": old}
    assert _rows(_record("mac", _facts(peer_link=refused)))["peer_link"]["state"] == "action"
    unreadable = _rows(_record("mac", _facts(peer_link={"enrolled": True, "readable": False})))
    assert unreadable["peer_link"]["state"] == "unknown"


def test_incomplete_telemetry_qualifies_the_counts_it_feeds(tmp_root: Path):
    lossy = _rows(_record("mac", _facts(coverage={"losses": {"malformed": 2, "gaps": 0}})))
    for key in ("hook_errors", "vault_display"):
        assert lossy[key]["state"] == "unknown"
        assert "telemetry incomplete (2 malformed)" in lossy[key]["headline"]
        assert lossy[key]["action"]
    assert lossy["notes_sync"]["state"] == "ok"
    unread = _rows(_record("mac", _facts(coverage={"plugin_metrics": False})))
    assert unread["hook_errors"]["state"] == "ok"
    assert unread["vault_display"]["headline"].endswith("(tab metrics missing or unreadable)")
    counted = _rows(
        _record(
            "mac",
            _facts(
                hook_errors={
                    "count": 3,
                    "last": {"hook": "activity", "reason": "OSError", "at": core.iso(core.now())},
                },
                coverage={"events": False},
            ),
        )
    )
    assert counted["hook_errors"]["state"] == "action"
    assert counted["hook_errors"]["headline"] == f"3 errors today · last: activity (OSError), {_hhmm()}"
    display = _rows(_record("mac", _facts(vault_display={"tab_paint_failures": 1, "render_failures": 2})))
    assert display["vault_display"]["headline"] == "1 tab paint failure · 2 render failures today"
    silent = _rows(_record("mac", _facts(vault_display={"heartbeat_at": None})))["vault_display"]
    assert silent["state"] == "unknown"


def test_an_open_usage_day_alone_does_not_make_telemetry_incomplete(tmp_root: Path, monkeypatch):
    from agent_cli import config_file, notes_sync

    monkeypatch.setattr(notes_sync, "last_outcomes", lambda root: {})
    monkeypatch.setattr(config_file, "notes_sync_mode", lambda root: "on")
    moment = core.now()
    _emit(
        tmp_root, "hook.error", moment - timedelta(minutes=5), data={"hook": "activity", "reason": "OSError"}
    )
    _emit(tmp_root, "hook.error", moment - timedelta(minutes=1), data={"hook": "usage", "reason": "KeyError"})
    result = capture(
        tmp_root, moment.astimezone().date(), {"machine": "mac", "status": "partial"}, now=moment
    )
    assert result["complete"] is False, "the record still says the usage day is open"
    coverage = result["health"]["coverage"]
    assert coverage["events"] is True and not any(coverage["losses"].values())
    assert coverage["plugin_metrics"] is True, "no metrics file yet is nothing lost"
    assert result["health"]["hook_errors"]["count"] == 2
    assert result["health"]["hook_errors"]["last"]["hook"] == "usage"
    assert result["health"]["hook_errors"]["last"]["reason"] == "KeyError"
    assert "verify_queue" not in result["health"] and "spend_today" not in result["health"]
    rows = _rows(result)
    assert rows["hook_errors"]["headline"].startswith("2 errors today · last: usage (KeyError), ")


def test_a_silent_machine_is_one_line_and_keeps_its_problems_as_history(tmp_root: Path):
    (tmp_root / "agent.local.yaml").write_text("machine: mac\n", encoding="utf-8")
    facts = _facts(hook_errors={"count": 1, "last": {"hook": "activity", "at": core.iso(core.now())}})
    _write(tmp_root / summary_rel(core.today(), "windows"), _record("windows", facts, age=timedelta(hours=3)))
    _write(
        core.agent_path(tmp_root, "usage", "telemetry", f"{core.today()}.mac.json"), _record("mac", _facts())
    )
    rows = {r["key"]: r for r in live_health(tmp_root)["rows"]}
    windows = rows["machine:windows"]
    assert windows["state"] == "unknown" and windows["title"] == "windows"
    assert windows["headline"] == "no report" and windows["ages"][0]["at"] == windows["updated"]
    assert windows["note"] == "last report had 1 problem"
    hooks = rows["hook_errors:windows"]
    assert _at(hooks["valid_until"]) < core.now()
    assert hooks["expired"]["state"] == "unknown"
    assert hooks["expired"]["headline"].startswith("last report: 1 error today")
    assert rows["notes_sync:windows"]["expired"] is None, "a healthy row folds into the machine line"
    assert _at(rows["notes_sync:mac"]["valid_until"]) > core.now()
    assert "lessons:" in rows
    assert not any(k.startswith(("verify_queue", "spend_today", "telemetry")) for k in rows)


def test_a_machine_with_no_record_today_is_unknown(tmp_root: Path):
    (tmp_root / "agent.local.yaml").write_text("machine: mac\n", encoding="utf-8")
    checkpoint = tmp_root / "usage/machines/windows.md"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("", encoding="utf-8")
    rows = {r["key"]: r for r in live_health(tmp_root)["rows"]}
    assert rows["machine:windows"]["headline"] == "no report today"
    assert rows["machine:windows"]["show_after"] is None
    assert rows["machine:mac"]["headline"] == "no report today"


def test_a_record_from_an_older_harness_keeps_its_own_status(tmp_root: Path):
    record = {
        "machine": "windows",
        "updated": core.iso(core.now()),
        "health": {
            "notes_sync": {"status": "fail", "detail": "push failed"},
            "render_failures": {"status": "ok", "detail": "0"},
            "spend_today": {"status": "warn", "detail": "$2.00"},
        },
    }
    rows = _rows(record)
    assert rows["notes_sync"]["state"] == "action" and rows["notes_sync"]["headline"] == "push failed"
    assert rows["vault_display"]["state"] == "ok"
    assert set(rows) == {"notes_sync", "vault_display", "machine"}
    record["health"]["tab_paint_failures"] = {"status": "ok", "detail": "0"}
    record["health"]["render_failures"] = {"status": "fail", "detail": "2"}
    merged = _rows(record)["vault_display"]
    assert merged["state"] == "action", "a healthy tab paint must not hide the render failures"
    assert merged["headline"] == "0 · 2"


def test_vault_display_is_only_as_fresh_as_the_plugin_heartbeat(tmp_root: Path):
    old = core.iso(core.now() - timedelta(hours=3))
    stale = _rows(_record("mac", _facts(vault_display={"heartbeat_at": old})))["vault_display"]
    assert stale["state"] == "unknown"
    assert stale["headline"] == "the Agent Terminal plugin has not reported since"
    assert stale["ages"] == [{"prefix": " ", "at": old, "suffix": " ago"}]
    minutes = core.iso(core.now() - timedelta(minutes=10))
    aging = _rows(_record("mac", _facts(vault_display={"heartbeat_at": minutes})))
    assert aging["vault_display"]["state"] == "ok"
    assert aging["vault_display"]["updated"] == minutes
    assert _at(aging["vault_display"]["valid_until"]) == core.now() + timedelta(minutes=5)
    assert aging["vault_display"]["expired"]["state"] == "unknown", "it expires alone, never silently"
    assert _at(aging["hook_errors"]["valid_until"]) == core.now() + timedelta(minutes=15)


def test_a_silent_machine_keeps_its_unknown_reasons_and_next_steps(tmp_root: Path):
    lossy = _facts(coverage={"events": False})
    rows = _rows(_record("windows", lossy, age=timedelta(hours=3)))
    for key in ("hook_errors", "vault_display"):
        expired = rows[key]["expired"]
        assert expired["state"] == "unknown"
        assert expired["headline"] == f"last report: {rows[key]['headline']}"
        assert "event log missing or unreadable" in expired["headline"]
        assert expired["action"].endswith(rows[key]["action"])
    assert rows["notes_sync"]["expired"] is None
    assert rows["machine"]["note"] == "last report had 2 problems"


@pytest.mark.parametrize(
    ("count", "state", "headline"),
    [
        (0, "ok", "lessons: 0 open"),
        (9, "ok", "lessons: 9 open"),
        (10, "action", "lessons: 10 open — review needed"),
    ],
)
def test_the_lesson_row_counts_the_local_queue(tmp_root: Path, count: int, state: str, headline: str):
    from agent_cli import lessons

    for n in range(count):
        lessons.add(tmp_root, title=f"finding {n}", source="none", evidence="ordinary evidence", home="")
    row = lesson_row(tmp_root, {"observed_at": "a", "valid_until": "b"})
    assert (row["state"], row["headline"], row["count"], row["machine"]) == (state, headline, count, None)
    assert row["brief"] == headline
    assert row["observed_at"] == "a" and row["valid_until"] == "b" and row["updated"] is None
    assert (row["action"] is not None) is (state == "action")


def test_an_unreadable_lesson_queue_is_unknown_not_empty(tmp_root: Path):
    from agent_cli import lessons

    assert lesson_row(tmp_root, {})["headline"] == "lessons: 0 open", "a missing queue is empty"
    (tmp_root / lessons.QUEUE).mkdir(parents=True)
    row = lesson_row(tmp_root, {})
    assert row["state"] == "unknown" and row["count"] is None
    assert row["headline"] == "lessons: unknown — queue unreadable"
    assert row["action"]


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
    assert result["health"]["peer_link"] == {"enrolled": False}


def test_peer_link_reads_enrollment_and_the_newest_send(tmp_root: Path):
    import sqlite3

    path = core.agent_path(tmp_root, "peer-net", "state.db")
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE enrollment (k TEXT, v TEXT)")
    conn.execute("CREATE TABLE outbox (state TEXT, updated_at REAL, alias TEXT)")
    conn.commit()
    assert peer_link(tmp_root) == {"enrolled": False}
    conn.execute("INSERT INTO enrollment VALUES ('device', 'mac')")
    conn.commit()
    assert peer_link(tmp_root) == {"enrolled": True, "readable": True, "state": None}
    conn.executemany(
        "INSERT INTO outbox VALUES (?, ?, ?)", [("accepted", 100.0, "win"), ("pending", 200.0, "win")]
    )
    conn.commit()
    conn.close()
    assert peer_link(tmp_root) == {
        "enrolled": True,
        "readable": True,
        "state": "pending",
        "at": datetime.fromtimestamp(200.0, UTC).isoformat(),
        "alias": "win",
    }
    path.write_bytes(b"not a database")
    assert peer_link(tmp_root) == {"enrolled": True, "readable": False}


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
