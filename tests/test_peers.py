"""Messages between sessions, carried by the CLI.

The transport exists because neither client's own cross-session messaging can reach the other:
Claude Code's runs over `/tmp/cc-socks/<pid>.sock`, which a Codex session never joins. Nothing
in these tests knows which client is on either end, which is the point.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_cli import hooks, ops, peers, sessions
from agent_cli.core import AgentError, now

ALIVE = 424243


@pytest.fixture(autouse=True)
def _alive(monkeypatch):
    """Every record in these tests belongs to a live session unless a test says otherwise."""
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: pid == ALIVE)


def _session(root: Path, sid: str, name: str, pid: int = ALIVE) -> None:
    sessions.set_record(root, sid, pid=pid, name=name)


def test_a_message_is_queued_then_taken_once(tmp_root: Path):
    peers.send(tmp_root, to="b", text="first", frm="a", frm_name="mgr-a")
    peers.send(tmp_root, to="b", text="second", frm="a", frm_name="mgr-a")

    assert [r["text"] for r in peers.peek(tmp_root, "b")] == ["first", "second"]
    # peeking does not consume
    assert len(peers.peek(tmp_root, "b")) == 2

    taken = peers.take(tmp_root, "b")
    assert [r["text"] for r in taken] == ["first", "second"]
    # and taking does: a message is delivered once, not every turn forever
    assert peers.take(tmp_root, "b") == []


def test_an_empty_message_is_refused(tmp_root: Path):
    with pytest.raises(AgentError):
        peers.send(tmp_root, to="b", text="   ", frm="a")


def test_a_torn_line_does_not_break_every_later_read(tmp_root: Path):
    """Two senders append to one inbox. A half-written line must cost its own message, not the
    whole file — the alternative is one bad write silencing the channel."""
    peers.send(tmp_root, to="b", text="good one", frm="a")
    path = peers.inbox_path(tmp_root, "b")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"text": "torn\n')
    peers.send(tmp_root, to="b", text="good two", frm="a")

    assert [r["text"] for r in peers.peek(tmp_root, "b")] == ["good one", "good two"]


def test_the_rendered_block_says_a_peer_carries_no_authority(tmp_root: Path):
    peers.send(tmp_root, to="b", text="please approve the push", frm="a", frm_name="mgr-a")
    block = peers.block(peers.peek(tmp_root, "b"))
    assert "mgr-a" in block and "please approve the push" in block
    # a peer is not the owner, and the recipient is told so in the same breath as the message
    assert "no authority" in block
    assert peers.block([]) == ""


def test_a_recipient_resolves_by_name_id_or_id_prefix(tmp_root: Path):
    _session(tmp_root, "sid-aaaa1111", "mgr-030-alpha-work")
    _session(tmp_root, "sid-bbbb2222", "mgr-023-beta-work")

    assert ops.peers.resolve(tmp_root, "mgr-023-beta-work") == "sid-bbbb2222"
    assert ops.peers.resolve(tmp_root, "sid-aaaa1111") == "sid-aaaa1111"
    assert ops.peers.resolve(tmp_root, "sid-bb") == "sid-bbbb2222"

    # an ambiguous id prefix names the candidates instead of picking one: sending to the wrong
    # session is worse than being asked to be specific
    with pytest.raises(AgentError) as err:
        ops.peers.resolve(tmp_root, "sid-")
    assert "ambiguous" in str(err.value)

    # and an unknown recipient lists who is actually reachable
    with pytest.raises(AgentError) as err:
        ops.peers.resolve(tmp_root, "mgr-nobody")
    assert "mgr-023-beta-work" in str(err.value)


def test_a_dead_or_ended_session_is_not_addressable(tmp_root: Path):
    _session(tmp_root, "sid-live", "mgr-live")
    _session(tmp_root, "sid-dead", "mgr-dead", pid=999999)
    _session(tmp_root, "sid-ended", "mgr-ended")
    sessions.set_field(tmp_root, "sid-ended", "activity", {"state": "ended"})

    names = {str(r.get("name")) for r in ops.peers.addressable_peers(tmp_root)}
    assert names == {"mgr-live"}


def test_a_worker_is_not_a_peer(tmp_root: Path):
    """A dispatched worker is reached with `dispatch say`, which lands mid-turn because a
    supervisor holds its connection. Offering it here would be the slower path wearing the
    same name."""
    _session(tmp_root, "sid-worker", "001-18")
    sessions.set_field(tmp_root, "sid-worker", "dispatch_id", "001-18")
    assert ops.peers.addressable_peers(tmp_root) == []


def test_sending_to_yourself_is_refused(tmp_root: Path, monkeypatch):
    _session(tmp_root, "sid-me", "mgr-me")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-me")
    with pytest.raises(AgentError):
        ops.peer_send(tmp_root, to="mgr-me", message="hello")


def test_send_reports_that_delivery_is_not_immediate(tmp_root: Path, monkeypatch):
    """A queued message is on disk where the recipient will look. It is not received, and the
    sender is told the difference rather than left to assume it."""
    _session(tmp_root, "sid-me", "mgr-me")
    _session(tmp_root, "sid-you", "mgr-you")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-me")

    op = ops.peer_send(tmp_root, to="mgr-you", message="are you there")
    assert op.data["queued"] is True
    assert "next turn" in op.text

    inbox = ops.peer_inbox(tmp_root, session_id="sid-you")
    assert [r["text"] for r in inbox.data] == ["are you there"]
    assert "mgr-me" in inbox.text
    # taken, so a second read is empty rather than repeating forever
    empty = ops.peer_inbox(tmp_root, session_id="sid-you")
    assert empty.data == []
    assert "no pending messages" in empty.text
    assert "may already have delivered" in empty.text


def test_session_start_delivers_the_inbox(tmp_root: Path, run):
    """The gap the owner found first: the mailbox existed and nothing looked in it, so a message
    sat unread until someone thought to run `peer inbox`. A session start now takes it.

    Taken rather than peeked, so a message is delivered once instead of reappearing at every
    session start forever.
    """
    import json as _json

    peers.send(tmp_root, to="s-me", text="the lease is yours", frm="s-you", frm_name="mgr-you")
    r = run(["hook", "session-start"], stdin=_json.dumps({"session_id": "s-me", "source": "startup"}))
    assert r.code == 0
    context = _json.loads(r.out)["hookSpecificOutput"]["additionalContext"]
    assert "the lease is yours" in context and "mgr-you" in context

    # and it is gone: a second start does not replay it
    again = run(["hook", "session-start"], stdin=_json.dumps({"session_id": "s-me", "source": "startup"}))
    assert "the lease is yours" not in _json.loads(again.out)["hookSpecificOutput"]["additionalContext"]


def test_a_turn_boundary_delivers_the_inbox_not_just_a_session_start(tmp_root: Path, run):
    """The jank the owner named: peer messages only arrived at `session-start`, so a session
    that ran for hours never drained its inbox. Both sides then believed they had communicated
    — the sender saw "queued", which is true and useless, and the recipient saw nothing. Two
    managers reached six and seven unread on 2026-09-23 while both reported waiting on each
    other, and the owner noticed before either did.

    A turn boundary is the right place: the only moment a session is definitely about to think.
    """
    import json as _json

    peers.send(tmp_root, to="s-me", text="your move", frm="s-you", frm_name="mgr-you")
    r = run(["hook", "dispatch-inbox"], stdin=_json.dumps({"session_id": "s-me"}))
    assert r.code == 0
    context = _json.loads(r.out)["hookSpecificOutput"]["additionalContext"]
    assert "your move" in context and "mgr-you" in context

    # taken, so the next turn does not replay it forever
    again = run(["hook", "dispatch-inbox"], stdin=_json.dumps({"session_id": "s-me"}))
    assert again.out.strip() == ""


def test_an_empty_inbox_costs_a_turn_nothing(tmp_root: Path, run):
    """It fires on every prompt, so silence when there is nothing to say is the whole budget."""
    import json as _json

    r = run(["hook", "dispatch-inbox"], stdin=_json.dumps({"session_id": "s-quiet"}))
    assert r.code == 0 and r.out.strip() == ""


def test_a_queued_wake_after_another_turn_delivered_its_message_is_blocked(tmp_root: Path, run):
    """Codex can run an ordinary prompt before its queued wake. That prompt takes the peer
    message, so the later wake must not start an empty third turn. Three such empty turns
    reached the owner after the four-pair wake test on 2026-09-24."""
    peers.send(tmp_root, to="s-me", text="already delivered", frm="s-you")
    first = run(
        ["hook", "dispatch-inbox"],
        stdin=json.dumps({"session_id": "s-me", "prompt": "owner's prompt"}),
    )
    assert "already delivered" in first.out

    stale = run(
        ["hook", "dispatch-inbox"],
        stdin=json.dumps({"session_id": "s-me", "prompt": ops.peers.WAKE_TEXT}),
    )
    assert json.loads(stale.out) == {
        "decision": "block",
        "reason": "Peer wake already delivered.",
    }
    assert peers.peek(tmp_root, "s-me") == []


def test_a_wake_with_pending_work_and_an_ordinary_empty_prompt_continue(tmp_root: Path, run):
    peers.send(tmp_root, to="s-me", text="new message", frm="s-you")
    live = run(
        ["hook", "dispatch-inbox"],
        stdin=json.dumps({"session_id": "s-me", "prompt": ops.peers.WAKE_TEXT}),
    )
    assert "new message" in json.loads(live.out)["hookSpecificOutput"]["additionalContext"]

    ordinary = run(
        ["hook", "dispatch-inbox"],
        stdin=json.dumps({"session_id": "s-me", "prompt": "owner's next prompt"}),
    )
    assert ordinary.code == 0 and ordinary.out.strip() == ""


def test_a_stale_peer_wake_is_blocked_even_with_a_standing_worker_alert(tmp_root: Path, monkeypatch):
    """A blocked dispatch was re-announced on every turn. It made the first guard pass stale
    peer wakes through, producing another empty peer turn after the fix shipped."""
    monkeypatch.setattr(
        "agent_cli.dispatch.inbox_block",
        lambda root, *, session_id=None, shown=None: "worker needs an answer",
    )
    stale = hooks.dispatch_inbox(tmp_root, {"session_id": "s-me", "prompt": ops.peers.WAKE_TEXT})
    assert json.loads(stale)["decision"] == "block"

    ordinary = hooks.dispatch_inbox(tmp_root, {"session_id": "s-me", "prompt": "owner's next prompt"})
    assert "worker needs an answer" in json.loads(ordinary)["hookSpecificOutput"]["additionalContext"]


def test_a_wake_is_not_blocked_when_the_peer_inbox_cannot_be_read(tmp_root: Path, monkeypatch):
    def unreadable(root: Path, sid: str, *, acknowledge_wake: bool = False):
        raise OSError("inbox unavailable")

    monkeypatch.setattr(peers, "take", unreadable)
    assert hooks.dispatch_inbox(tmp_root, {"session_id": "s-me", "prompt": ops.peers.WAKE_TEXT}) == ""


def test_a_subagents_turn_never_drains_its_managers_inbox(tmp_root: Path, run):
    """A subagent shares the session id it was spawned from. Draining there would hand the
    manager's messages to a subagent that cannot act on them, and delete them on the way."""
    import json as _json

    peers.send(tmp_root, to="s-mgr", text="for the manager", frm="s-you", frm_name="mgr-you")
    r = run(
        ["hook", "dispatch-inbox"],
        stdin=_json.dumps({"session_id": "s-mgr", "agent_id": "a1", "agent_type": "Explore"}),
    )
    assert r.out.strip() == ""
    assert [row["text"] for row in peers.peek(tmp_root, "s-mgr")] == ["for the manager"]


def test_send_requests_wake_for_both_working_and_idle_peers(tmp_root: Path, monkeypatch):
    """Carrying and requesting wake are separate jobs; provider delivery needs a live check.

    A peer worth messaging is usually idle: a session still holding a lease you need has
    stopped, not started. So a message to the one peer that mattered sat unread until the owner
    prompted that session, and two managers could each report waiting on the other while
    neither had read a word. The owner found it three times.
    """
    rung: list = []
    _session(tmp_root, "sid-me", "mgr-me")
    _session(tmp_root, "sid-busy", "mgr-busy")
    _session(tmp_root, "sid-idle", "mgr-idle")
    sessions.set_field(tmp_root, "sid-busy", "activity", {"state": "working"})
    sessions.set_field(tmp_root, "sid-idle", "activity", {"state": "idle"})
    address = str(ops.peers.MESSAGING_SOCKET_DIR / f"{ALIVE}.sock")
    sessions.set_field(tmp_root, "sid-busy", "inbox", address)
    sessions.set_field(tmp_root, "sid-idle", "inbox", address)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-me")
    monkeypatch.setattr("agent_cli.ops.peers._ring_socket", lambda path: rung.append(path))

    # A working peer can finish before its next boundary, so it must be rung too.
    busy = ops.peer_send(tmp_root, to="mgr-busy", message="hello")
    assert "wrote a wake frame" in busy.text
    assert rung == [address]

    idle = ops.peer_send(tmp_root, to="mgr-idle", message="hello")
    assert "wrote a wake frame" in idle.text
    assert rung == [address] * 2


def test_the_wake_frame_is_the_json_the_client_parses():
    """The bare line this used to write connected, wrote, and was discarded unparsed: Claude
    Code reads each socket line as a JSON frame and drops the rest with a debug-only
    `[uds-messaging] ... dropped: malformed frame`, before its inbound controls run. So the
    sender saw a clean write and the recipient saw nothing at all — no dialog, no preview, no
    refusal (2026-09-24). Verified live against a running session with exactly this shape.

    Asserting the parsed frame rather than the byte string: the shape is the contract, the
    spelling is not."""
    raw = ops.peers.wake_frame("read your inbox")
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1  # one frame per line, or the client drops the remainder
    assert json.loads(raw) == {
        "type": "user",
        "message": {"role": "user", "content": "read your inbox"},
    }


@pytest.mark.skipif(
    sys.platform == "win32", reason="opens a real AF_UNIX listener; Windows inboxes are named pipes"
)
def test_the_wake_frame_survives_a_round_trip_over_a_real_socket():
    """`_ring_socket` against a listener that records what arrived. Covers the two things the
    old code got wrong and nothing noticed: that a frame is actually written, and that it is
    one newline-terminated JSON object.

    Its own short temp directory, not `tmp_path`: an AF_UNIX path is capped near 104 bytes and
    pytest's per-test directory alone is longer than that."""
    import socket as socket_mod
    import tempfile
    import threading

    with tempfile.TemporaryDirectory(dir="/tmp") as short:  # noqa: S108 — AF_UNIX path length
        path = Path(short) / "inbox.sock"
        _round_trip(socket_mod, threading, path)


def _round_trip(socket_mod, threading, path: Path) -> None:
    got: list[bytes] = []

    server = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def accept() -> None:
        conn, _ = server.accept()
        with conn:
            got.append(conn.recv(4096))

    listener = threading.Thread(target=accept)
    listener.start()
    try:
        assert ops.peers._ring_socket(str(path)) is None
        listener.join(timeout=5)
    finally:
        server.close()

    assert json.loads(got[0])["message"]["content"] == ops.peers.WAKE_TEXT


def test_a_ring_at_a_path_nothing_is_listening_on_names_the_error(tmp_path: Path, monkeypatch):
    """A dead socket is the normal case for a session that has exited, and it must read as a
    failed ring rather than raise: the mailbox still holds the message. The error is named, so a
    sandbox denial (PermissionError) is not mistaken for a gone session."""
    monkeypatch.setattr(ops.peers, "inbox_token", lambda address: "token")
    address = ops.peers.PIPE_PREFIX + "LOCAL\\cc-msg-nobody-home" if sys.platform == "win32" else None
    error = ops.peers._ring_socket(address or str(tmp_path / "nobody-home.sock"))
    assert error and error.split(":")[0].endswith("Error")  # e.g. FileNotFoundError, or OSError


def test_each_client_is_rung_by_its_own_mechanism(tmp_root: Path, monkeypatch):
    """Claude uses a per-session inbox socket; Codex uses `codex queue --thread`."""
    ran: list = []
    _session(tmp_root, "sid-me", "mgr-me")
    _session(tmp_root, "sid-cdx", "mgr-cdx")
    sessions.set_field(tmp_root, "sid-cdx", "activity", {"state": "idle"})
    sessions.set_field(tmp_root, "sid-cdx", "provider", "codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-me")
    monkeypatch.setattr("agent_cli.ops.peers._run", lambda argv: ran.append(argv) or True)
    monkeypatch.setattr("agent_cli.codex_executable.command", lambda *args: ["resolved-codex", *args])

    op = ops.peer_send(tmp_root, to="mgr-cdx", message="hello")
    assert "codex queue accepted" in op.text
    assert ran == [["resolved-codex", "queue", "--thread", "sid-cdx", "--message", ops.peers.WAKE_TEXT]]


def test_five_peer_messages_queue_one_codex_wake_until_that_wake_runs(tmp_root: Path, monkeypatch):
    """An owner prompt may drain messages before Codex runs the queued wake."""
    ran: list[list[str]] = []
    _session(tmp_root, "sid-me", "mgr-me")
    _session(tmp_root, "sid-cdx", "mgr-cdx")
    sessions.set_field(tmp_root, "sid-cdx", "provider", "codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-me")
    monkeypatch.setattr("agent_cli.ops.peers._run", lambda argv: ran.append(argv) or True)
    monkeypatch.setattr("agent_cli.codex_executable.command", lambda *args: ["codex", *args])

    for number in range(5):
        ops.peer_send(tmp_root, to="mgr-cdx", message=f"message {number}")
    assert len(peers.peek(tmp_root, "sid-cdx")) == 5
    assert len(ran) == 1

    owner_prompt = hooks.dispatch_inbox(tmp_root, {"session_id": "sid-cdx", "prompt": "owner prompt"})
    assert "Messages from other sessions (5)" in owner_prompt
    assert peers.peek(tmp_root, "sid-cdx") == []

    ops.peer_send(tmp_root, to="mgr-cdx", message="after owner prompt")
    assert len(ran) == 1
    wake = hooks.dispatch_inbox(tmp_root, {"session_id": "sid-cdx", "prompt": ops.peers.WAKE_TEXT})
    assert "after owner prompt" in wake
    assert peers.peek(tmp_root, "sid-cdx") == []
    assert (sessions.get(tmp_root, "sid-cdx") or {})[peers.WAKE_FIELD] is None

    ops.peer_send(tmp_root, to="mgr-cdx", message="new batch")
    assert len(ran) == 2


def test_queued_wake_is_acknowledged_even_when_owner_already_read_all_messages(tmp_root: Path, monkeypatch):
    ran: list[list[str]] = []
    _session(tmp_root, "sid-me", "mgr-me")
    _session(tmp_root, "sid-cdx", "mgr-cdx")
    sessions.set_field(tmp_root, "sid-cdx", "provider", "codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-me")
    monkeypatch.setattr("agent_cli.ops.peers._run", lambda argv: ran.append(argv) or True)
    monkeypatch.setattr("agent_cli.codex_executable.command", lambda *args: ["codex", *args])

    ops.peer_send(tmp_root, to="mgr-cdx", message="first")
    assert "first" in hooks.dispatch_inbox(tmp_root, {"session_id": "sid-cdx", "prompt": "owner prompt"})
    assert json.loads(
        hooks.dispatch_inbox(tmp_root, {"session_id": "sid-cdx", "prompt": ops.peers.WAKE_TEXT})
    ) == {
        "decision": "block",
        "reason": "Peer wake already delivered.",
    }
    ops.peer_send(tmp_root, to="mgr-cdx", message="new batch")
    assert len(ran) == 2


def test_failed_peer_wake_retries_on_next_message(tmp_root: Path, monkeypatch):
    outcomes = iter((False, True))
    ran: list[list[str]] = []
    _session(tmp_root, "sid-me", "mgr-me")
    _session(tmp_root, "sid-cdx", "mgr-cdx")
    sessions.set_field(tmp_root, "sid-cdx", "provider", "codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-me")
    monkeypatch.setattr("agent_cli.ops.peers._run", lambda argv: ran.append(argv) or next(outcomes))
    monkeypatch.setattr("agent_cli.codex_executable.command", lambda *args: ["codex", *args])

    first = ops.peer_send(tmp_root, to="mgr-cdx", message="first")
    assert "could not reach" in first.text
    second = ops.peer_send(tmp_root, to="mgr-cdx", message="second")
    assert "accepted the wake request" in second.text
    assert len(ran) == 2
    assert [row["text"] for row in peers.peek(tmp_root, "sid-cdx")] == ["first", "second"]


@pytest.mark.parametrize(
    ("state", "age"),
    [
        ("attempting", ops.peers.WAKE_ATTEMPT_TTL_S + 1),
        ("accepted", ops.peers.WAKE_ACCEPTED_TTL_S + 1),
    ],
)
def test_stale_wake_reservation_retries_with_unread_messages(
    tmp_root: Path, monkeypatch, state: str, age: float
):
    ran: list[list[str]] = []
    _session(tmp_root, "sid-me", "mgr-me")
    _session(tmp_root, "sid-cdx", "mgr-cdx")
    sessions.set_field(tmp_root, "sid-cdx", "provider", "codex")
    sessions.set_field(
        tmp_root,
        "sid-cdx",
        peers.WAKE_FIELD,
        {"id": "old", "state": state, "at": now().timestamp() - age},
    )
    peers.send(tmp_root, to="sid-cdx", text="still unread", frm="sid-me")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-me")
    monkeypatch.setattr("agent_cli.ops.peers._run", lambda argv: ran.append(argv) or True)
    monkeypatch.setattr("agent_cli.codex_executable.command", lambda *args: ["codex", *args])

    ops.peer_send(tmp_root, to="mgr-cdx", message="new message")
    assert len(ran) == 1
    assert len(peers.peek(tmp_root, "sid-cdx")) == 2


def test_the_wake_text_points_at_the_context_not_at_an_emptied_mailbox():
    """Waking a peer starts a turn, and `UserPromptSubmit` drains the mailbox into that same
    turn. A wake that says "run bin/agent peer inbox" therefore sends the recipient to look in
    a box the hook already emptied: the first woken Codex manager did that, reported "no pending
    messages", and stopped without replying (2026-09-24). Both legs send the same text, so this
    covers both."""
    assert "in this turn's context" in ops.peers.WAKE_TEXT
    assert "the `peer` tool, action `send`" in ops.peers.WAKE_TEXT
    # The inbox survives as the fallback, and must read as one: tools only.
    assert ops.peers.WAKE_TEXT.endswith("If absent, call it with action `inbox`.")
    assert "bin/agent" not in ops.peers.WAKE_TEXT


def test_a_ring_that_fails_still_leaves_the_message_queued(tmp_root: Path, monkeypatch):
    """The mailbox is the transport and the ring is a courtesy. A peer that cannot be woken
    reads the message at its next turn, which is the old behaviour — worse, but not a failure,
    and never a reason to lose what was written."""
    _session(tmp_root, "sid-me", "mgr-me")
    _session(tmp_root, "sid-idle", "mgr-idle")
    sessions.set_field(tmp_root, "sid-idle", "activity", {"state": "idle"})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-me")
    monkeypatch.setattr("agent_cli.ops.peers._ring_socket", lambda path: "ConnectionRefusedError")

    op = ops.peer_send(tmp_root, to="mgr-idle", message="still queued")
    assert "next turn" in op.text
    assert [r["text"] for r in peers.peek(tmp_root, "sid-idle")] == ["still queued"]
