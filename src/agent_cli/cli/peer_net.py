"""`peer-net`: cross-device peer transport (see ADR-0035).

`receive` is the sshd forced command on a receiving device. The rest pair devices and are run
by the owner, never by a model (`mcp/coverage.py` lists all of them as person-only). `receive`
reads only stdin and writes one JSON line, bypassing `Output` so nothing but that line reaches
the sending device.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core import EXIT_USAGE, Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    pn = sub.add_parser("peer-net", help="cross-device peer transport").add_subparsers(
        dest="sub", required=True
    )
    pr = pn.add_parser("receive", help="the sshd forced command; reads one envelope from stdin")
    pr.add_argument("--from-device", required=True, help="the sending device, written by enrollment")
    pi = pn.add_parser("init", help="make this device's peer key; prints the key and host fingerprint")
    pi.add_argument("--device", required=True, help="this device's alias, e.g. mac or windows")
    pa = pn.add_parser("authorize", help="let another device's key reach this receiver")
    pa.add_argument("alias", help="the sending device's alias")
    pa.add_argument("--key", required=True, help="the sending device's public key line, from its init")
    pd = pn.add_parser("add", help="route sends to another device, pinning its confirmed host key")
    pd.add_argument("alias")
    pd.add_argument("target", help="user@host")
    pd.add_argument("--port", type=int, default=22)
    pd.add_argument("--fingerprint", required=True, help="SHA256:... from the other device's init")
    pv = pn.add_parser("revoke", help="remove a device's access here and the route to it")
    pv.add_argument("alias")
    pn.add_parser("on", help="accept enrolled devices' messages")
    pn.add_parser("off", help="refuse all; the queue and enrollment are kept")
    pn.add_parser("show", help="this device's enrollment")
    ps = pn.add_parser("setup-server", help="optional: get this machine's SSH server ready (idempotent)")
    ps.add_argument(
        "--firewall",
        choices=["private-only"],
        help="also narrow an existing OpenSSH firewall rule to Private networks",
    )
    pp = pn.add_parser(
        "pair",
        help="pair with another device over one SSH login of yours; runs only `peer-net accept` there",
    )
    pp.add_argument("alias", help="the other device's alias, as its init printed")
    pp.add_argument("target", help="user@host of your own account on the other device")
    pp.add_argument("--repo", required=True, help="the harness's absolute path on the other device")
    pp.add_argument("--port", type=int, default=22)
    pp.add_argument("--device", help="this device's alias; only needed before this device's init")
    pp.add_argument("--enable", action="store_true", help="turn receiving on at both ends")
    pp.add_argument("--restart", action="store_true", help="start a new pairing instead of resuming")
    pp.add_argument("--login-key", help="log in with this existing key of yours instead of a password")
    pp.add_argument("--self-user", help="your user name on this device, for the route back (default: you)")
    pp.add_argument("--self-port", type=int, default=22)
    pn.add_parser("accept", help="the other side of `pair`; started by `pair` over SSH, never by hand")
    pc = pn.add_parser("recover", help="resolve an enrollment command that stopped midway")
    pc.add_argument("op_id", nargs="?")
    pc.add_argument("--action", choices=["complete", "undo", "restore-backup", "abandon"])
    pc.add_argument("--expect", help="current sha256 of the file, for --action restore-backup")


def peer_net_receive(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..peer_net import receive
    from ..peer_net.envelope import read_stdin

    try:
        raw = read_stdin(sys.stdin.buffer)
    except Exception as exc:  # noqa: BLE001 — PeerNetError or worse; the code is all that leaves
        code = getattr(exc, "code", "bad-input")
        response = {"v": 1, "ok": False, "error": code, "detail": "stdin"}
    else:
        response = receive(root, args.from_device, raw)
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if response.get("ok") else 1


def _run(out: Output, call: Callable[[], Any]) -> int:
    from ..ops._shared import Op
    from ..peer_net.errors import PeerNetError

    try:
        data: Any = call()
    except PeerNetError as exc:
        return out.done(
            Op({"error": exc.code, "detail": exc.detail}, f"refused: {exc.code} ({exc.detail})", EXIT_USAGE)
        )
    return out.done(Op(data, json.dumps(data, indent=2)))


def peer_net_init(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..peer_net import enroll

    return _run(out, lambda: enroll.init(root, args.device))


def peer_net_authorize(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..peer_net import enroll

    return _run(out, lambda: enroll.authorize(root, args.alias, args.key))


def peer_net_add(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..peer_net import enroll
    from ..peer_net.errors import PeerNetError

    def call():
        user, sep, host = args.target.partition("@")
        if not sep:
            raise PeerNetError("bad-value", "target")
        return enroll.add(root, args.alias, user, host, args.port, args.fingerprint)

    return _run(out, call)


def peer_net_revoke(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..peer_net import enroll

    return _run(out, lambda: enroll.revoke(root, args.alias))


def peer_net_on(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..peer_net import enroll

    return _run(out, lambda: enroll.set_enabled(root, True))


def peer_net_off(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..peer_net import enroll

    return _run(out, lambda: enroll.set_enabled(root, False))


def peer_net_show(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..peer_net import enroll

    return _run(out, lambda: enroll.show(root))


def peer_net_setup_server(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..ops._shared import Op
    from ..peer_net.errors import PeerNetError
    from ..peer_net.server_setup import setup_server

    try:
        report = setup_server(firewall_private_only=args.firewall == "private-only")
    except PeerNetError as exc:
        return out.done(
            Op({"error": exc.code, "detail": exc.detail}, f"refused: {exc.code} ({exc.detail})", 2)
        )
    return out.done(Op(report, setup_report(report), setup_exit(report)))


def setup_report(report: dict[str, Any]) -> str:
    lines = [
        f"{s['status']:>7}  {s['step']}" + (f": {s['detail']}" if s["detail"] else "")
        for s in report["steps"]
    ]
    return "\n".join([*lines, f"result: {report['result']}"])


def setup_exit(report: dict[str, Any]) -> int:
    """Exit 1 when a step failed; 0 otherwise. Warnings print but do not fail: they are the
    owner's decisions (network trust, an existing rule), not errors."""
    return 1 if report["result"] == "failed" else 0


def peer_net_pair(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..ops._shared import Op
    from ..peer_net.errors import PeerNetError

    try:
        result = pair(root, args)
    except PeerNetError as exc:
        return out.done(
            Op({"error": exc.code, "detail": exc.detail}, f"refused: {exc.code} ({exc.detail})", EXIT_USAGE)
        )
    return out.done(Op(result, *pair_report(result)))


def pair_report(result: dict[str, Any]) -> tuple[str, int]:
    """The text and exit status: 0 only when both directions verified, 3 when the protocol
    completed but either route did not."""
    text = json.dumps(result, indent=2)
    unverified = [
        name
        for name, ok in (
            ("this device's route", result["verified"]),
            ("the route back", result["their_probe"]["verified"]),
        )
        if not ok
    ]
    problems: list[str] = []
    if unverified:
        problems.append(
            f"not verified: {', '.join(unverified)}. Re-run pair with --enable, or check `peer-net show` on both"
        )
    remote_exit = result.get("remote_exit")
    if remote_exit not in (0, None):
        problems.append(
            f"`peer-net accept` on {result.get('to')} exited {remote_exit}; see its message above"
        )
    if not problems:
        return text, 0
    return "\n".join([text, *problems]), 3


def pair(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    import getpass

    from ..peer_net import bootstrap, enroll, pairing, remote, ssh, store
    from ..peer_net.config import valid_alias
    from ..peer_net.errors import PeerNetError

    # Every argument is checked before anything is written.
    user, sep, host = args.target.partition("@")
    if not sep:
        raise PeerNetError("bad-value", "target")
    ssh.valid_user(user)
    host = ssh.valid_host(host)
    ssh.valid_port(args.port)
    ssh.valid_port(args.self_port)
    self_user = ssh.valid_user(args.self_user or getpass.getuser())
    remote_command = remote.command(args.repo)
    device = args.device or enroll.show(root).get("device")
    if not device:
        raise PeerNetError("not-initialized", "run `peer-net init --device <alias>` or pass --device")
    if not valid_alias(args.alias) or not valid_alias(device) or args.alias == device:
        raise PeerNetError("bad-value", "alias")
    login_key = Path(args.login_key).expanduser() if args.login_key else None
    # init first: it makes the store and the peer-net directory private before the bootstrap
    # creates anything there. It is idempotent, and pair runs it again.
    enroll.init(root, device)
    if login_key is not None:
        conn = store.connect(root)
        try:
            bootstrap.refuse_peer_key(root, conn, login_key)
        finally:
            conn.close()
    command = bootstrap.argv(
        root,
        alias=args.alias,
        user=user,
        host=host,
        port=args.port,
        remote_command=remote_command,
        login_key=login_key,
    )

    def confirm(fp: str) -> bool:
        return input(confirm_prompt(args.alias, host, fp)).strip().lower() in {"y", "yes"}

    sessions: list[bootstrap.SshSession] = []

    def open_session() -> bootstrap.SshSession:
        session = bootstrap.SshSession(command, bootstrap.LOGIN_TIMEOUT_S + pairing.SESSION_TIMEOUT_S)
        sessions.append(session)
        return session

    result = pairing.pair(
        root,
        b_alias=args.alias,
        user=user,
        host=host,
        port=args.port,
        device=device,
        enable=args.enable,
        restart=args.restart,
        confirm=confirm,
        session=open_session,
        self_user=self_user,
        self_port=args.self_port,
        login_timeout_s=bootstrap.LOGIN_TIMEOUT_S,
    )
    # The session is closed and reaped by now: this is accept's own exit status, through ssh.
    return {**result, "remote_exit": sessions[-1].returncode if sessions else None}


def confirm_prompt(alias: str, host: str, fp: str) -> str:
    """Name the other device beside its fingerprint so confirmation cannot confuse it with this
    device's init output.
    """
    return (
        f"Host key of {alias} at {host}:\n  {fp}\n"
        f"Compare it with the host_fingerprints line that `peer-net init` printed on {alias} "
        "(not on this machine). Same key? [y/N] "
    )


ACCEPT_EOF_WAIT_S = 5.0


def peer_net_accept(root: Path, args: argparse.Namespace, out: Output) -> int:
    """stdout carries only protocol lines, so this bypasses `Output`; the owner sees the
    outcome on stderr, which ssh passes through."""
    from ..peer_net import pairing
    from ..peer_net.channel import LineChannel
    from ..peer_net.errors import PeerNetError

    channel = LineChannel(sys.stdin.buffer, sys.stdout.buffer)
    try:
        result = pairing.accept(root, channel)
        sys.stderr.write(f"accept: paired with {result['alias']}, route verified: {result['verified']}\n")
        code = 0
    except PeerNetError as exc:
        sys.stderr.write(f"accept refused: {exc.code} ({exc.detail})\n")
        code = 1
    except Exception as exc:  # noqa: BLE001 -- reported, then the same clean exit
        sys.stderr.write(f"accept failed: {type(exc).__name__}\n")
        code = 1
    return accept_exit(channel, code)


def accept_exit(channel: Any, code: int, *, hard_exit: Callable[[int], object] = os._exit) -> int:
    """End accept without shutting the interpreter down while a reader holds stdin or a writer holds
    stdout. Pair normally closes after bye, letting EOF end the reader. If EOF or write completion
    never arrives, exit without interpreter cleanup or flushing blocked stdout; both could deadlock
    on buffered I/O locks.
    """
    channel.reader.join(ACCEPT_EOF_WAIT_S)
    if channel.reader.is_alive() or channel.writing:
        sys.stderr.flush()
        hard_exit(code)
    return code


def peer_net_recover(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..peer_net.recover import recover

    return _run(out, lambda: recover(root, args.op_id, args.action, args.expect))


HANDLERS = {
    ("peer-net", "receive"): peer_net_receive,
    ("peer-net", "init"): peer_net_init,
    ("peer-net", "authorize"): peer_net_authorize,
    ("peer-net", "add"): peer_net_add,
    ("peer-net", "revoke"): peer_net_revoke,
    ("peer-net", "on"): peer_net_on,
    ("peer-net", "off"): peer_net_off,
    ("peer-net", "show"): peer_net_show,
    ("peer-net", "recover"): peer_net_recover,
    ("peer-net", "pair"): peer_net_pair,
    ("peer-net", "accept"): peer_net_accept,
    ("peer-net", "setup-server"): peer_net_setup_server,
}
