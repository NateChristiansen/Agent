"""`peer list` across paired devices: merged, allowlisted, bounded, and never at the local
list's expense."""

from __future__ import annotations

import json
import threading
import time

from agent_cli import ops
from agent_cli.peer_net import receive, sender, ssh, store
from peer_net_route_fixture import paired  # noqa: F401 -- the fixture

FORBIDDEN = {"session_id", "pid", "inbox", "socket", "transcript", "path", "cwd"}


def _remote(items):
    return [i for i in items if i.get("kind") == "remote"]


def test_remote_sessions_are_listed_with_the_device_prefix_and_allowlist_only(paired):
    devices, wire, *_ = paired
    op = ops.peer_list(devices.a)
    (row,) = _remote(op.data)
    assert row["name"] == "mac/mgr-b" and row["provider"] == "codex" and row["kind"] == "remote"
    assert not FORBIDDEN & set(row)  # task 8: no session id, pid, socket path or transcript
    assert "mac/mgr-b" in op.text and "paired devices (mac)" in op.text
    assert "mgr-a" in [i.get("name") for i in op.data]  # the local list is unchanged


def test_extra_fields_long_strings_and_control_characters_are_dropped(paired, monkeypatch):
    devices, wire, *_ = paired
    rows = [
        {"name": "ok", "tab": "cx 1", "provider": "codex", "feature": 17, "state": "idle"},
        {"name": "x", "tab": "t", "provider": "p", "feature": 1, "state": "s", "pid": 4},
        {"name": "y" * 129, "tab": "t", "provider": "p", "feature": 1, "state": "s"},
        {"name": "bad\x07", "tab": "t", "provider": "p", "feature": 1, "state": "s"},
        {"name": "z", "tab": "t", "provider": "p", "feature": "17", "state": "s"},
    ]
    answer = {"v": 1, "ok": True, "device": "mac", "sessions": rows}
    monkeypatch.setattr(ssh, "run_bounded", lambda *a, **k: (0, (json.dumps(answer) + "\n").encode(), False))
    blocks, _ = sender.list_remote(devices.a)
    assert [r["name"] for r in blocks[0]["rows"]] == ["ok"] and blocks[0]["dropped"] == 4


def test_rows_beyond_the_cap_are_dropped_and_counted(paired, monkeypatch):
    devices, *_ = paired
    row = {"name": "n", "tab": "t", "provider": "p", "feature": None, "state": "s"}
    answer = {"v": 1, "ok": True, "device": "mac", "sessions": [row] * 250}
    monkeypatch.setattr(ssh, "run_bounded", lambda *a, **k: (0, (json.dumps(answer) + "\n").encode(), False))
    (block,), _ = sender.list_remote(devices.a)
    assert len(block["rows"]) == 200 and block["dropped"] == 50


def test_an_answer_from_the_wrong_device_or_oversized_is_unknown(paired, monkeypatch):
    devices, *_ = paired
    wrong = {"v": 1, "ok": True, "device": "linux", "sessions": []}
    monkeypatch.setattr(ssh, "run_bounded", lambda *a, **k: (0, (json.dumps(wrong) + "\n").encode(), False))
    assert sender.list_remote(devices.a)[0][0]["reason"] == "no-answer"
    monkeypatch.setattr(ssh, "run_bounded", lambda *a, **k: (None, b"x" * 10, True))
    assert sender.list_remote(devices.a)[0][0]["reason"] == "oversized"


def test_an_offline_device_is_one_unknown_row_and_the_local_list_stays(paired):
    devices, wire, *_ = paired
    wire.faults = ["lost"]
    op = ops.peer_list(devices.a)
    (row,) = _remote(op.data)
    assert row["name"] == "mac/*" and row["state"] == "unknown" and row["reason"] == "no-answer"
    assert "mgr-a" in [i.get("name") for i in op.data]


def test_a_disabled_device_shows_its_reason(paired):
    devices, *_ = paired
    from agent_cli.peer_net import enroll
    from peer_net_pair_fixture import as_b

    as_b(lambda: enroll.set_enabled(devices.b, False))
    (row,) = _remote(ops.peer_list(devices.a).data)
    assert row["reason"] == "disabled"


def test_the_list_deadline_holds_and_queued_devices_never_start(paired, monkeypatch):
    devices, *_ = paired
    conn = store.connect(devices.a)
    try:
        with store.transaction(conn):
            for alias in ("d1", "d2", "d3"):
                conn.execute(
                    "INSERT INTO outbound (alias, user, host, port, host_key_fp, verified, op_id) VALUES (?, 'u', '192.0.2.9', 22, 'fp', 1, 'x')",
                    (alias,),
                )
    finally:
        conn.close()
    started_calls: list[str] = []
    lock = threading.Lock()
    release = threading.Event()

    def blocked(command, stdin, *, timeout, limit):
        with lock:
            started_calls.append(command[-1])
        release.wait(10)  # held until the test has seen list_remote return
        return None, b"", False

    monkeypatch.setattr(ssh, "run_bounded", blocked)
    monkeypatch.setattr(sender, "LIST_WORKERS", 1)
    began = time.monotonic()
    blocks, _ = sender.list_remote(devices.a, budget_s=1.5)  # above the 1 s minimum per call
    assert time.monotonic() - began < 2.5
    assert len(blocks) == 4 and all(b["reason"] == "timeout" for b in blocks)
    release.set()
    time.sleep(0.5)  # the one running worker finishes; a queued one would start now
    assert len(started_calls) == 1  # the queued devices were cancelled, never started


def test_a_list_worker_runs_its_permission_checks_under_the_shared_deadline(paired, monkeypatch):
    devices, *_ = paired
    from agent_cli.peer_net import system

    seen: list[float] = []
    real_argv = ssh.argv

    def argv(*args, **kwargs):
        seen.append(system._budget(30.0))  # what a permission check in the worker would get
        return real_argv(*args, **kwargs)

    monkeypatch.setattr(ssh, "argv", argv)
    sender.list_remote(devices.a, budget_s=2.0)
    assert seen and all(budget <= 2.0 for budget in seen)


def test_the_local_list_survives_an_absent_or_corrupt_peer_net_store(paired):
    devices, *_ = paired
    (devices.a / store.STORE_REL).write_bytes(b"this is not sqlite")
    op = ops.peer_list(devices.a)
    assert "mgr-a" in [i.get("name") for i in op.data]
    assert "remote devices unavailable" in op.text


def test_the_receivers_list_never_asks_other_devices(paired, monkeypatch):
    devices, *_ = paired

    def loop(*a, **k):
        raise AssertionError("the receiver queried a remote device")

    monkeypatch.setattr(sender, "list_remote", loop)
    import uuid
    from datetime import timedelta

    from agent_cli.core import now

    at = now()
    body = {
        "v": 1,
        "verb": "list",
        "id": uuid.uuid4().hex,
        "sent_at": at.isoformat(),
        "expires_at": (at + timedelta(seconds=60)).isoformat(),
    }
    answer = receive(devices.b, "win", json.dumps(body).encode())
    assert answer["ok"] is True and [s["name"] for s in answer["sessions"]] == ["mgr-b"]
