"""`agent land` queues for the land lock and can run detached.

On 2026-10-06 a second landing was refused at once ("another the `harness` tool with action
`land` or recovery is running", the message mangled by the MCP wording substitution), so the
manager watched main's SHA and retried; and one MCP land call timed out at 300 s while the
landing went on."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agent_cli import land_journal as jn
from agent_cli import ops
from agent_cli.core import AgentError, is_alive


def _hold(main: Path, seconds: float, held: threading.Event) -> None:
    with jn.flock(main):
        held.set()
        time.sleep(seconds)


def test_a_second_landing_queues_for_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(jn, "LOCK_POLL_S", 0.02)
    held = threading.Event()
    holder = threading.Thread(target=_hold, args=(tmp_path, 0.3, held))
    holder.start()
    held.wait(5)
    started = time.monotonic()
    with jn.flock(tmp_path, wait_s=10):
        waited = time.monotonic() - started
    holder.join()
    assert waited >= 0.2


def test_without_a_wait_a_held_lock_refuses_in_plain_words(tmp_path: Path):
    held = threading.Event()
    holder = threading.Thread(target=_hold, args=(tmp_path, 0.5, held))
    holder.start()
    held.wait(5)
    with pytest.raises(AgentError, match="^another landing or recovery holds the land lock$"):
        with jn.flock(tmp_path):
            pass
    holder.join()


def _repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    return path


def test_status_lists_recent_landings(tmp_path: Path):
    _repo(tmp_path)
    journal = {"id": jn.new_id(), "state": "done", "branch": "agent/x", "ts": jn.now()}
    jn.write(tmp_path, journal)
    op = ops.land(tmp_path, status=True)
    assert op.data["landings"][0]["id"] == journal["id"]
    assert "agent/x" in op.text


def test_a_detached_landing_is_visible_from_the_start_and_after_it_dies(tmp_path: Path):
    """Codex's phase-1 review: the detached response had no operation id, and status showed
    nothing while queued, while verifying, or after a failure before any journal existed."""
    _repo(tmp_path)
    op = ops.land(tmp_path, branch="no-such-branch", detach=True, wait_s=0)
    assert op.data["detached"] is True and op.data["op"] and op.data["pid"] > 0
    first = ops.land(tmp_path, status=True).data["operations"]
    assert first and first[0]["id"] == op.data["op"] and first[0]["branch"] == "no-such-branch"
    if os.name == "nt":  # no zombies to reap: wait for the pid to go
        deadline = time.monotonic() + 60
        while is_alive(op.data["pid"]) and time.monotonic() < deadline:
            time.sleep(0.1)
    else:
        os.waitpid(op.data["pid"], 0)  # our own child: reap it so its pid is gone
    [settled] = ops.land(tmp_path, status=True).data["operations"]
    assert settled["state"] in ("stopped", "failed") and settled["why"]


def test_a_detached_landing_rings_the_session_that_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The asking manager got no wake when its detached landing ended, so a Codex manager
    held its turn polling `land status`. The outcome is now rung to it, success or failure."""
    from agent_cli import land as land_mod
    from agent_cli.ops import peer_delivery

    sent: list[dict[str, str]] = []
    monkeypatch.setattr(peer_delivery, "deliver", lambda root, **kw: sent.append(kw))
    monkeypatch.setattr(land_mod, "main_of", lambda root: tmp_path)
    monkeypatch.setenv("AGENT_LAND_SESSION", "asker-sid")

    def refuse(*_a: object, **_k: object) -> None:
        raise AgentError("did not pass")

    monkeypatch.setattr(land_mod, "land", refuse)
    with pytest.raises(AgentError):
        ops.land(tmp_path, branch="agent/x", op="op9")
    [failed] = sent
    assert failed["target"] == "asker-sid" and failed["frm"] == "agent-land"
    assert "op9" in failed["message"] and "failed" in failed["message"]

    sent.clear()
    journal = {
        "id": "j1",
        "cand": "c" * 40,
        "old": "b" * 40,
        "strategy": "ff",
        "record": "r1",
        "record_reused": False,
        "members": [{"branch": "agent/x"}],
    }
    monkeypatch.setattr(land_mod, "land", lambda *_a, **_k: journal)
    ops.land(tmp_path, branch="agent/x", op="op10")
    [done] = sent
    assert done["target"] == "asker-sid" and "landed agent/x" in done["message"]

    sent.clear()
    ops.land(tmp_path, branch="agent/x")  # an attached landing answers its caller directly
    assert sent == []


def test_a_late_note_never_moves_a_finished_operation_back(tmp_path: Path):
    """Codex's round-2 review: the parent's pid note reverted the child's `failed` to `queued`."""
    jn.op_update(tmp_path, "op1", state="queued", branch="agent/x")
    jn.op_update(tmp_path, "op1", state="failed", why="did not pass")
    jn.op_update(tmp_path, "op1", state="queued", pid=4242)
    [op] = jn.ops_recent(tmp_path)
    assert op["state"] == "failed" and op["why"] == "did not pass" and op["pid"] == 4242


def test_every_returns_journals_not_operation_files(tmp_path: Path):
    jn.write(tmp_path, {"id": "j1", "state": "done", "ts": jn.now()})
    jn.op_update(tmp_path, "o1", state="done", journal="j1")
    assert [j["id"] for j in jn.every(tmp_path)] == ["j1"]
    assert jn.pending(tmp_path) == []
    assert [o["id"] for o in jn.ops_recent(tmp_path)] == ["o1"]


def test_concurrent_notes_all_land(tmp_path: Path):
    jn.op_update(tmp_path, "op2", state="queued")
    threads = [
        threading.Thread(target=jn.op_update, args=(tmp_path, "op2"), kwargs={f"k{i}": i}) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    [op] = jn.ops_recent(tmp_path)
    assert all(op.get(f"k{i}") == i for i in range(8))
