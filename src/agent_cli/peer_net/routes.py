"""Outbound routes: a pinned host key plus a route row, verified only by this side's own probe.
Shared by `add` and the pairing protocol. The caller holds the enrollment guard
and an `open_store`ed store.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, NamedTuple

from ..core import now as core_now
from ..records.narrow import as_dict_or_none
from . import authorized, intents, mutate, ssh, store
from .errors import PeerNetError


class Route(NamedTuple):
    user: str
    host: str
    port: int
    host_key_fp: str
    op_id: str


def existing(conn: sqlite3.Connection, alias: str) -> Route | None:
    row = conn.execute(
        "SELECT user, host, port, host_key_fp, op_id FROM outbound WHERE alias=?", (alias,)
    ).fetchone()
    return Route(*row) if row is not None else None


def install(
    root: Path,
    conn: sqlite3.Connection,
    alias: str,
    user: str,
    host: str,
    port: int,
    fp: str,
    key_line: str,
    *,
    op_kind: str,
    parent: str | None = None,
) -> str:
    """Pin `key_line` for `alias` and record the route, unverified. `same` when the identical
    route and pin are already there (nothing written); a route or pin that differs is refused.
    The pin is written through a journaled intent, temp file then replace, never a link.
    `parent` names the pairing this route belongs to, so a re-run of it can roll the route
    back (`roll_back`)."""
    pin = ssh.pin_path(root, alias)
    authorized.refuse_links(pin)
    pin_bytes = ssh.pin_line(alias, key_line).encode("utf-8")
    route = existing(conn, alias)
    if route is not None or pin.exists():
        if route is None or tuple(route[:4]) != (user, host, port, fp):
            raise PeerNetError("route-exists", "revoke it first")
        if not pin.is_file() or pin.read_bytes() != pin_bytes:
            raise PeerNetError("pin-drift", "the pin differs from the confirmed key; revoke it first")
        return "same"
    payload: dict[str, Any] = {
        "pin_before": "absent",
        "host": host,
        "port": port,
        "pin_line": pin_bytes.decode(),
    }
    if parent is not None:
        payload["parent"] = parent
    op = intents.begin(conn, op_kind, alias, payload)
    store.prepare_private(pin.parent)
    tmp = _tmp(pin, alias, op)
    mutate.write_private(tmp, pin_bytes)
    if tmp.read_bytes() != pin_bytes:
        raise PeerNetError("pin-mismatch", "known_hosts")
    os.replace(tmp, pin)
    with store.transaction(conn):
        conn.execute(
            "INSERT INTO outbound (alias, user, host, port, host_key_fp, verified, op_id) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (alias, user, host, port, fp, op),
        )
        intents.update(conn, op, state="done")
    return "new"


def _tmp(pin: Path, alias: str, op: str) -> Path:
    return pin.with_name(f"{alias}.tmp-{op[:8]}")


def owned(path: Path, expected: bytes) -> str:
    """`absent`, `planned` (a plain file holding exactly `expected`), or `conflict`: anything
    else, a link or reparse point at the file or its directory included."""
    try:
        authorized.refuse_links(path)
    except PeerNetError:
        return "conflict"
    if not os.path.lexists(path):
        return "absent"
    if not path.is_file() or path.read_bytes() != expected:
        return "conflict"
    return "planned"


def pin_states(root: Path, intent: intents.Intent) -> tuple[str, str]:
    """(pin, temp file) states for an open route op, against its own journaled pin line."""
    expected = str(intent.payload.get("pin_line", "")).encode("utf-8")
    pin = ssh.pin_path(root, intent.alias)
    return owned(pin, expected), owned(_tmp(pin, intent.alias, intent.op_id), expected)


def roll_back(root: Path, conn: sqlite3.Connection, intent: intents.Intent, *, state: str) -> None:
    """Undo an open route op. Only the exact pin and temp file this op wrote are removed, and
    the pin only while no route row claims it; anything else is a conflict and is refused."""
    pin_state, tmp_state = pin_states(root, intent)
    if "conflict" in (pin_state, tmp_state):
        raise PeerNetError("pin-conflict", "the pin is not the one this op wrote; see `peer-net recover`")
    pin = ssh.pin_path(root, intent.alias)
    with store.transaction(conn):
        conn.execute("DELETE FROM outbound WHERE alias=? AND op_id=?", (intent.alias, intent.op_id))
        other = conn.execute("SELECT 1 FROM outbound WHERE alias=?", (intent.alias,)).fetchone()
    if tmp_state == "planned":
        _tmp(pin, intent.alias, intent.op_id).unlink()
    if pin_state == "planned" and other is None:
        pin.unlink()
    with store.transaction(conn):
        intents.update(conn, intent.op_id, state=state)


def set_verified(conn: sqlite3.Connection, alias: str, verified: bool) -> None:
    """A probe's outcome is the route's verified flag, either way."""
    with store.transaction(conn):
        conn.execute("UPDATE outbound SET verified=? WHERE alias=?", (1 if verified else 0, alias))


def verified_answer(answer: dict[str, Any], alias: str) -> bool:
    """A `status` answer proves the route only if it is exactly the receiver's own shape."""
    return (
        answer.get("ok") is True
        and type(answer.get("v")) is int
        and answer["v"] == 1
        and type(answer.get("proto")) is int
        and answer["proto"] == 1
        and answer.get("device") == alias
        and answer.get("enabled") is True
    )


def probe_status(
    root: Path, *, device: str, alias: str, user: str, host: str, port: int, timeout: float | None = None
) -> dict[str, Any]:
    """One `status` envelope over the real SSH hop: proves the receiver starts from an actual
    SSH logon on the other device. Exit 0 and exactly one line of output, or no answer.
    `timeout` bounds the whole ssh call (a pairing passes its remaining budget)."""
    at = core_now()
    body = {
        "v": 1,
        "verb": "status",
        "id": uuid.uuid4().hex,
        "sent_at": at.isoformat(),
        "expires_at": (at + timedelta(seconds=60)).isoformat(),
    }
    try:
        command = ssh.argv(root, local_alias=device, alias=alias, user=user, host=host, port=port)
        code, out, _ = ssh.run(
            command, json.dumps(body).encode("utf-8"), ssh.CALL_TIMEOUT_S if timeout is None else timeout
        )
        lines = [line for line in out.decode("utf-8").splitlines() if line.strip()]
        if code != 0 or len(lines) != 1:
            return {"ok": False, "error": "no-answer", "exit": code, "lines": len(lines)}
        answer = json.loads(lines[0])
    except PeerNetError, ValueError, UnicodeDecodeError:
        return {"ok": False, "error": "no-answer"}
    except OSError as exc:  # ssh could not be started at all
        return {"ok": False, "error": "ssh-failed", "detail": type(exc).__name__}
    answer = as_dict_or_none(answer)
    return answer if answer is not None else {"ok": False, "error": "no-answer"}


def probe(root: Path, conn: sqlite3.Connection, *, device: str, alias: str) -> tuple[bool, dict[str, Any]]:
    """Probe an installed route and record the outcome as its verified flag."""
    route = existing(conn, alias)
    if route is None:
        raise PeerNetError("no-route", alias)
    answer = probe_status(root, device=device, alias=alias, user=route[0], host=route[1], port=route[2])
    verified = verified_answer(answer, alias)
    set_verified(conn, alias, verified)
    return verified, answer
