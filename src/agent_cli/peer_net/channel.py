"""The pairing session's wire: line-framed JSON with hard bounds, and the exact shape of every
message.

Every bound is local and fails closed. A message is at most 16 KiB, the reader stops after
MAX_MESSAGES lines, a write that does not finish in time breaks the channel, and the whole
pairing has one deadline (`Clock`). A blocked read ends when the other side's process ends;
the CLI that opens the SSH session (slice 3b) terminates it on abort, so its own process
timeout complements these bounds rather than standing in for them.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from typing import Any, BinaryIO, Protocol

from ..records.narrow import as_dict_or_none
from .errors import PeerNetError

VERSION = 1
MAX_LINE = 16 * 1024
MAX_MESSAGES = 8  # a pairing exchanges four messages each way
WRITE_TIMEOUT_S = 30.0
PAIR_ID = re.compile(r"[0-9a-f]{32}")
ERROR_CODE = re.compile(r"[a-z][a-z0-9-]{0,39}")

# Every message's fields beyond `v` and `phase`, with their exact types (bool is not int).
FIELDS: dict[str, dict[str, type]] = {
    "hello": {
        "pair_id": str,
        "restart": bool,
        "a_alias": str,
        "a_user": str,
        "a_port": int,
        "a_peer_pub": str,
        "a_host_pub": str,
    },
    "offer": {"pair_id": str, "b_alias": str, "b_peer_pub": str},
    "ready": {"pair_id": str, "enable": bool},
    "ack": {"pair_id": str},
    "probe-back": {"pair_id": str},
    "probed": {"pair_id": str, "verified": bool, "answer": dict},
    "done": {"pair_id": str},
    "bye": {"pair_id": str},
}


class Session(Protocol):
    def send(self, message: dict[str, Any], timeout: float) -> None: ...
    def recv(self, timeout: float) -> Any: ...
    def close(self) -> None: ...


class Clock:
    """One deadline for the whole pairing, whatever each message's own timeout."""

    def __init__(self, total_s: float) -> None:
        self.end = time.monotonic() + total_s

    def remaining(self) -> float:
        return max(0.0, self.end - time.monotonic())

    def cap(self, total_s: float) -> None:
        """From now on at most `total_s` more: the deadline only ever moves earlier."""
        self.end = min(self.end, time.monotonic() + total_s)

    def left(self, per_step_s: float) -> float:
        """The budget for the next step (a read, a write, a probe, a state transition), or
        refused once the pairing's deadline has passed."""
        remaining = self.remaining()
        if remaining <= 0:
            raise PeerNetError("pair-timeout", "the pairing took too long")
        return min(per_step_s, remaining)


def message(value: object, phase: str, pair_id: str | None = None) -> dict[str, Any]:
    """`value` as the `phase` message, or refused: the version, the phase, exactly its fields
    with their exact types, and the pairing's id. An `error` message raises its code."""
    raw = as_dict_or_none(value)
    if raw is None or type(raw.get("v")) is not int or raw["v"] != VERSION:
        raise PeerNetError("pair-protocol", "unknown message version")
    if raw.get("phase") == "error":
        code = raw.get("code")
        detail = raw.get("detail")
        raise PeerNetError(
            code if isinstance(code, str) and ERROR_CODE.fullmatch(code) else "pair-refused",
            detail[:200] if isinstance(detail, str) else "the other side refused",
        )
    if raw.get("phase") != phase:
        raise PeerNetError("pair-protocol", f"expected {phase}")
    spec = FIELDS[phase]
    if set(raw) != {"v", "phase", *spec}:
        raise PeerNetError("pair-protocol", f"{phase} fields")
    for field, kind in spec.items():
        if type(raw[field]) is not kind:
            raise PeerNetError("pair-protocol", f"{phase} {field}")
    got = raw["pair_id"]
    if not isinstance(got, str) or not PAIR_ID.fullmatch(got) or (pair_id is not None and got != pair_id):
        raise PeerNetError("pair-protocol", f"{phase} pair_id")
    return raw


class LineChannel:
    """Line-framed JSON over a pair of byte streams. A reader thread makes per-message
    deadlines work on pipes on every OS; it holds at most MAX_MESSAGES lines and never reads
    past an oversized or unterminated one."""

    def __init__(
        self, reader: BinaryIO, writer: BinaryIO, *, write_timeout_s: float = WRITE_TIMEOUT_S
    ) -> None:
        self.writer = writer
        self.write_timeout_s = write_timeout_s
        self.lines: queue.Queue[bytes | None] = queue.Queue(maxsize=MAX_MESSAGES + 1)
        self.stopped = threading.Event()
        self.broken = False
        self.writing = False
        self.reader = threading.Thread(target=self._pump, args=(reader,), daemon=True)
        self.reader.start()

    def _pump(self, reader: BinaryIO) -> None:
        try:
            for _ in range(MAX_MESSAGES):
                line = reader.readline(MAX_LINE + 1)
                if not line or self.stopped.is_set():
                    return
                self.lines.put_nowait(line)  # never full: at most MAX_MESSAGES lines and the end
                if not line.endswith(b"\n"):
                    return  # oversized or unterminated: nothing after it is read
        except OSError, ValueError:
            return
        finally:
            self.lines.put_nowait(None)

    def send(self, message: dict[str, Any], timeout: float) -> None:
        """Write one message completely within min(timeout, write_timeout_s). A short write
        is continued; a write that makes no progress, fails or runs out of time breaks the
        channel."""
        if self.broken:
            raise PeerNetError("pair-closed", "the channel is closed")
        data = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) > MAX_LINE:
            raise PeerNetError("pair-protocol", "message too large")
        finished = threading.Event()
        failed: list[BaseException] = []

        def write() -> None:
            try:
                view = memoryview(data)
                while view:
                    written = self.writer.write(view)
                    if not written:
                        raise OSError("write made no progress")
                    view = view[written:]
                self.writer.flush()
            except BaseException as exc:  # noqa: BLE001 -- reported to the sender below
                failed.append(exc)
            finally:
                finished.set()

        self.writing = True
        threading.Thread(target=write, daemon=True).start()
        if not finished.wait(max(0.0, min(timeout, self.write_timeout_s))):
            self.broken = True  # the write thread stays blocked until the process ends
            raise PeerNetError("pair-timeout", "the other side stopped reading")
        self.writing = False
        if failed:
            self.broken = True
            raise PeerNetError("pair-closed", type(failed[0]).__name__)

    def recv(self, timeout: float) -> dict[str, Any]:
        if self.broken:
            raise PeerNetError("pair-closed", "the channel is closed")
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            raise PeerNetError("pair-timeout", "no reply in time") from None
        if line is None:
            self.broken = True
            raise PeerNetError("pair-closed", "the other side ended the session")
        if len(line) > MAX_LINE or not line.endswith(b"\n"):
            self.broken = True
            raise PeerNetError("pair-protocol", "oversized or unterminated message")
        try:
            decoded = as_dict_or_none(json.loads(line))
        except ValueError:
            raise PeerNetError("pair-protocol", "malformed message") from None
        if decoded is None or type(decoded.get("v")) is not int or decoded["v"] != VERSION:
            raise PeerNetError("pair-protocol", "unknown message version")
        if decoded.get("phase") == "error":
            message(decoded, "error")  # raises the other side's code
        return decoded

    def close(self) -> None:
        """Stop delivering and end the write side. A write still blocked holds the writer's
        lock, so it is left to the process's end rather than closed under it."""
        self.stopped.set()
        self.broken = True
        if self.writing:
            return
        try:
            self.writer.close()
        except OSError, ValueError:
            pass
