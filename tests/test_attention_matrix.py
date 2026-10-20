"""Executed clock paints, question matrix and recorded owner-wait forms."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_cli import core, obsidian_tabs, peers, sessions, tab_drain
from agent_cli.hooks import ask_guard
from agent_cli.hooks.attention import clear_question
from agent_cli.hooks.task_waits import pending_after, reported_tasks, wait_count
from agent_cli.ops import peers as peer_ops
from agent_cli.ops import sessions as session_ops
from agent_cli.vault import live, managers

activity = importlib.import_module("agent_cli.hooks.activity")
SID = "01a00000-0000-7000-8000-0000abcd1234"
PLUGIN = Path(__file__).parents[1] / "harness/obsidian/plugins/agent-terminal-focus/main.js"
NODE = pytest.mark.skipif(shutil.which("node") is None, reason="Node executes the plugin")


@pytest.fixture
def manager(tmp_root, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(peer_ops, "is_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(managers, "claude_live_sessions", lambda: {})
    monkeypatch.setattr(sessions, "resolve_identity", lambda: (4242, True))
    monkeypatch.setattr("agent_cli.hooks.naming.live_name", lambda pid: (None, ""))
    sessions.set_record(tmp_root, SID, name="mgr-codex-abcd1234", pid=4242)
    sessions.set_field(tmp_root, SID, "provider", "codex")
    sessions.set_field(tmp_root, SID, "tab_display_name", "new · cx abcd")
    sessions.set_field(tmp_root, SID, "activity", {"state": "working", "at": core.iso(core.now())})
    return tmp_root


def event(root, name, **fields):
    activity.codex_activity(root, {"session_id": SID, "hook_event_name": name, **fields})


def record(root: Path) -> dict[str, Any]:
    result = sessions.get(root, SID)
    assert result is not None
    return result


def tab(root):
    snap = live.build(root)
    return next(t for t in snap["tabs"] if t["identity"] == SID)


PAINT = r"""
const Module = require('module');
const load = Module._load;
Module._load = function (r, ...args) {
  return r === 'obsidian' ? {Plugin: class {}, Modal: class {}} : load.call(this, r, ...args);
};
const [plugin, entry, moments] = process.argv.slice(1);
const {paintTabs} = require(plugin);
const leaf = {view: {state: {userTitle: '✅ new · cx abcd'}}, getDisplayText() {return this.view.state.userTitle;}};
const labels = JSON.parse(moments).map((now) => {
  paintTabs([leaf], [JSON.parse(entry)], now);
  return leaf.view.state.userTitle;
});
console.log(JSON.stringify(labels));
"""


def paint(entry, moments):
    run = subprocess.run(
        ["node", "-e", PAINT, str(PLUGIN), json.dumps(entry), json.dumps([t * 1000 for t in moments])],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(run.stdout)


@NODE
def test_permission_crosses_twenty_seconds_without_another_hook(manager, monkeypatch):
    start = core.now().timestamp()
    event(manager, "PermissionRequest", tool_name="Bash")
    entry = tab(manager)
    assert core.datetime.fromisoformat(entry["show_after"]).timestamp() == start + 20
    assert paint(entry, [start, start + 19, start + 20, start + 21]) == [
        "⚙️ new · cx abcd",
        "⚙️ new · cx abcd",
        "✋ new · cx abcd",
        "✋ new · cx abcd",
    ]
    live.write(manager)
    monkeypatch.setattr(tab_drain.time, "time", lambda: start + 19)
    key = entry["key"]
    assert tab_drain._targets(manager)[key]["label"].startswith("⚙️")
    state = {"painted": {key: {"label": entry["before_label"], "at": start}}}
    assert tab_drain._next_expiry(manager, state) == start + 20
    monkeypatch.setattr(tab_drain.time, "time", lambda: start + 20)
    assert tab_drain._targets(manager)[key]["label"].startswith("✋")


def test_fallback_runner_waits_and_paints_the_delayed_hand(manager, monkeypatch):
    from agent_cli import obsidian_run

    event(manager, "PermissionRequest", tool_name="Bash")
    live.write(manager)
    clock = [core.now().timestamp()]
    start = clock[0]
    monkeypatch.setattr(tab_drain.time, "time", lambda: clock[0])
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(obsidian_tabs, "enabled", lambda root=None: True)
    monkeypatch.setattr(obsidian_run, "running", lambda: True)
    paints = []

    def apply(root, entries):
        paints.extend((clock[0] - start, e["label"]) for e in entries)
        return {e["key"] for e in entries}, set()

    monkeypatch.setattr(tab_drain, "_apply", apply)
    tab_drain._save_state(manager, {"runner": os.getpid()})
    tab_drain.run(manager)
    assert paints == [(0, "⚙️ new · cx abcd"), (20.5, "✋ new · cx abcd")]


@pytest.mark.parametrize("resolution", ["PostToolUse", "Stop", "new-permission"])
def test_deadline_paint_rechecks_transition_after_selection(manager, monkeypatch, resolution):
    event(manager, "PermissionRequest", tool_name="Bash")
    live.write(manager)
    start = core.now()
    monkeypatch.setattr(tab_drain.time, "time", lambda: start.timestamp() + 20.5)
    entry = next(iter(tab_drain._targets(manager).values()))
    assert entry["label"].startswith("✋")
    monkeypatch.setenv("AGENT_NOW", core.iso(start + timedelta(seconds=18.6)))
    event(manager, "PostToolUse" if resolution == "new-permission" else resolution, tool_name="Bash")
    if resolution == "new-permission":
        event(manager, "PermissionRequest", tool_name="Bash")
    painted = []
    monkeypatch.setattr(tab_drain, "_eval", lambda entries: painted.extend(entries) or set())
    assert tab_drain._apply(manager, [entry]) == (set(), set())
    assert painted == []


@NODE
def test_needs_you_repaints_the_deadline_without_another_hook(manager, tmp_path):
    from live_pane_helpers import texts, view

    event(manager, "PermissionRequest", tool_name="Bash")
    snap = json.loads(json.dumps(live.build(manager), default=str))
    rendered = [texts(view(tmp_path, "needs_you", seconds, snap)) for seconds in (19, 20)]
    assert "Nothing is waiting on you" in rendered[0]
    assert "is waiting on you (" in rendered[1]


@NODE
@pytest.mark.parametrize("resolution, glyph", [("PostToolUse", "⚙️"), ("Stop", "✅")])
def test_permission_resolved_before_deadline_never_paints_a_hand(manager, monkeypatch, resolution, glyph):
    start = core.now()
    event(manager, "PermissionRequest", tool_name="Bash")
    before = tab(manager)
    monkeypatch.setenv("AGENT_NOW", core.iso(start + timedelta(seconds=10)))
    event(manager, resolution, tool_name="Bash")
    after = tab(manager)
    assert not after.get("show_after")
    assert paint(before, [start.timestamp() + 9])[0].startswith("⚙️")
    assert all(s.startswith(glyph) for s in paint(after, [start.timestamp() + 10, start.timestamp() + 21]))


@NODE
def test_owner_question_during_debounce_is_immediate_on_all_surfaces(manager):
    event(manager, "PermissionRequest", tool_name="Bash")
    session_ops.owner_question(manager, session_id=SID, summary="Which branch should I push?")
    entry = tab(manager)
    assert not entry.get("show_after")
    assert paint(entry, [core.now().timestamp()])[0].startswith("✋")
    needs = live.obligations(manager)["needs_you"]
    assert [(n["reason"], n["text"], n["show_after"]) for n in needs] == [
        ("question", "Which branch should I push?", None)
    ]


def test_claude_permissions_have_no_delay(manager):
    sessions.set_field(manager, SID, "provider", "claude")
    activity.activity(manager, {"session_id": SID, "hook_event_name": "PermissionRequest"})
    entry = tab(manager)
    assert entry["label"].startswith("✋") and not entry.get("show_after")


@pytest.mark.parametrize("alive, expected", [(False, live.VALID_IDLE_S), (True, live.VALID_RUNNING_S)])
def test_only_live_supervisors_select_the_short_validity_window(manager, alive, expected):
    ds = [{"id": "001-01", "state": "running", "supervisor_pid": 4242 if alive else 9999}]
    owed = live.obligations(manager, dispatches=ds)
    a = core.datetime.fromisoformat(owed["observed_at"])
    b = core.datetime.fromisoformat(owed["valid_until"])
    assert (b - a).total_seconds() == expected


def test_dead_workers_do_not_keep_a_manager_waiting(manager):
    sessions.set_field(manager, SID, "activity", {"state": "idle"})
    d = {"id": "001-01", "state": "running", "supervisor_pid": 9999, "launched_by_session": SID}
    label = activity.desired_label(manager, SID, record(manager), dispatches=[d])
    assert label is not None and label[2].startswith("✅")
    row = managers.rows(manager, dispatches=[d])[0]
    assert row["attention"] == "idle" and row["running"] == []


def test_counts_stay_on_the_dashboard_and_old_labels_still_match():
    assert obsidian_tabs.label_for("new · cx abcd", "idle", [], manager_action=12) == "✅ new · cx abcd"
    assert obsidian_tabs.label_name("✅? +12 new · cx abcd") == "new · cx abcd"


@pytest.mark.parametrize("tool", ["AskUserQuestion", "functions.request_user_input"])
def test_question_matrix_native_text_status_prompt_and_tool_resolution(manager, tool):
    questions = [{"question": "Which branch should I push?"}, {"question": "Should I merge it?"}]
    event(manager, "PreToolUse", tool_name=tool, tool_input={"questions": questions})
    question = record(manager)
    assert question["owner_question"] == "Which branch should I push?\nShould I merge it?"
    assert live.obligations(manager)["needs_you"][0]["text"] == question["owner_question"]
    event(manager, "UserPromptSubmit", prompt="What is the status?")
    assert record(manager)["owner_question_pending"]
    event(manager, "PostToolUse", tool_name="exec")
    assert record(manager)["owner_question_pending"]
    event(manager, "PostToolUse", tool_name=tool)
    assert not record(manager)["owner_question_pending"]
    assert record(manager)["owner_question_resolution"] == "answered"


@pytest.mark.parametrize("resolution", ["answered", "withdrawn", "replaced"])
def test_question_matrix_manual_resolution(manager, resolution):
    old = session_ops.owner_question(manager, session_id=SID, summary="Should I push?").data["question_id"]
    event(manager, "UserPromptSubmit", prompt="What is the status?")
    assert record(manager)["owner_question_id"] == old
    session_ops.owner_question(manager, session_id=SID, resolution=resolution)
    event(manager, "Stop")
    assert record(manager)["owner_question_resolution"] == resolution
    assert not record(manager)["owner_question_pending"]


def test_manual_reply_resolves_at_stop_after_work(manager):
    old = session_ops.owner_question(manager, session_id=SID, summary="Should I push?").data["question_id"]
    event(manager, "Stop")
    assert tab(manager)["label"].startswith("✋")
    event(manager, "UserPromptSubmit", prompt="Yes, push it.")
    assert record(manager)["owner_question_replied"]
    assert record(manager)["owner_question_id"] == old
    assert tab(manager)["label"].startswith("⚙️")
    assert live.obligations(manager)["needs_you"] == []
    event(manager, "PreToolUse", tool_name="exec")
    event(manager, "PostToolUse", tool_name="exec")
    assert tab(manager)["label"].startswith("⚙️")
    event(manager, "Stop")
    assert record(manager)["owner_question_resolution"] == "answered"
    assert record(manager)["owner_question_cleared_id"] == old
    assert not record(manager)["owner_question_replied"]
    assert not record(manager)["owner_question_pending"]
    assert tab(manager)["label"].startswith("✅")


def test_manual_question_reasked_after_a_status_prompt_stays_pending(manager):
    old = session_ops.owner_question(manager, session_id=SID, summary="Should I push?").data["question_id"]
    event(manager, "UserPromptSubmit", prompt="What is the status?")
    assert tab(manager)["label"].startswith("⚙️")
    new = session_ops.owner_question(manager, session_id=SID, summary="Should I push?").data["question_id"]
    event(manager, "Stop")
    assert old == new
    assert record(manager)["owner_question_pending"]
    assert not record(manager)["owner_question_replied"]
    assert tab(manager)["label"].startswith("✋")


@pytest.mark.parametrize("tool", ["AskUserQuestion", "functions.request_user_input"])
def test_sync_question_after_a_manual_reply_stays_pending_until_its_answer(manager, tool):
    old = session_ops.owner_question(manager, session_id=SID, summary="Should I push?").data["question_id"]
    event(manager, "UserPromptSubmit", prompt="What is the status?")
    event(manager, "PreToolUse", tool_name=tool, tool_input={"questions": [{"question": "Should I push?"}]})
    event(manager, "Stop")
    assert record(manager)["owner_question_cleared_id"] == old
    assert record(manager)["owner_question_resolution"] == "replaced"
    assert record(manager)["owner_question_pending"]
    assert not record(manager)["owner_question_replied"]
    assert tab(manager)["label"].startswith("✋")
    event(manager, "PostToolUse", tool_name=tool)
    assert record(manager)["owner_question_resolution"] == "answered"
    assert not record(manager)["owner_question_pending"]


def test_changed_manual_question_after_reply_replaces_old_question(manager):
    old = session_ops.owner_question(manager, session_id=SID, summary="Should I push?").data["question_id"]
    event(manager, "UserPromptSubmit", prompt="Yes.")
    new = session_ops.owner_question(manager, session_id=SID, summary="Should I merge?").data["question_id"]
    event(manager, "Stop")
    assert new != old
    assert record(manager)["owner_question_cleared_id"] == old
    assert record(manager)["owner_question_resolution"] == "replaced"
    assert record(manager)["owner_question_pending"]
    assert tab(manager)["label"].startswith("✋")


def test_blocked_stop_does_not_resolve_a_manual_reply(manager):
    session_ops.owner_question(manager, session_id=SID, summary="Should I push?")
    event(manager, "UserPromptSubmit", prompt="Yes.")
    event(manager, "Stop", _stop_blocked=True)
    assert record(manager)["owner_question_replied"]
    assert tab(manager)["label"].startswith("⚙️")
    event(manager, "Stop")
    assert record(manager)["owner_question_resolution"] == "answered"


@pytest.mark.parametrize(
    "prompt",
    [
        "<task-notification><task-id>shell</task-id><status>completed</status></task-notification>",
        "<agent-message>Finished.</agent-message>",
        "<cross-session-message>Finished.</cross-session-message>",
    ],
)
def test_machine_prompt_does_not_mark_a_manual_question_replied(manager, prompt):
    session_ops.owner_question(manager, session_id=SID, summary="Should I push?")
    event(manager, "UserPromptSubmit", prompt=prompt)
    event(manager, "Stop")
    assert record(manager)["owner_question_pending"]
    assert not record(manager).get("owner_question_replied")
    assert tab(manager)["label"].startswith("✋")


def test_a_different_manual_question_replaces_the_old_id(manager):
    a = session_ops.owner_question(manager, session_id=SID, summary="Should I push?").data["question_id"]
    b = session_ops.owner_question(manager, session_id=SID, summary="Should I merge?").data["question_id"]
    question = record(manager)
    assert a != b and question["owner_question_cleared_id"] == a
    assert question["owner_question_resolution"] == "replaced" and question["owner_question_pending"]


def test_compaction_keeps_background_tasks():
    assert pending_after({"hook_event_name": "SessionStart", "source": "compact"}, {"monitor": "t"}) is None


def test_resume_still_clears_tasks_from_a_previous_process():
    assert pending_after({"hook_event_name": "SessionStart", "source": "resume"}, {"monitor": "t"}) == {}


def test_monitor_omitted_at_stop_survives_until_its_finished_notice():
    tracked = pending_after({"hook_event_name": "PostToolUse", "tool_response": {"taskId": "monitor"}}, {})
    assert tracked and "monitor" in tracked
    assert reported_tasks({"hook_event_name": "Stop", "background_tasks": []}, tracked) is None
    assert (
        pending_after(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "<task-notification><task-id>monitor</task-id><status>completed</status></task-notification>",
            },
            tracked,
        )
        == {}
    )


def test_stop_clears_a_previously_listed_shell_but_keeps_an_unlisted_monitor(manager):
    event(manager, "PostToolUse", tool_response={"backgroundTaskId": "shell"})
    event(manager, "PostToolUse", tool_response={"taskId": "monitor"})
    event(manager, "Stop", background_tasks=[{"id": "shell", "type": "shell", "status": "running"}])
    assert set(record(manager)["background_tasks"]) == {"shell", "monitor"}
    assert tab(manager)["label"].startswith("⏳")
    event(manager, "Stop", background_tasks=[])
    assert set(record(manager)["background_tasks"]) == {"monitor"}
    assert tab(manager)["label"].startswith("⏳")
    event(
        manager,
        "UserPromptSubmit",
        prompt="<task-notification><task-id>monitor</task-id><status>completed</status></task-notification>",
    )
    event(manager, "Stop", background_tasks=[])
    assert record(manager)["background_tasks"] == {}
    assert tab(manager)["label"].startswith("✅")


@pytest.mark.parametrize("middle", ["missing-list", "compact"])
def test_stop_task_history_survives_a_missing_list_or_compaction(manager, middle):
    event(manager, "Stop", background_tasks=[{"id": "shell"}])
    if middle == "compact":
        event(manager, "SessionStart", source="compact")
    else:
        event(manager, "Stop")
    assert "shell" in record(manager)["background_tasks"]
    event(manager, "Stop", background_tasks=[])
    assert record(manager)["background_tasks"] == {}


@pytest.mark.parametrize("source", ["startup", "resume"])
def test_fresh_start_clears_stop_task_history_for_a_reused_id(manager, source):
    event(manager, "Stop", background_tasks=[{"id": "task"}])
    event(manager, "SessionStart", source=source)
    event(manager, "PostToolUse", tool_response={"taskId": "task"})
    event(manager, "Stop", background_tasks=[])
    assert "task" in record(manager)["background_tasks"]


def test_stop_reads_session_crons_and_clears_them_when_removed(manager):
    event(manager, "Stop", session_crons=[{"id": "cron-1", "schedule": "* * * * *"}])
    assert tab(manager)["label"].startswith("⏳")
    event(manager, "Stop", session_crons=[])
    assert tab(manager)["label"].startswith("✅")


@NODE
def test_expected_peer_reply_clears_wait_and_timeout_repaints_without_a_hook(manager, monkeypatch):
    monkeypatch.setattr(peer_ops, "wake_peer", lambda *a, **k: (True, "accepted"))
    sessions.set_record(manager, "peer-1", name="peer-one", pid=4242)
    peer_ops.peer_send(
        manager,
        session_id=SID,
        to="peer-one",
        message="Which result is ready?",
        expect_reply=True,
        reply_timeout=30,
    )
    event(manager, "Stop")
    entry = tab(manager)
    start = core.now().timestamp()
    assert paint(entry, [start + 29, start + 30]) == ["⏳ new · cx abcd", "✅ new · cx abcd"]
    live.write(manager)
    monkeypatch.setattr(tab_drain.time, "time", lambda: start + 29)
    state = {"painted": {entry["key"]: {"label": entry["label"], "at": start}}}
    assert tab_drain._next_expiry(manager, state) == start + 30
    monkeypatch.setattr(tab_drain.time, "time", lambda: start + 30)
    assert tab_drain._targets(manager)[entry["key"]]["label"].startswith("✅")
    peers.send(manager, to=SID, frm="other-peer", text="Unrelated news.")
    assert wait_count(record(manager)) == 1
    peers.send(manager, to=SID, frm="peer-1", text="The result is ready.")
    assert wait_count(record(manager)) == 0


def test_peer_wait_expires_by_clock_and_informational_sends_create_none(manager, monkeypatch):
    start = core.now()
    peers.begin_wait(manager, SID, "peer-1", 30)
    assert wait_count(record(manager)) == 1
    monkeypatch.setenv("AGENT_NOW", core.iso(start + timedelta(seconds=30)))
    assert wait_count(record(manager)) == 0
    monkeypatch.setattr(peer_ops, "wake_peer", lambda *a, **k: (True, "accepted"))
    sessions.set_record(manager, "peer-1", name="peer-one", pid=4242)
    peer_ops.peer_send(manager, session_id=SID, to="peer-one", message="Progress recorded.")
    assert wait_count(record(manager)) == 0


def test_long_peer_timeout_keeps_the_fallback_guard_alive(manager, monkeypatch):
    peers.begin_wait(manager, SID, "peer-1", 600)
    event(manager, "Stop")
    live.write(manager)
    entry = tab(manager)
    start = core.now().timestamp()
    monkeypatch.setattr(tab_drain.time, "time", lambda: start)
    state = {"painted": {entry["key"]: {"label": entry["label"], "at": start}}}
    assert tab_drain._next_expiry(manager, state) == start + tab_drain.MAX_WAIT_S


def test_remote_reply_only_clears_its_own_device_wait(manager):
    peers.begin_wait(manager, SID, "cx ef12", 600, device="other-machine")
    peers.reply_arrived(manager, SID, {"from": "peer", "from_name": "cx ef12"})
    assert wait_count(record(manager)) == 1
    peers.reply_arrived(
        manager, SID, {"from": "01a00000-0000-7000-8000-0000ef123456", "remote": {"device": "other-machine"}}
    )
    assert wait_count(record(manager)) == 0


def test_failed_enqueue_does_not_leave_a_peer_wait(manager, monkeypatch):
    from agent_cli.ops import peer_delivery

    sessions.set_record(manager, "peer-1", name="peer-one", pid=4242)
    monkeypatch.setattr(
        peer_delivery, "deliver", lambda *a, **k: (_ for _ in ()).throw(OSError("unavailable"))
    )
    with pytest.raises(OSError):
        peer_ops.peer_send(manager, session_id=SID, to="peer-one", message="Which result?", expect_reply=True)
    assert wait_count(record(manager)) == 0
    assert not live.build(manager)["tabs"][0].get("show_until")


# Reported text only. W1 is excluded: its ending announces reconciliation, phase B's contract.
# Windows provides exact excerpts for these cases, but not the complete command-handoff replies.
OWNER_WAITS = [
    ("authorization-1", "The push awaits your authorization."),
    ("authorization-2", "Awaiting your authorization."),
    ("grants", "The grants are still unapproved."),
    ("finish", "Tell me when the run finishes."),
    ("enter", "Press enter in its tab."),
    ("mac-commands", "The Mac cells are waiting on your three `!` commands from my last message."),
    ("push-handoff", "1. Push `a1b2c3d` to origin from the Mac."),
    ("command-only", "Run the approved command, then tell me when it finishes."),
    ("commands-list", "Needs you:\n- Run the approved command."),
]


@pytest.mark.parametrize("case,text", OWNER_WAITS, ids=[r[0] for r in OWNER_WAITS])
def test_owner_wait_corpus_blocks_once_and_rechecks_the_next_turn(manager, case, text):
    for _ in range(2):
        ask_guard.ask_guard(manager, {"session_id": SID, "hook_event_name": "UserPromptSubmit"})
        payload = {"session_id": SID, "hook_event_name": "Stop", "last_assistant_message": text}
        assert json.loads(ask_guard.ask_guard(manager, payload))["decision"] == "block"
        assert ask_guard.ask_guard(manager, payload) == ""
    session_ops.owner_question(manager, session_id=SID, summary=text)
    assert tab(manager)["label"].startswith("✋")
    assert ask_guard.ask_guard(manager, payload) == ""
    clear_question(manager, SID, "withdrawn")


@pytest.mark.parametrize(
    "text",
    [
        "No authorization is waiting on you.",
        "The worker awaits the build.",
        "I'm reconciling prior attempt usage before running the authorized pair.",
        "> Run the approved command.",
        'The documentation says "Press enter in its tab."',
    ],
)
def test_owner_wait_controls_do_not_classify_other_endings(text):
    assert ask_guard.asks_in(text) == []


@NODE
def test_provisional_titles_expire_at_thirty_seconds_and_claimed_tabs_survive():
    body = (
        PAINT.split("const [plugin, entry, moments]")[0]
        + r"""
const {expireProvisional} = require(process.argv[1]);
const leaves = ['✅ Codex manager', '⏳ Claude manager', '✋ new · cx abcd'].map(userTitle => ({view: {state: {userTitle}}}));
const seen = new WeakMap();
expireProvisional(leaves, seen, 1000);
expireProvisional(leaves, seen, 30999);
const before = leaves.map(l => l.view.state.userTitle);
expireProvisional(leaves, seen, 31000);
console.log(JSON.stringify({before, after: leaves.map(l => l.view.state.userTitle)}));
"""
    )
    result = subprocess.run(
        ["node", "-e", body, str(PLUGIN)], capture_output=True, encoding="utf-8", check=True
    )
    assert json.loads(result.stdout) == {
        "before": ["✅ Codex manager", "⏳ Claude manager", "✋ new · cx abcd"],
        "after": ["", "", "✋ new · cx abcd"],
    }


@NODE
def test_expired_provisional_tab_can_still_be_claimed_by_its_marker():
    marker = "codex:11111111-1111-4111-8111-111111111111"
    code = obsidian_tabs.eval_code(
        "mgr-x",
        "✅ x",
        claim_active=True,
        provisional_title=obsidian_tabs.CODEX_STARTING_TITLE,
        launch_marker=marker,
    )
    script = (
        "const leaf = {view: {state: {userTitle: '', profile: {args: ["
        + json.dumps(marker)
        + "]}}}, getDisplayText: () => 'terminal'};"
        "const app = {workspace: {activeLeaf: null, getLeavesOfType: () => [leaf]}};"
        "const result = "
        + code
        + "; console.log(JSON.stringify({result, title: leaf.view.state.userTitle}));"
    )
    run = subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", check=True)
    outcome = json.loads(run.stdout)
    assert "relabeled 1 hits=1" in outcome["result"] and outcome["title"] == "✅ x"
