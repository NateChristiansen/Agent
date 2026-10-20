# ruff: noqa
from __future__ import annotations
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, BinaryIO
from ..records.narrow import as_dict_or_none
from .errors import PeerNetError

VERSION = 1
MAX_BYTES = 16384
MAX_TEXT_BYTES = 8192
READ_DEADLINE_S = 10.0
SKEW = timedelta(seconds=300)
MAX_TTL = timedelta(seconds=3600)
COMMON = {"v", "verb", "id", "sent_at", "expires_at"}
REQUIRED = {"enqueue": COMMON | {"to", "text", "from_session"}, "list": COMMON, "status": COMMON}
OPTIONAL: dict[str, set[str]] = {"enqueue": {"from_name"}, "list": set(), "status": set()}
ID_RE = re.compile("[0-9a-f]{32}")
SESSION_RE = re.compile("[A-Za-z0-9._-]{1,64}")
NAME_RE = re.compile("[A-Za-z0-9._ -]{0,64}")
CONTROL_RE = re.compile("[\\x00-\\x1f\\x7f]")


@dataclass(frozen=True)
class Envelope:
    verb: str
    id: str
    sent_at: datetime
    expires_at: datetime
    to: str = ""
    text: str = ""
    from_session: str = ""
    from_name: str = ""

    def digest(self) -> str:
        body = {
            "to": self.to,
            "text": self.text,
            "from_session": self.from_session,
            "from_name": self.from_name,
            "sent_at": self.sent_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_stdin(stream: BinaryIO, *, limit: int = MAX_BYTES, deadline: float = READ_DEADLINE_S) -> bytes:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["data"] = stream.read(limit + 1)
        except Exception as exc:
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        raise PeerNetError("timeout", "stdin")
    if "error" in box:
        raise PeerNetError("bad-input", "stdin")
    data = box.get("data") or b""
    if len(data) > limit:
        raise PeerNetError("oversized", "stdin")
    return data


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise PeerNetError("duplicate-key", "envelope")
        out[key] = value
    return out


def _constant(_name: str) -> Any:
    raise PeerNetError("bad-number", "envelope")


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if type(value) is not str:
        raise PeerNetError("bad-type", key)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise PeerNetError("bad-value", key) from None
    return value


def _time(data: dict[str, Any], key: str) -> datetime:
    value = _string(data, key)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise PeerNetError("bad-value", key) from None
    if parsed.tzinfo is None:
        raise PeerNetError("bad-value", key)
    return parsed


def parse(raw: bytes, *, now: datetime) -> Envelope:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise PeerNetError("bad-encoding", "envelope") from None
    try:
        data = as_dict_or_none(json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant))
    except ValueError:
        raise PeerNetError("bad-json", "envelope") from None
    if data is None:
        raise PeerNetError("not-object", "envelope")
    if type(data.get("v")) is not int or data["v"] != VERSION:
        raise PeerNetError("bad-version", "v")
    verb = data.get("verb")
    if type(verb) is not str or verb not in REQUIRED:
        raise PeerNetError("unknown-verb", "verb")
    missing = REQUIRED[verb] - set(data)
    if missing:
        raise PeerNetError("missing-key", sorted(missing)[0])
    extra = set(data) - REQUIRED[verb] - OPTIONAL[verb]
    if extra:
        raise PeerNetError("unknown-key", "envelope")
    msg_id = _string(data, "id")
    if not ID_RE.fullmatch(msg_id):
        raise PeerNetError("bad-value", "id")
    sent_at = _time(data, "sent_at")
    expires_at = _time(data, "expires_at")
    if sent_at > now + SKEW:
        raise PeerNetError("future", "sent_at")
    if expires_at <= sent_at or expires_at - sent_at > MAX_TTL:
        raise PeerNetError("bad-ttl", "expires_at")
    env = Envelope(verb=verb, id=msg_id, sent_at=sent_at, expires_at=expires_at)
    if verb != "enqueue":
        return env
    to = _string(data, "to")
    if not 1 <= len(to) <= 128 or CONTROL_RE.search(to):
        raise PeerNetError("bad-value", "to")
    body = _string(data, "text")
    if not body.strip() or len(body.encode("utf-8")) > MAX_TEXT_BYTES:
        raise PeerNetError("bad-value", "text")
    from_session = _string(data, "from_session")
    if not SESSION_RE.fullmatch(from_session):
        raise PeerNetError("bad-value", "from_session")
    from_name = _string(data, "from_name") if "from_name" in data else ""
    if not NAME_RE.fullmatch(from_name):
        raise PeerNetError("bad-value", "from_name")
    return Envelope(
        verb=verb,
        id=msg_id,
        sent_at=sent_at,
        expires_at=expires_at,
        to=to,
        text=body,
        from_session=from_session,
        from_name=from_name,
    )


def expired(env: Envelope, now: datetime) -> bool:
    return now > env.expires_at + SKEW
