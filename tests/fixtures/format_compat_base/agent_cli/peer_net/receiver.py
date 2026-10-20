# ruff: noqa
from __future__ import annotations
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from .. import peers as peers_mod
from .. import sessions as sessions_mod
from ..core import now as core_now
from . import config, envelope, store
from .errors import PeerNetError

PROTO = 1
LIST_FIELDS = ("name", "tab", "provider", "feature", "state")
WAKE_RESULT = {True: "wake_accepted", False: "wake_failed", None: "wake_coalesced"}
WAKE_REPORT = {
    "wake_accepted": "accepted",
    "wake_failed": "failed",
    "wake_coalesced": "coalesced",
    "wake_not_needed": "skipped",
}


def receive(
    root: Path, from_device: str, raw: bytes, *, clock: Callable[[], datetime] = core_now
) -> dict[str, Any]:
    try:
        cfg = config.load(root)
        if cfg is None or not cfg.enabled:
            raise PeerNetError("disabled", "receiver")
        if not config.valid_alias(from_device) or not cfg.admits(from_device):
            raise PeerNetError("unknown-device", "device")
        env = envelope.parse(raw, now=clock())
        if env.verb != "enqueue" and envelope.expired(env, clock()):
            raise PeerNetError("expired", "expires_at")
        if env.verb == "status":
            return {"v": PROTO, "ok": True, "device": cfg.device, "enabled": True, "proto": PROTO}
        if env.verb == "list":
            return {"v": PROTO, "ok": True, "device": cfg.device, "sessions": _sessions(root)}
        return _enqueue(root, from_device, env, clock)
    except PeerNetError as exc:
        return {"v": PROTO, "ok": False, "error": exc.code, "detail": exc.detail}
    except Exception:
        return {"v": PROTO, "ok": False, "error": "internal", "detail": "receiver"}


def _sessions(root: Path) -> list[dict[str, Any]]:
    from ..ops.peers import peer_list_local

    return [{key: item.get(key) for key in LIST_FIELDS} for item in peer_list_local(root).data]


def _resolve(root: Path, wanted: str) -> tuple[str, str]:
    from ..ops.peers import TAB_SHORT_RE, addressable_peers, quiet_codex, tab_label

    records = addressable_peers(root)
    live = {str(r["session_id"]) for r in records}
    candidates = records + [r for r in quiet_codex(root) if str(r["session_id"]) not in live]
    matches = [r for r in candidates if str(r.get("name") or "") == wanted or str(r["session_id"]) == wanted]
    if not matches:
        tab = TAB_SHORT_RE.fullmatch(wanted.lower())
        if tab:
            tag, short = tab.groups()
            forms = {f"{tag} {short}"} if tag else {f"cl {short}", f"cx {short}"}
            matches = [r for r in candidates if tab_label(r) in forms]
    if not matches:
        matches = [r for r in candidates if wanted in (r.get("former_names") or [])]
    if len(matches) > 1:
        raise PeerNetError("ambiguous-recipient", "to")
    if not matches:
        raise PeerNetError("unknown-recipient", "to")
    record = matches[0]
    return (str(record["session_id"]), str(record.get("name") or record["session_id"]))


def _enqueue(
    root: Path, device: str, env: envelope.Envelope, clock: Callable[[], datetime]
) -> dict[str, Any]:
    conn = store.connect(root)
    try:
        digest = env.digest()
        with store.transaction(conn):
            row = store.lookup(conn, device, env.id)
        existing = row is not None
        if row is None:
            if envelope.expired(env, clock()):
                raise PeerNetError("expired", "expires_at")
            target_sid, to_name = _resolve(root, env.to)
            with store.transaction(conn):
                row = store.lookup(conn, device, env.id)
                existing = row is not None
                if row is None:
                    if envelope.expired(env, clock()):
                        raise PeerNetError("expired", "expires_at")
                    row = store.insert(
                        conn,
                        device=device,
                        msg_id=env.id,
                        digest=digest,
                        target_sid=target_sid,
                        to_name=to_name,
                        expires_at=env.expires_at.timestamp(),
                        now=clock().timestamp(),
                    )
        if row.digest != digest:
            raise PeerNetError("conflict", "id")
        return _progress(root, conn, device, env, row, existing, clock)
    finally:
        conn.close()


def _progress(
    root: Path,
    conn: Any,
    device: str,
    env: envelope.Envelope,
    row: store.Row,
    existing: bool,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    recovery = "none"
    if row.state == "accepted" and (not envelope.expired(env, clock())):
        if _queue(root, conn, device, env, row, clock) and existing:
            recovery = "appended"
    with store.transaction(conn):
        row = store.lookup(conn, device, env.id) or row
    wake = WAKE_REPORT.get(row.state, "skipped")
    if row.state == "wake_requested":
        wake = "in-progress"
    retryable = {"queued", "wake_failed", "wake_coalesced", "wake_requested"}
    if row.state in retryable and (not envelope.expired(env, clock())):
        claimed = _ring(root, conn, device, env, row, clock)
        if claimed is not None:
            wake = claimed
            if existing and recovery == "none" and (claimed in {"accepted", "failed"}):
                recovery = "wake_requested"
    queued = row.state != "accepted"
    return {
        "v": PROTO,
        "ok": True,
        "state": "already-accepted" if existing else "accepted",
        "queue": "queued" if queued else "pending",
        "wake": wake,
        "recovery": recovery,
        "id": env.id,
        "to_name": row.to_name,
    }


def _pending(root: Path, target: str, device: str, msg_id: str) -> bool:
    return any(
        (
            isinstance(r.get("remote"), dict)
            and r["remote"].get("device") == device
            and (r["remote"].get("msg_id") == msg_id)
            for r in peers_mod.peek(root, target)
        )
    )


def _queue(
    root: Path, conn: Any, device: str, env: envelope.Envelope, row: store.Row, clock: Callable[[], datetime]
) -> bool:
    target = row.target_sid
    appended = False
    with peers_mod.inbox_guard(root, target):
        with store.transaction(conn):
            fresh = store.lookup(conn, device, env.id)
        if fresh is None or fresh.state != "accepted" or envelope.expired(env, clock()):
            return False
        if not _pending(root, target, device, env.id):
            peers_mod.send(
                root,
                to=target,
                text=env.text,
                frm=f"{device}/{env.from_session}",
                frm_name=f"{device}/{env.from_name or env.from_session}",
                remote={"device": device, "msg_id": env.id},
            )
            appended = True
        with store.transaction(conn):
            store.mark_queued(conn, device, env.id, clock().timestamp())
    return appended


def _codex_launch(conn: Any) -> Any:
    from ..ops.peers import REMOTE_WAKE
    from . import wake_launch

    try:
        with store.transaction(conn):
            launch = wake_launch.load(conn)
    except PeerNetError:
        launch = None
    return launch or REMOTE_WAKE


def _ring(
    root: Path, conn: Any, device: str, env: envelope.Envelope, row: store.Row, clock: Callable[[], datetime]
) -> str | None:
    from ..ops import peer_wake
    from ..ops.peers import wake_peer

    rid = uuid.uuid4().hex
    with store.transaction(conn):
        if not store.claim_wake(conn, device, env.id, rid, clock().timestamp()):
            return None

    def give_up() -> str:
        with store.transaction(conn):
            store.settle_wake(conn, device, env.id, rid, "wake_failed", clock().timestamp())
        return "expired"

    if envelope.expired(env, clock()):
        return give_up()
    target = row.target_sid
    with peers_mod.inbox_guard(root, target):
        if envelope.expired(env, clock()):
            return give_up()
        if not _pending(root, target, device, env.id):
            with store.transaction(conn):
                store.settle_wake(conn, device, env.id, rid, "wake_not_needed", clock().timestamp())
            return "skipped"
        target_record = sessions_mod.get(root, target) or {}
        wake_id = peer_wake.reserve(root, target)
    accepted: bool | None = None
    if wake_id:
        launch = _codex_launch(conn) if target_record.get("provider") == "codex" else None
        if envelope.expired(env, clock()):
            peer_wake.settle(root, target, wake_id, False)
            return give_up()
        accepted, _ = wake_peer(target_record, str(target_record.get("name") or target), launch)
        peer_wake.settle(root, target, wake_id, accepted)
    state = WAKE_RESULT[accepted]
    with store.transaction(conn):
        store.settle_wake(conn, device, env.id, rid, state, clock().timestamp())
    return WAKE_REPORT[state]
