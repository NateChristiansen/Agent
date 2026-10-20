"""Inbound grants: one managed authorized_keys line per enrolled device.

Shared by `authorize` and the pairing protocol (`accept`, `pair`). The caller holds the
enrollment guard and an `open_store`ed store, and has decided the grant may be written (no other
intent blocks it). A grant is written `pending` and only `activate` makes the receiver admit
it; an active grant is never downgraded.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from . import authorized, intents, launcher, mutate, store, system
from .errors import PeerNetError


def state(data: bytes, alias: str, op8: str, blob: bytes, command: str) -> str:
    """`exact` when the key's only line is the managed line: the exact options managed_line
    writes, the ed25519 key, and this grant's marker. `absent` when neither the key nor the
    alias's marker appears. `drift` otherwise."""
    related: list[tuple[authorized.Entry, re.Match[str] | None]] = []
    for _, entry in authorized.entries(data):
        marker = authorized.MARKER_RE.search(entry.comment)
        if entry.blob == blob or (marker and marker.group(1) == alias):
            related.append((entry, marker))
    if not related:
        return "absent"
    if len(related) == 1:
        entry, marker = related[0]
        # The whole options field must be exactly what managed_line writes: one extra option
        # (port-forwarding, agent-forwarding, pty) would re-enable what restrict took away.
        expected = authorized.parse_line(authorized.managed_line(command, blob, alias, op8))
        if (
            expected is not None
            and entry.options == expected.options
            and entry.key_type == "ssh-ed25519"
            and entry.blob == blob
            and marker is not None
            and marker.groups() == (alias, op8)
        ):
            return "exact"
    return "drift"


def install(
    root: Path,
    conn: sqlite3.Connection,
    alias: str,
    public_key: str,
    *,
    activate: bool,
    op_kind: str,
    parent: str | None = None,
) -> dict[str, Any]:
    """Make sure `alias` has a managed line for `public_key`.

    - the same key, active, with its exact line: nothing written (`already`)
    - the same key, active, its line gone: the line is reinstalled, the grant stays active
    - no grant, or a revoked one: the line is written and the grant is `pending`; `activate`
      makes it active at once (authorize), else the caller activates it later (pairing)
    - a pending grant for the same key whose line is already exact: kept, then activated if asked
    - anything else (another key, drift, a duplicate copy): refused
    """
    blob = authorized.ed25519_blob(public_key)
    key_fp = authorized.fingerprint(blob)
    row = conn.execute("SELECT revoked, key_fp, state, op_id FROM inbound WHERE alias=?", (alias,)).fetchone()
    path, admin = system.authorized_file()
    if admin and not system.is_elevated():
        raise PeerNetError("needs-elevation", "run this from an elevated shell")
    authorized.refuse_links(path)
    data = path.read_bytes() if path.exists() else b""
    if path.exists() and admin and system.acl_sids(path) != {system.ADMINS_SID, system.SYSTEM_SID}:
        raise PeerNetError("acl", "administrators_authorized_keys")
    command = launcher.forced_command(launcher.resolve(root), alias)
    reinstall = False
    if row is not None and not row[0]:
        if row[1] != key_fp:
            raise PeerNetError("already-authorized", "alias")
        current = state(data, alias, str(row[3])[:8], blob, command)
        if current == "exact":
            if activate and row[2] != "active":
                with store.transaction(conn):
                    conn.execute("UPDATE inbound SET state='active' WHERE alias=? AND revoked=0", (alias,))
            return {"alias": alias, "file": str(path), "key_fp": key_fp, "already": True}
        if current == "drift":
            raise PeerNetError("grant-drift", "revoke and authorize again, or fix the line by hand")
        reinstall = row[2] == "active"
    authorized.refuse_duplicate(data, blob, alias)
    payload: dict[str, Any] = {"key_fp": key_fp, "reinstall": reinstall}
    if parent is not None:  # the pairing this grant belongs to; its re-run reconciles it
        payload["parent"] = parent
    op = intents.begin(conn, op_kind, alias, payload)
    line = authorized.managed_line(command, blob, alias, op)
    with store.transaction(conn):
        intents.update(conn, op, line=line)
        if reinstall:
            # Never downgrade an active grant: only its op id moves to the new line's.
            conn.execute("UPDATE inbound SET op_id=? WHERE alias=?", (op, alias))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO inbound (alias, key_fp, state, revoked, op_id) VALUES (?, ?, 'pending', 0, ?)",
                (alias, key_fp, op),
            )
    mutate.rewrite(root, conn, op, path, data, authorized.with_line(data, line), admin=admin)
    with store.transaction(conn):
        if activate:
            conn.execute("UPDATE inbound SET state='active' WHERE alias=? AND op_id=?", (alias, op))
        intents.update(conn, op, state="done")
    return {"alias": alias, "file": str(path), "key_fp": key_fp}


def activate(conn: sqlite3.Connection, alias: str, key_fp: str) -> None:
    """pending to active, re-checked in the same transaction: the grant must still exist, be
    unrevoked, and hold the key this pairing exchanged. An active grant stays active."""
    with store.transaction(conn):
        row = conn.execute("SELECT revoked, key_fp FROM inbound WHERE alias=?", (alias,)).fetchone()
        if row is None or row[0]:
            raise PeerNetError("pair-denied", "the grant was revoked; pair --restart to grant again")
        if row[1] != key_fp:
            raise PeerNetError("key-drift", "the grant holds another key")
        conn.execute("UPDATE inbound SET state='active' WHERE alias=?", (alias,))


def reconcile(conn: sqlite3.Connection, intent: intents.Intent) -> str:
    """An open grant op a crash left behind, on a re-run of its own pairing. A file holding the
    planned bytes is kept (`done`, the grant stays pending); nothing written, or the before
    bytes, is rolled back. A conflict, a link, or a revoked grant is refused: those belong to
    `peer-net recover` or to the owner's denial."""
    p, alias, op = intent.payload, intent.alias, intent.op_id
    row = conn.execute("SELECT revoked FROM inbound WHERE alias=?", (alias,)).fetchone()
    if row is not None and row[0]:
        raise PeerNetError("pair-denied", "the grant was revoked; pair --restart to grant again")
    if "file" in p:
        authorized.refuse_links(Path(p["file"]))
        current, _ = mutate.file_state(p)
        if current == "conflict":
            raise PeerNetError("intent-open", "authorized_keys changed underneath; run `peer-net recover`")
        if current == "planned":
            with store.transaction(conn):
                intents.update(conn, op, state="done")
            return "done"
    with store.transaction(conn):
        conn.execute("DELETE FROM inbound WHERE alias=? AND op_id=? AND state='pending'", (alias, op))
        intents.update(conn, op, state="rolled-back")
    return "rolled-back"
