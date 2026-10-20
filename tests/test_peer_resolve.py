"""`peer send` resolves the name the owner reads off a tab, and never guesses.

The owner names managers by their Obsidian tab: "cl 834e" for Claude, "cx 8ddb" for Codex. The
Codex short id is `session_id[-8:-4]`, not a prefix, so nothing could map it. Name prefixes are
gone: one delivered a reply to the wrong manager after the named one ended.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import ops, peers, sessions
from agent_cli.core import AgentError

ALIVE = 424243
CLAUDE_SID = "834e1f00-0000-4000-8000-00000000aaaa"
CODEX_SID = "01a1007c-0000-7000-8000-00008ddb0001"


@pytest.fixture(autouse=True)
def _alive(monkeypatch):
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: pid == ALIVE)


def _session(root: Path, sid: str, name: str, pid: int = ALIVE) -> None:
    sessions.set_record(root, sid, pid=pid, name=name)


def _codex(root: Path, sid: str = CODEX_SID, name: str = "mgr-unmapped-01a100-codex-8ddb3102") -> None:
    _session(root, sid, name)
    sessions.set_field(root, sid, "provider", "codex")


def _quiet(root: Path, sid: str, name: str) -> None:
    """A Codex manager past the 30-minute rule: no pid, idle since long ago."""
    from agent_cli import session_liveness

    sessions.set_record(root, sid, name=name)
    sessions.set_field(root, sid, "provider", "codex")
    session_liveness.set_identity(root, sid, None, False)
    sessions.set_field(root, sid, "activity", {"state": "idle", "at": "2026-09-03T14:04:00-07:00"})


def test_a_name_prefix_is_suggested_never_sent(tmp_root: Path):
    """The owner dropped name prefixes (2026-10-03): a prefix that happens to be unique today is
    a guess, and a guess once delivered a reply to the wrong manager."""
    _session(tmp_root, "sid-aaaa1111", "mgr-030-alpha-work")
    _session(tmp_root, "sid-bbbb2222", "mgr-023-beta-work")

    with pytest.raises(AgentError) as err:
        ops.peers.resolve(tmp_root, "mgr-023")
    assert "Did you mean mgr-023-beta-work?" in str(err.value)


def test_a_near_miss_suggests_the_nearest_live_name(tmp_root: Path):
    _session(tmp_root, "sid-aaaa1111", "mgr-030-alpha-work")
    _session(tmp_root, "sid-bbbb2222", "mgr-023-beta-work")

    with pytest.raises(AgentError) as err:
        ops.peers.resolve(tmp_root, "mgr-023-beta-wrok")
    assert "Did you mean mgr-023-beta-work?" in str(err.value)


def test_an_ended_name_never_reaches_the_live_one_it_prefixes(tmp_root: Path):
    """A reply to a manager that had just ended went by prefix to a
    different live session, with no warning."""
    _session(tmp_root, "sid-aaaa1111", "mgr-030-alpha-work")
    _session(tmp_root, "sid-ended000", "mgr-030-alpha")
    sessions.set_field(tmp_root, "sid-ended000", "activity", {"state": "ended"})

    with pytest.raises(AgentError) as err:
        ops.peers.resolve(tmp_root, "mgr-030-alpha")
    assert "mgr-030-alpha has ended." in str(err.value)
    assert "Did you mean mgr-030-alpha-work?" in str(err.value)


@pytest.mark.parametrize("typed", ["cx 8ddb", "cx8ddb", "CX 8DDB", "8ddb"])
def test_a_codex_tab_short_name_resolves(tmp_root: Path, typed: str):
    """The owner typed "cx8ddb" for this exact session on 2026-10-03."""
    _session(tmp_root, CLAUDE_SID, "mgr-006-peer-short-names")
    _codex(tmp_root)
    assert ops.peers.resolve(tmp_root, typed) == CODEX_SID


@pytest.mark.parametrize("typed", ["cl 834e", "cl834e", "834e"])
def test_a_claude_tab_short_name_resolves(tmp_root: Path, typed: str):
    _session(tmp_root, CLAUDE_SID, "mgr-006-peer-short-names")
    _codex(tmp_root)
    assert ops.peers.resolve(tmp_root, typed) == CLAUDE_SID


def test_a_tab_short_name_with_the_wrong_client_tag_misses(tmp_root: Path):
    _session(tmp_root, CLAUDE_SID, "mgr-006-peer-short-names")
    _codex(tmp_root)
    with pytest.raises(AgentError):
        ops.peers.resolve(tmp_root, "cl 8ddb")


def test_two_sessions_with_one_tab_short_name_are_refused(tmp_root: Path):
    _codex(tmp_root)
    _codex(tmp_root, sid="02b2007c-0000-7000-8000-00008ddb9999", name="mgr-other-codex")
    with pytest.raises(AgentError) as err:
        ops.peers.resolve(tmp_root, "cx 8ddb")
    assert "ambiguous" in str(err.value)
    assert "(cx 8ddb)" in str(err.value)


def test_a_live_and_a_quiet_codex_with_one_short_name_are_refused(tmp_root: Path, monkeypatch):
    """Review finding (cx 8ddb, 2026-10-03): resolving live sessions first would let a live
    `cx 8ddb` win silently over a quiet one the owner may have meant."""
    monkeypatch.setenv("AGENT_NOW", "2026-09-03T16:36:00-07:00")
    _codex(tmp_root)
    _quiet(tmp_root, "03c3007c-0000-7000-8000-00008ddb7777", "mgr-quiet-codex")
    with pytest.raises(AgentError) as err:
        ops.peers.resolve(tmp_root, "cx 8ddb")
    assert "ambiguous" in str(err.value)


def test_a_live_codex_that_also_passes_the_quiet_filter_is_counted_once(tmp_root: Path):
    """A live record can carry `pid_unknown` too; counted twice, a unique match looked
    ambiguous (review note, cx 8ddb, 2026-10-03)."""
    _codex(tmp_root)
    sessions.set_field(tmp_root, CODEX_SID, "pid_unknown", True)
    assert ops.peers.resolve(tmp_root, "cx 8ddb") == CODEX_SID
    assert ops.peers.resolve(tmp_root, "mgr-unmapped-01a100-codex-8ddb3102") == CODEX_SID


def test_two_quiet_codex_managers_with_one_name_are_refused(tmp_root: Path, monkeypatch):
    """`quiet_codex` used to turn two matches into None, which read as "no such session"."""
    monkeypatch.setenv("AGENT_NOW", "2026-09-03T16:36:00-07:00")
    _quiet(tmp_root, "q1", "mgr-030-codex")
    _quiet(tmp_root, "q2", "mgr-030-codex")
    with pytest.raises(AgentError) as err:
        ops.peers.resolve(tmp_root, "mgr-030-codex")
    assert "matches 2 sessions" in str(err.value)


def test_an_unrelated_record_with_a_short_id_does_not_break_a_tab_lookup(tmp_root: Path, monkeypatch):
    """Review finding (cx 8ddb, 2026-10-03): a quiet record with id `q1` has a tab of `cx `,
    and indexing its suffix raised IndexError for every tab lookup."""
    monkeypatch.setenv("AGENT_NOW", "2026-09-03T16:36:00-07:00")
    _quiet(tmp_root, "q1", "mgr-030-codex")
    _codex(tmp_root)
    assert ops.peers.resolve(tmp_root, "cx 8ddb") == CODEX_SID
    with pytest.raises(AgentError):
        ops.peers.resolve(tmp_root, "cx 9999")


def test_peer_list_shows_a_quiet_codex_manager_as_quiet(tmp_root: Path, monkeypatch):
    """An idle Codex manager dropped out of `peer list`, so its peers concluded it was
    gone and the owner woke it by hand. It is listed, marked quiet, and an ended one is not."""
    monkeypatch.setattr("agent_cli.vault.managers.claude_live_sessions", lambda: {})
    monkeypatch.setenv("AGENT_NOW", "2026-09-03T16:36:00-07:00")
    _session(tmp_root, CLAUDE_SID, "mgr-006-peer-short-names")
    _quiet(tmp_root, "q1", "mgr-045-codex")
    _quiet(tmp_root, "q2", "mgr-old-codex")
    sessions.set_field(tmp_root, "q2", "activity", {"state": "ended", "at": "2026-09-03T14:04:00-07:00"})

    by_name = {item["name"]: item for item in ops.peer_list(tmp_root).data}
    assert by_name["mgr-045-codex"]["kind"] == "quiet"
    assert by_name["mgr-006-peer-short-names"]["kind"] == "foreground"
    assert "mgr-old-codex" not in by_name
    assert ops.peers.resolve(tmp_root, "mgr-045-codex") == "q1"


def test_peer_list_shows_the_tab_short_name_and_the_machine_scope(tmp_root: Path, monkeypatch):
    monkeypatch.setattr("agent_cli.vault.managers.claude_live_sessions", lambda: {})
    _session(tmp_root, CLAUDE_SID, "mgr-006-peer-short-names")
    _codex(tmp_root)
    peers.send(tmp_root, to=CODEX_SID, text="queued", frm=CLAUDE_SID)

    op = ops.peer_list(tmp_root)
    by_name = {item["name"]: item for item in op.data}
    assert by_name["mgr-006-peer-short-names"]["tab"] == "cl 834e"
    codex = by_name["mgr-unmapped-01a100-codex-8ddb3102"]
    assert codex["tab"] == "cx 8ddb"
    # named fields, not row positions: the new column must not shift these
    assert codex["kind"] == "foreground"
    assert codex["pending"] == 1
    assert codex["hooks"] in {"ok", "stale"}
    assert "cx 8ddb" in op.text and "cl 834e" in op.text
    assert op.text.splitlines()[0].split()[:3] == ["name", "tab", "id"]

    # say the scope, and the route that exists, before anyone guesses
    assert "no other device" in op.text
    assert "`peer-net pair`; then `<device>/<name>`" in op.text  # the route once paired
    assert "Remote Control" in op.text
    assert "`ListAgents`, then `SendMessage`" in op.text
