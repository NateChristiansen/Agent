# ruff: noqa
from __future__ import annotations
import json
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .. import peers as peers_mod
from .. import sessions as sessions_mod
from ..core import AgentError, agent_path, atomic_write, is_alive, now
from ..records.narrow import as_dict_or_none, as_list_or_none, dig
from ..session_liveness import record_live
from . import notices
from .state import DEFAULT_NOTIFY, load, save

CLAIM_TTL_S = 30.0
GUARD_TIMEOUT_S = 3.0
STORE_DIR = agent_path(Path(), "dispatch-notices")
STORE_VERSION = 1
FAILURES = (
    "suppressed",
    "guard-timeout",
    "store-unavailable",
    "route-missing",
    "route-ended",
    "route-unsupported",
    "auth-unavailable",
    "transport-timeout",
    "transport-refused",
    "queue-failed",
    "stale",
    "claim-expired",
)
RETRYABLE = ("pending", "attempting", "inbox-accepted", "wake-failed", "fallback")
Clock = Callable[[], float]
Transport = Callable[[dict[str, Any], str], tuple[bool, str, str | None]]


def wall_clock() -> float:
    return now().timestamp()


class Unavailable(Exception):
    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


@contextmanager
def step(guard: AbstractContextManager[Any]) -> Generator[Any]:
    with ExitStack() as stack:
        try:
            value = stack.enter_context(guard)
        except (AgentError, OSError) as exc:
            raise Unavailable(_failure(exc)) from None
        try:
            yield value
        except (AgentError, OSError, ValueError) as exc:
            raise Unavailable(_failure(exc)) from None


def _failure(exc: Exception) -> str:
    return "guard-timeout" if isinstance(exc, AgentError) and "is busy" in str(exc) else "store-unavailable"


def bounded_record_guard(
    root: Path, dispatch_id: str, timeout: float
) -> AbstractContextManager[dict[str, Any]]:
    from .verification_gate import record_guard

    return record_guard(root, dispatch_id, timeout=timeout)


def store_path(root: Path, session_id: str) -> Path:
    if not notices.session_ok(session_id):
        raise notices.NoticeError("invalid recipient session id")
    return root / STORE_DIR / f"{session_id}.json"


def read_store(root: Path, session_id: str) -> dict[str, Any]:
    path = store_path(root, session_id)
    if not path.exists():
        return {"version": STORE_VERSION, "arrival": 0, "items": {}, "tombstones": {}}
    data = as_dict_or_none(json.loads(path.read_text(encoding="utf-8")))
    if (
        data is None
        or data.get("version") != STORE_VERSION
        or type(data.get("arrival")) is not int
        or (not isinstance(data.get("items"), dict))
        or (not isinstance(data.get("tombstones"), dict))
    ):
        raise AgentError(f"{path} is not a version-{STORE_VERSION} notice store")
    return data


def _write_store(root: Path, session_id: str, store: dict[str, Any]) -> None:
    atomic_write(store_path(root, session_id), json.dumps(store, indent=2, sort_keys=True) + "\n")


def append(
    root: Path,
    session_id: str,
    envelope: dict[str, Any],
    *,
    clock: Clock = wall_clock,
    timeout: float | None = None,
) -> tuple[str, int | None]:
    notices.validate(envelope)
    if envelope["recipient"] != session_id:
        raise notices.NoticeError("a notice is queued only for its own recipient")
    store = read_store(root, session_id)
    nid = envelope["notice_id"]
    if nid in store["tombstones"]:
        return ("consumed", None)
    if nid in store["items"]:
        return ("present", store["items"][nid].get("arrival"))
    seq = peers_mod.note_arrival(root, session_id, timeout=timeout)
    store["items"][nid] = {"arrival": seq, "appended_at": clock(), "envelope": envelope}
    store["arrival"] = max(store["arrival"], seq)
    _write_store(root, session_id, store)
    return ("appended", seq)


def pending(root: Path, session_id: str, *, upto: int | None = None) -> list[dict[str, Any]]:
    store = read_store(root, session_id)
    items = [
        item
        for nid, item in store["items"].items()
        if nid not in store["tombstones"] and (upto is None or int(item.get("arrival") or 0) <= upto)
    ]
    return sorted(items, key=lambda item: int(item.get("arrival") or 0))


def tombstone(root: Path, session_id: str, nid: str, consumption: str, *, clock: Clock = wall_clock) -> None:
    tombstone_all(root, session_id, [nid], consumption, clock=clock)


def tombstone_all(
    root: Path, session_id: str, nids: list[str], consumption: str, *, clock: Clock = wall_clock
) -> None:
    if consumption not in notices.CONSUMPTION_STATES or consumption == "none":
        raise notices.NoticeError("unknown notice consumption state")
    store = read_store(root, session_id)
    for nid in nids:
        store["tombstones"].setdefault(nid, {"consumption": consumption, "at": clock()})
    _write_store(root, session_id, store)


def consumed_as(root: Path, session_id: str, nid: str) -> str | None:
    value = dig(read_store(root, session_id)["tombstones"], nid, "consumption")
    return (
        value
        if isinstance(value, str) and value in notices.CONSUMPTION_STATES and (value != "none")
        else None
    )


@dataclass(frozen=True)
class Claim:
    dispatch_id: str
    notice_id: str
    token: str
    generation: int
    started: float
    expires: float
    envelope: dict[str, Any]
    route: dict[str, Any]


@dataclass(frozen=True)
class Outcome:
    notice_id: str
    delivery: str | None
    failure: str | None
    duration_s: float
    settled: bool
    skipped: str | None = None


def _claim_current(entry: dict[str, Any], at: float) -> bool:
    held = as_dict_or_none(entry.get("claim"))
    if held is None:
        return False
    expires = held.get("expires")
    return isinstance(expires, (int, float)) and at < expires


def eligible(record: dict[str, Any], *, clock: Clock = wall_clock) -> list[str]:
    at = clock()
    out: list[str] = []
    for nid in notices.outbox(record):
        entry = notices.actionable(record, nid)
        if (
            entry is not None
            and isinstance(entry.get("envelope"), dict)
            and (entry.get("delivery") in RETRYABLE)
            and (not _claim_current(entry, at))
        ):
            out.append(nid)
    return sorted(out, key=lambda nid: str(notices.outbox(record)[nid].get("recorded_at") or ""))


def claim(
    root: Path, dispatch_id: str, nid: str, *, clock: Clock = wall_clock, timeout: float = GUARD_TIMEOUT_S
) -> Claim | str:
    with step(bounded_record_guard(root, dispatch_id, timeout)) as d:
        entry = notices.actionable(d, nid)
        if entry is None:
            return "stale"
        if not isinstance(entry.get("envelope"), dict) or entry.get("delivery") not in RETRYABLE:
            return "ineligible"
        at = clock()
        if _claim_current(entry, at):
            return "claimed"
        if "chat" not in (d.get("notify", DEFAULT_NOTIFY) or []):
            _audit(entry, "fallback", "suppressed", 0.0)
            save(root, d)
            return "suppressed"
        token = uuid.uuid4().hex
        entry["claim"] = {"token": token, "started": at, "expires": at + CLAIM_TTL_S}
        entry["delivery"] = "attempting"
        save(root, d)
        return Claim(
            dispatch_id,
            nid,
            token,
            entry["generation"],
            at,
            at + CLAIM_TTL_S,
            entry["envelope"],
            entry["route"],
        )


def recheck(root: Path, held: Claim, *, clock: Clock = wall_clock) -> str | None:
    try:
        d = load(root, held.dispatch_id)
    except AgentError, OSError:
        return "store-unavailable"
    entry = notices.actionable(d, held.notice_id)
    if entry is None:
        return "stale"
    if dig(entry, "claim", "token") != held.token or clock() >= held.expires:
        return "claim-expired"
    return None


def _audit(entry: dict[str, Any], delivery: str, failure: str | None, duration: float) -> None:
    audit = as_list_or_none(entry.get("audit"))
    if audit is None:
        audit = entry["audit"] = list[object]()
    audit.append(
        {"at": notices.utc_stamp(), "delivery": delivery, "failure": failure, "duration_s": duration}
    )
    if entry.get("delivery") != "superseded":
        entry["delivery"] = delivery
        entry["failure"] = failure


def settle(
    root: Path,
    held: Claim,
    delivery: str,
    failure: str | None,
    *,
    clock: Clock = wall_clock,
    timeout: float = GUARD_TIMEOUT_S,
    consumption: str | None = None,
) -> Outcome:
    duration = round(max(0.0, clock() - held.started), 3)
    try:
        with step(bounded_record_guard(root, held.dispatch_id, timeout)) as d:
            entry = notices.outbox(d).get(held.notice_id)
            if (
                not isinstance(entry, dict)
                or dig(entry, "claim", "token") != held.token
                or entry.get("generation") != notices.generation(d)
            ):
                return Outcome(held.notice_id, delivery, failure or "claim-expired", duration, settled=False)
            entry["claim"] = None
            _audit(entry, delivery, failure, duration)
            if consumption in notices.CONSUMPTION_STATES and entry.get("consumption") == "none":
                entry["consumption"] = consumption
            save(root, d)
    except Unavailable as exc:
        return Outcome(held.notice_id, delivery, failure or exc.failure, duration, settled=False)
    return Outcome(held.notice_id, delivery, failure, duration, settled=True)


def route_target(root: Path, held: Claim) -> tuple[dict[str, Any] | None, str | None]:
    from ..ops import peers as ops_peers

    problem = notices.route_problem(held.route, held.envelope["harness"])
    if problem is not None:
        return (None, problem)
    sid = held.route["launched_by_session"]
    if sid != held.envelope["recipient"]:
        return (None, "route-unsupported")
    try:
        record = sessions_mod.get(root, sid)
    except AgentError, OSError:
        return (None, "store-unavailable")
    if not record:
        return (None, "route-missing")
    if record.get("dispatch_id"):
        return (None, "route-unsupported")
    if dig(record, "activity", "state") == "ended":
        return (None, "route-ended")
    provider = record.get("provider") or "claude"
    if provider == "codex":
        return (
            (record, None)
            if record.get("pid_unknown") or record_live(record, is_alive)
            else (None, "route-ended")
        )
    if provider != "claude":
        return (None, "route-unsupported")
    if not record_live(record, is_alive):
        return (None, "route-ended")
    address = ops_peers.inbox_address(record)
    if not address:
        return (None, "route-missing")
    if ops_peers.inbox_token(address) is None:
        return (None, "auth-unavailable")
    return (record, None)


def ring_dispatch(target: dict[str, Any], name: str) -> tuple[bool, str, str | None]:
    from ..ops import peers as ops_peers

    return ops_peers.ring(target, name, purpose="dispatch")


def deliver(
    root: Path,
    dispatch_id: str,
    nid: str,
    *,
    transport: Transport = ring_dispatch,
    clock: Clock = wall_clock,
    timeout: float = GUARD_TIMEOUT_S,
) -> Outcome:
    from ..ops import peer_wake

    began = clock()
    try:
        got = claim(root, dispatch_id, nid, clock=clock, timeout=timeout)
    except Unavailable as exc:
        return Outcome(nid, None, exc.failure, round(max(0.0, clock() - began), 3), settled=False)
    if isinstance(got, str):
        failure = got if got in FAILURES else None
        return Outcome(nid, None, failure, round(max(0.0, clock() - began), 3), settled=False, skipped=got)
    held = got

    def done(delivery: str, failure: str | None, consumption: str | None = None) -> Outcome:
        return settle(root, held, delivery, failure, clock=clock, timeout=timeout, consumption=consumption)

    failure = recheck(root, held, clock=clock)
    if failure is not None:
        return done("pending", failure)
    target, failure = route_target(root, held)
    if target is None:
        return done("fallback", failure)
    recipient = held.envelope["recipient"]
    suppressed = peer_wake.dispatch_ring_suppressed()
    deadline = time.monotonic() + timeout
    consumed = wake_id = None
    try:
        with step(peers_mod.inbox_guard(root, recipient, timeout=timeout)):
            left = peers_mod.time_left(deadline)
            status, _ = append(root, recipient, held.envelope, clock=clock, timeout=left)
            if status == "consumed":
                consumed = consumed_as(root, recipient, nid)
            elif not suppressed:
                wake_id = peer_wake.reserve(root, recipient, timeout=peers_mod.time_left(deadline))
    except Unavailable as exc:
        return done("fallback", exc.failure)
    if status == "consumed":
        return done("inbox-accepted", "stale", consumed)
    if suppressed:
        return done("inbox-accepted", "suppressed")
    if wake_id is None:
        return done("inbox-accepted", None)
    failure = recheck(root, held, clock=clock)
    if failure is not None:
        _settle_reservation(root, recipient, wake_id, False, timeout)
        return done("inbox-accepted", failure)
    accepted, _, ring_failure = transport(target, str(target.get("name") or recipient))
    _settle_reservation(root, recipient, wake_id, accepted, timeout)
    if accepted:
        return done("wake-accepted", None)
    return done("wake-failed", ring_failure if ring_failure in FAILURES else "transport-refused")


def _settle_reservation(root: Path, recipient: str, wake_id: str, accepted: bool, timeout: float) -> None:
    from ..ops import peer_wake

    try:
        peer_wake.settle(root, recipient, wake_id, accepted, timeout=timeout)
    except AgentError, OSError:
        return
