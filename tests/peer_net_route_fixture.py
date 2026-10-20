"""Two paired devices with a fake SSH transport into the real receiver.

`pair_both` pairs `win` (A) and `mac` (B) through the real pairing protocol. `Wire` then
replaces `ssh.run_bounded`: a call meant for alias X runs the real `receive()` on X's root, as
the device named by the key in the argv. Faults are queued per call: "drop" delivers but loses
the answer, "lost" never delivers, "garbage" answers with junk, "hang" waits out the timeout.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_cli.core import now
from agent_cli.ops import peers as ops_peers
from agent_cli.peer_net import receive, ssh
from peer_net_helpers import recipient
from peer_net_pair_fixture import Devices, make_devices


class Wire:
    def __init__(self, roots: dict[str, Path]) -> None:
        self.roots = roots
        self.faults: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.receiver_skew_s = 0.0  # the receiving device's clock, relative to the sender's

    def run(self, command: list[str], stdin: bytes, *, timeout: float, limit: int):
        key = command[command.index("-i") + 1]
        sender = Path(key).name.removeprefix("agent_peer_")
        alias = next(c.split("=", 1)[1] for c in command if c.startswith("HostKeyAlias=")).removeprefix(
            "agent-peer-"
        )
        fault = self.faults.pop(0) if self.faults else "ok"
        self.calls.append({"to": alias, "from": sender, "fault": fault, "verb": json.loads(stdin)["verb"]})
        if fault == "hang":
            time.sleep(timeout)
            return None, b"", False
        if fault == "lost":
            return None, b"", False
        skew = timedelta(seconds=self.receiver_skew_s)
        answer = receive(self.roots[alias], sender, stdin, clock=lambda: now() + skew)
        if fault == "drop":
            return None, b"", False
        if fault == "garbage":
            return 0, b"{}\n", False
        line = (json.dumps(answer) + "\n").encode()
        if len(line) > limit:
            return None, line[:limit], True
        return (0 if answer.get("ok") else 1), line, False


@pytest.fixture
def paired(tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    devices: Devices = make_devices(tmp_root, tmp_path, monkeypatch)
    devices.pair()
    monkeypatch.setattr(ops_peers, "wake_peer", lambda target, name, codex_launch=None: (True, "rang"))
    wire = Wire({"win": devices.a, "mac": devices.b})
    monkeypatch.setattr(ssh, "run_bounded", wire.run)
    a_sid = recipient(devices.a, sid="cl-sender-a", name="mgr-a", provider="claude")
    b_sid = recipient(devices.b, sid="cx-recipient-b", name="mgr-b")
    return devices, wire, a_sid, b_sid
