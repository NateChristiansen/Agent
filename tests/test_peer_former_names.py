"""Peer resolution by a session's former name (phase F)."""

from __future__ import annotations

from pathlib import Path

from agent_cli import ops
from test_peers import _alive, _session  # noqa: F401 -- autouse fixture


def test_a_renamed_session_is_still_reached_by_its_former_name(tmp_root: Path):
    """Sends to a manager that feature_use had just renamed failed with "no live session
    matches". A unique former name resolves; a current name always wins over it."""
    from agent_cli import sessions as sessions_mod

    _session(tmp_root, "sid-aaaa1111", "mgr-030-alpha-work")
    _session(tmp_root, "sid-bbbb2222", "mgr-unmapped-bbbb22")
    sessions_mod.set_field(tmp_root, "sid-aaaa1111", "former_names", ["mgr-unmapped-aaaa11"])
    assert ops.peers.resolve(tmp_root, "mgr-unmapped-aaaa11") == "sid-aaaa1111"
    sessions_mod.set_field(tmp_root, "sid-bbbb2222", "former_names", ["mgr-030-alpha-work"])
    assert ops.peers.resolve(tmp_root, "mgr-030-alpha-work") == "sid-aaaa1111"


def test_an_ambiguous_former_name_raises_instead_of_falling_through(tmp_root: Path):
    import pytest

    from agent_cli import sessions as sessions_mod
    from agent_cli.core import AgentError

    _session(tmp_root, "sid-aaaa1111", "mgr-030-alpha-work")
    _session(tmp_root, "sid-bbbb2222", "mgr-031-beta-work")
    _session(tmp_root, "mgr-old-x-prefixed", "mgr-032-gamma")  # an id the name would prefix-match
    for sid in ("sid-aaaa1111", "sid-bbbb2222"):
        sessions_mod.set_field(tmp_root, sid, "former_names", ["mgr-old"])
    with pytest.raises(AgentError, match="was the name of 2 sessions"):
        ops.peers.resolve(tmp_root, "mgr-old")
