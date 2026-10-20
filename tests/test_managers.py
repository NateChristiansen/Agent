"""Every live manager session and what it is doing, recorded by the
`activity` hook from the session's own events. Since then the hook writes
`.agent/live.json`, which the Dashboard renders, instead of splicing a table into Dashboard.md."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import core, hooks, sessions
from agent_cli.vault import live

pytestmark = pytest.mark.usefixtures("notes_remote")


def _event(event: str, tool: str | None = None, session_id: str = "s1") -> dict:
    payload = {"session_id": session_id, "hook_event_name": event}
    if tool:
        payload["tool_name"] = tool
    return payload


def _write_dispatch(root: Path, dispatch_id: str, **fields) -> None:
    record = {"id": dispatch_id, "feature": 20, "short": "alpha", "state": "running", "cost_usd": 0.0}
    record.update(fields)
    base = root / ".agent" / "dispatches"
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _managers(root: Path) -> list[dict]:
    return json.loads(live.path(root).read_text(encoding="utf-8"))["managers"]


def _by_name(root: Path) -> dict[str, dict]:
    return {m["name"]: m for m in _managers(root)}


def test_activity_follows_the_session_s_events_into_the_snapshot(tmp_root: Path, run, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    run(["feature", "new", "worker-management", "--title", "W"])
    run(["feature", "use", "1", "--session-id", "s1"])
    sessions.set_record(tmp_root, "s1", name="mgr-001-worker-management", pid=1)
    name = "mgr-001-worker-management"

    hooks.activity(tmp_root, _event("UserPromptSubmit"))
    row = _by_name(tmp_root)[name]
    assert (row["feature"], row["state"], row["activity"]) == ("001 worker-management", "working", "working")
    hooks.activity(tmp_root, _event("PermissionRequest"))
    assert _by_name(tmp_root)[name]["activity"] == "waiting_on_you"
    needs = json.loads(live.path(tmp_root).read_text())["needs_you"]
    assert {"kind": "manager", "session_id": "s1", "name": name}.items() <= needs[0].items()
    hooks.activity(tmp_root, _event("PostToolUse", "Edit"))
    assert _by_name(tmp_root)[name]["activity"] == "working"
    hooks.activity(tmp_root, _event("PreToolUse", "AskUserQuestion"))
    assert _by_name(tmp_root)[name]["activity"] == "waiting_on_you"
    hooks.activity(tmp_root, _event("Stop"))
    # The question is still open after the turn ends: the row says so, as the tab and Needs you
    # do (P1: the shared attention projection), until the owner answers.
    assert _by_name(tmp_root)[name]["state"] == "question for you"
    hooks.activity(tmp_root, _event("UserPromptSubmit"))
    hooks.activity(tmp_root, _event("Stop"))
    assert _by_name(tmp_root)[name]["state"] == "question for you"
    hooks.activity(tmp_root, _event("PostToolUse", "AskUserQuestion"))
    hooks.activity(tmp_root, _event("Stop"))
    assert _by_name(tmp_root)[name]["state"] == "idle"

    # idle while a worker it launched runs reads as waiting on that worker
    _write_dispatch(tmp_root, "001-01", launched_by_session="s1")
    live.write(tmp_root)
    row = _by_name(tmp_root)[name]
    assert row["state"] == "waiting on worker 001-01" and row["running"] == ["001-01"]

    hooks.activity(tmp_root, _event("SessionEnd"))
    assert _managers(tmp_root) == []


def test_a_new_or_resumed_session_shows_despite_the_hook_race(tmp_root: Path, monkeypatch, tmp_path):
    """Found live: `activity` runs alongside `session-start`. A brand-new session's first
    activity landed before any pid was on the record, and a resumed session kept the old
    process's pid and its `ended` state, so both were missing from the Dashboard."""
    claude = tmp_path / "claude" / "sessions"
    claude.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude.parent))
    monkeypatch.setenv("CLAUDE_PID", "4242")
    monkeypatch.setattr(core, "is_alive", lambda pid: pid == 4242)
    # new session: no record at all, the activity hook writes the pid itself
    hooks.activity(tmp_root, _event("SessionStart", session_id="new"))
    assert (sessions.get(tmp_root, "new") or {})["pid"] == 4242
    assert [m["session_id"] for m in _managers(tmp_root)] == ["new"]
    # resumed session: the old process ended it; the resume's SessionStart brings it back
    monkeypatch.setenv("CLAUDE_PID", "5151")
    monkeypatch.setattr(core, "is_alive", lambda pid: pid in (4242, 5151))
    sessions.set_record(tmp_root, "old", name="mgr-001-a", pid=999)
    sessions.set_field(tmp_root, "old", "activity", {"state": "ended", "at": "2026-09-03T14:00:00-07:00"})
    live.write(tmp_root)
    assert "mgr-001-a" not in _by_name(tmp_root)
    hooks.activity(tmp_root, _event("SessionStart", session_id="old"))
    assert "mgr-001-a" in _by_name(tmp_root)
    # a stale pid alone still shows when Claude Code lists the session as running
    sessions.set_field(tmp_root, "old", "pid", 999)
    (claude / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "old"}), encoding="utf-8")
    live.write(tmp_root)
    assert "mgr-001-a" in _by_name(tmp_root)


def test_a_claude_session_in_the_vault_shows_without_a_record(tmp_root: Path, monkeypatch, tmp_path):
    """Found live: a manager whose record had lost its fields was missing from the
    Dashboard while Claude Code listed it. Claude Code's list is now the first source; a session
    elsewhere on the machine with no record is not a manager."""
    claude = tmp_path / "claude" / "sessions"
    claude.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude.parent))
    monkeypatch.setattr(core, "is_alive", lambda pid: pid in (68998, 21220))
    here = {
        "pid": 68998,
        "sessionId": "d840",
        "cwd": str(tmp_root),
        "name": "agent-ba",
        "kind": "interactive",
    }
    elsewhere = {"pid": 21220, "sessionId": "e0bf", "cwd": str(tmp_path / "repos" / "app"), "name": "app-21"}
    (claude / "68998.json").write_text(json.dumps(here), encoding="utf-8")
    (claude / "21220.json").write_text(json.dumps(elsewhere), encoding="utf-8")
    live.write(tmp_root)
    assert [(m["session_id"], m["name"], m["state"]) for m in _managers(tmp_root)] == [
        ("d840", "agent-ba", "idle")
    ]
    # a worker's session is never a manager, even when its record says it runs in the vault
    sessions.set_record(tmp_root, "d840", dispatch_id="001-01")
    live.write(tmp_root)
    assert _managers(tmp_root) == []


def test_a_background_job_is_marked(tmp_root: Path, monkeypatch, tmp_path):
    claude = tmp_path / "claude" / "sessions"
    claude.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude.parent))
    monkeypatch.setattr(core, "is_alive", lambda pid: pid == 61057)
    sessions.set_record(tmp_root, "bg1", pid=61057)
    # a job someone started has had a prompt; a never-prompted one is a daemon spare (#71)
    sessions.set_field(tmp_root, "bg1", "activity", {"state": "idle", "at": "2026-09-30T09:00:00-06:00"})
    entry = {
        "pid": 61057,
        "sessionId": "bg1",
        "kind": "bg",
        "name": "harness-audit-hardening",
        "status": "idle",
    }
    (claude / "61057.json").write_text(json.dumps(entry), encoding="utf-8")
    live.write(tmp_root)
    row = _by_name(tmp_root)["harness-audit-hardening (bg)"]
    assert (row["feature"], row["state"], row["background"]) == ("—", "background, idle", True)


def test_activity_ignores_workers_subagents_and_repeat_states(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    sessions.set_record(tmp_root, "w1", dispatch_id="001-01", pid=1)
    hooks.activity(tmp_root, _event("UserPromptSubmit", session_id="w1"))
    assert "activity" not in (sessions.get(tmp_root, "w1") or {})
    sub = {**_event("UserPromptSubmit"), "agent_type": "Explore"}
    hooks.activity(tmp_root, sub)
    assert sessions.get(tmp_root, "s1") is None
    # PostToolUse after working changes nothing and writes nothing
    sessions.set_record(tmp_root, "s1", pid=1)
    hooks.activity(tmp_root, _event("UserPromptSubmit"))
    before = (sessions.get(tmp_root, "s1") or {})["activity"]
    stamp = live.path(tmp_root).stat().st_mtime_ns
    hooks.activity(tmp_root, _event("PostToolUse", "Read"))
    assert (sessions.get(tmp_root, "s1") or {})["activity"] == before
    assert live.path(tmp_root).stat().st_mtime_ns == stamp


def test_the_activity_hook_no_longer_touches_the_dashboard(tmp_root: Path, run, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    run(["status", "render"])
    before = (tmp_root / "Dashboard.md").read_text(encoding="utf-8")
    sessions.set_record(tmp_root, "s1", pid=1)
    hooks.activity(tmp_root, _event("UserPromptSubmit"))
    hooks.activity(tmp_root, _event("PermissionRequest"))
    assert (tmp_root / "Dashboard.md").read_text(encoding="utf-8") == before


def test_the_activity_hook_is_wired_on_every_event_it_reads():
    from agent_cli.hooks import groups

    settings = json.loads((Path(__file__).resolve().parents[1] / ".claude" / "settings.json").read_text())

    def wired(event: str, tool: str | None = None) -> bool:
        hook_name = "manager-prompt" if event == "UserPromptSubmit" else "activity"
        return groups.wired(settings, event, tool).count(hook_name) == 1

    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PermissionRequest"):
        assert wired(event), event
    assert wired("PreToolUse", "AskUserQuestion") and wired("PreToolUse", "ExitPlanMode")
    for tool in ("Bash", "Read", "mcp__agent__log", "WebFetch"):
        assert wired("PostToolUse", tool), tool


def _claude_entry(claude: Path, pid: int, entry: dict) -> None:
    (claude / f"{pid}.json").write_text(json.dumps({"pid": pid, **entry}), encoding="utf-8")


def test_a_parked_fork_folds_into_its_tab_and_a_spare_is_left_out(tmp_root: Path, monkeypatch, tmp_path):
    """#71, fresh Windows clone 2026-09-30: a Remote Control message parked tab 4792f4 into bg job
    c9cb2073, and the daemon started spare 57be72ee beside it; both showed as managers."""
    claude = tmp_path / "claude" / "sessions"
    claude.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude.parent))
    monkeypatch.setattr(core, "is_alive", lambda pid: pid in (1, 2, 3))
    prompted = {"state": "idle", "at": "2026-09-30T09:00:00-06:00"}
    for sid, pid in (("tab", 1), ("fork", 2), ("spare", 3)):
        sessions.set_record(tmp_root, sid, pid=pid, name=f"mgr-{sid}")
    sessions.set_field(tmp_root, "tab", "activity", prompted)
    sessions.set_field(tmp_root, "fork", "activity", prompted)
    sessions.set_field(tmp_root, "fork", "cost_usd", 0.3)
    _claude_entry(claude, 1, {"sessionId": "tab", "kind": "interactive", "parkedJobId": "c9cb2073"})
    _claude_entry(claude, 2, {"sessionId": "fork", "kind": "bg", "jobId": "c9cb2073"})
    _claude_entry(claude, 3, {"sessionId": "spare", "kind": "bg"})
    live.write(tmp_root)
    rows = _managers(tmp_root)
    assert [r["session_id"] for r in rows] == ["tab"]
    [child] = rows[0]["parked"]
    assert (child["session_id"], child["cost_usd"]) == ("fork", 0.3)  # its own cost, not summed


@pytest.mark.parametrize("case", ["no-record", "holds-lease", "no-park-fields"])
def test_a_bg_session_stays_visible_without_proof_it_is_unused(tmp_root: Path, monkeypatch, tmp_path, case):
    """A spare is hidden only on a real harness record with no activity,
    lease, dispatch or message. A live session whose record was lost, one holding a
    lease, and a fork whose tab entry lacks parkedJobId all stay visible as bg rows."""
    from agent_cli import leases

    claude = tmp_path / "claude" / "sessions"
    claude.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude.parent))
    monkeypatch.setattr(core, "is_alive", lambda pid: pid in (1, 2))
    sessions.set_record(tmp_root, "tab", pid=1, name="mgr-tab")
    sessions.set_field(tmp_root, "tab", "activity", {"state": "idle", "at": "2026-09-30T09:00:00-06:00"})
    _claude_entry(claude, 1, {"sessionId": "tab", "kind": "interactive", "cwd": str(tmp_root)})
    bg_entry = {"sessionId": "bg", "kind": "bg", "cwd": str(tmp_root)}
    if case != "no-record":
        sessions.set_record(tmp_root, "bg", pid=2, name="mgr-bg")
    if case == "holds-lease":
        leases.take(tmp_root, "vault:src", session_id="bg", pid=2)
    if case == "no-park-fields":
        sessions.set_field(tmp_root, "bg", "activity", {"state": "idle", "at": "2026-09-30T09:00:00-06:00"})
        bg_entry["jobId"] = "c9cb2073"  # the tab entry names no parkedJobId
    _claude_entry(claude, 2, bg_entry)
    live.write(tmp_root)
    rows = {r["session_id"]: r for r in _managers(tmp_root)}
    assert rows["bg"]["background"] is True
    assert rows["tab"]["parked"] == []
