"""Sending to another device: `peer send <alias>/<who>` and the remote half of `peer list`.

A message is journaled in the outbox before its first attempt, goes only to the exact
destination it was written for, and is resent with its identical bytes only while it is
unexpired on this clock. One `Clock` bounds the whole call: the store guard and every
transaction wait, the claim, the pin snapshot, and each ssh call's spawn, kill and reap.
Expiry, the full route, the pin and the deadline are re-checked after the claim, immediately
before ssh starts. A result reports the row's durable state; when that cannot be recorded or
read within the budget it says `uncertain`, never an outcome it did not establish. An ACK
means the receiver accepted the message, nothing more.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..core import agent_path
from ..core import now as core_now
from . import ack, authorized, envelope, mutate, outbox, routes, ssh, store, system
from .channel import Clock
from .config import valid_alias
from .errors import PeerNetError

SEND_BUDGET_S = 45.0
CALL_TIMEOUT_S = 30.0
MIN_CALL_S = 1.0  # an ssh call is not started with less than this left (cleanup included)
TTL_S = 600.0  # the owner's choice, 2026-10-04
ATTEMPTS = 3
REPLAY_BATCH = 3
LIST_BUDGET_S = 10.0
LIST_WORKERS = 4
LEASE_GRACE_S = 5.0
SNAPSHOTS_REL = agent_path(Path(), "peer-net", "pin-snapshots")


def _now() -> float:
    """One clock for envelope times, expiry and the outbox: the harness's `now`."""
    return core_now().timestamp()


def split(who: str) -> tuple[str | None, str]:
    """(device alias or None, the rest). A name with no `/` is local."""
    alias, sep, rest = (who or "").partition("/")
    if not sep:
        return None, who
    if not valid_alias(alias) or not rest.strip():
        raise PeerNetError("bad-value", "to")
    return alias, rest


def _tx(conn: sqlite3.Connection, clock: Clock) -> None:
    """Cap the next transaction's lock wait at what is left; refuse once nothing is."""
    remaining = clock.remaining()
    if remaining <= 0:
        raise PeerNetError("timeout", "send")
    outbox.busy(conn, remaining)


def own_device(root: Path, clock: Clock) -> str | None:
    if not store.exists(root):
        return None
    conn = store.connect(root, timeout=clock.remaining())
    try:
        _tx(conn, clock)
        row = conn.execute("SELECT v FROM enrollment WHERE k='device'").fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else None


def _route(conn: sqlite3.Connection, alias: str) -> dict[str, Any] | None:
    row = routes.existing(conn, alias)
    if row is None:
        return None
    return {"alias": alias, "user": row[0], "host": row[1], "port": int(row[2]), "fp": row[3]}


def _routed(conn: sqlite3.Connection) -> list[str]:
    return sorted(str(r[0]) for r in conn.execute("SELECT alias FROM outbound").fetchall())


def _envelope(*, to: str, text: str, from_session: str, from_name: str) -> tuple[str, bytes, float]:
    at = core_now()
    msg_id = uuid.uuid4().hex
    body = {
        "v": 1,
        "verb": "enqueue",
        "id": msg_id,
        "to": to,
        "text": text,
        "from_session": from_session,
        "from_name": from_name if envelope.NAME_RE.fullmatch(from_name or "") else "",
        "sent_at": at.isoformat(),
        "expires_at": (at + timedelta(seconds=TTL_S)).isoformat(),
    }
    try:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate such as "\ud800"
        raise PeerNetError("bad-value", "text") from None
    # The receiver's own bounds, checked before any journal or I/O. Its stdin reader caps the
    # serialized size, which escaping can push far past the text's own byte limit.
    if len(raw) > envelope.MAX_BYTES:
        raise PeerNetError("oversized", "envelope")
    envelope.parse(raw, now=at)
    return msg_id, raw, (at + timedelta(seconds=TTL_S)).timestamp()


def pin_snapshot(root: Path, route: dict[str, Any]) -> Path:
    """A private copy of the pin, taken only if the live pin is a plain file holding exactly
    one line for this alias whose key has the frozen fingerprint. ssh then trusts the copy, so
    a pin replaced while the attempt runs can never redirect it."""
    alias = route["alias"]
    pin = ssh.pin_path(root, alias)
    authorized.refuse_links(pin)
    try:
        data = pin.read_bytes()
    except OSError:
        raise PeerNetError("pin-unreadable", alias) from None
    lines = data.decode("utf-8", errors="replace").splitlines()
    parts = lines[0].split() if len(lines) == 1 else []
    try:
        ok = (
            len(parts) == 3
            and parts[0] == f"agent-peer-{alias}"
            and parts[1] == "ssh-ed25519"
            and authorized.fingerprint(base64.b64decode(parts[2], validate=True)) == route["fp"]
        )
    except ValueError:
        ok = False
    if not ok:
        raise PeerNetError("pin-drift", alias)
    directory = root / SNAPSHOTS_REL
    snap = directory / f"{alias}.{uuid.uuid4().hex}"
    try:
        store.prepare_private(directory)
        authorized.refuse_links(snap)  # the directory itself, and its parent
        if not system.is_private(directory):
            raise PeerNetError("unsafe-permissions", "pin snapshots")
        mutate.write_private(snap, data)
        if snap.read_bytes() != data:
            raise PeerNetError("snapshot", alias)
    except (OSError, PeerNetError) as exc:
        _unlink(snap)  # never leave a partial snapshot behind
        if isinstance(exc, PeerNetError):
            raise
        raise PeerNetError("snapshot", alias) from None
    return snap


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _durable(conn: sqlite3.Connection, msg_id: str, clock: Clock) -> str:
    """The row's state as recorded: a closed state, `skipped` when another attempt holds it
    open, or `uncertain` when it cannot be read within the budget."""
    try:
        _tx(conn, clock)
        item = outbox.get(conn, msg_id)
    except PeerNetError:
        return "uncertain"
    if item is None:
        return "uncertain"
    return "skipped" if item.state in outbox.OPEN else item.state


def _settle(
    conn: sqlite3.Connection, msg_id: str, rid: str, state: str, outcome: str, clock: Clock
) -> tuple[str, bool]:
    """Record this attempt's result: (durable state, whether this claim's settle won). A lost
    compare-and-set reports the row's actual state, which can equal ours when another attempt
    settled the same thing; only a win makes this attempt's own result the record. A store
    that cannot be reached within the budget reports `uncertain`; the lease then lapses and a
    later replay re-establishes it."""
    try:
        outbox.busy(conn, max(0.0, clock.remaining()))
        won = outbox.settle(conn, msg_id, rid, state, outcome, now=_now())
    except PeerNetError:
        return "uncertain", False
    return (state, True) if won else (_durable(conn, msg_id, clock), False)


def _settled(
    conn: sqlite3.Connection, msg_id: str, rid: str, state: str, outcome: str, clock: Clock
) -> tuple[str, None]:
    """A settle whose own answer is never returned (no ACK came with it)."""
    return _settle(conn, msg_id, rid, state, outcome, clock)[0], None


def _close(conn: sqlite3.Connection, item: outbox.Item, state: str, outcome: str, clock: Clock) -> str:
    """Close a row without any network call: claim, then settle."""
    try:
        _tx(conn, clock)
        rid = outbox.claim(conn, item.id, lease_s=LEASE_GRACE_S, now=_now())
    except PeerNetError:
        return "uncertain"
    if rid is None:
        return _durable(conn, item.id, clock)
    return _settle(conn, item.id, rid, state, outcome, clock)[0]


def _attempt(
    root: Path, conn: sqlite3.Connection, device: str, item: outbox.Item, clock: Clock
) -> tuple[str, dict[str, Any] | None]:
    """One claimed attempt: (state, answer). `budget` when it could not start."""
    if clock.remaining() < MIN_CALL_S:
        return "budget", None
    try:
        _tx(conn, clock)
        rid = outbox.claim(
            conn, item.id, lease_s=min(CALL_TIMEOUT_S, clock.remaining()) + LEASE_GRACE_S, now=_now()
        )
    except PeerNetError:
        return "uncertain", None
    if rid is None:
        return _durable(conn, item.id, clock), None
    snap: Path | None = None
    try:
        try:
            _tx(conn, clock)
            current = _route(conn, item.alias)
        except PeerNetError:
            return _settled(conn, item.id, rid, "unconfirmed", "budget", clock)
        if current is None:
            return _settled(conn, item.id, rid, "abandoned:no-route", "the route was revoked", clock)
        if current != item.route:  # never a substitute destination for the frozen one
            return _settled(conn, item.id, rid, "abandoned:route-changed", "the destination changed", clock)
        try:
            snap = pin_snapshot(root, item.route)
            command = ssh.argv(
                root,
                local_alias=device,
                alias=item.alias,
                user=item.route["user"],
                host=item.route["host"],
                port=item.route["port"],
                known_hosts=snap,
            )
        except PeerNetError as exc:
            if exc.code == "pin-drift":
                return _settled(conn, item.id, rid, "abandoned:route-changed", "the pin changed", clock)
            return _settled(conn, item.id, rid, "unconfirmed", exc.code, clock)
        except OSError:  # a snapshot or argv file problem: this attempt never ran
            return _settled(conn, item.id, rid, "unconfirmed", "local-io", clock)
        # Immediately before spawn: the last moment expiry and the deadline are decided.
        if _now() > item.expires_at:
            return _settled(conn, item.id, rid, "acceptance-unknown", "expired before an ACK", clock)
        timeout = min(CALL_TIMEOUT_S, clock.remaining())
        if timeout < MIN_CALL_S:
            return _settled(conn, item.id, rid, "unconfirmed", "budget", clock)
        try:
            status, out, _ = ssh.run_bounded(command, item.envelope, timeout=timeout, limit=ack.ACK_LIMIT)
        except OSError:  # ssh missing, or a sandbox refused it
            status, out = None, b""
    finally:
        if snap is not None:
            _unlink(snap)
    answer = ack.enqueue_answer(status, out, item.id)
    if answer is None:
        return _settled(conn, item.id, rid, "unconfirmed", "no-answer", clock)
    state = "accepted" if answer["ok"] else f"refused:{answer['error']}"
    settled, won = _settle(conn, item.id, rid, state, json.dumps(answer, sort_keys=True), clock)
    return settled, (answer if won else None)  # this attempt's ACK only when its own settle won


def _replay(root: Path, conn: sqlite3.Connection, device: str, item: outbox.Item, clock: Clock) -> str:
    """One step for an older open row: closed without network once expired or unroutable,
    otherwise one attempt, which re-checks everything itself."""
    if _now() > item.expires_at:
        return _close(conn, item, "acceptance-unknown", "expired before an ACK", clock)
    return _attempt(root, conn, device, item, clock)[0]


def send(
    root: Path,
    *,
    alias: str,
    to: str,
    text: str,
    from_session: str,
    from_name: str,
    clock: Clock | None = None,
) -> dict[str, Any]:
    clock = clock or Clock(SEND_BUDGET_S)
    # Every OS permission check inside (Windows PowerShell, whoami, icacls) shares this budget.
    with system.deadline(clock.end):
        return _send(
            root, alias=alias, to=to, text=text, from_session=from_session, from_name=from_name, clock=clock
        )


def _send(
    root: Path, *, alias: str, to: str, text: str, from_session: str, from_name: str, clock: Clock
) -> dict[str, Any]:
    if not store.exists(root):
        raise PeerNetError("not-initialized", "run `peer-net init` and `peer-net pair` first")
    store.check_private(root)
    conn = store.connect(root, timeout=clock.remaining())
    try:
        _tx(conn, clock)
        device_row = conn.execute("SELECT v FROM enrollment WHERE k='device'").fetchone()
        if device_row is None:
            raise PeerNetError("not-initialized", "run `peer-net init` first")
        device = str(device_row[0])
        route = _route(conn, alias)
        if route is None:
            raise PeerNetError("unknown-device", f"routed: {', '.join(_routed(conn)) or 'none'}")
        msg_id, raw, expires_at = _envelope(to=to, text=text, from_session=from_session, from_name=from_name)
        _tx(conn, clock)
        outbox.journal(
            conn, msg_id=msg_id, alias=alias, route=route, envelope=raw, expires_at=expires_at, now=_now()
        )
        item = outbox.Item(msg_id, alias, route, raw, expires_at, "pending", 0)
        # From here the message is journaled: whatever fails, the caller gets its id and an
        # honest outcome, never an exception that hides that it may already be on its way.
        state, answer = "unconfirmed", None
        try:
            for _ in range(ATTEMPTS):  # the new message first
                state, answer = _attempt(root, conn, device, item, clock)
                if state != "unconfirmed":
                    break
        except OSError, PeerNetError:
            state, answer = "uncertain", None
        if state == "budget":
            state = "unconfirmed"
        earlier: list[dict[str, str]] = []
        backlog: int | None = None
        try:
            _tx(conn, clock)
            older = outbox.open_for(conn, alias, exclude=msg_id, limit=REPLAY_BATCH)
            for old in older:
                if clock.remaining() < MIN_CALL_S:
                    break
                earlier.append({"id": old.id, "outcome": _replay(root, conn, device, old, clock)})
            _tx(conn, clock)
            outbox.prune(conn, now=_now())
            _tx(conn, clock)
            backlog = outbox.open_count(conn, alias, exclude=msg_id)
        except OSError, PeerNetError:
            pass  # out of budget: the new message's outcome stands; the backlog is unknown
    finally:
        conn.close()
    return {
        "id": msg_id,
        "to": f"{alias}/{to}",
        "outcome": state,
        "answer": answer,
        "earlier": earlier,
        "backlog": backlog,
    }


# --- list ----------------------------------------------------------------------------------


def _list_one(
    root: Path, device: str, route: dict[str, Any], clock: Clock
) -> tuple[list[dict[str, Any]], int] | str:
    # A pool worker does not inherit the caller's context variables, so the shared deadline
    # is set again here, for any permission check ssh.argv's setup makes.
    with system.deadline(clock.end):
        return _list_one_bounded(root, device, route, clock)


def _list_one_bounded(
    root: Path, device: str, route: dict[str, Any], clock: Clock
) -> tuple[list[dict[str, Any]], int] | str:
    if clock.remaining() < MIN_CALL_S:  # a queued device whose turn came too late never starts
        return "timeout"
    at = core_now()
    body = {
        "v": 1,
        "verb": "list",
        "id": uuid.uuid4().hex,
        "sent_at": at.isoformat(),
        "expires_at": (at + timedelta(seconds=60)).isoformat(),
    }
    try:
        command = ssh.argv(
            root,
            local_alias=device,
            alias=route["alias"],
            user=route["user"],
            host=route["host"],
            port=route["port"],
        )
    except PeerNetError as exc:
        return exc.code
    timeout = clock.remaining()
    if timeout < MIN_CALL_S:
        return "timeout"
    started = time.monotonic()
    try:
        status, out, overflow = ssh.run_bounded(
            command, json.dumps(body).encode(), timeout=timeout, limit=ack.LIST_LIMIT
        )
    except OSError:
        return "no-answer"
    if overflow:
        return "oversized"
    if status is None and time.monotonic() - started >= timeout - 0.05:
        return "timeout"  # killed at the deadline, not a malformed answer
    return ack.list_answer(status, out, route["alias"])


def list_remote(root: Path, *, budget_s: float = LIST_BUDGET_S) -> tuple[list[dict[str, Any]], list[str]]:
    """(blocks, notes). Each block is {alias, rows, dropped, reason, backlog}. Never raises: an
    absent or broken store, or any failure here, becomes a note and leaves the local list alone.
    The budget covers setup too, OS permission checks included."""
    clock = Clock(budget_s)
    with system.deadline(clock.end):
        return _list_remote(root, clock)


def _list_remote(root: Path, clock: Clock) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        if not store.exists(root):
            return [], []
        conn = store.connect(root, timeout=clock.remaining())
        try:
            _tx(conn, clock)
            device_row = conn.execute("SELECT v FROM enrollment WHERE k='device'").fetchone()
            targets = [r for r in (_route(conn, a) for a in _routed(conn)) if r is not None]
            _tx(conn, clock)  # its own transaction gets what is left, afresh
            backlog = outbox.open_counts(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- the local list must survive any store problem
        return [], [f"remote devices unavailable: {getattr(exc, 'code', type(exc).__name__)}"]
    if device_row is None or not targets:
        return [], []
    device = str(device_row[0])
    pool = ThreadPoolExecutor(max_workers=LIST_WORKERS)
    futures = {pool.submit(_list_one, root, device, route, clock): route["alias"] for route in targets}
    done, _ = wait(futures, timeout=max(0.0, clock.remaining()))
    blocks: list[dict[str, Any]] = []
    for future, alias in futures.items():
        result: Any = "timeout"
        if future in done:
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 -- one device's failure is its own row
                result = getattr(exc, "code", "no-answer")
        else:
            future.cancel()  # queued tasks never start after the deadline
        if isinstance(result, str):
            blocks.append(
                {"alias": alias, "rows": [], "dropped": 0, "reason": result, "backlog": backlog.get(alias, 0)}
            )
        else:
            rows, dropped = result
            blocks.append(
                {
                    "alias": alias,
                    "rows": rows,
                    "dropped": dropped,
                    "reason": None,
                    "backlog": backlog.get(alias, 0),
                }
            )
    pool.shutdown(wait=False, cancel_futures=True)  # a running ssh ends by its own timeout
    return sorted(blocks, key=lambda b: b["alias"]), []
