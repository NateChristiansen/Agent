"""Dispatch supervisor notices: the version-1 envelope, its identity, and the source outbox
embedded in a dispatch record.

A notice tells the launching manager that a dispatch needs attention. It names the dispatch and
its persisted disposition and nothing else: no summary, question, error, command output or
worker text, so a notice can never carry what a worker wrote into a manager's context.

The outbox lives inside the dispatch record, so the guarded save that exposes a disposition
persists its intent too, with no second journal to keep in step. The helpers here only change
the record in memory; the caller holds the record guard and saves. Delivery is
`notice_delivery`'s.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core import AgentError, now
from ..peer_net.envelope import SESSION_RE
from ..records.narrow import as_dict_or_none, as_int, as_str
from . import ids
from .state import STATES

VERSION = 1
KIND = "dispatch.notice"
ORIGIN = "local-supervisor"
MAX_BYTES = 4096
MAX_DISPATCH_ID = 128

EVENT_CLASSES = ("terminal", "attention", "verification-complete")
TERMINAL_NOTICE_STATES = ("done", "blocked", "failed", "question")
VERIFICATION_STATUSES = ("verified", "failed", "incomplete", "unverified", "exempt")
ATTENTION_REASONS = ("question-pending", "cleanup-incomplete", "verification-incomplete")

CONSUMPTION_STATES = ("none", "hook-consumed", "show-consumed", "acknowledged")

ROUTE_VERSION = 1
ROUTE_LOCALITY = "local"

GENERATION_FIELD = "notice_generation"
FINGERPRINT_FIELD = "notice_fingerprint"
OUTBOX_FIELD = "notice_outbox"

FIELDS = (
    "version",
    "kind",
    "origin",
    "harness",
    "recipient",
    "dispatch_id",
    "feature",
    "attempt",
    "generation",
    "event_class",
    "state",
    "verification",
    "run_id",
    "cleanup_pending",
    "attention_reason",
    "recorded_at",
    "notice_id",
)

HARNESS_RE = re.compile(r"[0-9a-f]{16,64}")
RUN_ID_RE = re.compile(r"[0-9a-f]{32}")


class NoticeError(AgentError):
    """An envelope, disposition or route that no notice may carry."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def harness_identity(root: Path) -> str:
    """This installation's persisted identity, minted once on first use."""
    from .machine import install

    token = install(root)["token"]
    if not HARNESS_RE.fullmatch(token):
        raise NoticeError("this installation's identity is not a usable notice harness id")
    return token


def notice_id(
    harness: str, dispatch_id: str, attempt: int, event_class: str, generation: int, run_id: str | None
) -> str:
    return hashlib.sha256(
        canonical([VERSION, harness, dispatch_id, attempt, event_class, generation, run_id])
    ).hexdigest()


def assess_verification(value: object) -> str:
    """The notice's verification status. Anything not a known status reads as unverified."""
    return value if isinstance(value, str) and value in VERIFICATION_STATUSES else "unverified"


def utc_stamp(moment: datetime | None = None) -> str:
    return (moment or now()).astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Disposition:
    """The actionable facts a notice reports. Its fingerprint decides the generation."""

    attempt: int
    event_class: str
    state: str
    verification: str
    run_id: str | None
    cleanup_pending: bool
    attention_reason: str | None

    def fingerprint(self) -> str:
        return hashlib.sha256(canonical(list(astuple(self)))).hexdigest()

    def validate(self) -> None:
        if not _positive(self.attempt):
            raise NoticeError("notice attempt must be a positive integer")
        if self.event_class not in EVENT_CLASSES:
            raise NoticeError("unknown notice event class")
        if self.state not in STATES:
            raise NoticeError("unknown dispatch state")
        if self.event_class == "terminal" and self.state not in TERMINAL_NOTICE_STATES:
            raise NoticeError(f"a terminal notice cannot report state {self.state}")
        if self.verification not in VERIFICATION_STATUSES:
            raise NoticeError("unknown verification status")
        if self.run_id is not None and not (
            isinstance(self.run_id, str)  # pyright: ignore[reportUnnecessaryIsInstance] -- notices are read from disk
            and RUN_ID_RE.fullmatch(self.run_id)
        ):
            raise NoticeError("invalid verification run id")
        if self.event_class == "verification-complete" and self.run_id is None:
            raise NoticeError("a verification-complete notice needs its run id")
        if type(self.cleanup_pending) is not bool:
            raise NoticeError("cleanup_pending must be a boolean")
        if self.event_class == "attention":
            if self.attention_reason not in ATTENTION_REASONS:
                raise NoticeError("an attention notice needs a known attention reason")
        elif self.attention_reason is not None:
            raise NoticeError("only an attention notice carries an attention reason")


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _dispatch_id_ok(value: object) -> bool:
    return isinstance(value, str) and len(value) <= MAX_DISPATCH_ID and ids.ID_RE.fullmatch(value) is not None


def session_ok(value: object) -> bool:
    return isinstance(value, str) and SESSION_RE.fullmatch(value) is not None


def _utc_ok(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None and utc_stamp(parsed) == value


def build(
    *,
    harness: str,
    recipient: str,
    dispatch_id: str,
    feature: int | None,
    disposition: Disposition,
    generation: int,
    recorded_at: str,
) -> dict[str, Any]:
    """The internal constructor: the only way a `dispatch.notice` envelope is made."""
    envelope = {
        "version": VERSION,
        "kind": KIND,
        "origin": ORIGIN,
        "harness": harness,
        "recipient": recipient,
        "dispatch_id": dispatch_id,
        "feature": feature,
        "attempt": disposition.attempt,
        "generation": generation,
        "event_class": disposition.event_class,
        "state": disposition.state,
        "verification": disposition.verification,
        "run_id": disposition.run_id,
        "cleanup_pending": disposition.cleanup_pending,
        "attention_reason": disposition.attention_reason,
        "recorded_at": recorded_at,
        "notice_id": notice_id(
            harness, dispatch_id, disposition.attempt, disposition.event_class, generation, disposition.run_id
        ),
    }
    return validate(envelope)


def validate(envelope: object) -> dict[str, Any]:
    """The envelope unchanged, or NoticeError. Never trims an invalid envelope into a valid one."""
    notice = as_dict_or_none(envelope)
    if notice is None:
        raise NoticeError("a notice must be an object")
    if set(notice) != set(FIELDS):
        raise NoticeError("a notice has exactly the version-1 fields")
    if len(canonical(notice)) > MAX_BYTES:
        raise NoticeError(f"a notice is at most {MAX_BYTES} bytes")
    if type(notice["version"]) is not int or notice["version"] != VERSION:
        raise NoticeError("unsupported notice version")
    if notice["kind"] != KIND or notice["origin"] != ORIGIN:
        raise NoticeError("not a local dispatch notice")
    harness = notice["harness"]
    if not (isinstance(harness, str) and HARNESS_RE.fullmatch(harness)):
        raise NoticeError("invalid harness id")
    if not session_ok(notice["recipient"]):
        raise NoticeError("invalid recipient session id")
    if not _dispatch_id_ok(notice["dispatch_id"]):
        raise NoticeError("invalid dispatch id")
    if notice["feature"] is not None and not _positive(notice["feature"]):
        raise NoticeError("feature must be a positive integer or null")
    if not _positive(notice["generation"]):
        raise NoticeError("generation must be a positive integer")
    if not _utc_ok(notice["recorded_at"]):
        raise NoticeError("recorded_at must be a UTC ISO timestamp")
    disposition = disposition_of(notice)
    disposition.validate()
    expected = notice_id(
        harness,
        as_str(notice["dispatch_id"]),
        disposition.attempt,
        disposition.event_class,
        as_int(notice["generation"]),
        disposition.run_id,
    )
    if notice["notice_id"] != expected:
        raise NoticeError("notice id does not match its identity fields")
    return notice


def disposition_of(envelope: dict[str, Any]) -> Disposition:
    return Disposition(
        attempt=envelope["attempt"],
        event_class=envelope["event_class"],
        state=envelope["state"],
        verification=envelope["verification"],
        run_id=envelope["run_id"],
        cleanup_pending=envelope["cleanup_pending"],
        attention_reason=envelope["attention_reason"],
    )


def route(harness: str, launched_by_session: str) -> dict[str, Any]:
    """A version-1 local launch route, as L3 captures it at launch."""
    return {
        "version": ROUTE_VERSION,
        "locality": ROUTE_LOCALITY,
        "harness": harness,
        "launched_by_session": launched_by_session,
    }


def route_problem(value: object, harness: str) -> str | None:
    """The fixed failure class a launch route earns, or None when it names a local recipient."""
    if value is None:
        return "route-missing"
    launch = as_dict_or_none(value)
    if (
        launch is None
        or set(launch) != {"version", "locality", "harness", "launched_by_session"}
        or launch.get("version") != ROUTE_VERSION
        or launch.get("locality") != ROUTE_LOCALITY
        or launch.get("harness") != harness
        or not session_ok(launch.get("launched_by_session"))
    ):
        return "route-unsupported"
    return None


# ------------------------------------------------------------------ the record's outbox


def outbox(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value: dict[str, dict[str, Any]] | None = record.get(OUTBOX_FIELD)  # entries unchecked
    if not isinstance(value, dict):
        value = {}
        record[OUTBOX_FIELD] = value
    return value


def generation(record: dict[str, Any]) -> int:
    value = record.get(GENERATION_FIELD)
    return value if type(value) is int and value >= 0 else 0


def supersede(record: dict[str, Any], *, by: str | None = None, at: str | None = None) -> list[str]:
    """Mark every outstanding entry superseded, keeping its audit. Returns the ids changed."""
    changed: list[str] = []
    for nid, entry in outbox(record).items():
        if nid == by or entry.get("delivery") == "superseded":
            continue
        entry["delivery"] = "superseded"
        entry["claim"] = None
        entry["superseded_by"] = by
        entry["superseded_at"] = at or utc_stamp()
        changed.append(nid)
    return changed


def record_intent(
    record: dict[str, Any],
    disposition: Disposition,
    *,
    harness: str,
    launch_route: object,
    consumption: str = "none",
) -> dict[str, Any]:
    """Add the intent for `disposition` to `record`'s outbox, or return the existing entry when
    its fingerprint is unchanged. Call inside the caller's record guard, before its save.

    A changed fingerprint advances the generation and supersedes every older entry. A route that
    names no local recipient still records the intent, as a fallback with no envelope.
    `consumption="show-consumed"` records an intent the caller has already shown its recipient.
    """
    disposition.validate()
    if consumption not in CONSUMPTION_STATES:
        raise NoticeError("unknown notice consumption state")
    fingerprint = disposition.fingerprint()
    current = generation(record)
    entries = outbox(record)
    if record.get(FINGERPRINT_FIELD) == fingerprint and current > 0:
        nid = notice_id(
            harness, record["id"], disposition.attempt, disposition.event_class, current, disposition.run_id
        )
        if nid in entries:
            return entries[nid]
    gen = current + 1
    recorded_at = utc_stamp()
    nid = notice_id(
        harness, record["id"], disposition.attempt, disposition.event_class, gen, disposition.run_id
    )
    problem = route_problem(launch_route, harness)
    launch = as_dict_or_none(launch_route)
    envelope = None
    if problem is None and launch is not None:
        feature = record.get("feature")
        envelope = build(
            harness=harness,
            recipient=as_str(launch["launched_by_session"]),
            dispatch_id=record["id"],
            feature=feature if _positive(feature) else None,
            disposition=disposition,
            generation=gen,
            recorded_at=recorded_at,
        )
    supersede(record, by=nid, at=recorded_at)
    entry: dict[str, Any] = {
        "notice_id": nid,
        "generation": gen,
        "fingerprint": fingerprint,
        "recorded_at": recorded_at,
        "envelope": envelope,
        "route": dict(launch) if launch is not None else None,
        "delivery": "pending" if problem is None else "fallback",
        "failure": problem,
        "consumption": consumption,
        "claim": None,
        "superseded_by": None,
        "audit": [],
    }
    entries[nid] = entry
    record[GENERATION_FIELD] = gen
    record[FINGERPRINT_FIELD] = fingerprint
    if consumption == "none":
        record["chat_pending"] = True
    return entry


def actionable(record: dict[str, Any], nid: str) -> dict[str, Any] | None:
    """The entry when it is the current generation, unconsumed and not superseded."""
    entry = outbox(record).get(nid)
    if (
        not isinstance(entry, dict)
        or entry.get("generation") != generation(record)
        or entry.get("delivery") == "superseded"
        or entry.get("consumption") != "none"
    ):
        return None
    return entry
