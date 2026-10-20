# ruff: noqa
from __future__ import annotations
import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from .. import obsidian_tabs
from .. import peers as peers_mod
from .. import sessions as sessions_mod
from ..core import AgentError, is_alive, table
from ..records.narrow import as_dict, feature_number
from ..session_liveness import record_live
from ._shared import Op
from .peer_wake import WAKE_ACCEPTED_TTL_S as WAKE_ACCEPTED_TTL_S
from .peer_wake import WAKE_ATTEMPT_TTL_S as WAKE_ATTEMPT_TTL_S
from .peer_wake import WAKE_TEXT as WAKE_TEXT
from .peer_wake import WAKE_TIMEOUT_S, WakePurpose, dispatch_ring_suppressed, wake_text


def addressable_peers(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for record in sessions_mod.all_records(root):
        sid = str(record.get("session_id") or "")
        if not sid or record.get("dispatch_id"):
            continue
        if record_live(record, is_alive) and as_dict(record.get("activity")).get("state") != "ended":
            out.append(record)
            continue
        revived = sessions_mod.pid_running_session(sid)
        if revived:
            sessions_mod.set_record(root, sid, pid=revived)
            out.append({**record, "pid": revived})
    return out


def tab_label(record: dict[str, Any]) -> str:
    return obsidian_tabs.short_name(str(record["session_id"]), str(record.get("provider") or "claude"))


def _label(record: dict[str, Any]) -> str:
    return f"{record.get('name') or record['session_id']} ({tab_label(record)})"


TAB_SHORT_RE = re.compile("(?:(cl|cx)\\s?)?([0-9a-z-]{4})")


def resolve(root: Path, who: str) -> str:
    wanted = (who or "").strip()
    if not wanted:
        raise AgentError("no recipient; pass a session name or id")
    records = addressable_peers(root)
    live = {str(r["session_id"]) for r in records}
    candidates = records + [r for r in quiet_codex(root) if str(r["session_id"]) not in live]
    exact = [r for r in candidates if str(r.get("name") or "") == wanted or str(r["session_id"]) == wanted]
    if len(exact) == 1:
        return str(exact[0]["session_id"])
    if len(exact) > 1:
        names = ", ".join(sorted((_label(r) for r in exact)))
        raise AgentError(f"{wanted} matches {len(exact)} sessions: {names}. Use the session id")
    if renamed := [r for r in candidates if wanted in (r.get("former_names") or [])]:
        if len(renamed) > 1:
            raise AgentError(f"{wanted} was the name of {len(renamed)} sessions; use the current name or id")
        return str(renamed[0]["session_id"])
    tab = TAB_SHORT_RE.fullmatch(wanted.lower())
    if tab:
        tag, short = tab.groups()
        forms = {f"{tag} {short}"} if tag else {f"cl {short}", f"cx {short}"}
        tabbed = [r for r in candidates if tab_label(r) in forms]
        if len(tabbed) == 1:
            return str(tabbed[0]["session_id"])
        if len(tabbed) > 1:
            names = ", ".join(sorted((_label(r) for r in tabbed)))
            raise AgentError(f"{wanted} is ambiguous: {names}. Use the full name or session id")
    partial = [r for r in records if str(r["session_id"]).startswith(wanted)]
    if len(partial) == 1:
        return str(partial[0]["session_id"])
    if len(partial) > 1:
        names = ", ".join(sorted((_label(r) for r in partial)))
        raise AgentError(f"{wanted} is ambiguous: {names}")
    raise AgentError(_miss(root, wanted, records))


def _miss(root: Path, wanted: str, records: list[dict[str, Any]]) -> str:
    text = f"no live session matches {wanted}."
    gone = [r for r in sessions_mod.all_records(root) if str(r.get("name") or "") == wanted]
    if gone:
        ended = as_dict(gone[0].get("activity")).get("state") == "ended"
        text += f" {wanted} has ended." if ended else f" {wanted} is not running."
    names = [str(r.get("name") or "") for r in records if r.get("name")]
    near = sorted((n for n in names if n.startswith(wanted))) or difflib.get_close_matches(
        wanted, names + [tab_label(r) for r in records], n=1, cutoff=0.6
    )
    if near:
        text += f" Did you mean {', '.join(near)}?"
    known = ", ".join(sorted((_label(r) for r in records))) or "none"
    return text + f" Addressable now: {known}"


def quiet_codex(root: Path) -> list[dict[str, Any]]:
    return [
        r
        for r in sessions_mod.all_records(root)
        if r.get("session_id")
        and r.get("provider") == "codex"
        and r.get("pid_unknown")
        and (not r.get("dispatch_id"))
        and (as_dict(r.get("activity")).get("state") != "ended")
    ]


def peer_send(
    root: Path,
    *,
    to: str,
    message: str,
    session_id: str | None = None,
    expect_reply: bool = False,
    reply_timeout: float = 600,
) -> Op:
    from . import peer_remote

    frm = sessions_mod.resolve_session_id(root, session_id)
    if expect_reply and (not 0 < reply_timeout <= 86400):
        raise AgentError("reply_timeout must be between 0 and 86400 seconds")
    record = sessions_mod.get(root, frm) or {}
    clock = peer_remote.clock_for(to)
    alias, to = peer_remote.target(root, to, clock)
    if alias is not None:
        key = peers_mod.begin_wait(root, frm, to, reply_timeout, device=alias) if expect_reply else None
        try:
            result = peer_remote.send(
                root, alias=alias, who=to, message=message, frm=frm, record=record, clock=clock
            )
        except Exception:
            if key:
                peers_mod.end_wait(root, frm, key)
            raise
        if key and str(result.data.get("outcome") or "").startswith(
            ("refused:", "abandoned:", "acceptance-unknown")
        ):
            peers_mod.end_wait(root, frm, key)
        return result
    target = resolve(root, to)
    if target == frm:
        raise AgentError("that is this session; a message to yourself is not a message")
    from .peer_delivery import deliver

    key = peers_mod.begin_wait(root, frm, target, reply_timeout) if expect_reply else None
    try:
        delivery = deliver(root, target=target, frm=frm, message=message, policy=True)
    except Exception:
        if key:
            peers_mod.end_wait(root, frm, key)
        raise
    result, wake = (delivery.queued, delivery.detail)
    target_record = sessions_mod.get(root, target) or {}
    name = str(target_record.get("name") or target)
    text = f"queued for {name}." + (f" {wake}." if wake else " It reads this at its next turn.")
    if not record_live(target_record, is_alive):
        text += f" {name} has been quiet over 30 minutes and may be closed."
    return Op({**result, "wake": wake}, text)


MESSAGING_SOCKET_DIR = Path("/tmp/cc-socks")
PIPE_PREFIX = "\\\\.\\pipe\\"
REMOTE_WAKE = object()


def wake_peer(
    target: dict[str, Any], name: str, codex_launch: Any = None, *, purpose: WakePurpose = "peer"
) -> tuple[bool, str]:
    return ring(target, name, codex_launch, purpose=purpose)[:2]


def ring(
    target: dict[str, Any], name: str, codex_launch: Any = None, *, purpose: WakePurpose
) -> tuple[bool, str, str | None]:
    text = wake_text(purpose)
    state = str(as_dict(target.get("activity")).get("state") or "")
    provider = str(target.get("provider") or "claude")
    sid = str(target.get("session_id") or "")
    note = f"{name} is {state or 'not working'}"
    if purpose == "dispatch" and dispatch_ring_suppressed():
        return (
            False,
            f"{note}; dispatch rings are off here, so it reads this at its next turn",
            "suppressed",
        )
    if provider == "codex":
        from ..codex_executable import command as codex_command

        args = ("queue", "--thread", sid, "--message", text)
        try:
            if codex_launch is None:
                accepted = _run(codex_command(*args))
            elif codex_launch is REMOTE_WAKE:
                accepted = False
            else:
                from ..peer_net import wake_launch

                accepted = _run([*codex_launch["argv"], *args], env=wake_launch.child_env(codex_launch))
        except OSError:
            accepted = False
        if accepted:
            return (True, f"{note}; codex queue accepted the wake request; delivery unconfirmed", None)
        failed = f"{note}; could not reach `codex queue` — it reads this at its next turn"
        return (False, failed, "queue-failed")
    address = inbox_address(target)
    if not address:
        return (
            False,
            f"{note}; no inbox address recorded, so it reads this at its next turn",
            "route-missing",
        )
    error = _ring_socket(address) if purpose == "peer" else _ring_socket(address, purpose)
    if error is None:
        kind = "pipe" if address.lower().startswith(PIPE_PREFIX) else "socket"
        return (True, f"{note}; wrote a wake frame to its inbox {kind}; delivery unconfirmed", None)
    timed_out = "timeout" in error.lower() or "timed out" in error.lower()
    failure = "transport-timeout" if timed_out else "transport-refused"
    return (False, f"{note}; could not reach {address} ({error}), so it reads this at its next turn", failure)


def inbox_address(target: dict[str, Any]) -> str | None:
    address = str(target.get("inbox") or "")
    if address:
        return address
    pid = target.get("pid")
    if not pid or sys.platform == "win32":
        return None
    return str(MESSAGING_SOCKET_DIR / f"{int(pid)}.sock")


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _canonical_address(address: str) -> str:
    if address.lower().startswith(PIPE_PREFIX):
        return PIPE_PREFIX + address[len(PIPE_PREFIX) :].lower()
    return address


def inbox_token(address: str) -> str | None:
    digest = hashlib.sha256(_canonical_address(address).encode()).hexdigest()
    for key in sorted((claude_config_dir() / "sessions").glob(f"*.{digest}.key")):
        try:
            token = json.loads(key.read_text(encoding="utf-8")).get("peerToken")
        except OSError, ValueError, AttributeError:
            continue
        if isinstance(token, str) and token:
            return token
    return None


def _write_pipe(address: str, payload: bytes) -> str | None:
    import threading

    outcome: list[str | None] = []

    def attempt() -> None:
        try:
            with open(address, "r+b", buffering=0) as handle:
                handle.write(payload)
            outcome.append(None)
        except (OSError, ValueError) as exc:
            outcome.append(f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(WAKE_TIMEOUT_S)
    if not outcome:
        return f"TimeoutError: the pipe did not take the frame within {WAKE_TIMEOUT_S}s"
    return outcome[0]


def wake_frame(text: str) -> bytes:
    frame = {"type": "user", "message": {"role": "user", "content": text}}
    return (json.dumps(frame, ensure_ascii=False) + "\n").encode()


def auth_frame(token: str) -> bytes:
    return (json.dumps({"type": "auth", "token": token}) + "\n").encode()


def _ring_socket(address: str, purpose: WakePurpose = "peer") -> str | None:
    import socket as socket_mod

    token = inbox_token(address)
    payload = (auth_frame(token) if token else b"") + wake_frame(wake_text(purpose))
    try:
        if address.lower().startswith(PIPE_PREFIX):
            if not token:
                return "no readable inbox key for this pipe"
            return _write_pipe(address, payload)
        if not hasattr(socket_mod, "AF_UNIX"):
            return "Unix inbox sockets are unavailable on this platform"
        with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as sock:
            sock.settimeout(WAKE_TIMEOUT_S)
            sock.connect(address)
            sock.sendall(payload)
        return None
    except (OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"


def _run(argv: list[str], env: dict[str, str] | None = None) -> bool:
    import subprocess
    from ..codex_executable import child_env

    try:
        proc = subprocess.run(
            argv,
            env=child_env() if env is None else env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=WAKE_TIMEOUT_S,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return proc.returncode == 0


def peer_inbox(root: Path, *, session_id: str | None = None, peek: bool = False) -> Op:
    sid = sessions_mod.resolve_session_id(root, session_id)
    rows = peers_mod.peek(root, sid) if peek else peers_mod.take(root, sid)
    if not rows:
        return Op([], "no pending messages; the prompt hook may already have delivered them")
    return Op(rows, peers_mod.block(rows))


def peer_list(root: Path) -> Op:
    from . import peer_remote

    local = peer_list_local(root)
    remote_items, remote_text, footer = peer_remote.listing(root)
    text = local.text + (f"\n\n{remote_text}" if remote_text else "") + "\n\n" + footer
    return Op([*local.data, *remote_items], text)


def peer_list_local(root: Path) -> Op:
    from .. import dispatch as dispatch_mod
    from .. import hook_freshness
    from .. import leases as leases_mod
    from ..vault.managers import background_roles, claude_live_sessions

    live = claude_live_sessions()
    addressable = addressable_peers(root)
    pairs = [(r, live.get(str(r["session_id"])) or {}) for r in addressable]
    roles = background_roles(root, pairs, leases_mod.list_all(root), dispatch_mod.load_all(root))
    kinds = {sid: role for sid, (role, _) in roles.items()}
    parent_of = {sid: parent for sid, (_, parent) in roles.items() if parent}
    shown = [r for r in addressable if kinds[str(r["session_id"])] != "spare"]
    order = {str(r["session_id"]): i for i, r in enumerate(shown)}

    def rank(r: dict[str, Any]) -> tuple[int, int, int]:
        sid = str(r["session_id"])
        if kinds[sid] == "bg":
            return (1, order[sid], 0)
        anchor = parent_of.get(sid, sid)
        return (0, order.get(anchor, order[sid]), 1 if sid in parent_of else 0)

    records = sorted(shown, key=rank)
    listed = {str(r["session_id"]) for r in addressable}
    for quiet in quiet_codex(root):
        if str(quiet["session_id"]) not in listed:
            records.append(quiet)
            kinds[str(quiet["session_id"])] = "quiet"
    cache: dict[str, Any] = {}
    items = [
        {
            "name": r.get("name"),
            "tab": tab_label(r),
            "session_id": r["session_id"],
            "provider": r.get("provider") or "claude",
            "feature": as_dict(r.get("feature")).get("id"),
            "state": as_dict(r.get("activity")).get("state"),
            "kind": kinds.get(str(r["session_id"]), "foreground"),
            "pending": len(peers_mod.peek(root, str(r["session_id"]))),
            "hooks": "stale" if hook_freshness.stale_since(root, r, cache) else "ok",
        }
        for r in records
    ]
    rows = [
        [
            str(item["name"] or "—"),
            str(item["tab"]),
            str(item["session_id"])[:8],
            str(item["kind"]),
            feature_number(item["feature"]) if item["feature"] else "—",
            str(item["state"] or "—"),
            str(item["pending"]),
            str(item["hooks"]),
        ]
        for item in items
    ]
    return Op(items, table(rows, ["name", "tab", "id", "kind", "feature", "state", "pending", "hooks"]))


THIS_MACHINE_ONLY = "Peers on this machine only: no other device is paired. The owner pairs one with `peer-net pair`; then `<device>/<name>` reaches its sessions from Claude and Codex alike. Without pairing, Claude managers reach another machine through Remote Control: `ListAgents`, then `SendMessage`."
