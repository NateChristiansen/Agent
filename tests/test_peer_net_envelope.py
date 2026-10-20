"""The cross-device envelope is refused unless it is exactly the documented shape."""

from __future__ import annotations

import io
import time
from datetime import timedelta
from typing import BinaryIO, cast

import pytest

from agent_cli.peer_net import envelope as env_mod
from agent_cli.peer_net.errors import PeerNetError
from peer_net_helpers import NOW, envelope, raw


def _code(data: bytes, now=NOW) -> str:
    with pytest.raises(PeerNetError) as caught:
        env_mod.parse(data, now=now)
    return caught.value.code


def test_a_valid_enqueue_parses():
    env = env_mod.parse(raw(envelope()), now=NOW)
    assert env.verb == "enqueue" and env.text == "hello from the mac" and env.from_name == "mgr-001"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"\xff\xfe", "bad-encoding"),
        (b"{not json", "bad-json"),
        (b"[1, 2]", "not-object"),
        (b'{"v": 1, "v": 1}', "duplicate-key"),
        (b'{"v": NaN}', "bad-number"),
        (b'{"v": Infinity}', "bad-number"),
    ],
)
def test_malformed_input_is_refused_with_its_own_code(data, code):
    assert _code(data) == code


def test_version_must_be_the_int_one_and_never_a_bool():
    assert _code(raw(envelope(v=True))) == "bad-version"
    assert _code(raw(envelope(v=2))) == "bad-version"
    assert _code(raw(envelope(v="1"))) == "bad-version"


def test_an_unknown_verb_or_key_is_refused():
    assert _code(raw(envelope(verb="exec"))) == "unknown-verb"
    assert _code(raw(envelope(device="mac"))) == "unknown-key"  # the envelope cannot name a device
    assert _code(raw(envelope(text=...))) == "missing-key"


def test_list_and_status_take_only_the_common_keys():
    common = {k: envelope()[k] for k in ("v", "id", "sent_at", "expires_at")}
    assert env_mod.parse(raw({**common, "verb": "list"}), now=NOW).verb == "list"
    assert _code(raw({**common, "verb": "status", "to": "x"})) == "unknown-key"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "A" * 32),
        ("id", "abc"),
        ("to", ""),
        ("to", "x" * 129),
        ("to", "bad\nname"),
        ("text", ""),
        ("text", "é" * 4097),  # 8194 bytes as UTF-8, under 8192 characters
        ("text", "\ud800"),
        ("from_session", "a/b"),
        ("from_name", "mgr\x1b[31m"),
        ("from_name", "slash/name"),
    ],
)
def test_field_values_are_bounded(field, value):
    assert _code(raw(envelope(**{field: value}))) == "bad-value"


def test_exact_types_are_required():
    assert _code(raw(envelope(text=5))) == "bad-type"
    assert _code(raw(envelope(to=None))) == "bad-type"


def test_time_bounds_are_inclusive_as_documented():
    future = NOW + timedelta(seconds=300)
    assert env_mod.parse(
        raw(envelope(sent_at=future.isoformat(), expires_at=(future + timedelta(1 / 24)).isoformat())),
        now=NOW,
    )
    too_far = NOW + timedelta(seconds=301)
    assert (
        _code(
            raw(
                envelope(sent_at=too_far.isoformat(), expires_at=(too_far + timedelta(minutes=1)).isoformat())
            )
        )
        == "future"
    )
    assert _code(raw(envelope(expires_at=NOW.isoformat()))) == "bad-ttl"
    assert _code(raw(envelope(expires_at=(NOW + timedelta(seconds=3601)).isoformat()))) == "bad-ttl"
    assert env_mod.parse(raw(envelope(expires_at=(NOW + timedelta(seconds=3600)).isoformat())), now=NOW)
    assert _code(raw(envelope(sent_at="2026-09-03T14:05:00"))) == "bad-value"  # no offset


def test_expiry_allows_the_skew_and_no_more():
    env = env_mod.parse(raw(envelope()), now=NOW)
    assert not env_mod.expired(env, env.expires_at + timedelta(seconds=300))
    assert env_mod.expired(env, env.expires_at + timedelta(seconds=301))


def test_the_digest_covers_the_content_not_the_id():
    a = env_mod.parse(raw(envelope(id="a" * 32)), now=NOW)
    b = env_mod.parse(raw(envelope(id="b" * 32)), now=NOW)
    c = env_mod.parse(raw(envelope(id="a" * 32, text="changed")), now=NOW)
    assert a.digest() == b.digest() != c.digest()


def test_stdin_is_bounded_in_bytes():
    assert env_mod.read_stdin(io.BytesIO(b"x" * 16384)) == b"x" * 16384
    with pytest.raises(PeerNetError) as caught:
        env_mod.read_stdin(io.BytesIO(b"x" * 16385))
    assert caught.value.code == "oversized"


def test_stdin_is_bounded_in_time():
    class Slow(io.RawIOBase):
        def read(self, n=-1):
            time.sleep(2)
            return b""

    with pytest.raises(PeerNetError) as caught:
        env_mod.read_stdin(cast(BinaryIO, Slow()), deadline=0.1)
    assert caught.value.code == "timeout"
