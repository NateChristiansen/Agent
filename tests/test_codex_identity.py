"""A Codex session's pid is proven or unknown, never the shared daemon's
the owner chose positive association on 2026-09-27). On the owner's Mac the hooks of a Codex
0.157 session ran under `codex app-server --listen unix:// --managed-daemon`, the ancestor walk
stopped there, and every Codex session on the machine shared that one pid."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli import leases, session_identity, session_liveness, sessions
from agent_cli.core import AgentError
from agent_cli.ops import sessions as ops_sessions

DAEMON = "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex app-server --listen unix:// --managed-daemon"
TUI = "node /opt/homebrew/bin/codex"


def _tree(monkeypatch: pytest.MonkeyPatch, chain: dict[int, tuple[str, int]], start: int) -> None:
    """Fake `ps`: pid -> (command, parent), with the hook's parent at `start`."""
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.setattr(session_identity, "_command", lambda pid: chain.get(pid, ("", 1))[0])
    monkeypatch.setattr(session_identity, "_parent_pid", lambda pid: chain.get(pid, ("", 1))[1])
    monkeypatch.setattr(session_identity.os, "getppid", lambda: start)
    monkeypatch.setattr(session_identity, "pid_running_session", lambda sid: None)


@pytest.fixture
def codex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in session_identity.CLAUDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "019b76da-a800-7000-8000-000000000002")


def test_hooks_under_the_managed_daemon_have_no_pid(monkeypatch, codex_env):
    _tree(monkeypatch, {900: ("/bin/sh -c bin/agent hook activity", 321), 321: (DAEMON, 69627)}, 900)
    assert session_identity.resolve_identity() == (None, False)
    assert session_identity.resolve_pid() is None


def test_a_codex_hook_with_inherited_claude_variables_is_still_codex(monkeypatch, codex_env):
    """Codex started from a Claude terminal: both clients' variables reach its hooks. The walk
    must still label it Codex from the daemon, and still give it no pid (2026-09-28: the label
    fell back to claude and the hook's launcher, pid 28715, was recorded as proven)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "outer-claude-session")
    launcher = "node -e require(...agent-hook.cjs) agent hook codex-activity"
    _tree(monkeypatch, {28715: (launcher, 58746), 58746: (DAEMON, 69627)}, 28715)
    assert session_identity.resolve_provider() == "codex"
    assert session_identity.resolve_identity() == (None, False)


def test_a_resume_command_line_proves_the_pid(monkeypatch, codex_env):
    _tree(monkeypatch, {900: ("/bin/sh", 321), 321: (DAEMON, 1)}, 900)
    monkeypatch.setattr(session_identity, "pid_running_session", lambda sid: 4242 if sid else None)
    assert session_identity.resolve_identity() == (4242, True)


def test_only_a_resume_of_this_thread_proves_a_pid():
    """A tool call that merely mentions the thread id must not claim the session (2026-09-28:
    the Codex manager recorded pid 15728, a finished process, as its own)."""
    sid = "019b76da-a800-7000-8000-000000000002"
    assert session_identity._resumes(f"node /opt/homebrew/bin/codex resume {sid}", sid)
    assert not session_identity._resumes(f"/usr/local/bin/codex sandbox -- zsh -c 'cat {sid}.json'", sid)
    assert not session_identity._resumes(f"{DAEMON} resume {sid}", sid)
    assert not session_identity._resumes(f"grep resume {sid}", sid)
    assert not session_identity._resumes(f"grep codex resume {sid}", sid)
    assert not session_identity._resumes(f"sh -c codex resume {sid}", sid)
    assert session_identity._resumes(f"/usr/local/bin/codex resume {sid}", sid)
    assert not session_identity._resumes(f"/usr/local/bin/codex sandbox -- zsh -c resume {sid}", sid)


def test_a_client_name_in_argument_position_is_not_a_client(monkeypatch, codex_env):
    """A short-lived shell whose command merely names codex is not the client; the walk goes
    on to the real `node /path/codex` ancestor (Codex review of group 4)."""
    sid = "019b76da-a800-7000-8000-000000000002"
    shell = f"/bin/sh -c codex resume {sid}"
    _tree(monkeypatch, {900: (shell, 20574), 20574: (TUI, 500)}, 900)
    assert session_identity._client_name(shell) is None
    assert session_identity.resolve_identity() == (20574, True)


def test_an_embedded_tui_ancestor_proves_the_pid(monkeypatch, codex_env):
    _tree(monkeypatch, {900: ("/bin/sh -c bin/agent hook activity", 20574), 20574: (TUI, 500)}, 900)
    assert session_identity.resolve_identity() == (20574, True)


def test_claude_keeps_its_answers(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s")
    monkeypatch.setenv("CLAUDE_PID", "777")
    for var in session_identity.CODEX_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert session_identity.resolve_identity() == (777, True)
    monkeypatch.delenv("CLAUDE_PID")
    _tree(monkeypatch, {900: ("/bin/sh", 1)}, 900)
    assert session_identity.resolve_identity() == (900, True)  # the parent, as before


def test_set_identity_replaces_a_daemon_pid_and_a_proof_clears_it(tmp_root: Path):
    sessions.set_record(tmp_root, "codex", pid=321)
    record = session_liveness.set_identity(tmp_root, "codex", None, False)
    assert record["pid"] is None and record["pid_unknown"] is True
    record = session_liveness.set_identity(tmp_root, "codex", 4242, True)
    assert record["pid"] == 4242 and "pid_unknown" not in record


def test_record_live_for_known_recent_and_quiet(tmp_root: Path, monkeypatch):
    import os

    assert session_liveness.record_live({"pid": os.getpid()})
    assert not session_liveness.record_live({"pid": 2**22})
    assert not session_liveness.record_live({"pid": None})  # an old record with no pid
    recent = {"pid": None, "pid_unknown": True, "activity": {"at": "2026-09-03T14:04:00-07:00"}}
    assert session_liveness.record_live(recent)
    ended = {**recent, "activity": {"state": "ended", "at": "2026-09-03T14:04:00-07:00"}}
    assert not session_liveness.record_live(ended)
    monkeypatch.setenv("AGENT_NOW", "2026-09-03T14:36:00-07:00")
    assert not session_liveness.record_live(recent)


def test_a_working_codex_turn_with_tool_events_stays_live_past_thirty_minutes(
    tmp_root: Path, monkeypatch, codex_env
):
    """`activity.at` only moved on a state change, so a 78-minute working turn read as
    closed after 30. A throttled `seen_at` heartbeat keeps a turn with continuing hook events
    live, and `at` keeps meaning "since" for the Dashboard."""
    import importlib
    import json

    from agent_cli.hooks.activity import codex_activity

    _tree(monkeypatch, {900: ("/bin/sh -c bin/agent hook activity", 321), 321: (DAEMON, 69627)}, 900)
    # the package re-exports a function named `activity`, which hides the submodule attribute
    monkeypatch.setattr(
        importlib.import_module("agent_cli.hooks.activity"), "refresh_tab", lambda *a, **k: None
    )
    sid = "cdx-long"
    sessions.set_record(tmp_root, sid, name="mgr-052-codex")
    monkeypatch.setenv("AGENT_NOW", "2026-10-08T10:00:00-06:00")
    codex_activity(tmp_root, {"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": "go"})
    since = (sessions.get(tmp_root, sid) or {})["activity"]["at"]
    for minute in ("10:10:00", "10:10:10", "10:29:00", "10:45:00"):
        monkeypatch.setenv("AGENT_NOW", f"2026-10-08T{minute}-06:00")
        codex_activity(tmp_root, {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "exec"})
        if minute == "10:10:10":  # ten seconds after the last write: throttled
            assert (sessions.get(tmp_root, sid) or {})["activity"]["seen_at"].startswith(
                "2026-10-08T10:10:00"
            )
    record = sessions.get(tmp_root, sid) or {}
    assert record["activity"]["state"] == "working"
    assert record["activity"]["at"] == since
    assert session_liveness.record_live(record)
    live = json.loads((tmp_root / ".agent" / "live.json").read_text())
    assert sid in json.dumps(live["managers"])
    monkeypatch.setenv("AGENT_NOW", "2026-10-08T11:16:00-06:00")  # 31 minutes with no event
    assert not session_liveness.record_live(sessions.get(tmp_root, sid))


def test_a_malformed_seen_at_never_outlives_a_stale_at(monkeypatch):
    """`minutes_ago` reads garbage as zero minutes, so an invalid
    `seen_at` won over a two-hour-old `at` and kept a dead session listed forever."""
    monkeypatch.setenv("AGENT_NOW", "2026-10-08T12:00:00-06:00")
    stale = {
        "pid": None,
        "pid_unknown": True,
        "activity": {"state": "working", "at": "2026-10-08T10:00:00-06:00"},
    }
    for bad in ("invalid", "", None, 7):
        record = {**stale, "activity": {**stale["activity"], "seen_at": bad}}
        assert session_liveness.last_seen(record) == "2026-10-08T10:00:00-06:00"
        assert not session_liveness.record_live(record)
    fresh = {**stale, "activity": {**stale["activity"], "seen_at": "2026-10-08T11:50:00-06:00"}}
    assert session_liveness.record_live(fresh)
    assert session_liveness.last_seen({"activity": {"at": "nope"}}) is None


def test_overlapping_heartbeats_write_and_publish_once(tmp_root: Path, monkeypatch):
    """Two hooks reading the same stale record both wrote and both
    republished live.json. The throttle is rechecked under the record lock."""
    from agent_cli.hooks import attention

    monkeypatch.setenv("AGENT_NOW", "2026-10-08T10:05:00-06:00")
    sessions.set_record(tmp_root, "cdx", name="mgr-codex")
    sessions.set_field(tmp_root, "cdx", "activity", {"state": "working", "at": "2026-10-08T10:00:00-06:00"})
    published: list[int] = []
    writes: list[object] = []
    monkeypatch.setattr("agent_cli.vault.live.write_quietly", lambda root: published.append(1))
    original = sessions.atomic_write
    monkeypatch.setattr(sessions, "atomic_write", lambda *a, **k: (writes.append(a[0]), original(*a, **k)))
    stale = sessions.get(tmp_root, "cdx") or {}
    attention.heartbeat(tmp_root, "cdx", stale)
    attention.heartbeat(tmp_root, "cdx", stale)  # the same stale read, as a parallel hook has
    assert len(published) == 1
    assert len(writes) == 1  # the second call changed nothing and wrote nothing
    assert (sessions.get(tmp_root, "cdx") or {})["activity"]["seen_at"].startswith("2026-10-08T10:05:00")


def test_an_active_unknown_codex_session_stays_addressable(tmp_root: Path):
    from agent_cli.ops import peers

    sessions.set_record(tmp_root, "codex", name="mgr-codex")
    session_liveness.set_identity(tmp_root, "codex", None, False)
    sessions.set_field(tmp_root, "codex", "activity", {"state": "idle", "at": "2026-09-03T14:04:00-07:00"})
    assert [r["session_id"] for r in peers.addressable_peers(tmp_root)] == ["codex"]


def _quiet_codex(root: Path, sid: str = "cdx-quiet", name: str = "mgr-030-codex") -> None:
    sessions.set_record(root, sid, name=name)
    sessions.set_field(root, sid, "provider", "codex")
    session_liveness.set_identity(root, sid, None, False)
    sessions.set_field(root, sid, "activity", {"state": "idle", "at": "2026-09-03T14:04:00-07:00"})


def test_a_quiet_codex_manager_named_exactly_still_gets_the_message(tmp_root: Path, monkeypatch):
    """The owner chose (2026-09-29) to keep the 30-minute rule for `peer list` and let an exact
    name still reach an idle Codex manager: a manager's Codex went unreachable after two quiet
    hours and had to be poked by hand."""
    from agent_cli.ops import peers

    monkeypatch.setenv("AGENT_NOW", "2026-09-03T16:36:00-07:00")
    _quiet_codex(tmp_root)
    monkeypatch.setattr(peers, "_run", lambda argv: True)
    # Linux CI has no codex binary; without this the argv lookup fails before `_run`.
    monkeypatch.setattr("agent_cli.codex_executable.command", lambda *args: ["codex", *args])
    assert peers.addressable_peers(tmp_root) == []  # not listed
    assert peers.resolve(tmp_root, "mgr-030-codex") == "cdx-quiet"
    with pytest.raises(AgentError):
        peers.resolve(tmp_root, "mgr-030")  # a name prefix never resolves
    sessions.set_record(tmp_root, "sender", pid=os.getpid(), name="mgr-sender")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sender")
    sent = peers.peer_send(tmp_root, to="mgr-030-codex", message="hello")
    assert "quiet over 30 minutes" in sent.text
    assert "delivery unconfirmed" in sent.text


def test_a_quiet_codex_manager_is_reached_by_its_tab_short_name(tmp_root: Path, monkeypatch):
    """The owner names a manager by its tab, and a quiet Codex tab still shows `cx` plus four
    characters."""
    from agent_cli.ops import peers

    monkeypatch.setenv("AGENT_NOW", "2026-09-03T16:36:00-07:00")
    sid = "01a1007c-0000-7000-8000-00008ddb0001"
    _quiet_codex(tmp_root, sid=sid)
    assert peers.addressable_peers(tmp_root) == []
    assert peers.resolve(tmp_root, "cx 8ddb") == sid
    assert peers.resolve(tmp_root, "cx8ddb") == sid
    with pytest.raises(AgentError):
        peers.resolve(tmp_root, "01a1007c")  # an id prefix still needs a live session


def test_an_ended_quiet_codex_manager_is_not_reached(tmp_root: Path, monkeypatch):
    from agent_cli.ops import peers

    monkeypatch.setenv("AGENT_NOW", "2026-09-03T16:36:00-07:00")
    _quiet_codex(tmp_root)
    sessions.set_field(
        tmp_root, "cdx-quiet", "activity", {"state": "ended", "at": "2026-09-03T14:04:00-07:00"}
    )
    with pytest.raises(AgentError):
        peers.resolve(tmp_root, "mgr-030-codex")


def test_a_lease_taken_from_an_unknown_codex_session_carries_null(tmp_root: Path, monkeypatch, codex_env):
    monkeypatch.setattr(sessions, "resolve_identity", lambda: (None, False))
    monkeypatch.setattr(sessions, "resolve_provider", lambda: "codex")
    assert ops_sessions._lease_pid() is None
    leases.take(tmp_root, "vault:src", session_id="codex", pid=ops_sessions._lease_pid())
    lease = leases.get(tmp_root, "vault:src")
    assert lease is not None and lease["pid"] is None


# A Windows chain with codex.exe under node.exe and a hook run by a venv launcher.
# Tokens are `_windows_command` output, so a space in a path is %20.
WIN_TUI = r"C:\Users\example\AppData\Roaming\npm\node_modules\@openai\codex\vendor\codex.exe"
WIN_SHIM = r"C:\Program%20Files\nodejs\node.exe C:\Users\example\AppData\Roaming\npm\node_modules\@openai\codex\bin\codex.js"
WIN_HOOK = r"C:\Program%20Files\nodejs\node.exe -e require(agent-hook.cjs) agent hook codex-activity"


def test_a_windows_codex_exe_is_the_session_pid(monkeypatch, codex_env):
    """The walk matched only `codex`, never `codex.exe`, so a Windows Codex manager kept a
    short-lived ancestor's pid and dropped out of peer list."""
    _tree(monkeypatch, {7001: (WIN_HOOK, 9680), 9680: (WIN_TUI, 9600), 9600: (WIN_SHIM, 4)}, 7001)
    assert session_identity.resolve_provider() == "codex"
    assert session_identity.resolve_identity() == (9680, True)


def test_windows_names_and_the_npm_shim_are_recognised():
    assert session_identity._client_name(WIN_TUI.lower()) == "codex"
    assert session_identity._client_name(WIN_SHIM.lower()) == "codex"  # node.exe running codex.js
    assert session_identity._client_name(WIN_HOOK.lower()) is None
    assert session_identity._client_name(r"c:\users\example\.local\bin\claude.exe") == "claude"
    assert (
        session_identity._windows_command([r"C:\Program Files\nodejs\node.exe", "x"])
        .split()[0]
        .endswith("node.exe")
    )
