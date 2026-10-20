"""Person-only enrollment: init, authorize, add, revoke, on, off, show.

Each command validates everything first, writes an intent, then changes things in an order
that fails closed: an interruption leaves the receiver refusing, never admitting, and a route
unverified, never usable. The receiver never calls any of this.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from . import authorized, grants, intents, mutate, routes, ssh, store, system
from .config import valid_alias
from .errors import PeerNetError

GUARD = "peer-net:enroll"


def enroll_guarded(root: Path):
    from ..dispatch.guards import area_guard

    return area_guard(root, GUARD)


def _alias(value: str, field: str = "alias") -> str:
    if not valid_alias(value):
        raise PeerNetError("bad-value", field)
    return value


def device_name(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT v FROM enrollment WHERE k='device'").fetchone()
    if row is None:
        raise PeerNetError("not-initialized", "run `peer-net init` first")
    return str(row[0])


def writer_checks(root: Path, conn: sqlite3.Connection, *, denial: bool = False) -> None:
    """Every writing command, recovery included: not root, a private store, and this store's own
    account. A store from before accounts were recorded adopts the account of its first writing
    command, journaled. While intents are open, only denial
    commands (off, revoke) and recovery may adopt, so an unresolved change is never re-owned by
    a granting command. show, doctor and the receiver never call this, and never adopt."""
    system.refuse_root()
    store.check_private(root)
    me = system.account_id()
    with store.transaction(conn):
        row = conn.execute("SELECT v FROM enrollment WHERE k='account'").fetchone()
        if row is not None:
            if row[0] != me:
                raise PeerNetError("wrong-account", "this enrollment belongs to another account")
            return
        enrolled = conn.execute("SELECT 1 FROM enrollment WHERE k='device'").fetchone() is not None
        pending = intents.open_for(conn)
        if enrolled and pending and not denial:
            raise PeerNetError("intent-open", "run `peer-net recover` first")
        conn.execute("INSERT INTO enrollment (k, v) VALUES ('account', ?)", (me,))
        if enrolled:
            adopt = intents.begin_in(
                conn, "adopt-account", "-", {"account": me, "open_intents": [i.op_id for i in pending]}
            )
            intents.update(conn, adopt, state="done")


def open_store(root: Path, *, denial: bool = False):
    """The store for a writing command, after `writer_checks`."""
    system.refuse_root()  # before the store can be created under root
    conn = store.connect(root)
    try:
        writer_checks(root, conn, denial=denial)
    except BaseException:
        conn.close()
        raise
    return conn


def init(root: Path, device: str) -> dict[str, Any]:
    _alias(device, "device")
    with enroll_guarded(root):
        fps = system.host_fingerprints()
        if not fps:  # before any write: nothing is half-made
            step = "enable Remote Login" if os.name != "nt" else "start the sshd service"
            raise PeerNetError("no-host-keys", f"{step}, then run init again")
        key = ssh.key_path(device)
        pub = key.with_name(key.name + ".pub")
        authorized.refuse_links(key)  # the key and ~/.ssh itself
        conn = open_store(root)
        try:
            row = conn.execute("SELECT v FROM enrollment WHERE k='device'").fetchone()
            if row is not None:
                if row[0] != device:
                    raise PeerNetError("already-initialized", "device")
                pub_line = verified_identity(conn, key, pub)
                _accept_cmd(root, conn)  # repairs a crash between its replace and its hash
                wake = _codex_wake(conn)  # a re-run refreshes it, e.g. after a Codex upgrade
                return {
                    "device": device,
                    "public_key": pub_line,
                    "host_fingerprints": fps,
                    "codex_wake": wake,
                    "already": True,
                }
            if key.exists() or pub.exists():
                raise PeerNetError("key-exists", "key")
            intents.refuse_open(conn)
            op = intents.begin(conn, "init", device, {"key": str(key), "pub": str(pub)})
            tmp = key.parent / f"agent_peer.tmp-{op[:8]}"
            with store.transaction(conn):
                intents.update(conn, op, tmp=str(tmp))  # recorded before the directory exists
            pub_line = _generate(key, pub, tmp, device, op, conn)
            key_fp = authorized.fingerprint(authorized.ed25519_blob(pub_line))
            with store.transaction(conn):
                conn.executemany(
                    "INSERT INTO enrollment (k, v) VALUES (?, ?)",
                    [("device", device), ("enabled", "0"), ("key", str(key)), ("key_fp", key_fp)],
                )
                intents.update(conn, op, state="done")
            _accept_cmd(root, conn)
            wake = _codex_wake(conn)
        finally:
            conn.close()
    return {"device": device, "public_key": pub_line, "host_fingerprints": fps, "codex_wake": wake}


def _codex_wake(conn: sqlite3.Connection) -> str:
    """Record how the receiver may start `codex queue` (task 16). A device without Codex records
    nothing and init still succeeds; Codex recipients there are then only reached at their next
    turn."""
    from . import wake_launch

    try:
        launch = wake_launch.resolve()
    except FileNotFoundError as exc:
        wake_launch.record(conn, None)
        return f"unavailable: {exc}"
    wake_launch.record(conn, launch)
    return "recorded: " + " ".join(launch["argv"])


def _accept_cmd(root: Path, conn: sqlite3.Connection) -> None:
    """Windows only: the generated `accept.cmd` that `pair` names on this device."""
    if os.name == "nt":
        from . import launcher, remote

        remote.write_accept_cmd(root, conn, launcher.resolve(root).interpreter)


def verified_identity(conn: sqlite3.Connection, key: Path, pub: Path) -> str:
    """For an init re-run: the device key is reprinted only if both files are plain, the private
    key is private, and the private key, the .pub and the recorded fingerprint all agree. A
    replaced or readable key is never taken as this enrollment's identity."""
    authorized.refuse_links(key)
    authorized.refuse_links(pub)
    if not (key.is_file() and pub.is_file()) or not system.is_private(key):
        raise PeerNetError("key-drift", "the device key is missing or readable by others")
    pub_line = pub.read_text(encoding="utf-8").strip()
    pub_fp = authorized.fingerprint(authorized.ed25519_blob(pub_line))
    if mutate.derived_fingerprint(key) != pub_fp:
        raise PeerNetError("key-drift", "the .pub does not match the private key")
    row = conn.execute("SELECT v FROM enrollment WHERE k='key_fp'").fetchone()
    if row is None:  # recorded from now on; older stores agree with their own key files
        with store.transaction(conn):
            conn.execute("INSERT INTO enrollment (k, v) VALUES ('key_fp', ?)", (pub_fp,))
    elif row[0] != pub_fp:
        raise PeerNetError("key-drift", "the device key is not the enrolled one")
    return pub_line


def _generate(key: Path, pub: Path, tmp: Path, device: str, op: str, conn: sqlite3.Connection) -> str:
    """ssh-keygen into a user-only temp dir, then promote by `os.link`, which never overwrites.
    The fingerprint is recorded before promotion, so recovery adopts or removes only files whose
    own key material matches it."""
    if not key.parent.exists():
        key.parent.mkdir(mode=0o700, parents=True)
    tmp.mkdir(mode=0o700)
    system.restrict_to_user(tmp)
    tmp_key = tmp / "key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"agent-peer-net:{device}", "-f", str(tmp_key)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    pub_line = (tmp / "key.pub").read_text(encoding="utf-8").strip()
    key_fp = authorized.fingerprint(authorized.ed25519_blob(pub_line))
    with store.transaction(conn):
        intents.update(conn, op, key_fp=key_fp)
    os.link(tmp_key, key)
    system.restrict_to_user(key)
    os.link(tmp / "key.pub", pub)
    tmp_key.unlink()
    (tmp / "key.pub").unlink()
    tmp.rmdir()
    return pub_line


def authorize(root: Path, alias: str, public_key: str) -> dict[str, Any]:
    _alias(alias)
    blob = authorized.ed25519_blob(public_key)
    key_fp = authorized.fingerprint(blob)
    with enroll_guarded(root):
        conn = open_store(root)
        try:
            if device_name(conn) == alias:
                raise PeerNetError("bad-value", "alias")
            intents.refuse_open(conn, alias)
            row = conn.execute(
                "SELECT revoked, key_fp, state FROM inbound WHERE alias=?", (alias,)
            ).fetchone()
            if row is not None and not row[0] and (row[1] != key_fp or row[2] != "active"):
                raise PeerNetError("already-authorized", "alias")
            # Idempotent through grants.install: the exact active line is a no-op, a missing one
            # is reinstalled, drift is refused and never rewritten.
            return grants.install(root, conn, alias, public_key, activate=True, op_kind="authorize")
        finally:
            conn.close()


def revoke(root: Path, alias: str) -> dict[str, Any]:
    """Denial first: the receiver refuses before the line goes. Available even while another
    intent is open, because it only takes access away."""
    _alias(alias)
    with enroll_guarded(root):
        conn = open_store(root, denial=True)
        try:
            op = intents.begin(conn, "revoke", alias, {})
            with store.transaction(conn):
                conn.execute("UPDATE inbound SET revoked=1 WHERE alias=?", (alias,))
                conn.execute("UPDATE outbound SET verified=0 WHERE alias=?", (alias,))
            removed = finish_revoke(root, conn, op, alias)
        finally:
            conn.close()
    return {"alias": alias, "lines_removed": removed}


def finish_revoke(root: Path, conn: sqlite3.Connection, op: str, alias: str) -> int:
    """Remove the alias's managed line through a journaled rewrite, then its route and pin."""
    inbound = conn.execute("SELECT key_fp, op_id FROM inbound WHERE alias=?", (alias,)).fetchone()
    removed = 0
    if inbound is not None:
        path, admin = system.authorized_file()
        if path.exists():
            authorized.refuse_links(path)
            data = path.read_bytes()
            new, removed = authorized.without_line(data, alias, inbound[1][:8], inbound[0])
            if removed:
                mutate.rewrite(root, conn, op, path, data, new, admin=admin)
    pin = ssh.pin_path(root, alias)
    if pin.exists():
        pin.unlink()
    with store.transaction(conn):
        conn.execute("DELETE FROM outbound WHERE alias=?", (alias,))
        intents.update(conn, op, state="done", removed=removed)
    return removed


def add(root: Path, alias: str, user: str, host: str, port: int, fingerprint: str) -> dict[str, Any]:
    _alias(alias)
    ssh.valid_user(user)
    host = ssh.valid_host(host)
    ssh.valid_port(port)
    with enroll_guarded(root):
        conn = open_store(root)
        try:
            device = device_name(conn)
            intents.refuse_open(conn, alias)
            try:
                # The scan and pin check run every time: on a re-run, a scan that fails or a
                # host key that changed is this side's own failure and clears verified.
                key_line = ssh.pick(ssh.scan(host, port), fingerprint)
                outcome = routes.install(
                    root, conn, alias, user, host, port, fingerprint, key_line, op_kind="add"
                )
            except (PeerNetError, OSError) as exc:
                if routes.existing(conn, alias) is not None:
                    routes.set_verified(conn, alias, False)
                if isinstance(exc, OSError):  # an unreadable pin, ssh-keyscan missing
                    raise PeerNetError("reverify-failed", type(exc).__name__) from exc
                raise
            verified, answer = routes.probe(root, conn, device=device, alias=alias)
        finally:
            conn.close()
    result = {"alias": alias, "verified": verified, "probe": answer}
    return {**result, "already": True} if outcome == "same" else result


verified_answer = routes.verified_answer
probe_status = routes.probe_status


def set_enabled(root: Path, enabled: bool) -> dict[str, Any]:
    with enroll_guarded(root):
        conn = open_store(root, denial=not enabled)
        try:
            device_name(conn)
            if enabled:
                intents.refuse_open(conn)
            with store.transaction(conn):
                conn.execute("UPDATE enrollment SET v=? WHERE k='enabled'", ("1" if enabled else "0",))
                if not enabled:  # journaled, so a pairing in flight can see the denial
                    intents.update(conn, intents.begin_in(conn, "off", "-", {}), state="done")
        finally:
            conn.close()
    return {"enabled": enabled}


def show(root: Path) -> dict[str, Any]:
    if not store.exists(root):
        return {"enrolled": False}
    conn = store.connect(root)
    try:
        values = dict(conn.execute("SELECT k, v FROM enrollment").fetchall())
        inbound = [
            {"alias": a, "key_fp": f, "state": s, "revoked": bool(r)}
            for a, f, s, r in conn.execute("SELECT alias, key_fp, state, revoked FROM inbound")
        ]
        outbound = [
            {"alias": a, "user": u, "host": h, "port": p, "verified": bool(v)}
            for a, u, h, p, v in conn.execute("SELECT alias, user, host, port, verified FROM outbound")
        ]
        pending = [{"op_id": i.op_id, "op": i.op, "alias": i.alias} for i in intents.open_for(conn)]
    finally:
        conn.close()
    return {
        "enrolled": "device" in values,
        "device": values.get("device"),
        "enabled": values.get("enabled") == "1",
        "inbound": inbound,
        "outbound": outbound,
        "open_intents": pending,
    }
