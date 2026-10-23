"""Lease coordination between live sessions, so the owner never brokers a deadlock.

2026-09-16: one manager session held vault:harness and needed vault:src, another held vault:src
and needed vault:harness. Two rules: a denied edit records a request on the lease, and the
holder's turn end makes its lease resting, so the next edit takes it over.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_cli import core, hooks, leases, sessions


@pytest.fixture
def active_managers(tmp_root: Path, run, monkeypatch: pytest.MonkeyPatch, notes_remote: Path) -> None:
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    assert run(["feature", "new", "lease-owner", "--title", "Lease owner"]).code == 0
    for session in ("holder", "needer", "mine"):
        assert run(["feature", "use", "1", "--session-id", session]).code == 0


def _edit(session_id: str, rel: str = "src/agent_cli/x.py") -> dict:
    return {
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": rel},
    }


def _stop(session_id: str) -> dict:
    return {"session_id": session_id, "hook_event_name": "Stop", "last_assistant_message": "ok"}


def test_a_resting_holder_s_lease_is_taken_over(tmp_root: Path, monkeypatch, active_managers):
    """The holder here sits on the whole-subtree area while the needer edits one file inside
    it. Since then the needer's area is the narrower path, so the takeover has to clear
    the resting parent rather than layer a child lease under it — two overlapping leases
    disagree about who holds the files, and the stale one blocks the next taker for nothing."""
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    leases.take(tmp_root, "vault:src", session_id="holder", pid=os.getpid())
    assert leases.status(leases.get(tmp_root, "vault:src"), tmp_root) == "live"
    # the holder's turn ends after its last touch: it is at a prompt, not mid-edit
    hooks.lease_yield(tmp_root, _stop("holder"))
    assert leases.status(leases.get(tmp_root, "vault:src"), tmp_root) == "resting"
    out = hooks.protect_generated(tmp_root, _edit("needer"))
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "took vault:src/agent_cli/x.py over from holder" in context
    assert leases.get(tmp_root, "vault:src") is None
    held = [(lease["area"], lease["session_id"]) for lease in leases.list_all(tmp_root)]
    assert held == [("vault:src/agent_cli/x.py", "needer")]


def test_a_file_lease_already_held_stands_in_for_its_directory(tmp_root: Path, monkeypatch, active_managers):
    """Two managers each leased one file in `hooks/`; the edit hook still asked for the whole
    directory and refused both (2026-09-24)."""
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    leases.take(tmp_root, "vault:src/agent_cli/hooks/a.py", session_id="peer", pid=os.getpid())
    rel = "src/agent_cli/hooks/b.py"
    assert "permissionDecision" in hooks.protect_generated(tmp_root, _edit("mine", rel))
    leases.take(tmp_root, f"vault:{rel}", session_id="mine", pid=os.getpid())
    assert hooks.protect_generated(tmp_root, _edit("mine", rel)) == ""
    assert leases.get(tmp_root, "vault:src/agent_cli/hooks") is None


def test_a_busy_holder_is_asked_and_yields_at_turn_end(tmp_root: Path, monkeypatch, active_managers):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    sessions.set_record(tmp_root, "needer", name="mgr-007-example")
    area = "vault:src/agent_cli/x.py"
    hooks.protect_generated(tmp_root, _edit("holder"))  # the holder is mid-turn in that file
    denied = json.loads(hooks.protect_generated(tmp_root, _edit("needer")))["hookSpecificOutput"]
    assert denied["permissionDecision"] == "deny"
    assert "request is on the lease" in denied["permissionDecisionReason"]
    lease = leases.get(tmp_root, area)
    assert lease is not None and [r["session_id"] for r in lease["requests"]] == ["needer"]
    # the holder touching its own lease keeps the request, and the lease stays live
    hooks.protect_generated(tmp_root, _edit("holder"))
    assert leases.status(leases.get(tmp_root, area), tmp_root) == "live"
    # turn end: the lease rests, and the needer's next edit takes it over
    hooks.run(tmp_root, "lease-yield", json.dumps(_stop("holder")))
    assert leases.status(leases.get(tmp_root, area), tmp_root) == "resting"
    out = hooks.protect_generated(tmp_root, _edit("needer"))
    assert "over from holder" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    lease = leases.get(tmp_root, area)
    assert lease is not None and lease["session_id"] == "needer"


def test_a_subagent_stop_is_not_a_turn_end(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    leases.take(tmp_root, "vault:harness", session_id="holder", pid=os.getpid())
    hooks.lease_yield(tmp_root, {**_stop("holder"), "agent_id": "a1"})
    assert leases.status(leases.get(tmp_root, "vault:harness"), tmp_root) == "live"
    assert leases.request(tmp_root, "vault:harness", session_id="holder", pid=os.getpid()) is False


def test_a_blocked_stop_is_not_a_turn_end(tmp_root: Path, monkeypatch):
    """An earlier Stop-group member (peer-stop, ask-guard) blocked it: the turn goes on (C8)."""
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    leases.take(tmp_root, "vault:harness", session_id="holder", pid=os.getpid())
    hooks.lease_yield(tmp_root, {**_stop("holder"), "_stop_blocked": True})
    assert leases.status(leases.get(tmp_root, "vault:harness"), tmp_root) == "live"
    hooks.lease_yield(tmp_root, _stop("holder"))
    assert leases.status(leases.get(tmp_root, "vault:harness"), tmp_root) == "resting"


def test_the_turn_end_hook_is_wired_on_both_clients():
    root = Path(__file__).resolve().parents[1]
    settings = json.loads((root / ".claude" / "settings.json").read_text())
    codex = json.loads((root / ".codex" / "hooks.json").read_text())
    from agent_cli.hooks.groups import wired

    for config in (settings, codex):
        assert "lease-yield" in wired(config, "Stop")
