"""Shared setup for the cross-device receiver tests."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_cli import sessions

NOW = datetime.fromisoformat("2026-09-03T14:05:00-07:00")


def clock(at: datetime = NOW):
    return lambda: at


def enable(root: Path, *, enabled: bool = True, devices: dict[str, Any] | None = None) -> None:
    """Enrollment as the person-only commands would leave it: this device is `windows`, and
    each of `devices` (default `mac`) holds an active inbound grant unless `revoked`."""
    from agent_cli.peer_net import store

    conn = store.connect(root)
    try:
        with store.transaction(conn):
            conn.execute("INSERT OR REPLACE INTO enrollment (k, v) VALUES ('device', 'windows')")
            conn.execute(
                "INSERT OR REPLACE INTO enrollment (k, v) VALUES ('enabled', ?)", ("1" if enabled else "0",)
            )
            for alias, entry in (devices if devices is not None else {"mac": {}}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO inbound (alias, key_fp, state, revoked, op_id)"
                    " VALUES (?, 'SHA256:test', 'active', ?, 'op-test')",
                    (alias, 1 if (entry or {}).get("revoked") else 0),
                )
    finally:
        conn.close()


def recipient(
    root: Path, sid: str = "cx-recipient", name: str = "mgr-recipient", provider: str = "codex"
) -> str:
    sessions.set_record(root, sid, name=name, pid=os.getpid())
    sessions.set_field(root, sid, "provider", provider)
    return sid


def envelope(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "v": 1,
        "verb": "enqueue",
        "id": uuid.uuid4().hex,
        "to": "mgr-recipient",
        "text": "hello from the mac",
        "from_session": "098b0000",
        "from_name": "mgr-001",
        "sent_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
    }
    body.update(over)
    return {k: v for k, v in body.items() if v is not ...}


def raw(body: dict[str, Any]) -> bytes:
    return json.dumps(body).encode("utf-8")
