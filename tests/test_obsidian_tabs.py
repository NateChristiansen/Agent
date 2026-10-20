"""A manager session's Obsidian Terminal tab reads ✋ while it waits on the owner
and ⏳ while its worker runs, set through the Terminal plugin's own rename path."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent_cli import obsidian_tabs, tab_drain


def test_compact_manager_display_names_keep_a_fixed_identity():
    claude = "1b0699aa-1111-2222-3333-444444444444"
    codex = "01a00000-0000-7000-8000-0000abcd1234"
    assert (
        obsidian_tabs.manager_display_name("mgr-038-tab-width-and-titles", claude, "claude", generated=True)
        == "038 tab-width-and-tit… · cl 1b06"
    )
    assert (
        obsidian_tabs.manager_display_name(
            "mgr-038-tab-width-and-titles-codex-abcd1234", codex, "codex", generated=True
        )
        == "038 tab-width-and-tit… · cx abcd"
    )
    assert (
        obsidian_tabs.manager_display_name("mgr-unmapped-1b0699", claude, "claude", generated=True)
        == "new · cl 1b06"
    )
    assert (
        obsidian_tabs.manager_display_name("my-name", claude, "claude", generated=False)
        == "my-name · cl 1b06"
    )
    assert obsidian_tabs.manager_identity(claude, "claude") == " · cl 1b06"


def test_the_short_name_is_what_the_tab_shows_for_both_clients():
    """`peer list` and `peer send` use this; the tab suffix is built from it."""
    claude = "1b0699aa-1111-2222-3333-444444444444"
    codex = "01a1007c-0000-7000-8000-00008ddb0001"
    assert obsidian_tabs.short_name(claude, "claude") == "cl 1b06"
    assert obsidian_tabs.short_name(codex, "codex") == "cx 8ddb"
    assert obsidian_tabs.manager_identity(codex, "codex") == " · " + obsidian_tabs.short_name(codex, "codex")


def test_variable_tab_name_is_capped_before_the_stable_suffix():
    sid = "1b0699aa-1111-2222-3333-444444444444"
    display = obsidian_tabs.manager_display_name
    assert display("mgr-039-rubric-and-codex-dispatch", sid, "claude", generated=True) == (
        "039 rubric-and-codex… · cl 1b06"
    )
    assert display("mgr-039-abcdefghijklmnopqr", sid, "claude", generated=True) == (
        "039 abcdefghijklmnopqr · cl 1b06"
    )
    assert display("long-owner-chosen-name", sid, "claude", generated=False) == (
        "long-owner-chosen… · cl 1b06"
    )
    assert display("abcdefghijklmnopq-rest", sid, "claude", generated=False) == (
        "abcdefghijklmnopq… · cl 1b06"
    )


def test_labels_by_state():
    """All four states are the harness's own. The label used to be written for two
    and cleared otherwise, which let the client's own glyph stand for working and idle — fine
    under Claude Code, meaningless under anything else."""
    assert obsidian_tabs.label_for("mgr-020-x", "waiting_on_you", []) == "✋ mgr-020-x"
    assert obsidian_tabs.label_for("mgr-020-x", "idle", ["001-01"]) == "⏳ mgr-020-x"
    assert obsidian_tabs.label_for("mgr-020-x", "idle", []) == "✅ mgr-020-x"
    assert obsidian_tabs.label_for("mgr-020-x", "working", ["001-01"]) == "⚙️ mgr-020-x"
    # nothing ever clears the label, or the client draws whatever it likes in the gap
    assert obsidian_tabs.label_for("mgr-020-x", None, []) == "✅ mgr-020-x"


def test_background_work_waits_like_a_worker_does():
    """Idle with a backgrounded command outstanding is waiting, not at rest.

    The owner reported the tab reading as finished while this session sat blocked on a background
    test suite. With no worker and no background work the label still clears, or every session
    would read ⏳ forever.
    """
    assert obsidian_tabs.label_for("mgr-030-x", "idle", [], 1) == "⏳ mgr-030-x"
    assert obsidian_tabs.label_for("mgr-030-x", "idle", [], 0) == "✅ mgr-030-x"
    # working wins over both: the session is not waiting on anything, it is running
    assert obsidian_tabs.label_for("mgr-030-x", "working", [], 3) == "⚙️ mgr-030-x"
    # waiting on the owner outranks background work, because only one of them needs them
    assert obsidian_tabs.label_for("mgr-030-x", "waiting_on_you", [], 3) == "✋ mgr-030-x"


def test_a_worker_waiting_on_the_owner_reads_as_needing_them():
    """Two idle managers were seen holding hourglasses while the session
    that actually needed them showed nothing. A finished worker nobody has dealt with is the
    owner's to action, so it reads ✋, and it outranks a running worker's ⏳."""
    assert obsidian_tabs.label_for("mgr-023-x", "idle", [], 0, ["002-18"]) == "✋ mgr-023-x"
    # even while another worker still runs: the person is blocked, the machine is not
    assert obsidian_tabs.label_for("mgr-023-x", "idle", ["002-20"], 0, ["002-18"]) == "✋ mgr-023-x"
    # once it is actioned the list empties and the tab falls back to whatever is still true
    assert obsidian_tabs.label_for("mgr-023-x", "idle", ["002-20"], 0, []) == "⏳ mgr-023-x"
    assert obsidian_tabs.label_for("mgr-023-x", "idle", [], 0, []) == "✅ mgr-023-x"


def test_a_background_task_opens_on_its_result_and_closes_on_its_notification():
    """Tracked from events, never from `ps`: a session owns plenty of live
    processes it is not waiting on, and every exclusion invited the next one.

    Both handles are real and differ by tool, which is why both are read: a backgrounded `Bash`
    returns `backgroundTaskId`, a `Monitor` returns `taskId`. The notification that ends either
    arrives as an ordinary `UserPromptSubmit` carrying the same id in a `<task-id>` tag.
    """
    from agent_cli.hooks.task_waits import pending_after

    started = pending_after(
        {"hook_event_name": "PostToolUse", "tool_response": {"backgroundTaskId": "b9fmq5zfx"}}, {}
    )
    assert started is not None and list(started) == ["b9fmq5zfx"]

    both = pending_after(
        {"hook_event_name": "PostToolUse", "tool_response": {"taskId": "b5casmac7"}}, started
    )
    assert both is not None and sorted(both) == ["b5casmac7", "b9fmq5zfx"]

    done = pending_after(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "<task-notification>\n<task-id>b9fmq5zfx</task-id>\n<status>completed</status>",
        },
        both,
    )
    assert done is not None and list(done) == ["b5casmac7"]

    # an ordinary prompt changes nothing, or typing would clear the tab
    assert pending_after({"hook_event_name": "UserPromptSubmit", "prompt": "carry on"}, done) is None
    # nor does a notification for a task this session never recorded
    assert (
        pending_after(
            {"hook_event_name": "UserPromptSubmit", "prompt": "<task-id>someone-elses</task-id>"}, done
        )
        is None
    )
    # a backgrounded subagent is the third handle, and closes the same way
    with_agent = pending_after(
        {
            "hook_event_name": "PostToolUse",
            "tool_response": {"isAsync": True, "agentId": "a554bb00fa2d78815"},
        },
        done,
    )
    assert with_agent is not None and "a554bb00fa2d78815" in with_agent

    # an inline subagent has already finished by the time this hook sees it. Opening a task for
    # one would never close, and would pin the tab to the waiting glyph — the inverted failure.
    assert (
        pending_after({"hook_event_name": "PostToolUse", "tool_response": {"agentId": "inline-one"}}, done)
        is None
    )

    # a tool result without a handle is not a background task
    assert pending_after({"hook_event_name": "PostToolUse", "tool_response": {"stdout": ""}}, done) is None
    # and a crashed session cannot strand one: the set is emptied when a session starts
    assert pending_after({"hook_event_name": "SessionStart"}, done) == {}
    assert pending_after({"hook_event_name": "SessionStart"}, {}) is None


def test_a_watch_tab_carries_claude_s_glyphs_by_worker_state():
    assert obsidian_tabs.worker_label("003-22", "running") == "◐ 003-22 watch"
    assert obsidian_tabs.worker_label("003-22", "queued") == "◐ 003-22 watch"
    for state in ("question", "blocked", "failed"):
        assert obsidian_tabs.worker_label("003-22", state) == "✋ 003-22 watch"
    assert obsidian_tabs.worker_label("003-22", "done") == "✳ 003-22 watch"
    # a watch tab is found only by its own label, never by the displayed title
    code = obsidian_tabs.eval_code("003-22 watch", "✳ 003-22 watch", match_generated=False)
    assert "getDisplayText" not in code and '"003-22 watch"' in code


def test_eval_code_uses_the_rename_path_and_matches_the_displayed_title():
    """The view state has no `titleGenerated` in this Terminal build: probed 2026-09-17, every
    leaf had it null while the tabs read `◑ mgr-NNN-slug`, so the old matcher labelled nothing
    and still reported success."""
    code = obsidian_tabs.eval_code("mgr-020-x", "✋ mgr-020-x")
    assert "getLeavesOfType('terminal:terminal')" in code
    assert "c.userTitle = " in code and "v.state = c" in code  # what the plugin's rename dialog does
    assert "leaf.getDisplayText()" in code and '"mgr-020-x"' in code
    assert "titleGenerated" not in code


def test_manager_tab_match_requires_the_full_name():
    """Claude's feature name is a prefix of Codex's; one update must not select both."""
    code = obsidian_tabs.eval_code("mgr-001-example-feature", "✅ mgr-001-example-feature")
    assert ".endsWith(' ' + name)" in code
    assert ".includes(name)" not in code
    assert ".endsWith(name)" not in code


def test_codex_claim_requires_a_confirmed_relabel(tmp_root: Path, monkeypatch):
    from subprocess import CompletedProcess

    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.setenv("AGENT_ROOT", str(tmp_root))
    monkeypatch.setattr(obsidian_tabs.time, "sleep", lambda seconds: None)
    monkeypatch.setattr("agent_cli.dispatch.opener.obsidian_cli", lambda: "/usr/local/bin/obsidian")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kw: CompletedProcess(
            args[0], 0, stdout="=> relabeled 0 active_terminal=true agent_title=false", stderr=""
        ),
    )
    assert not obsidian_tabs.claim_active("mgr-codex-x", "⚙️ mgr-codex-x")
    failures = tmp_root / ".agent/tabs/claim-failures.jsonl"
    assert (
        json.loads(failures.read_text().splitlines()[-1])["reason"]
        == "active-terminal-title-not-ready; attempts=3"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kw: CompletedProcess(args[0], 0, stdout="=> relabeled 1", stderr=""),
    )
    assert obsidian_tabs.claim_active("mgr-codex-x", "⚙️ mgr-codex-x")
    assert len(failures.read_text().splitlines()) == 1


def test_codex_claim_retries_until_the_terminal_title_is_ready(tmp_root: Path, monkeypatch):
    from subprocess import CompletedProcess

    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.setenv("AGENT_ROOT", str(tmp_root))
    monkeypatch.setattr("agent_cli.dispatch.opener.obsidian_cli", lambda: "/usr/local/bin/obsidian")
    monkeypatch.setattr(obsidian_tabs.time, "sleep", lambda seconds: None)
    outputs = iter(
        [
            "=> relabeled 0 active_terminal=true agent_title=false",
            "=> relabeled 1 active_terminal=true agent_title=true",
        ]
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kw: CompletedProcess(args[0], 0, stdout=next(outputs), stderr=""),
    )
    assert obsidian_tabs.claim_active("mgr-codex-x", "⚙️ mgr-codex-x")
    assert not (tmp_root / ".agent/tabs/claim-failures.jsonl").exists()


def test_a_permission_prompt_relabels_a_session_whose_record_has_no_name(
    tmp_root: Path, tmp_path: Path, monkeypatch
):
    """Found live: the record's `name` was null while Claude Code showed
    `mgr-022-...`, so the relabel quit with "unnamed session" and no ✋ appeared. The relabel
    now falls back to Claude Code's own name for the session's pid."""
    import json

    from agent_cli import core, hooks, sessions

    claude = tmp_path / "claude" / "sessions"
    claude.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude.parent))
    monkeypatch.setenv("CLAUDE_PID", "4242")
    monkeypatch.setattr(core, "is_alive", lambda pid: pid == 4242)
    (claude / "4242.json").write_text(
        json.dumps({"pid": 4242, "sessionId": "s1", "name": "mgr-022-live", "nameSource": "hook"}),
        encoding="utf-8",
    )
    sessions.set_record(tmp_root, "s1", pid=4242)
    calls: list = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, sid, match, label, **kw: calls.append((match, label)) or {"started": True},
    )
    hooks.activity(tmp_root, {"session_id": "s1", "hook_event_name": "PermissionRequest"})
    assert calls == [("mgr-022-live", "\u270b 022 live · cl s1")]


def test_repeated_hook_events_keep_one_desired_label(tmp_root: Path, monkeypatch):
    """The first version of this ran `obsidian eval` on every hook event.
    Across three live managers that thrashed xterm's WebGL glyph atlas until bold text in the
    owner's terminal rendered as wrong characters, and it was reverted.

    Recomputing on every event is still required, but the shared drainer owns deduplication
    and aggregate rate limiting. A one-turn probe saw 32 events and four state transitions.
    """
    from agent_cli import hooks, sessions
    from agent_cli.hooks.activity import refresh_tab

    sessions.set_record(tmp_root, "s1", pid=4242, name="mgr-030-x")
    labels: list[str] = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, sid, match, label, **kw: labels.append(label) or {"started": True},
    )

    for _ in range(12):
        refresh_tab(tmp_root, "s1")
    assert labels == ["✅ mgr-030-x · cl s1"] * 12

    hooks.activity(tmp_root, {"session_id": "s1", "hook_event_name": "UserPromptSubmit"})
    assert labels[-1] == "⚙️ mgr-030-x · cl s1"

    for _ in range(12):
        refresh_tab(tmp_root, "s1")
    assert labels[-12:] == ["⚙️ mgr-030-x · cl s1"] * 12


def test_codex_claims_once_then_relabels_only_on_state_changes(tmp_root: Path, monkeypatch):
    from agent_cli import sessions
    from agent_cli.hooks.activity import codex_activity

    sessions.set_record(tmp_root, "12345678-codex", feature={"id": 1, "slug": "example-feature"})
    # a SessionStart claim needs a positive marker since #67
    monkeypatch.setattr(
        "agent_cli.codex_tab_link.marker_for", lambda s: "codex:" + "4" * 8 + "-4444-4444-8444-" + "4" * 12
    )
    claimed: list[tuple[str, str, bool]] = []
    changed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        obsidian_tabs,
        "claim_active",
        lambda name, label, **kw: claimed.append((name, label, kw.get("allow_untitled", False))) or True,
    )
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, sid, match, label, **kw: (
            changed.append((match, kw.get("before_label") or label)) or {"started": True}
        ),
    )
    payload = {"session_id": "12345678-codex", "hook_event_name": "SessionStart"}
    codex_activity(tmp_root, payload)
    name = "mgr-001-example-feature-codex-78-codex"
    assert claimed == [(name, "✅ 001 example-feature · cx 78-c", True)]
    record = sessions.get(tmp_root, "12345678-codex")
    assert record is not None and record["provider"] == "codex"

    for _ in range(8):
        codex_activity(tmp_root, payload)
    assert len(claimed) == 1 and len(changed) == 8

    codex_activity(tmp_root, {**payload, "hook_event_name": "UserPromptSubmit"})
    codex_activity(tmp_root, {**payload, "hook_event_name": "PermissionRequest"})
    codex_activity(tmp_root, {**payload, "hook_event_name": "PostToolUse"})
    codex_activity(tmp_root, {**payload, "hook_event_name": "Stop"})
    assert [label for _, label in changed[-4:]] == [
        f"{icon} 001 example-feature · cx 78-c" for icon in ("⚙️", "⚙️", "⚙️", "✅")
    ]


def test_codex_async_question_stays_needs_owner_until_next_prompt(tmp_root: Path, monkeypatch):
    from agent_cli import sessions
    from agent_cli.hooks.activity import codex_activity

    session_id = "12345678-codex"
    sessions.set_record(tmp_root, session_id)
    labels: list[str] = []
    monkeypatch.setattr(obsidian_tabs, "claim_active", lambda name, label, **kw: labels.append(label) or True)
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, sid, match, label, **kw: labels.append(label) or {"started": True},
    )
    codex_activity(tmp_root, {"session_id": session_id, "hook_event_name": "SessionStart"})
    codex_activity(tmp_root, {"session_id": session_id, "hook_event_name": "UserPromptSubmit"})
    codex_activity(
        tmp_root,
        {
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "functions.request_user_input_async",
        },
    )
    assert labels[-1].startswith("✋ ")
    codex_activity(
        tmp_root,
        {
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": "functions.request_user_input_async",
        },
    )
    codex_activity(tmp_root, {"session_id": session_id, "hook_event_name": "Stop"})
    assert labels[-1].startswith("✋ ")
    record = sessions.get(tmp_root, session_id)
    assert record is not None and record["owner_question_pending"] is True
    codex_activity(tmp_root, {"session_id": session_id, "hook_event_name": "UserPromptSubmit"})
    assert labels[-1].startswith("⚙️ ")


def test_a_renamed_session_relabels_the_tab_it_actually_has(tmp_root: Path, monkeypatch):
    """The owner's bug: `feature_use` onto another feature renames the session,
    and the tab froze on the old feature's name and on whichever glyph was current at that
    moment.

    `eval_code` finds a tab by the title it carries. The Claude path searched for the *new*
    name, so nothing matched and `relabeled 0` came back — but the spawn is fire-and-forget and
    reports `started` regardless, so `tab_label` was written anyway. The throttle then compared
    every later label against that phantom and returned early. Stuck, not merely stale.
    """
    from agent_cli import sessions
    from agent_cli.hooks.activity import refresh_tab

    calls: list = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, sid, match, label, **kw: calls.append((match, label)) or {"started": True},
    )
    sessions.set_record(tmp_root, "s1", pid=4242, name="mgr-030-old")
    refresh_tab(tmp_root, "s1")
    assert calls == [("mgr-030-old", "✅ mgr-030-old · cl s1")]
    sessions.set_field(tmp_root, "s1", "tab_name", "mgr-030-old")

    # the rename: the tab still says mgr-030-old, so that is what must be matched
    calls.clear()
    sessions.set_record(tmp_root, "s1", pid=4242, name="mgr-032-new")
    refresh_tab(tmp_root, "s1")
    assert calls == [("mgr-030-old", "✅ mgr-032-new · cl s1")]
    # The drainer records the painted name only after Obsidian confirms it found the tab.
    sessions.set_field(tmp_root, "s1", "tab_name", "mgr-032-new")

    # and the next event matches the new name, because that is what is on the tab now
    calls.clear()
    sessions.set_field(tmp_root, "s1", "activity", {"state": "working"})
    refresh_tab(tmp_root, "s1")
    assert calls == [("mgr-032-new", "⚙️ mgr-032-new · cl s1")]


def test_a_tab_renamed_before_tab_name_existed_still_recovers(tmp_root: Path, monkeypatch):
    """A session already diverged when the fix landed has no `tab_name`. The name is recoverable
    from `tab_label`, because `label_for` always emits "<glyph> <name>" — so the tab repaints on
    its next event instead of needing a hand-written relabel."""
    from agent_cli import sessions
    from agent_cli.hooks.activity import name_in, refresh_tab

    assert name_in("⏳ mgr-001-example-feature") == "mgr-001-example-feature"
    assert name_in("") == ""

    calls: list = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, sid, match, label, **kw: calls.append((match, label)) or {"started": True},
    )
    sessions.set_record(tmp_root, "s1", pid=4242, name="mgr-032-new")
    sessions.set_field(tmp_root, "s1", "tab_label", "⏳ mgr-030-old")
    refresh_tab(tmp_root, "s1")
    assert calls == [("mgr-030-old", "✅ mgr-032-new · cl s1")]


def test_a_label_that_was_never_sent_is_not_recorded(tmp_root: Path, monkeypatch):
    """Writing `tab_label` unconditionally is what turned one missed paint into a permanently
    frozen tab: the throttle compares against it and returns early forever after."""
    from agent_cli import sessions
    from agent_cli.hooks.activity import refresh_tab

    monkeypatch.setattr(tab_drain, "request", lambda *args, **kw: {"started": False})
    sessions.set_record(tmp_root, "s1", pid=4242, name="mgr-032-new")
    refresh_tab(tmp_root, "s1")
    record = sessions.get(tmp_root, "s1") or {}
    assert "tab_label" not in record and "tab_display_name" not in record


def test_a_marker_file_turns_relabelling_off_for_every_live_session(tmp_path, monkeypatch):
    """The kill switch has to reach sessions that are already running.

    `AGENT_NO_TABS` turns relabelling off for one process, which is no use when the reason to
    stop is that four sessions are between them corrupting the owner's terminal: an env var in a
    settings file reaches them only after a restart, and a restart is what the owner is trying to
    avoid. A file is read on the next hook event, by every session and both clients.

    Background: one manager relabelling on every hook event thrashed xterm's WebGL glyph atlas
    on 2026-09-22 until bold text rendered as wrong characters. A throttle fixed that by spawning
    only on a label change. It recurred on 2026-09-23 with four sessions, because the throttle is
    per session and the atlas is not.
    """
    root = tmp_path / "harness"
    (root / ".agent").mkdir(parents=True)
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.setenv("AGENT_ROOT", str(root))

    assert obsidian_tabs.enabled(root) is True

    (root / obsidian_tabs.TABS_OFF_FILE).touch()
    assert obsidian_tabs.enabled(root) is False
    # and nothing spawns: both entry points refuse before reaching the Obsidian CLI
    assert tab_drain.request(root, "manager", "s1", "mgr-x", "✅ mgr-x") == {
        "started": False,
        "reason": "disabled",
    }
    assert obsidian_tabs.claim_active("mgr-x", "✅ mgr-x") is False

    # removing it brings tabs back without restarting anything
    (root / obsidian_tabs.TABS_OFF_FILE).unlink()
    assert obsidian_tabs.enabled(root) is True
