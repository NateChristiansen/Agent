"""Advisory area leases: core.is_alive, area_for_path, take/release/
wait/list_all, idle and dead takeover, the refusal message, release_all_for_session,
and the `session list` / `lease *` CLI wiring."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_cli import core, leases, sessions
from agent_cli.core import AgentError

pytestmark = pytest.mark.usefixtures("notes_remote")

DEAD_PID = 2**22  # far past any real pid. os.kill on it always raises ProcessLookupError


# ---------------------------------------------------------------- core.is_alive


def test_is_alive_none_zero_and_dead():
    assert core.is_alive(None) is False
    assert core.is_alive(0) is False
    assert core.is_alive(DEAD_PID) is False


def test_is_alive_true_for_the_current_process():
    assert core.is_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no init pid 1 that a caller cannot signal")
def test_is_alive_true_on_permission_error():
    """pid 1 (launchd/init) exists but a non-root caller cannot signal it. That raises
    PermissionError, which `is_alive` reads as alive (it exists, just isn't ours)."""
    assert core.is_alive(1) is True


# ---------------------------------------------------------------- area_for_path


def test_area_for_path_table():
    """A path names the narrowest area it falls in, capped per top-level by AREA_DEPTH, so two
    sessions in different packages do not queue for each other."""
    cases = {
        "src/agent_cli/hooks.py": "vault:src/agent_cli/hooks.py",
        "src/agent_cli/dispatch/verification.py": "vault:src/agent_cli/dispatch",
        "src/agent_cli/cli/lease.py": "vault:src/agent_cli/cli",
        "tests/test_leases.py": "vault:tests/test_leases.py",
        "bin/agent": "vault:bin/agent",
        "harness/safety.md": "vault:harness/safety.md",
        "harness/obsidian/views/x.js": "vault:harness/obsidian",
        ".claude/settings.json": "vault:.claude/settings.json",
        ".agents/skills/x/SKILL.md": "vault:.agents/skills/x",
        # a top-level file that is its own member keeps the coarse name it always had
        "AGENTS.md": "vault:harness",
        # ADR-0024 deleted CLAUDE.md, so it is no longer a member of any area
        "CLAUDE.md": None,
        "adrs/0001-x.md": None,
        "features/013-x/plan.md": None,
        "STATUS.md": None,
        "journal/2026-09-14.md": None,
        "": None,
        "./": None,
    }
    for rel, expected in cases.items():
        assert leases.area_for_path(rel) == expected, rel


def test_conflicts_is_prefix_overlap_within_one_coarse_root():
    """The whole point of the narrowing: two areas collide only when their paths actually
    overlap. A coarse area still covers everything inside it, both directions."""
    same_work = [
        ("vault:src", "vault:src/agent_cli/cli"),
        ("vault:src/agent_cli/cli", "vault:src"),
        ("vault:src/agent_cli/cli", "vault:src/agent_cli/cli/lease.py"),
        ("vault:harness", "vault:.claude/settings.json"),
        ("vault:harness/safety.md", "vault:harness/safety.md"),
        ("repo:alpha", "repo:alpha"),
    ]
    apart = [
        ("vault:src/agent_cli/cli", "vault:src/agent_cli/dispatch"),
        ("vault:src/agent_cli/cli", "vault:tests/test_leases.py"),
        ("vault:harness/safety.md", "vault:harness/voice.md"),
        # different coarse roots never overlap, even sharing a first segment name
        ("vault:src", "vault:harness"),
        ("repo:alpha", "repo:beta"),
        ("repo:alpha", "vault:src"),
    ]
    for one, other in same_work:
        assert leases.conflicts(one, other), (one, other)
    for one, other in apart:
        assert not leases.conflicts(one, other), (one, other)
        assert not leases.conflicts(other, one), (other, one)


def test_two_sessions_hold_sibling_packages_but_not_a_parent(tmp_root: Path):
    """The case that cost two managers half a day: one holds a subtree, the other wants a
    sibling of it. That now succeeds, while the parent and a child still refuse."""
    leases.take(tmp_root, "vault:src/agent_cli/dispatch", session_id="s1", pid=os.getpid())
    leases.take(tmp_root, "vault:src/agent_cli/cli", session_id="s2", pid=os.getpid())
    assert leases.blocker(tmp_root, "vault:tests/test_leases.py", "s2") is None

    parent = leases.blocker(tmp_root, "vault:src", "s2")
    assert parent is not None and parent["area"] == "vault:src/agent_cli/dispatch"
    child = leases.blocker(tmp_root, "vault:src/agent_cli/dispatch/verification.py", "s2")
    assert child is not None and child["area"] == "vault:src/agent_cli/dispatch"
    with pytest.raises(AgentError, match="vault:src/agent_cli/dispatch is held by s1"):
        leases.take(tmp_root, "vault:src", session_id="s2", pid=os.getpid())


# ---------------------------------------------------------------- take / release


def test_take_release(tmp_root: Path):
    result = leases.take(tmp_root, "vault:src", session_id="s1", pid=os.getpid())
    assert result["taken"] is True and result["took_over_from"] is None
    lease = leases.get(tmp_root, "vault:src")
    assert lease is not None
    assert lease["session_id"] == "s1" and lease["pid"] == os.getpid() and lease["area"] == "vault:src"
    taken_at = lease["taken_at"]
    # a repeat take by the same session touches. It never resets taken_at or logs a takeover
    again = leases.take(tmp_root, "vault:src", session_id="s1", pid=os.getpid())
    assert again["took_over_from"] is None
    lease = leases.get(tmp_root, "vault:src")
    assert lease is not None
    assert lease["taken_at"] == taken_at
    # release is a no-op for a session that does not hold the lease
    assert leases.release(tmp_root, "vault:src", "someone-else") is False
    assert leases.get(tmp_root, "vault:src") is not None
    assert leases.release(tmp_root, "vault:src", "s1") is True
    assert leases.get(tmp_root, "vault:src") is None
    assert leases.release(tmp_root, "vault:src", "s1") is False  # already free: no-op


def test_take_over_an_idle_lease(tmp_root: Path, monkeypatch):
    leases.take(tmp_root, "vault:src", session_id="old", pid=os.getpid())
    monkeypatch.setenv("AGENT_NOW", "2026-09-03T14:36:00-07:00")  # 31 minutes past FIXED_NOW
    assert leases.status(leases.get(tmp_root, "vault:src")) == "idle"
    result = leases.take(tmp_root, "vault:src", session_id="new", pid=os.getpid())
    assert result["took_over_from"] == "old"
    lease = leases.get(tmp_root, "vault:src")
    assert lease is not None
    assert lease["session_id"] == "new"


def test_take_over_a_dead_lease(tmp_root: Path):
    leases.take(tmp_root, "vault:src", session_id="old", pid=DEAD_PID)
    assert leases.status(leases.get(tmp_root, "vault:src")) == "dead"
    result = leases.take(tmp_root, "vault:src", session_id="new", pid=os.getpid())
    assert result["took_over_from"] == "old"
    lease = leases.get(tmp_root, "vault:src")
    assert lease is not None
    assert lease["session_id"] == "new"


# ---------------------------------------------------------------- refusal


def test_refusal_message_on_a_live_holder(tmp_root: Path, monkeypatch):
    sessions.set_record(tmp_root, "s1", name="alice")
    leases.take(
        tmp_root, "vault:src", session_id="s1", pid=424242, feature={"id": 13, "slug": "sessions-and-leases"}
    )
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    with pytest.raises(AgentError) as err:
        leases.take(tmp_root, "vault:src", session_id="s2", pid=os.getpid())
    message = str(err.value)
    assert "vault:src is held by alice (feature 013, touched 0m ago)" in message
    # Both ways out, in this order. Naming only `wait` taught the wrong reflex: a session whose
    # wait timed out had no second option in view and escalated to the owner, who brokered the
    # same conflict by hand three times. `alice` is also the peer address, so the
    # ask is spelled out rather than composed at the moment of friction.
    assert (
        "the `lease` tool with action `wait`, area `vault:src`, and `ask` saying why you need it" in message
    )
    assert "messages alice" in message
    assert "without `ask`, timeout 120" in message
    assert message.index("and `ask` saying") < message.index("timeout 120")
    assert "arbitrate only after" in message
    # the session's own edit is never refused: same holder just touches
    leases.take(tmp_root, "vault:src", session_id="s1", pid=424242)


def test_refusal_falls_back_to_session_id_and_no_feature(tmp_root: Path, monkeypatch):
    leases.take(tmp_root, "vault:harness", session_id="no-name-session", pid=os.getpid())
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    with pytest.raises(
        AgentError, match=r"vault:harness is held by no-name-session \(no feature, touched 0m ago\)"
    ):
        leases.take(tmp_root, "vault:harness", session_id="s2", pid=os.getpid())


# ---------------------------------------------------------------- wait


def test_wait_returns_when_the_holder_releases(tmp_root: Path):
    leases.take(tmp_root, "vault:src", session_id="holder", pid=os.getpid())

    def release_soon() -> None:
        time.sleep(1.2)
        leases.release(tmp_root, "vault:src", "holder")

    th = threading.Thread(target=release_soon)
    th.start()
    result = leases.wait(tmp_root, "vault:src", session_id="waiter", pid=os.getpid(), timeout=10)
    th.join(timeout=5)
    assert result["taken"] is True
    lease = leases.get(tmp_root, "vault:src")
    assert lease is not None
    assert lease["session_id"] == "waiter"


def test_wait_raises_on_timeout(tmp_root: Path, monkeypatch):
    leases.take(tmp_root, "vault:src", session_id="holder", pid=os.getpid())
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    with pytest.raises(AgentError, match="without `ask`, timeout 120"):
        leases.wait(tmp_root, "vault:src", session_id="waiter", pid=os.getpid(), timeout=0.5)


def test_wait_returns_immediately_when_already_held_by_this_session(tmp_root: Path):
    leases.take(tmp_root, "vault:src", session_id="s1", pid=os.getpid())
    result = leases.wait(tmp_root, "vault:src", session_id="s1", pid=os.getpid(), timeout=1)
    assert result["taken"] is True and result["took_over_from"] is None


# ---------------------------------------------------------------- release_all_for_session / list_all


def test_release_all_for_session(tmp_root: Path):
    leases.take(tmp_root, "vault:src", session_id="s1", pid=os.getpid())
    leases.take(tmp_root, "vault:harness", session_id="s1", pid=os.getpid())
    leases.take(tmp_root, "repo:alpha", session_id="s2", pid=os.getpid())
    released = leases.release_all_for_session(tmp_root, "s1")
    assert sorted(released) == ["vault:harness", "vault:src"]
    assert leases.get(tmp_root, "vault:src") is None
    assert leases.get(tmp_root, "vault:harness") is None
    assert leases.get(tmp_root, "repo:alpha") is not None
    assert leases.release_all_for_session(tmp_root, "s1") == []  # nothing left: no-op


def test_list_all_statuses(tmp_root: Path, monkeypatch):
    leases.take(tmp_root, "vault:harness", session_id="dead", pid=DEAD_PID)
    monkeypatch.setenv("AGENT_NOW", "2026-09-03T14:36:00-07:00")  # 31 minutes past FIXED_NOW
    leases.take(tmp_root, "repo:alpha", session_id="fresh", pid=os.getpid())
    items = {lease["area"]: lease["status"] for lease in leases.list_all(tmp_root)}
    assert items == {"vault:harness": "dead", "repo:alpha": "live"}


def test_lease_statuses_names_every_status(tmp_root: Path, monkeypatch):
    """A live-vault shim test failed only when a real lease happened to be `resting`.

    Its hand-written expected statuses had never seen that state. This test reads the
    implementation's return values so the exported set used by live-state tests cannot
    silently omit a newly added status.
    """
    import inspect
    import re

    returned = set(re.findall(r'return "(\w+)"', inspect.getsource(leases.status)))
    assert returned == set(leases.LEASE_STATUSES)


def test_list_all_empty(tmp_root: Path):
    assert leases.list_all(tmp_root) == []


# ---------------------------------------------------------------- CLI wiring: session list, lease *


def test_cli_session_list_and_lease_actions(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    r = run(["hook", "session-start"], stdin='{"session_id": "s1", "source": "startup"}')
    assert r.code == 0
    r = run(["--json", "session", "list"])
    assert r.code == 0
    rows = {row["session_id"]: row for row in r.json}
    assert "s1" in rows and rows["s1"]["feature"] == "001 x"
    r = run(["--json", "lease", "take", "vault:src", "--session-id", "s1", "--note", "testing"])
    assert r.code == 0 and r.json["lease"]["session_id"] == "s1" and r.json["lease"]["note"] == "testing"
    r = run(["--json", "lease", "list"])
    assert r.code == 0
    assert any(lease["area"] == "vault:src" and lease["status"] == "live" for lease in r.json)
    r = run(["--json", "lease", "wait", "vault:src", "--session-id", "s1", "--timeout", "1"])
    assert r.code == 0 and r.json["taken"] is True  # already held by this session: returns at once
    r = run(["--json", "lease", "release", "vault:src", "--session-id", "s1"])
    assert r.code == 0 and r.json["released"] is True
    r = run(["--json", "lease", "release", "vault:src", "--session-id", "s1"])
    assert r.code == 0 and r.json["released"] is False


def test_wait_can_ask_the_holder_for_the_lease(tmp_root: Path, monkeypatch):
    """`--ask` exists because waiting and asking were separate commands.

    The refusal named `wait` alone, so the move after a timeout was to escalate to the owner
    rather than to message a session whose name the refusal had already printed. The owner
    brokered the same conflict by hand three times before saying so. One command
    now does both, and the ask lands in the holder's inbox as a record.
    """
    from agent_cli import ops, peers

    sessions.set_record(tmp_root, "holder", name="mgr-025-convention-gate", pid=os.getpid())
    sessions.set_record(tmp_root, "waiter", name="mgr-032-parity", pid=os.getpid())
    leases.take(tmp_root, "vault:src", session_id="holder", pid=os.getpid())
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "waiter")

    with pytest.raises(AgentError):
        ops.lease_wait(tmp_root, area="vault:src", timeout=0.5, ask="three edits, none under dispatch/")

    queued = peers.peek(tmp_root, "holder")
    assert len(queued) == 1
    body = queued[0]["text"]
    assert "vault:src" in body and "three edits, none under dispatch/" in body
    # the holder is told both ways to respond: release, or carve out what it still needs
    assert "action `release`, area `vault:src`" in body
    assert "still need" in body
    assert queued[0]["from_name"] == "mgr-032-parity"


def test_an_unsendable_ask_never_costs_the_wait(tmp_root: Path, monkeypatch):
    """The wait is the part that acquires the lease; the ask is a courtesy. A courtesy that
    raises would turn a recoverable wait into a failure."""
    from agent_cli import ops

    sessions.set_record(tmp_root, "holder", name="held", pid=os.getpid())
    leases.take(tmp_root, "vault:src", session_id="holder", pid=os.getpid())
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "waiter")
    monkeypatch.setattr("agent_cli.peers.send", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    # still the ordinary timeout refusal, not the OSError
    with pytest.raises(AgentError, match="vault:src is held by"):
        ops.lease_wait(tmp_root, area="vault:src", timeout=0.5, ask="why")
