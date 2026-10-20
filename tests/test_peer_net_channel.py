"""The pairing wire is bounded everywhere and accepts only exact messages."""

from __future__ import annotations

import io
import os
import threading
import time

import pytest

from agent_cli.peer_net import channel
from agent_cli.peer_net.channel import MAX_LINE, MAX_MESSAGES, Clock, LineChannel, message
from agent_cli.peer_net.errors import PeerNetError

PAIR_ID = "a" * 32


def _channel(data: bytes) -> LineChannel:
    r, w = os.pipe()

    def feed() -> None:  # a pipe holds only a buffer's worth: the writer needs its own thread
        with os.fdopen(w, "wb") as out:
            try:
                out.write(data)
            except OSError:  # the reader stopped at its bound and closed its end
                pass

    threading.Thread(target=feed, daemon=True).start()
    return LineChannel(os.fdopen(r, "rb"), io.BytesIO())


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"not json\n", "pair-protocol"),
        (b'{"v": 2, "phase": "offer"}\n', "pair-protocol"),
        (b'{"v": true, "phase": "offer"}\n', "pair-protocol"),
        (b"[1]\n", "pair-protocol"),
        (b'{"v": 1, "phase": "offer"}', "pair-protocol"),  # unterminated: the stream ended
        (b"x" * (MAX_LINE + 10) + b"\n", "pair-protocol"),
        (b'{"v": 1, "phase": "error", "code": "off", "detail": "b"}\n', "off"),
        (b'{"v": 1, "phase": "error", "code": "Not A Code; rm", "detail": 3}\n', "pair-refused"),
        (b"", "pair-closed"),
    ],
)
def test_the_line_channel_refuses_anything_but_one_versioned_message(data, code):
    with pytest.raises(PeerNetError) as caught:
        _channel(data).recv(2)
    assert caught.value.code == code


def test_the_line_channel_times_out():
    r, w = os.pipe()
    try:
        with pytest.raises(PeerNetError) as caught:
            LineChannel(os.fdopen(r, "rb"), io.BytesIO()).recv(0.1)
        assert caught.value.code == "pair-timeout"
    finally:
        os.close(w)


def test_a_flood_is_read_only_up_to_the_message_bound():
    line = b'{"v": 1, "phase": "ack", "pair_id": "' + PAIR_ID.encode() + b'"}\n'
    r, w = os.pipe()
    writer = threading.Thread(target=lambda: _flood(w, line), daemon=True)
    writer.start()
    stream = os.fdopen(r, "rb")
    ch = LineChannel(stream, io.BytesIO())
    got = [ch.recv(2) for _ in range(MAX_MESSAGES)]
    assert len(got) == MAX_MESSAGES
    with pytest.raises(PeerNetError) as caught:
        ch.recv(2)
    assert caught.value.code == "pair-closed"  # the reader stopped; nothing more is held
    ch.reader.join(2)
    assert not ch.reader.is_alive()
    stream.close()  # the flooding writer now fails and ends
    writer.join(2)


def _flood(fd: int, line: bytes) -> None:
    try:
        while True:
            os.write(fd, line)
    except OSError:
        pass


def test_nothing_is_read_past_an_oversized_line():
    ch = _channel(b"x" * (MAX_LINE + 10) + b"\n" + b'{"v": 1}\n')
    with pytest.raises(PeerNetError):
        ch.recv(2)
    with pytest.raises(PeerNetError) as caught:
        ch.recv(2)
    assert caught.value.code == "pair-closed"


class _Stuck(io.RawIOBase):
    """A writer the other side never drains."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.release.wait(5)
        return len(data)


ACK = {"v": 1, "phase": "ack", "pair_id": PAIR_ID}


def test_a_blocked_write_times_out_and_breaks_the_channel():
    r, w = os.pipe()
    stuck = _Stuck()
    ch = LineChannel(os.fdopen(r, "rb"), stuck, write_timeout_s=0.2)  # type: ignore[arg-type]
    started = time.monotonic()
    with pytest.raises(PeerNetError) as caught:
        ch.send(ACK, 30)
    assert caught.value.code == "pair-timeout" and time.monotonic() - started < 2
    with pytest.raises(PeerNetError):
        ch.send(ACK, 30)
    ch.close()  # returns at once: the stuck write is left to the process's end
    stuck.release.set()
    os.close(w)


def test_a_blocked_write_gets_only_the_sessions_remaining_budget():
    r, w = os.pipe()
    stuck = _Stuck()
    ch = LineChannel(os.fdopen(r, "rb"), stuck)  # type: ignore[arg-type] -- the 30 s default
    started = time.monotonic()
    with pytest.raises(PeerNetError) as caught:
        ch.send(ACK, 0.1)  # what is left of the session, less than the write timeout
    assert caught.value.code == "pair-timeout" and time.monotonic() - started < 1
    ch.close()
    stuck.release.set()
    os.close(w)


class _Trickle(io.RawIOBase):
    """Takes at most three bytes per write: a short write is not a finished one."""

    def __init__(self) -> None:
        self.data = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        chunk = bytes(data[:3])
        self.data += chunk
        return len(chunk)


class _NoProgress(io.RawIOBase):
    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        return 0


def test_a_short_write_is_continued_until_the_whole_line_is_out():
    r, w = os.pipe()
    trickle = _Trickle()
    LineChannel(os.fdopen(r, "rb"), trickle).send(ACK, 5)  # type: ignore[arg-type]
    assert bytes(trickle.data) == b'{"v":1,"phase":"ack","pair_id":"' + PAIR_ID.encode() + b'"}\n'
    os.close(w)


def test_a_write_that_makes_no_progress_breaks_the_channel():
    r, w = os.pipe()
    ch = LineChannel(os.fdopen(r, "rb"), _NoProgress())  # type: ignore[arg-type]
    with pytest.raises(PeerNetError) as caught:
        ch.send(ACK, 5)
    assert caught.value.code == "pair-closed"
    with pytest.raises(PeerNetError):
        ch.send(ACK, 5)
    os.close(w)


def test_close_stops_delivery_and_the_reader_ends_with_the_stream():
    r, w = os.pipe()
    ch = LineChannel(os.fdopen(r, "rb"), io.BytesIO())
    ch.close()
    os.write(w, b'{"v": 1, "phase": "ack"}\n')
    os.close(w)
    ch.reader.join(2)
    assert not ch.reader.is_alive()
    with pytest.raises(PeerNetError) as caught:
        ch.recv(1)
    assert caught.value.code == "pair-closed"


def test_the_session_clock_bounds_the_whole_pairing(monkeypatch):
    clock = Clock(0.05)
    assert clock.left(30) <= 0.05
    time.sleep(0.06)
    with pytest.raises(PeerNetError) as caught:
        clock.left(30)
    assert caught.value.code == "pair-timeout"


def _ready(**over):
    return {"v": 1, "phase": "ready", "pair_id": PAIR_ID, "enable": False, **over}


@pytest.mark.parametrize(
    "raw",
    [
        _ready(enable="false"),  # a string is not a bool
        _ready(enable=1),
        _ready(pair_id="A" * 32),  # upper-case hex is not a pair id
        _ready(pair_id="a" * 31),
        _ready(extra=1),
        {"v": 1, "phase": "ready", "pair_id": PAIR_ID},  # a field missing
        _ready(v=True),
        _ready(phase="ack"),
        "ready",
    ],
)
def test_a_message_must_have_exactly_its_fields_and_types(raw):
    with pytest.raises(PeerNetError) as caught:
        message(raw, "ready")
    assert caught.value.code == "pair-protocol"


def test_hello_fields_are_exact():
    hello = {
        "v": 1,
        "phase": "hello",
        "pair_id": PAIR_ID,
        "restart": False,
        "a_alias": "win",
        "a_user": "user",
        "a_port": 22,
        "a_peer_pub": "k",
        "a_host_pub": "h",
    }
    assert message(hello, "hello") == hello
    for field, bad in [("a_user", None), ("restart", "false"), ("a_port", True), ("a_port", "22")]:
        with pytest.raises(PeerNetError):
            message({**hello, field: bad}, "hello")


def test_a_message_from_another_pairing_is_refused():
    with pytest.raises(PeerNetError):
        message(_ready(), "ready", "b" * 32)
    assert channel.FIELDS["ready"] == {"pair_id": str, "enable": bool}
