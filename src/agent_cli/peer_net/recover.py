"""`peer-net recover`: resolve an enrollment command that died midway.

Person-only. Without an action it only reports. With one, it re-analyses under the enrollment
guard and does exactly the change that analysis offers, re-checking the file immediately before
it acts. It never undoes a later decision: an authorize superseded by a revoke can only be
cleaned up, never completed. A file that is neither the recorded before state nor the planned
one is a conflict, changed only by `--restore-backup` with the current hash the owner read.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import authorized, intents, mutate, routes, ssh, store
from .enroll import enroll_guarded, finish_revoke, writer_checks
from .errors import PeerNetError

GRANT_OPS = {"authorize", "pair-grant", "accept-grant"}


def recover(
    root: Path, op_id: str | None = None, action: str | None = None, expect: str | None = None
) -> dict[str, Any]:
    with enroll_guarded(root):
        if not store.exists(root):
            return {"open": []}
        conn = store.connect(root)
        try:
            if op_id is None:
                return {"open": [_analyse(root, conn, i) for i in intents.open_for(conn)]}
            intent = intents.get(conn, op_id)
            if intent is None or intent.state != intents.OPEN:
                raise PeerNetError("intent-missing", "op")
            report = _analyse(root, conn, intent)
            if action is None:
                return report
            if action not in report["options"]:
                raise PeerNetError("action-unavailable", "action")
            writer_checks(root, conn, denial=True)  # not root, private, this store's account
            _apply(root, conn, intent, report, action, expect)
            return {**report, "applied": action}
        finally:
            conn.close()


def _diff(payload: dict[str, Any]) -> str:
    import difflib

    path = Path(payload["file"])
    current = path.read_bytes() if path.exists() else b""
    lines = difflib.unified_diff(
        Path(payload["backup"]).read_bytes().decode("utf-8", errors="replace").splitlines(),
        current.decode("utf-8", errors="replace").splitlines(),
        "backup",
        "current",
        lineterm="",
    )
    return "\n".join(lines)


def _file_report(base: dict[str, Any], payload: dict[str, Any], written: list[str], unwritten: list[str]):
    state, current = mutate.file_state(payload)
    if state == "planned":
        return {**base, "status": "written", "options": written}
    if state == "before":
        return {**base, "status": "not-written", "options": unwritten}
    return {
        **base,
        "status": "conflict",
        "options": ["restore-backup"],
        "current_sha": current,
        "before_sha": payload["before_sha"],
        "planned_sha": payload["planned_sha"],
        "diff": _diff(payload),
    }


def _analyse(root: Path, conn: sqlite3.Connection, intent: intents.Intent) -> dict[str, Any]:
    p = intent.payload
    base = {"op_id": intent.op_id, "op": intent.op, "alias": intent.alias}
    if intent.op == "init":
        return {**base, **_init_state(conn, p)}
    if intent.op in GRANT_OPS:
        inbound = conn.execute("SELECT op_id, revoked FROM inbound WHERE alias=?", (intent.alias,)).fetchone()
        superseded = inbound is None or inbound[0] != intent.op_id or bool(inbound[1])
        if "file" not in p:
            return {**base, "status": "nothing-written", "options": ["undo"]}
        # A pairing grant is activated only by the protocol's own step, never by recovery.
        complete = ["complete", "undo"] if intent.op == "authorize" else ["undo"]
        if superseded:
            return {**_file_report(base, p, ["undo"], ["undo"]), "superseded": True}
        return _file_report(base, p, complete, ["undo"])
    if intent.op == "revoke":
        if "file" not in p:
            return {**base, "status": "denied", "options": ["complete"]}
        return _file_report(base, p, ["complete"], ["complete"])
    if intent.op in {"pair", "accept"}:
        # The pairing's own record: its grants and routes carry their own intents. Re-running
        # pair resumes it; abandoning only closes the record and changes nothing else.
        return {**base, "status": f"pairing at step {p.get('step')}", "options": ["abandon"]}
    # add and pairing routes: the pin is this op's only if it holds exactly its journaled line.
    pin, tmp = routes.pin_states(root, intent)
    if "conflict" in (pin, tmp):
        return {**base, "status": "pin-conflict", "options": ["abandon"], "pin": pin, "tmp": tmp}
    return {**base, "status": "route-unverified", "options": ["undo", "abandon"], "pin": pin}


def _init_state(conn: sqlite3.Connection, p: dict[str, Any]) -> dict[str, Any]:
    """Each destination is ours only if its own key material matches the recorded fingerprint:
    the private key by the public half derived from it, the .pub by its content."""
    if conn.execute("SELECT 1 FROM enrollment WHERE k='device'").fetchone():
        return {"status": "recorded", "options": ["complete"]}
    fp = p.get("key_fp")
    key, pub = Path(p["key"]), Path(p["pub"])
    ours = {
        "key": bool(fp) and key.exists() and mutate.derived_fingerprint(key) == fp,
        "pub": bool(fp) and pub.exists() and _pub_fp(pub) == fp,
    }
    foreign = [n for n, path in (("key", key), ("pub", pub)) if path.exists() and not ours[n]]
    if foreign:
        return {"status": "conflict", "options": [], "foreign": foreign}
    if ours["key"] and ours["pub"]:
        return {"status": "keys-promoted", "options": ["complete", "undo"]}
    return {"status": "not-promoted", "options": ["undo"]}


def _pub_fp(pub: Path) -> str:
    try:
        return authorized.fingerprint(authorized.ed25519_blob(pub.read_text(encoding="utf-8")))
    except OSError, PeerNetError:
        return ""


def _apply(
    root: Path,
    conn: sqlite3.Connection,
    intent: intents.Intent,
    report: dict[str, Any],
    action: str,
    expect: str | None,
) -> None:
    p, alias, op = intent.payload, intent.alias, intent.op_id
    if intent.op == "init":
        _apply_init(conn, p, alias, op, action)
        return
    if action == "abandon":  # closes the record only; nothing on disk is touched
        with store.transaction(conn):
            intents.update(conn, op, state="abandoned")
        return
    if action == "restore-backup":
        mutate.restore(p, expect)
        if intent.op in GRANT_OPS:
            _close_authorize(conn, alias, op, "undone")
        return  # a revoke stays open; recover again to finish it
    if intent.op in GRANT_OPS:
        if action == "complete":
            if mutate.file_state(p)[0] != "planned":  # re-checked right before activation
                raise PeerNetError("changed-underneath", "recover again")
            with store.transaction(conn):
                conn.execute("UPDATE inbound SET state='active' WHERE alias=? AND op_id=?", (alias, op))
                intents.update(conn, op, state="done")
            return
        if "file" in p and mutate.file_state(p)[0] == "planned":
            path = Path(p["file"])
            authorized.rewrite_in_place(path, p["planned_sha"], mutate.backup_bytes(p))
        _close_authorize(conn, alias, op, "superseded" if report.get("superseded") else "undone")
        return
    if intent.op == "revoke":
        with store.transaction(conn):
            conn.execute("UPDATE inbound SET revoked=1 WHERE alias=?", (alias,))
        if "file" in p and mutate.file_state(p)[0] == "planned":
            _drop_route(root, conn, alias)
            with store.transaction(conn):
                intents.update(conn, op, state="done")
            return
        finish_revoke(root, conn, op, alias)
        return
    routes.roll_back(root, conn, intent, state="undone")  # re-classifies; a conflict is refused


def _apply_init(conn: sqlite3.Connection, p: dict[str, Any], alias: str, op: str, action: str) -> None:
    current = _init_state(conn, p)  # re-checked under the guard, right before acting
    if action not in current["options"]:
        raise PeerNetError("changed-underneath", "recover again")
    if action == "complete":
        from . import system

        if not system.is_private(Path(p["key"])):  # adopt only a key others cannot read
            raise PeerNetError("unsafe-permissions", "key")
        with store.transaction(conn):
            conn.executemany(
                "INSERT OR IGNORE INTO enrollment (k, v) VALUES (?, ?)",
                [("device", alias), ("enabled", "0"), ("key", p["key"])],
            )
            intents.update(conn, op, state="done")
        return
    for name in ("key", "pub"):  # only destinations whose own material is this op's
        path = Path(p[name])
        if path.exists():
            path.unlink()
    if p.get("tmp") and Path(p["tmp"]).exists():
        shutil.rmtree(p["tmp"])
    with store.transaction(conn):
        intents.update(conn, op, state="undone")


def _close_authorize(conn: sqlite3.Connection, alias: str, op: str, state: str) -> None:
    with store.transaction(conn):
        conn.execute("DELETE FROM inbound WHERE alias=? AND op_id=? AND state='pending'", (alias, op))
        intents.update(conn, op, state=state)


def _drop_route(root: Path, conn: sqlite3.Connection, alias: str) -> None:
    pin = ssh.pin_path(root, alias)
    if pin.exists():
        pin.unlink()
    with store.transaction(conn):
        conn.execute("DELETE FROM outbound WHERE alias=?", (alias,))
