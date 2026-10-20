# ruff: noqa
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
    "CREATE TABLE IF NOT EXISTS accepted (\n        device TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, target_sid TEXT NOT NULL,\n        to_name TEXT NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL,\n        wake_rid TEXT, claim_until REAL, updated_at REAL NOT NULL, PRIMARY KEY (device, id))",
    "CREATE TABLE IF NOT EXISTS rate (device TEXT NOT NULL, at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS enrollment (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS inbound (\n        alias TEXT PRIMARY KEY, key_fp TEXT NOT NULL, state TEXT NOT NULL,\n        revoked INTEGER NOT NULL, op_id TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS outbound (\n        alias TEXT PRIMARY KEY, user TEXT NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL,\n        host_key_fp TEXT NOT NULL, verified INTEGER NOT NULL, op_id TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS intents (\n        op_id TEXT PRIMARY KEY, op TEXT NOT NULL, alias TEXT NOT NULL, payload TEXT NOT NULL,\n        state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS outbox (\n        id TEXT PRIMARY KEY, alias TEXT NOT NULL, route TEXT NOT NULL, envelope BLOB NOT NULL,\n        expires_at REAL NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL,\n        claim_rid TEXT, claim_until REAL, outcome TEXT, created_at REAL NOT NULL,\n        updated_at REAL NOT NULL)",
)


def prepare_private(directory: Path, file: Path | None = None) -> None:
    import os
    from . import system

    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=448)
        system.restrict_to_user(directory)
    if file is not None and (not file.exists()):
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
        os.close(fd)
        system.restrict_to_user(file)


def check_private(root: Path) -> None:
    from . import system

    path = root / STORE_REL
    for candidate in (
        path.parent,
        path,
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
    ):
        if candidate.exists() and (not system.is_private(candidate)):
            raise PeerNetError("unsafe-permissions", candidate.name)


def exists(root: Path) -> bool:
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
    except AgentError as exc:
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
        query = f"SELECT {COLUMNS} FROM accepted WHERE device=? AND id=?"
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
    try:
        conn.execute("DELETE FROM accepted WHERE expires_at < ?", (now - RETAIN_S,))
        conn.execute("DELETE FROM rate WHERE at < ?", (now - RATE_WINDOW_S,))
        (count,) = conn.execute("SELECT COUNT(*) FROM rate WHERE device=?", (device,)).fetchone()
        if count >= RATE_LIMIT:
            raise PeerNetError("rate-limited", "device")
        conn.execute("INSERT INTO rate (device, at) VALUES (?, ?)", (device, now))
        conn.execute(
            "INSERT INTO accepted (device, id, digest, target_sid, to_name, expires_at, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?)",
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
    try:
        cur = conn.execute(
            "UPDATE accepted SET state='wake_requested', wake_rid=?, claim_until=?, updated_at=? WHERE device=? AND id=? AND (state IN ('queued', 'wake_failed', 'wake_coalesced') OR (state='wake_requested' AND claim_until < ?))",
            (rid, now + CLAIM_LEASE_S, now, device, msg_id, now),
        )
    except sqlite3.Error as exc:
        raise PeerNetError("store-unavailable", "store") from exc
    return cur.rowcount == 1


def settle_wake(conn: sqlite3.Connection, device: str, msg_id: str, rid: str, state: str, now: float) -> bool:
    try:
        cur = conn.execute(
            "UPDATE accepted SET state=?, claim_until=NULL, updated_at=? WHERE device=? AND id=? AND wake_rid=? AND state='wake_requested'",
            (state, now, device, msg_id, rid),
        )
    except sqlite3.Error as exc:
        raise PeerNetError("store-unavailable", "store") from exc
    return cur.rowcount == 1
