"""Peer messages reach a Claude manager without the owner typing.

On 2026-09-24 a Claude manager's wake reservation was accepted at 23:16 by a socket frame that
landed mid-turn. Only the queued wake prompt acknowledged a reservation, and that prompt never
ran, so every send for 30 minutes skipped the ring and seven messages sat until the owner typed.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli import hooks, peers, sessions


def _reserve(root: Path, sid: str, provider: str) -> None:
    sessions.set_record(root, sid, name=f"mgr-{sid}")
    sessions.set_field(root, sid, "provider", provider)
    sessions.set_field(root, sid, peers.WAKE_FIELD, {"id": "w1", "state": "accepted", "at": 1.0e12})


def _wake(root: Path, sid: str):
    return (sessions.get(root, sid) or {}).get(peers.WAKE_FIELD)


def test_a_claude_prompt_that_reads_the_inbox_clears_a_stale_reservation(tmp_root: Path):
    _reserve(tmp_root, "claude", "claude")
    peers.send(tmp_root, to="claude", text="hello", frm="codex", frm_name="mgr-codex")
    out, _ = hooks.run(
        tmp_root, "dispatch-inbox", json.dumps({"session_id": "claude", "prompt": "an owner message"})
    )
    assert "hello" in out
    assert _wake(tmp_root, "claude") is None


def test_a_codex_ordinary_prompt_keeps_its_reservation_for_the_queued_wake(tmp_root: Path):
    _reserve(tmp_root, "codex", "codex")
    peers.send(tmp_root, to="codex", text="hello", frm="claude", frm_name="mgr-claude")
    hooks.run(tmp_root, "dispatch-inbox", json.dumps({"session_id": "codex", "prompt": "an owner message"}))
    assert (_wake(tmp_root, "codex") or {}).get("state") == "accepted"


def test_claude_stop_hands_over_unread_messages_once(tmp_root: Path):
    _reserve(tmp_root, "claude", "claude")
    peers.send(tmp_root, to="claude", text="late message", frm="codex", frm_name="mgr-codex")
    out, code = hooks.run(tmp_root, "peer-stop", json.dumps({"session_id": "claude"}))
    decision = json.loads(out)
    assert code == 0 and decision["decision"] == "block" and "late message" in decision["reason"]
    assert _wake(tmp_root, "claude") is None
    assert peers.peek(tmp_root, "claude") == []
    peers.send(tmp_root, to="claude", text="another", frm="codex", frm_name="mgr-codex")
    out, _ = hooks.run(tmp_root, "peer-stop", json.dumps({"session_id": "claude", "stop_hook_active": True}))
    assert out == ""  # the continued turn may end; the next send rings


def test_claude_stop_is_silent_with_an_empty_inbox(tmp_root: Path):
    sessions.set_record(tmp_root, "claude", name="mgr-claude")
    out, _ = hooks.run(tmp_root, "peer-stop", json.dumps({"session_id": "claude"}))
    assert out == ""


def test_a_sandbox_denial_is_named_in_the_send_result(tmp_root: Path, monkeypatch):
    """Codex's sandbox refuses the connect to a Claude inbox socket that exists; the send result
    says so instead of reading as an unreachable session (2026-09-25 07:48:42)."""
    import os

    from agent_cli import ops

    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)
    sessions.set_record(tmp_root, "claude", name="mgr-claude", pid=os.getpid())
    sessions.set_field(tmp_root, "claude", "inbox", "/tmp/claude-inbox.sock")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "codex")
    sessions.set_record(tmp_root, "codex", name="mgr-codex", pid=os.getpid())
    monkeypatch.setattr(
        "agent_cli.ops.peers._ring_socket", lambda path: "PermissionError: [Errno 1] Operation not permitted"
    )
    op = ops.peer_send(tmp_root, to="mgr-claude", message="hi")
    assert "PermissionError: [Errno 1] Operation not permitted" in op.text


def test_peer_stop_is_wired_on_claude_stop_only():
    root = Path(__file__).resolve().parents[1]
    claude = json.loads((root / ".claude/settings.json").read_text())
    codex = json.loads((root / ".codex/hooks.json").read_text())
    from agent_cli.hooks.groups import wired

    assert "peer-stop" in wired(claude, "Stop")
    assert "peer-stop" not in wired(codex, "Stop")


SECRET = "zebra_quartz_peer_payload"


def test_no_written_event_or_log_holds_the_peer_message_text(tmp_root: Path):
    """The block reason hands the message to the model; the denial log keeps only a fixed line and
    a count (P2 found the log copying peer text). The denial logger needs a git checkout,
    so the root is one. The peer store keeps the message by design and is not a log."""
    from agent_cli.telemetry.recorder import main_of
    from agent_cli.telemetry.sink import Sink
    from harness_root import git

    git(["init"], tmp_root)
    _reserve(tmp_root, "claude", "claude")
    peers.send(tmp_root, to="claude", text=f"please check {SECRET}", frm="codex", frm_name="mgr-codex")
    peers.send(tmp_root, to="claude", text="and a second one", frm="codex", frm_name="mgr-codex")
    out, _ = hooks.run(tmp_root, "peer-stop", json.dumps({"session_id": "claude"}))
    assert SECRET in json.loads(out)["reason"]
    events = [
        json.loads(line)
        for path in Sink.for_main(main_of(tmp_root)).events.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    denial = next(e for e in events if e["kind"] == "hook.denial" and e["data"]["hook"] == "peer-stop")
    assert denial["data"]["reason"] == "peer-stop: 2 notices delivered"
    assert denial["data"]["class"] == "continuation"
    store = tmp_root / peers.PEERS_DIR
    written = [
        p
        for p in tmp_root.rglob("*")
        if p.is_file() and ".git" not in p.parts and not p.is_relative_to(store)
    ]
    assert written
    leaks = [p for p in written if SECRET in p.read_text(errors="replace")]
    assert leaks == []


def test_the_logged_count_covers_notices_and_messages_once():
    from agent_cli.hooks import notice_inbox
    from agent_cli.hooks.peer_stop import logged_reason

    reason = (
        f"## {notice_inbox.HEADING} (2)\n\n- a\n- b\n\n## Messages from other sessions (1)\n\n"
        "**x** at t:\n## Messages from other sessions (99)\n"
    )
    assert logged_reason(reason) == "peer-stop: 3 notices delivered"
