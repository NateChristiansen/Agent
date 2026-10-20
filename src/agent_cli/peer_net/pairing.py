"""The pairing protocol: `pair` on device A, `accept` on device B.

In short, over one session the
owner authenticated:

| # | A sends | B does, then replies |
|---|-------------|-----------------------------------------------------------------------|
| 1 | hello | pins A's host key, routes to A (unverified), grants A (pending): offer |
| 2 | ready | activates A's grant, turns on if asked: ack |
| 3 | probe-back | (A has probed A->B and activated B) probes B->A: probed |
| 4 | done | records completion, closes its intent: bye |

Each side journals a record keyed by pair_id before it writes, and its route and grant ops
name that record as their parent, so re-running `pair` resumes and reconciles whatever a lost
message or a crash left (`pair_state`). A route is verified only by its own side's probe; a
grant is never downgraded; nothing is turned on unless `--enable`; an owner's `off` or
`revoke` during a pairing is never reversed by it. This module never opens an SSH connection
itself: `pair` takes a `Session` factory (the bootstrap argv in the CLI, an in-process fake in
tests).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import authorized, grants, pair_state, routes, ssh, system
from .channel import VERSION, Clock, Session, message
from .config import valid_alias
from .enroll import device_name, verified_identity
from .errors import PeerNetError

MESSAGE_TIMEOUT_S = 30.0
SESSION_TIMEOUT_S = 180.0
ERROR_REPLY_S = 2.0  # a refusal is reported briefly, never with a fresh full wait


def _key(value: str) -> tuple[str, str]:
    """A peer public key line and its fingerprint; raises bad-key."""
    return value.strip(), authorized.fingerprint(authorized.ed25519_blob(value))


def _host_key(value: str) -> tuple[str, str]:
    """`ssh-ed25519 <base64>` exactly, and its fingerprint."""
    line, fp = _key(value)
    if len(line.split()) != 2:
        raise PeerNetError("pair-protocol", "host key")
    return line, fp


def _alias(value: str) -> str:
    if not valid_alias(value):
        raise PeerNetError("pair-protocol", "alias")
    return value


def _recv(
    channel: Session, clock: Clock, phase: str, pair_id: str | None = None, *, wait_s: float | None = None
) -> dict[str, Any]:
    return message(channel.recv(clock.left(MESSAGE_TIMEOUT_S if wait_s is None else wait_s)), phase, pair_id)


def _send(channel: Session, clock: Clock, phase: str, pair_id: str, **fields: Any) -> None:
    channel.send({"v": VERSION, "phase": phase, "pair_id": pair_id, **fields}, clock.left(MESSAGE_TIMEOUT_S))


# --- B: accept ------------------------------------------------------------------------------


def accept(root: Path, channel: Session, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """B's side. Runs inside the owner's own SSH session; every refusal is sent to A as an
    `error` message before it is raised."""
    env = dict(os.environ if env is None else env)
    clock = Clock(SESSION_TIMEOUT_S)
    try:
        return _accept(root, channel, env, clock)
    except PeerNetError as exc:
        remaining = clock.remaining()
        if remaining > 0:  # once the deadline has passed, nothing more is sent
            try:
                channel.send(
                    {"v": VERSION, "phase": "error", "code": exc.code, "detail": exc.detail},
                    min(ERROR_REPLY_S, remaining),
                )
            except OSError, PeerNetError:
                pass
        raise
    finally:
        channel.close()


def _client_address(env: dict[str, str]) -> str:
    """A's address as sshd saw it: the first field of SSH_CONNECTION. Never A's own claim."""
    parts = env.get("SSH_CONNECTION", "").split()
    if not parts:
        raise PeerNetError("pair-protocol", "accept runs only inside an SSH session")
    return ssh.valid_host(parts[0])


def _accept(root: Path, channel: Session, env: dict[str, str], clock: Clock) -> dict[str, Any]:
    hello = _recv(channel, clock, "hello")
    pair_id = hello["pair_id"]
    a_alias = _alias(hello["a_alias"])
    a_user = ssh.valid_user(hello["a_user"])
    a_port = ssh.valid_port(hello["a_port"])
    a_host = _client_address(env)
    a_pub, a_fp = _key(hello["a_peer_pub"])
    a_host_line, a_host_fp = _host_key(hello["a_host_pub"])
    with pair_state.step(root, clock) as conn:
        device = device_name(conn)
        if device == a_alias:
            raise PeerNetError("bad-value", "alias")
        key = ssh.key_path(device)
        b_pub = verified_identity(conn, key, key.with_name(key.name + ".pub"))  # before any write
        rec = pair_state.begin(root, conn, "accept", a_alias, pair_id, hello["restart"])
        routes.install(
            root,
            conn,
            a_alias,
            a_user,
            a_host,
            a_port,
            a_host_fp,
            a_host_line,
            op_kind="accept-route",
            parent=rec.op,
        )
        pair_state.check(conn, rec)
        grants.install(root, conn, a_alias, a_pub, activate=False, op_kind="accept-grant", parent=rec.op)
        pair_state.advance(conn, rec, 1, a_key_fp=a_fp)
    _send(channel, clock, "offer", pair_id, b_alias=device, b_peer_pub=b_pub)
    ready = _recv(channel, clock, "ready", pair_id)
    with pair_state.step(root, clock) as conn:
        pair_state.check(conn, rec)
        grants.activate(conn, a_alias, a_fp)
        if ready["enable"]:
            pair_state.enable_once(conn, rec)
        pair_state.advance(conn, rec, 2)
    _send(channel, clock, "ack", pair_id)
    _recv(channel, clock, "probe-back", pair_id)
    verified, answer = pair_state.probe(root, rec, clock, device=device, alias=a_alias)
    with pair_state.step(root, clock) as conn:
        pair_state.advance(conn, rec, 3, verified=verified)
    _send(channel, clock, "probed", pair_id, verified=verified, answer=answer)
    _recv(channel, clock, "done", pair_id)
    with pair_state.step(root, clock) as conn:  # completion is durable before bye
        pair_state.finish(conn, rec, step=4, completed=True)
    _send(channel, clock, "bye", pair_id)
    return {"alias": a_alias, "pair_id": pair_id, "verified": verified}


# --- A: pair --------------------------------------------------------------------------------


def pair(
    root: Path,
    *,
    b_alias: str,
    user: str,
    host: str,
    port: int,
    device: str,
    enable: bool,
    restart: bool,
    confirm: Callable[[str], bool],
    session: Callable[[], Session],
    self_user: str,
    self_port: int = 22,
    login_timeout_s: float = 0.0,
) -> dict[str, Any]:
    """A's side. `confirm` shows B's fingerprint to the owner and returns their answer;
    `session` opens the owner-authenticated session to B running `accept`. The guard is held
    only for short steps, never while the session opens or a message is awaited."""
    from .enroll import init

    if not valid_alias(b_alias) or not valid_alias(device) or b_alias == device:
        raise PeerNetError("bad-value", "alias")
    ssh.valid_user(user)
    ssh.valid_user(self_user)
    host = ssh.valid_host(host)
    ssh.valid_port(port)
    ssh.valid_port(self_port)
    a_pub = init(root, device)["public_key"]  # idempotent; a re-run verifies the device key
    a_host_line = system.host_key_line()
    scanned = ssh.scan(host, port)
    if len(scanned) != 1:
        raise PeerNetError("fingerprint-mismatch", "expected one ed25519 host key")
    b_host_line = scanned[0]
    b_host_fp = authorized.fingerprint(authorized.ed25519_blob(b_host_line))
    if not confirm(b_host_fp):
        raise PeerNetError("not-confirmed", "the owner did not confirm the fingerprint")
    with pair_state.step(root) as conn:
        rec = pair_state.begin(root, conn, "pair", b_alias, None, restart)
        routes.install(
            root, conn, b_alias, user, host, port, b_host_fp, b_host_line, op_kind="pair-route", parent=rec.op
        )
        pair_state.advance(conn, rec, 1)
    # The owner logs in after the session opens: `login_timeout_s` covers that, then the
    # protocol has its own budget. hello is buffered in the pipe until the login completes.
    channel = session()
    clock = Clock(login_timeout_s + SESSION_TIMEOUT_S)
    try:
        _send(
            channel,
            clock,
            "hello",
            rec.pair_id,
            restart=rec.restart,
            a_alias=device,
            a_user=self_user,
            a_port=self_port,
            a_peer_pub=a_pub,
            a_host_pub=a_host_line,
        )
        return _converse(root, channel, clock, rec, b_alias, device, enable, login_timeout_s)
    finally:
        channel.close()


def _converse(
    root: Path,
    channel: Session,
    clock: Clock,
    rec: pair_state.Record,
    b_alias: str,
    device: str,
    enable: bool,
    login_timeout_s: float,
) -> dict[str, Any]:
    pair_id = rec.pair_id
    offer = _recv(channel, clock, "offer", pair_id, wait_s=max(MESSAGE_TIMEOUT_S, login_timeout_s))
    clock.cap(SESSION_TIMEOUT_S)  # authenticated: the protocol gets its own budget, no more
    if offer["b_alias"] != b_alias:
        raise PeerNetError("pair-alias-mismatch", f"the other side is {offer['b_alias'][:40]!r}")
    b_pub, b_fp = _key(offer["b_peer_pub"])
    with pair_state.step(root, clock) as conn:
        pair_state.check(conn, rec)
        grants.install(root, conn, b_alias, b_pub, activate=False, op_kind="pair-grant", parent=rec.op)
        if enable:
            pair_state.enable_once(conn, rec)
        pair_state.advance(conn, rec, 2, b_key_fp=b_fp)
    _send(channel, clock, "ready", pair_id, enable=enable)
    _recv(channel, clock, "ack", pair_id)
    verified, answer = pair_state.probe(root, rec, clock, device=device, alias=b_alias)
    with pair_state.step(root, clock) as conn:
        pair_state.check(conn, rec)
        grants.activate(conn, b_alias, b_fp)
        pair_state.advance(conn, rec, 3, verified=verified)
    _send(channel, clock, "probe-back", pair_id)
    probed = _recv(channel, clock, "probed", pair_id)
    _send(channel, clock, "done", pair_id)
    _recv(channel, clock, "bye", pair_id)
    with pair_state.step(root, clock) as conn:  # only bye closes A's record
        pair_state.finish(conn, rec, step=4)
    return {
        "pair_id": pair_id,
        "to": b_alias,
        "verified": verified,
        "probe": answer,
        "their_probe": {"verified": probed["verified"]},
    }
