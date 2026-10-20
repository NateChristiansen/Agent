"""Unknown-pid leases never expire, and the owner clears a stuck session's leases in one step."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli import core, lease_clear, leases, sessions
from agent_cli.core import AgentError

LATER = "2026-09-03T14:36:00-07:00"  # 31 minutes past the fixture's FIXED_NOW


def test_a_lease_with_no_pid_key_is_malformed_not_unknown(tmp_root: Path):
    import json

    leases.take(tmp_root, "vault:src", session_id="codex", pid=None)
    path = leases.area_file(tmp_root, "vault:src")
    data = json.loads(path.read_text())
    del data["pid"]
    path.write_text(json.dumps(data))
    with pytest.raises(AgentError, match="malformed"):
        leases.get(tmp_root, "vault:src")


def test_clear_rechecks_recency_under_the_lock(tmp_root: Path, monkeypatch):
    """The holder touches its lease between clear's first look and the removal: it is kept,
    because the owner agreed only to clear a quiet session."""
    leases.take(tmp_root, "vault:src", session_id="codex", pid=None)
    monkeypatch.setenv("AGENT_NOW", LATER)
    real = leases.list_all
    calls = []

    def look_then_touch(root):
        seen = real(root)
        calls.append(1)
        if len(calls) == 2:  # the first call resolves the holder name; the second is clear's look
            leases.take(root, "vault:src", session_id="codex", pid=None)
        return seen

    monkeypatch.setattr(lease_clear.leases, "list_all", look_then_touch)
    result = lease_clear.clear(tmp_root, "codex")
    assert result["cleared"] == [] and result["kept"] == ["vault:src"]
    assert leases.get(tmp_root, "vault:src") is not None


def test_an_unknown_pid_lease_is_never_dead(tmp_root: Path):
    leases.take(tmp_root, "vault:src", session_id="codex", pid=None)
    assert leases.status(leases.get(tmp_root, "vault:src"), tmp_root) == "live"
    with pytest.raises(AgentError, match="held by codex"):
        leases.take(tmp_root, "vault:src", session_id="other", pid=os.getpid())


def test_a_quiet_unknown_pid_lease_is_unverified_and_still_blocks(tmp_root: Path, monkeypatch):
    leases.take(tmp_root, "vault:src", session_id="codex", pid=None)
    monkeypatch.setenv("AGENT_NOW", LATER)
    assert leases.status(leases.get(tmp_root, "vault:src"), tmp_root) == "unverified"
    with pytest.raises(AgentError, match="held by codex"):
        leases.take(tmp_root, "vault:src", session_id="other", pid=os.getpid())
    # a known-pid holder quiet as long is idle and taken over, as before
    leases.take(tmp_root, "vault:harness", session_id="claude", pid=os.getpid())
    monkeypatch.setenv("AGENT_NOW", "2026-09-03T15:10:00-07:00")
    assert (
        leases.take(tmp_root, "vault:harness", session_id="other", pid=os.getpid())["took_over_from"]
        == "claude"
    )


def test_an_unknown_holder_whose_turn_ended_is_resting(tmp_root: Path, monkeypatch):
    leases.take(tmp_root, "vault:src", session_id="codex", pid=None)
    sessions.set_counter(tmp_root, "codex", leases.HOLDER_STOPPED_KEY, 2**62)
    monkeypatch.setenv("AGENT_NOW", LATER)
    assert leases.status(leases.get(tmp_root, "vault:src"), tmp_root) == "resting"


def test_clear_asks_first_when_the_holder_was_active_recently(tmp_root: Path):
    sessions.set_record(tmp_root, "codex", name="mgr-030-codex")
    leases.take(tmp_root, "vault:src", session_id="codex", pid=None)
    leases.take(tmp_root, "vault:harness", session_id="codex", pid=None)
    asked = lease_clear.clear(tmp_root, "mgr-030-codex")
    assert asked["needs_confirm"] and asked["cleared"] == [] and asked["recent_minutes"] == 0
    assert leases.get(tmp_root, "vault:src") is not None
    done = lease_clear.clear(tmp_root, "mgr-030-codex", confirm=True)
    assert sorted(done["cleared"]) == ["vault:harness", "vault:src"]
    assert leases.list_all(tmp_root) == []


def test_clear_needs_no_confirm_for_a_quiet_or_dead_holder(tmp_root: Path, monkeypatch):
    leases.take(tmp_root, "vault:src", session_id="codex", pid=None)
    leases.take(tmp_root, "repo:alpha", session_id="gone", pid=2**22)
    leases.take(tmp_root, "vault:harness", session_id="other", pid=os.getpid())
    assert lease_clear.clear(tmp_root, "gone")["cleared"] == ["repo:alpha"]
    monkeypatch.setenv("AGENT_NOW", LATER)
    assert lease_clear.clear(tmp_root, "codex")["cleared"] == ["vault:src"]
    assert [lease["area"] for lease in leases.list_all(tmp_root)] == ["vault:harness"]


def test_clear_keeps_a_lease_the_verifier_protects(tmp_root: Path, monkeypatch):
    from agent_cli.dispatch import verification_gate

    leases.take(tmp_root, "repo:alpha", session_id="codex", pid=None)
    monkeypatch.setenv("AGENT_NOW", LATER)
    monkeypatch.setattr(verification_gate, "lease_blocks", lambda root, lease: True)
    result = lease_clear.clear(tmp_root, "codex")
    assert result["cleared"] == [] and result["kept"] == ["repo:alpha"]
    assert "running verifier" in lease_clear.format_result(result)


def test_clear_names_an_unknown_holder(tmp_root: Path):
    with pytest.raises(AgentError, match="no session or lease holder named 'nobody'"):
        lease_clear.clear(tmp_root, "nobody")


def test_cli_lease_clear(tmp_root: Path, run, monkeypatch):
    leases.take(tmp_root, "vault:src", session_id="codex", pid=None)
    asked = run(["--json", "lease", "clear", "codex"])
    assert asked.json["needs_confirm"] is True
    assert "--yes" in run(["lease", "clear", "codex"]).out
    assert run(["--json", "lease", "clear", "codex", "--yes"]).json["cleared"] == ["vault:src"]


def _unknown_pid_session(root: Path, sid: str, at: str) -> None:
    sessions.update_record(
        root,
        sid,
        lambda r: {**r, "pid": None, "pid_unknown": True, "activity": {"state": "idle", "at": at}},
    )


def test_the_managers_view_offers_clear_leases_for_a_quiet_unknown_pid_holder(tmp_root: Path, monkeypatch):
    from agent_cli.vault import managers

    monkeypatch.setattr(managers, "claude_live_sessions", lambda: {})
    _unknown_pid_session(tmp_root, "holder", "2026-09-03T14:00:00-07:00")
    _unknown_pid_session(tmp_root, "empty", "2026-09-03T14:00:00-07:00")
    _unknown_pid_session(tmp_root, "fresh", "2026-09-03T14:35:00-07:00")
    sessions.update_record(tmp_root, "old-record", lambda r: {**r, "activity": {"state": "idle"}})
    leases.take(tmp_root, "vault:src", session_id="holder", pid=None)
    monkeypatch.setenv("AGENT_NOW", LATER)
    rows = {row["session_id"]: row for row in managers.rows(tmp_root, dispatches=[], closed_features=set())}
    # quiet with a lease: shown, unverified, clearable; quiet with none: hidden; recent: shown
    # normally; an old record that never recorded a pid stays hidden, as before
    assert set(rows) == {"holder", "fresh"}
    assert rows["holder"]["unverified"] and rows["holder"]["clear_leases"]
    assert rows["holder"]["state"].startswith("unverified")
    assert not rows["fresh"]["unverified"] and not rows["fresh"]["clear_leases"]


def test_mcp_lease_clear(tmp_root: Path, monkeypatch):
    from agent_cli.mcp import specs_repos
    from agent_cli.mcp.tools import Ctx

    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    leases.take(tmp_root, "vault:src", session_id="codex", pid=None)
    ctx = Ctx(root=tmp_root, profile="manager")
    with pytest.raises(AgentError, match="holder"):
        specs_repos.lease(ctx, "clear")
    assert specs_repos.lease(ctx, "clear", holder="codex").data["needs_confirm"] is True
    assert specs_repos.lease(ctx, "clear", holder="codex", confirm=True).data["cleared"] == ["vault:src"]
