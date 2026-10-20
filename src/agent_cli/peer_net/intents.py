"""Operation intents for enrollment, kept in the SQLite store.

Every mutating enrollment command writes its intent, with what it plans to change, before it
changes anything, and marks it done last. An intent still open means the command died midway;
`peer-net recover` (person-only) resolves it. Rows are transactional, so there is no torn
record to skip.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from . import store
from .errors import PeerNetError

OPEN = "open"


@dataclass(frozen=True)
class Intent:
    op_id: str
    op: str
    alias: str
    payload: dict[str, Any]
    state: str


def begin(conn: sqlite3.Connection, op: str, alias: str, payload: dict[str, Any]) -> str:
    with store.transaction(conn):
        return begin_in(conn, op, alias, payload)


def begin_in(conn: sqlite3.Connection, op: str, alias: str, payload: dict[str, Any]) -> str:
    """`begin` inside the caller's open transaction."""
    op_id = uuid.uuid4().hex
    now = time.time()
    conn.execute(
        "INSERT INTO intents (op_id, op, alias, payload, state, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (op_id, op, alias, json.dumps(payload, sort_keys=True), OPEN, now, now),
    )
    return op_id


def update(conn: sqlite3.Connection, op_id: str, *, state: str | None = None, **payload: Any) -> None:
    """Merge `payload` into the intent and optionally move its state; inside the caller's
    transaction when one is open."""
    row = conn.execute("SELECT payload, state FROM intents WHERE op_id=?", (op_id,)).fetchone()
    if row is None:
        raise PeerNetError("intent-missing", "op")
    merged = {**json.loads(row[0]), **payload}
    conn.execute(
        "UPDATE intents SET payload=?, state=?, updated_at=? WHERE op_id=?",
        (json.dumps(merged, sort_keys=True), state or row[1], time.time(), op_id),
    )


def open_for(conn: sqlite3.Connection, alias: str | None = None) -> list[Intent]:
    query = "SELECT op_id, op, alias, payload, state FROM intents WHERE state=?"
    args: tuple[str, ...] = (OPEN,)
    if alias is not None:
        query += " AND alias=?"
        args = (OPEN, alias)
    rows = conn.execute(query + " ORDER BY created_at", args).fetchall()
    return [Intent(r[0], r[1], r[2], json.loads(r[3]), r[4]) for r in rows]


def get(conn: sqlite3.Connection, op_id: str) -> Intent | None:
    row = conn.execute(
        "SELECT op_id, op, alias, payload, state FROM intents WHERE op_id=?", (op_id,)
    ).fetchone()
    return Intent(row[0], row[1], row[2], json.loads(row[3]), row[4]) if row else None


def later(conn: sqlite3.Connection, op_id: str, op: str, alias: str) -> bool:
    """Whether an `op` intent for `alias` was journaled after `op_id`. Rowid order is journal
    order: intents are never deleted."""
    row = conn.execute(
        "SELECT 1 FROM intents WHERE op=? AND alias=?"
        " AND rowid > (SELECT rowid FROM intents WHERE op_id=?) LIMIT 1",
        (op, alias, op_id),
    ).fetchone()
    return row is not None


def refuse_open(conn: sqlite3.Connection, alias: str | None = None) -> None:
    """Block a granting command while an earlier one for the same alias is unresolved."""
    if open_for(conn, alias):
        raise PeerNetError("intent-open", "run `peer-net recover`")
