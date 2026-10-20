"""peer list marks Claude background sessions without hiding them (#69, #71). A Remote Control
message forks its target into a `claude --bg` session, and the daemon starts a never-prompted
spare beside it; the fresh Windows clone (2026-09-30) listed both among the managers. A fork is
listed as `parked` right after its tab, other bg sessions last, a spare not at all, and every
one of them stays addressable by exact name."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import ops, peers, sessions

ALIVE = 424243


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: pid == ALIVE)
    entries: dict[str, dict] = {}
    monkeypatch.setattr("agent_cli.vault.managers.claude_live_sessions", lambda: entries)
    return entries


def _session(root: Path, sid: str, name: str, *, prompted: bool = True) -> None:
    sessions.set_record(root, sid, pid=ALIVE, name=name)
    if prompted:
        sessions.set_field(root, sid, "activity", {"state": "idle", "at": "2026-09-30T09:00:00-06:00"})


def test_a_background_session_is_marked_last_and_still_addressable(tmp_root: Path, live):
    _session(tmp_root, "sid-fork", "mgr-fork")
    _session(tmp_root, "sid-tab", "mgr-tab")
    live.update({"sid-fork": {"kind": "bg"}, "sid-tab": {"kind": "interactive"}})
    items = ops.peers.peer_list(tmp_root).data
    assert [(i["name"], i["kind"]) for i in items] == [("mgr-tab", "foreground"), ("mgr-fork", "bg")]
    assert ops.peers.resolve(tmp_root, "mgr-fork") == "sid-fork"


def test_a_parked_fork_follows_its_tab(tmp_root: Path, live):
    _session(tmp_root, "sid-a", "mgr-a")
    _session(tmp_root, "sid-other", "mgr-other")
    _session(tmp_root, "c9cb2073-fork", "mgr-fork")
    live.update(
        {
            "sid-a": {"kind": "interactive", "parkedJobId": "c9cb2073"},
            "sid-other": {"kind": "interactive"},
            "c9cb2073-fork": {"kind": "bg", "jobId": "c9cb2073"},
        }
    )
    kinds = {i["name"]: i["kind"] for i in ops.peers.peer_list(tmp_root).data}
    names = [i["name"] for i in ops.peers.peer_list(tmp_root).data]
    assert kinds["mgr-fork"] == "parked"
    assert names.index("mgr-fork") == names.index("mgr-a") + 1


def test_a_never_used_spare_is_left_out_until_it_does_something(tmp_root: Path, live):
    _session(tmp_root, "sid-tab", "mgr-tab")
    _session(tmp_root, "sid-spare", "mgr-spare", prompted=False)
    live.update({"sid-tab": {"kind": "interactive"}, "sid-spare": {"kind": "bg"}})
    assert "mgr-spare" not in [i["name"] for i in ops.peers.peer_list(tmp_root).data]
    assert ops.peers.resolve(tmp_root, "mgr-spare") == "sid-spare"  # still addressable
    peers.send(tmp_root, to="sid-spare", text="hello", frm="sid-tab", frm_name="mgr-tab")
    assert "mgr-spare" in [i["name"] for i in ops.peers.peer_list(tmp_root).data]
