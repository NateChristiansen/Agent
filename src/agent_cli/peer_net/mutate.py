"""Journaled changes to authorized_keys and protected backups.

Before any rewrite the intent records the file, its before and planned hashes, and a backup
of the before bytes, and that record is committed. A rewrite interrupted at any byte then
leaves a file that matches before, planned, or neither, and recovery can tell which: a match
is resolved exactly, and anything else is a conflict the owner settles with the backup. The backup is created user-only before its content is written.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from ..core import agent_path
from . import authorized, intents, store, system
from .errors import PeerNetError

BACKUPS_REL = agent_path(Path(), "peer-net", "backups")


def write_all(fd: int, data: bytes) -> None:
    """`os.write` until every byte is written; a short write is not an error, only a retry."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("write made no progress")
        view = view[written:]


def write_private(path: Path, data: bytes) -> None:
    """Create a user-only file exclusively, write all bytes and fsync. Use O_BINARY on Windows so
    newline translation cannot change backed-up authorized_keys bytes.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        system.restrict_to_user(path)  # before the content, on Windows as on POSIX
        write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def backup(root: Path, op: str, data: bytes) -> Path:
    """A user-only copy of `data`, read back and checked before anything relies on it."""
    import uuid

    directory = root / BACKUPS_REL
    store.prepare_private(directory)
    path = directory / f"{op}.{uuid.uuid4().hex[:8]}.bak"  # a retried revoke backs up again
    write_private(path, data)
    if authorized.sha256(path.read_bytes()) != authorized.sha256(data):
        raise PeerNetError("backup-mismatch", "backup")
    return path


def create(path: Path, admin: bool) -> None:
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    if admin:
        system.restrict_admin_file(path)
    else:
        system.restrict_to_user(path)


def rewrite(
    root: Path, conn: sqlite3.Connection, op: str, path: Path, before: bytes, after: bytes, *, admin: bool
) -> None:
    """Record the change, then make it. `before` is what the caller read under the guard."""
    existed = path.exists()
    saved = backup(root, op, before)
    with store.transaction(conn):
        intents.update(
            conn,
            op,
            file=str(path),
            before_sha=authorized.sha256(before),
            planned_sha=authorized.sha256(after),
            backup=str(saved),
            created=not existed,
            admin=admin,
        )
    if not existed:
        create(path, admin)
    authorized.rewrite_in_place(path, authorized.sha256(before), after)


def file_state(payload: dict[str, Any]) -> tuple[str, str]:
    """`planned`, `before` or `conflict` for a recorded rewrite, and the file's current hash."""
    path = Path(payload["file"])
    current = authorized.sha256(path.read_bytes()) if path.exists() else authorized.sha256(b"")
    if current == payload["planned_sha"]:
        return "planned", current
    if current == payload["before_sha"]:
        return "before", current
    return "conflict", current


def restore(payload: dict[str, Any], expect: str | None) -> None:
    """Put the backup back, only if the file still has the hash the owner read and the backup
    still holds exactly the recorded before bytes. A damaged backup is never written over the
    owner's keys, whatever `expect` says."""
    path = Path(payload["file"])
    current = authorized.sha256(path.read_bytes()) if path.exists() else authorized.sha256(b"")
    if expect is None or current != expect:
        raise PeerNetError("changed-underneath", "pass --expect with the current sha256")
    authorized.rewrite_in_place(path, expect, backup_bytes(payload))


def backup_bytes(payload: dict[str, Any]) -> bytes:
    """The recorded backup, refused unless it still matches the recorded before hash."""
    saved = Path(payload["backup"]).read_bytes()
    if authorized.sha256(saved) != payload["before_sha"]:
        raise PeerNetError("backup-mismatch", "backup")
    return saved


def derived_fingerprint(private_key: Path) -> str:
    """The fingerprint of the public half derived from the private key itself, so a replaced
    private key is never taken for this operation's."""
    try:
        out = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(private_key)],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        ).stdout
        return authorized.fingerprint(authorized.ed25519_blob(out.strip()))
    except OSError, subprocess.SubprocessError, PeerNetError:
        return ""
