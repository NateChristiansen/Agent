"""A pairing's record on one device: begin or resume, reconcile what a crash left, and the
checks every step makes.

The pairing holds the enrollment guard only for short steps, never across a channel read,
a probe or opening the session, so `off`, `revoke` and `recover` run while a pairing stalls.
Each step re-reads the record under the guard: the pairing must still be open and owned by
this run, and no `revoke` of the other device may have been journaled since the pairing
began. An `off` journaled since then stops the pairing from turning the receiver on. A
denial is never reversed by resuming; only `--restart`, a new owner decision, pairs again.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import grants, intents, routes, store
from .channel import Clock
from .enroll import enroll_guarded, open_store
from .errors import PeerNetError

ROUTE_OPS = {"pair-route", "accept-route"}
GRANT_OPS = {"pair-grant", "accept-grant"}
DENIED = "the device was revoked during pairing; pair --restart to pair again"


@dataclass(frozen=True)
class Record:
    op: str
    pair_id: str
    run: str
    alias: str
    restart: bool


@contextmanager
def step(root: Path, clock: Clock | None = None) -> Generator[Any]:
    """One short guarded step with a checked store; refused once the session deadline has
    passed, including while it waited for the guard."""
    if clock is not None:
        clock.left(1.0)
    with enroll_guarded(root):
        if clock is not None:
            clock.left(1.0)
        conn = open_store(root)
        try:
            yield conn
        finally:
            conn.close()


def _history(conn: sqlite3.Connection, kind: str, alias: str, pair_id: str) -> list[intents.Intent]:
    rows = conn.execute("SELECT op_id FROM intents WHERE op=? AND alias=? ORDER BY rowid", (kind, alias))
    found = [intents.get(conn, r[0]) for r in rows.fetchall()]
    return [i for i in found if i is not None and i.payload.get("pair_id") == pair_id]


def begin(
    root: Path, conn: sqlite3.Connection, kind: str, alias: str, pair_id: str | None, restart: bool
) -> Record:
    """Resume this side's open pairing for `alias`, or begin one. The pairing's own open route
    and grant ops are reconciled; any other open intent for the alias blocks. `restart`
    supersedes an open pairing and keeps its record.

    A restart is consumed once per pair_id: a side that already holds a record of the
    pairing it names treats a repeated restart flag as a resume, with that record's history
    and denials. Only a new pair_id, the owner's new `--restart`, starts clean."""
    if restart and pair_id is not None and _history(conn, kind, alias, pair_id):
        restart = False
    pending = intents.open_for(conn, alias)
    mine = [i for i in pending if i.op == kind]
    ids = {i.op_id for i in mine}
    kids = [i for i in pending if i.payload.get("parent") in ids]
    if any(i not in mine and i not in kids for i in pending):
        raise PeerNetError("intent-open", "run `peer-net recover` first")
    target = None
    if not restart:
        for intent in mine:
            if pair_id is not None and intent.payload.get("pair_id") != pair_id:
                raise PeerNetError(
                    "pair-in-progress", "re-run pair on the other device to resume, or --restart"
                )
            target = intent
    pair_id = pair_id or (str(target.payload["pair_id"]) if target else uuid.uuid4().hex)
    history = [] if restart else _history(conn, kind, alias, pair_id)
    since = history[0].op_id if history else None  # the pairing's first record on this side
    if since is not None and intents.later(conn, since, "revoke", alias):
        raise PeerNetError("pair-denied", DENIED)
    for kid in kids:
        _reconcile(root, conn, kid, restart)
    run = uuid.uuid4().hex
    with store.transaction(conn):
        for intent in mine:
            if intent is not target:
                intents.update(conn, intent.op_id, state="superseded")
        if target is not None:
            intents.update(conn, target.op_id, run=run)
            return Record(target.op_id, pair_id, run, alias, bool(target.payload.get("restart")))
        op = intents.begin_in(
            conn, kind, alias, {"pair_id": pair_id, "step": 0, "run": run, "restart": restart}
        )
        # A pairing that already turned the receiver on never does it again, so an `off` the
        # owner gave after that stays in force when the pairing resumes.
        enabled = any(i.payload.get("enabled") for i in history)
        intents.update(conn, op, since=since or op, enabled=enabled)
    return Record(op, pair_id, run, alias, restart)


def _reconcile(root: Path, conn: sqlite3.Connection, kid: intents.Intent, restart: bool) -> None:
    if kid.op in ROUTE_OPS:
        routes.roll_back(root, conn, kid, state="rolled-back")
        return
    revoked = conn.execute("SELECT revoked FROM inbound WHERE alias=?", (kid.alias,)).fetchone()
    if revoked is not None and revoked[0]:
        if not restart:
            raise PeerNetError("pair-denied", DENIED)
        with store.transaction(conn):  # the revoke already removed the line; nothing to undo
            intents.update(conn, kid.op_id, state="superseded")
        return
    grants.reconcile(conn, kid)


def check(conn: sqlite3.Connection, rec: Record) -> intents.Intent:
    """The record is still open, still this run's, and not denied since it began."""
    intent = intents.get(conn, rec.op)
    if intent is None or intent.state != intents.OPEN or intent.payload.get("run") != rec.run:
        raise PeerNetError("pair-superseded", "the pairing was abandoned, restarted, or resumed elsewhere")
    if intents.later(conn, str(intent.payload.get("since") or rec.op), "revoke", rec.alias):
        raise PeerNetError("pair-denied", DENIED)
    return intent


def advance(conn: sqlite3.Connection, rec: Record, step_no: int, **payload: Any) -> None:
    check(conn, rec)
    with store.transaction(conn):
        intents.update(conn, rec.op, step=step_no, **payload)


def finish(conn: sqlite3.Connection, rec: Record, **payload: Any) -> None:
    check(conn, rec)
    with store.transaction(conn):
        intents.update(conn, rec.op, state="done", **payload)


def enable_once(conn: sqlite3.Connection, rec: Record) -> None:
    """Turn the receiver on, once per pairing. Never after an `off` journaled since the
    pairing began: that denial stands until the owner restarts the pairing."""
    intent = check(conn, rec)
    if intent.payload.get("enabled"):
        return
    if intents.later(conn, str(intent.payload.get("since") or rec.op), "off", "-"):
        raise PeerNetError(
            "pair-denied", "the receiver was turned off during pairing; pair --restart to turn it on"
        )
    with store.transaction(conn):
        conn.execute("UPDATE enrollment SET v='1' WHERE k='enabled'")
        intents.update(conn, rec.op, enabled=True)


PROBE_TIMEOUT_S = 30.0


def probe(root: Path, rec: Record, clock: Clock, *, device: str, alias: str) -> tuple[bool, dict[str, Any]]:
    """Probe outside the guard, within the session's remaining budget; record the outcome only
    if the route is still the one probed and the deadline has not passed."""
    with step(root, clock) as conn:
        check(conn, rec)
        route = routes.existing(conn, alias)
    if route is None:
        raise PeerNetError("no-route", alias)
    answer = routes.probe_status(
        root,
        device=device,
        alias=alias,
        user=route[0],
        host=route[1],
        port=route[2],
        timeout=clock.left(PROBE_TIMEOUT_S),
    )
    verified = routes.verified_answer(answer, alias)
    with step(root, clock) as conn:
        check(conn, rec)
        if routes.existing(conn, alias) != route:
            raise PeerNetError("pair-denied", "the route changed during the probe")
        routes.set_verified(conn, alias, verified)
    return verified, answer
