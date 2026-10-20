"""The receiver's durable state: one SQLite file per device.

SQLite rather than JSONL ledgers because acceptance, queueing and the wake claim each need a
read-decide-write that survives a crash or a concurrent retry, and a parser that silently
drops a torn line cannot keep a dedupe promise. WAL with `synchronous=FULL` makes a committed
row durable. Every transaction is short, and none is held while acquiring `inbox_guard` or
ringing a recipient.

States of an `accepted` row: accepted, queued, wake_requested, wake_accepted, wake_failed,
wake_coalesced.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..core import agent_path
from .errors import PeerNetError

STORE_REL = agent_path(Path(), "peer-net", "state.db")
RATE_LIMIT = 30
RATE_WINDOW_S = 600.0
RETAIN_S = 24 * 3600.0
CLAIM_LEASE_S = 30.0

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS accepted (
        device TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, target_sid TEXT NOT NULL,
        to_name TEXT NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL,
        wake_rid TEXT, claim_until REAL, updated_at REAL NOT NULL, PRIMARY KEY (device, id))""",
    "CREATE TABLE IF NOT EXISTS rate (device TEXT NOT NULL, at REAL NOT NULL)",
    # Enrollment: written only by person-only commands.
    "CREATE TABLE IF NOT EXISTS enrollment (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS inbound (
        alias TEXT PRIMARY KEY, key_fp TEXT NOT NULL, state TEXT NOT NULL,
        revoked INTEGER NOT NULL, op_id TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS outbound (
        alias TEXT PRIMARY KEY, user TEXT NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL,
        host_key_fp TEXT NOT NULL, verified INTEGER NOT NULL, op_id TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS intents (
        op_id TEXT PRIMARY KEY, op TEXT NOT NULL, alias TEXT NOT NULL, payload TEXT NOT NULL,
        state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    # Sending side: each envelope is journaled before its first attempt.
    """CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY, alias TEXT NOT NULL, route TEXT NOT NULL, envelope BLOB NOT NULL,
        expires_at REAL NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL,
        claim_rid TEXT, claim_until REAL, outcome TEXT, created_at REAL NOT NULL,
        updated_at REAL NOT NULL)""",
)


def prepare_private(directory: Path, file: Path | None = None) -> None:
    """Make `directory` (and an empty `file` in it) user-only before anything is written.

    The store holds enrollment and the backups hold the owner's authorized_keys, so neither may
    be readable by others even for a moment. A new directory is created 0700 and a new file
    0600 regardless of umask; on Windows both get a user-only ACL, inherited by later files.
    SQLite creates its -wal and -shm files with the database file's own permissions.
    """
    import os

    from . import system

    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=0o700)
        system.restrict_to_user(directory)
    if file is not None and not file.exists():
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        system.restrict_to_user(file)


def check_private(root: Path) -> None:
    """Refuse a store directory or file that others can reach. Enrollment calls this before it
    writes; the receiver does not, since it only reads what enrollment protected."""
    from . import system

    path = root / STORE_REL
    for candidate in (
        path.parent,
        path,
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
    ):
        if candidate.exists() and not system.is_private(candidate):
            raise PeerNetError("unsafe-permissions", candidate.name)


def exists(root: Path) -> bool:
    """Whether a store was ever created. The receiver checks this first, so a device that never
    enrolled stays disabled without a store being made for it."""
    return (root / STORE_REL).is_file()


@dataclass(frozen=True)
class Row:
    device: str
    id: str
    digest: str
    target_sid: str
    to_name: str
    expires_at: float
    state: str
    wake_rid: str | None
    claim_until: float | None


def connect(root: Path, *, timeout: float | None = None) -> sqlite3.Connection:
    """Open the store, setting WAL and the schema under a harness guard.

    Without the guard, concurrent first connects fail: switching `journal_mode` returns
    "database is locked" without consulting the busy timeout. Measured 2026-10-03 with four
    processes on fresh files: 73 failures in 160 connects. Ordinary transactions honour the
    timeout, so only this setup needs serializing.

    `timeout` is one deadline for the whole open, for a caller with its own budget (a
    cross-device send): the guard wait and every setup statement's lock wait share it, each
    given only what is left. Without it the guard waits 30 s and SQLite 10 s per statement.
    A failed open closes its connection.
    """
    import time

    from ..core import AgentError
    from ..dispatch.guards import area_guard

    deadline = None if timeout is None else time.monotonic() + timeout

    def left() -> float:
        if deadline is None:
            return 10.0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PeerNetError("timeout", "store")
        return remaining

    conn = None
    try:
        path = root / STORE_REL
        with area_guard(root, "peer-net:store-init", timeout=30.0 if deadline is None else left()):
            prepare_private(path.parent, path)
            conn = sqlite3.connect(path, timeout=min(10.0, left()), isolation_level=None)
            for statement in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL", *SCHEMA):
                conn.execute(f"PRAGMA busy_timeout={int(min(10.0, left()) * 1000)}")
                conn.execute(statement)
    except AgentError as exc:  # the guard stayed busy past the caller's deadline
        _close_quietly(conn)
        raise PeerNetError("timeout", "store") from exc
    except PeerNetError:
        _close_quietly(conn)
        raise
    except (OSError, sqlite3.Error) as exc:
        _close_quietly(conn)
        if deadline is not None and time.monotonic() >= deadline:
            raise PeerNetError("timeout", "store") from exc
        raise PeerNetError("store-unavailable", "store") from exc
    return conn


def _close_quietly(conn: sqlite3.Connection | None) -> None:
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """One `BEGIN IMMEDIATE` transaction; any SQLite failure becomes `store-unavailable`."""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise PeerNetError("store-unavailable", "store") from exc
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    try:
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        raise PeerNetError("store-unavailable", "store") from exc


COLUMNS = "device, id, digest, target_sid, to_name, expires_at, state, wake_rid, claim_until"


def lookup(conn: sqlite3.Connection, device: str, msg_id: str) -> Row | None:
    try:
        query = f"SELECT {COLUMNS} FROM accepted WHERE device=? AND id=?"  # noqa: S608 — COLUMNS is a constant
        values = conn.execute(query, (device, msg_id)).fetchone()
        return Row(*values) if values else None
    except sqlite3.Error as exc:
        raise PeerNetError("store-unavailable", "store") from exc


def insert(
    conn: sqlite3.Connection,
    *,
    device: str,
    msg_id: str,
    digest: str,
    target_sid: str,
    to_name: str,
    expires_at: float,
    now: float,
) -> Row:
    """Inside `transaction`: prune, apply the rate limit, and insert a new `accepted` row. The
    caller has already found no row for this id in the same transaction."""
    try:
        conn.execute("DELETE FROM accepted WHERE expires_at < ?", (now - RETAIN_S,))
        conn.execute("DELETE FROM rate WHERE at < ?", (now - RATE_WINDOW_S,))
        (count,) = conn.execute("SELECT COUNT(*) FROM rate WHERE device=?", (device,)).fetchone()
        if count >= RATE_LIMIT:
            raise PeerNetError("rate-limited", "device")
        conn.execute("INSERT INTO rate (device, at) VALUES (?, ?)", (device, now))
        conn.execute(
            "INSERT INTO accepted (device, id, digest, target_sid, to_name, expires_at, state, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?)",
            (device, msg_id, digest, target_sid, to_name, expires_at, now),
        )
    except sqlite3.Error as exc:
        raise PeerNetError("store-unavailable", "store") from exc
    return Row(device, msg_id, digest, target_sid, to_name, expires_at, "accepted", None, None)


def mark_queued(conn: sqlite3.Connection, device: str, msg_id: str, now: float) -> None:
    try:
        conn.execute(
            "UPDATE accepted SET state='queued', updated_at=? WHERE device=? AND id=? AND state='accepted'",
            (now, device, msg_id),
        )
    except sqlite3.Error as exc:
        raise PeerNetError("store-unavailable", "store") from exc


def claim_wake(conn: sqlite3.Connection, device: str, msg_id: str, rid: str, now: float) -> bool:
    """The conditional claim: from queued, wake_failed or wake_coalesced, or from a
    wake_requested whose lease lapsed. True when this caller holds the claim.

    `wake_coalesced` is claimable because the reservation it reused may since have failed or
    expired; the claimant asks the reservation again and rings only if it is no longer current."""
    try:
        cur = conn.execute(
            "UPDATE accepted SET state='wake_requested', wake_rid=?, claim_until=?, updated_at=?"
            " WHERE device=? AND id=? AND (state IN ('queued', 'wake_failed', 'wake_coalesced')"
            " OR (state='wake_requested' AND claim_until < ?))",
            (rid, now + CLAIM_LEASE_S, now, device, msg_id, now),
        )
    except sqlite3.Error as exc:
        raise PeerNetError("store-unavailable", "store") from exc
    return cur.rowcount == 1


def settle_wake(conn: sqlite3.Connection, device: str, msg_id: str, rid: str, state: str, now: float) -> bool:
    """Compare-and-set on `wake_rid`: a late completion never overwrites a newer claim."""
    try:
        cur = conn.execute(
            "UPDATE accepted SET state=?, claim_until=NULL, updated_at=?"
            " WHERE device=? AND id=? AND wake_rid=? AND state='wake_requested'",
            (state, now, device, msg_id, rid),
        )
    except sqlite3.Error as exc:
        raise PeerNetError("store-unavailable", "store") from exc
    return cur.rowcount == 1
