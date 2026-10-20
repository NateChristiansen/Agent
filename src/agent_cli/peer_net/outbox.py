"""The sending side's journal: every envelope is recorded before its first attempt.

Each change is one short `BEGIN IMMEDIATE` transaction, never held across SSH. An attempt
claims its row with a fresh `claim_rid` and a lease. Its result settles only by compare-and-set
on that rid, so a stale attempt whose lease lapsed never overwrites a newer one, and a closed
row is never reopened.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from . import store

OPEN = ("pending", "unconfirmed")
RETAIN_S = 24 * 3600.0


@dataclass(frozen=True)
class Item:
    id: str
    alias: str
    route: dict[str, Any]
    envelope: bytes
    expires_at: float
    state: str
    attempts: int


def busy(conn: sqlite3.Connection, seconds: float) -> None:
    """Cap SQLite's lock wait at the caller's remaining budget."""
    conn.execute(f"PRAGMA busy_timeout={max(0, int(seconds * 1000))}")


def journal(
    conn: sqlite3.Connection,
    *,
    msg_id: str,
    alias: str,
    route: dict[str, Any],
    envelope: bytes,
    expires_at: float,
    now: float,
) -> None:
    with store.transaction(conn):
        conn.execute(
            "INSERT INTO outbox (id, alias, route, envelope, expires_at, state, attempts, claim_rid,"
            " claim_until, outcome, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, NULL,"
            " NULL, ?, ?)",
            (msg_id, alias, json.dumps(route, sort_keys=True), envelope, expires_at, now, now),
        )


def claim(conn: sqlite3.Connection, msg_id: str, *, lease_s: float, now: float) -> str | None:
    """This process's rid for one attempt, or None when the row is closed or another attempt
    holds a live lease. A row whose lease lapsed (its process died) is claimable again."""
    rid = uuid.uuid4().hex
    with store.transaction(conn):
        cur = conn.execute(
            "UPDATE outbox SET claim_rid=?, claim_until=?, attempts=attempts+1, updated_at=?"
            " WHERE id=? AND state IN ('pending', 'unconfirmed')"
            " AND (claim_until IS NULL OR claim_until < ?)",
            (rid, now + lease_s, now, msg_id, now),
        )
    return rid if cur.rowcount == 1 else None


def settle(conn: sqlite3.Connection, msg_id: str, rid: str, state: str, outcome: str, *, now: float) -> bool:
    """Record an attempt's result, only if this attempt still holds the claim and the row is
    open. `unconfirmed` keeps the row open; every other state closes it."""
    with store.transaction(conn):
        cur = conn.execute(
            "UPDATE outbox SET state=?, outcome=?, claim_until=NULL, updated_at=?"
            " WHERE id=? AND claim_rid=? AND state IN ('pending', 'unconfirmed')",
            (state, outcome, now, msg_id, rid),
        )
    return cur.rowcount == 1


def get(conn: sqlite3.Connection, msg_id: str) -> Item | None:
    with store.transaction(conn):
        row = conn.execute(
            "SELECT id, alias, route, envelope, expires_at, state, attempts FROM outbox WHERE id=?", (msg_id,)
        ).fetchone()
    return _item(row) if row else None


def open_for(conn: sqlite3.Connection, alias: str, *, exclude: str = "", limit: int = 3) -> list[Item]:
    with store.transaction(conn):
        rows = conn.execute(
            "SELECT id, alias, route, envelope, expires_at, state, attempts FROM outbox"
            " WHERE alias=? AND id<>? AND state IN ('pending', 'unconfirmed') ORDER BY created_at LIMIT ?",
            (alias, exclude, limit),
        ).fetchall()
    return [_item(r) for r in rows]


def open_count(conn: sqlite3.Connection, alias: str, *, exclude: str = "") -> int:
    """Open rows for `alias` other than `exclude`: the earlier messages still unconfirmed."""
    with store.transaction(conn):
        row = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE alias=? AND id<>? AND state IN ('pending', 'unconfirmed')",
            (alias, exclude),
        ).fetchone()
    return int(row[0])


def open_counts(conn: sqlite3.Connection) -> dict[str, int]:
    with store.transaction(conn):
        rows = conn.execute(
            "SELECT alias, COUNT(*) FROM outbox WHERE state IN ('pending', 'unconfirmed') GROUP BY alias"
        ).fetchall()
    return {str(a): int(n) for a, n in rows}


def prune(conn: sqlite3.Connection, *, now: float) -> None:
    with store.transaction(conn):
        conn.execute("DELETE FROM outbox WHERE expires_at < ?", (now - RETAIN_S,))


def _item(row: tuple[str, str, str, bytes, float, str, int]) -> Item:
    return Item(
        id=str(row[0]),
        alias=str(row[1]),
        route=json.loads(row[2]),
        envelope=bytes(row[3]),
        expires_at=float(row[4]),
        state=str(row[5]),
        attempts=int(row[6]),
    )
