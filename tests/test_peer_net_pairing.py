"""The pairing protocol pairs two devices in one owner-authenticated session, resumes after
any lost message or crash without `recover`, never downgrades a grant or reverses the owner's
off or revoke, and verifies a route only by its own probe."""

from __future__ import annotations

import os
import queue
import sys
import time
from pathlib import Path

import pytest

import peer_net_pair_fixture
from agent_cli.peer_net import authorized, enroll, grants, intents, mutate, pair_state, pairing, routes, ssh
from agent_cli.peer_net.errors import PeerNetError
from agent_cli.peer_net.recover import recover
from peer_net_pair_fixture import Devices, End, as_b, crash_on, host_line, make_devices, on_b

GONE = {"pair-timeout", "pair-closed"}


@pytest.fixture
def devices(tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Devices:
    return make_devices(tmp_root, tmp_path, monkeypatch)


def _paired(d: Devices) -> None:
    assert d.grant(d.a, "mac") == "active" and d.grant(d.b, "win") == "active"
    assert d.route(d.a, "mac") == ("192.0.2.9", 1)
    assert d.route(d.b, "win") == ("192.0.2.5", 1)  # A's address as sshd saw it
    assert d.open_intents(d.a) == [] and d.open_intents(d.b) == []


def test_pair_grants_routes_and_verifies_both_directions(devices):
    result = devices.pair()
    assert result["verified"] is True and result["their_probe"] == {"verified": True}
    assert devices.b_result["ok"]["verified"] is True
    assert devices.enabled(devices.a) and devices.enabled(devices.b)
    _paired(devices)


def test_pairing_again_is_a_no_op_that_reverifies(devices):
    devices.pair()
    assert devices.pair()["verified"] is True
    _paired(devices)


def test_without_enable_nothing_is_turned_on_and_nothing_verifies(devices):
    result = devices.pair(enable=False)
    assert result["verified"] is False and result["their_probe"] == {"verified": False}
    assert not devices.enabled(devices.a) and not devices.enabled(devices.b)
    assert devices.grant(devices.a, "mac") == "active" and devices.grant(devices.b, "win") == "active"
    enroll.set_enabled(devices.a, True)
    as_b(lambda: enroll.set_enabled(devices.b, True))
    assert devices.pair(enable=False)["verified"] is True
    _paired(devices)


def test_an_unconfirmed_fingerprint_writes_nothing(devices):
    assert devices.pair_error(confirm=False) == "not-confirmed"
    assert devices.route(devices.a, "mac") is None and devices.open_intents(devices.a) == []
    assert devices.b_thread is None  # no session was opened


def test_a_different_device_on_the_other_end_is_refused(devices):
    assert devices.pair_error(b_alias="linux") == "pair-alias-mismatch"
    assert devices.grant(devices.a, "linux") is None


# --- lost messages and crashes: a re-run repairs, never `recover` -----------------------------

TO_B = ["hello", "ready", "probe-back", "done"]
TO_A = ["offer", "ack", "probed"]  # a lost bye: test_a_lost_bye_leaves_b_complete_and_a_resumes


@pytest.mark.parametrize(("direction", "phase"), [("to_b", p) for p in TO_B] + [("to_a", p) for p in TO_A])
def test_any_lost_message_times_out_and_pairing_again_completes(devices, direction, phase):
    (devices.drop_to_b if direction == "to_b" else devices.drop_to_a).add(phase)
    assert devices.pair_error() in GONE
    assert devices.open_intents(devices.a)  # only bye closes A's record
    devices.pair()
    _paired(devices)


def test_a_lost_bye_leaves_b_complete_and_a_resumes(devices):
    devices.drop_to_a.add("bye")
    devices.pair_error()
    assert devices.open_intents(devices.b) == []  # B recorded completion first
    pair_id = devices.open_intents(devices.a)[0].payload["pair_id"]
    assert devices.pair()["pair_id"] == pair_id
    _paired(devices)


@pytest.mark.parametrize("phase", ["ready", "probe-back", "done"])
def test_b_dying_on_a_message_is_repaired_by_pairing_again(devices, phase):
    devices.b_crash = phase
    assert devices.pair_error() in GONE
    assert devices.b_result == {"error": "RuntimeError"}
    devices.b_crash = None
    devices.pair()
    _paired(devices)


@pytest.mark.parametrize("side", ["A", "B"])
def test_a_step_slower_than_the_message_window_is_not_a_lost_message(devices, monkeypatch, side):
    monkeypatch.setattr(peer_net_pair_fixture, "PEER_WORKING_S", peer_net_pair_fixture.WINDOWS_PEER_WORKING_S)
    real = grants.install

    def slow(*args, **kwargs):
        if (side == "B") == on_b():
            time.sleep(pairing.MESSAGE_TIMEOUT_S + 0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(grants, "install", slow)
    devices.b_crash = "ready"
    assert devices.pair_error() in GONE
    assert devices.b_result == {"error": "RuntimeError"}


def _a_reads(monkeypatch) -> list[float]:
    """How long each of A's reads takes."""
    took: list[float] = []
    real = End._next

    def timed(self, timeout):
        start = time.monotonic()
        try:
            return real(self, timeout)
        finally:
            if not on_b():
                took.append(time.monotonic() - start)

    monkeypatch.setattr(End, "_next", timed)
    return took


def test_a_working_peer_that_never_replies_times_out_at_the_fixture_bound(devices, monkeypatch):
    real = grants.install

    def stalled(*args, **kwargs):
        if on_b():  # B works on its offer until A gives up
            given_up = time.monotonic() + 10
            while not devices.a_end.closed and time.monotonic() < given_up:
                time.sleep(0.01)
        return real(*args, **kwargs)

    monkeypatch.setattr(grants, "install", stalled)
    took = _a_reads(monkeypatch)
    assert devices.pair_error() == "pair-timeout"
    bound = pairing.MESSAGE_TIMEOUT_S + peer_net_pair_fixture.PEER_WORKING_S
    assert len(took) == 1 and bound <= took[0] < bound + 1.0
    assert devices.b_result == {"error": "pair-closed"}


def test_on_posix_a_reply_after_the_window_is_a_lost_message(devices, monkeypatch):
    monkeypatch.setattr(pairing, "MESSAGE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(peer_net_pair_fixture, "PEER_WORKING_S", 0.0)  # POSIX's values
    real = grants.install

    def slow(*args, **kwargs):
        if on_b():
            time.sleep(0.6)
        return real(*args, **kwargs)

    monkeypatch.setattr(grants, "install", slow)
    took = _a_reads(monkeypatch)
    assert devices.pair_error() == "pair-timeout"
    assert len(took) == 1 and 0.3 <= took[0] < 0.8


class _LateWake(queue.Queue):
    """A's inbox: a wait that has run out resumes only once the reply has landed, as if A
    was descheduled between its timeout and its next look at the pipe."""

    def __init__(self) -> None:
        super().__init__()
        self.woke_late = False

    def get(self, block=True, timeout=None):
        try:
            return super().get(block, timeout)
        except queue.Empty:
            if block:
                given_up = time.monotonic() + 5
                while self.empty() and time.monotonic() < given_up:
                    time.sleep(0.01)
                self.woke_late = not self.empty()
            raise


def test_on_posix_a_reply_landing_after_the_window_expired_is_still_lost(devices, monkeypatch):
    monkeypatch.setattr(pairing, "MESSAGE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(peer_net_pair_fixture, "PEER_WORKING_S", 0.0)  # POSIX's values
    real_install, real_session = grants.install, devices.session
    inbox = _LateWake()

    def slow(*args, **kwargs):
        if on_b():
            time.sleep(0.6)
        return real_install(*args, **kwargs)

    def session():
        a_end = real_session()
        a_end.inbox.q = inbox
        return a_end

    monkeypatch.setattr(grants, "install", slow)
    monkeypatch.setattr(devices, "session", session)
    assert devices.pair_error() == "pair-timeout"
    assert inbox.woke_late  # the offer was already waiting when A timed out


# Each point is a step inside a write, so a crash there leaves a half-done route or grant op.
CRASH_POINTS = [
    ("pin temp write", mutate, "write_private", False),
    ("pin published, route not inserted", os, "replace", True),
    ("pin not published", os, "replace", False),
    ("grant backup", mutate, "backup", False),
    ("grant line not written", authorized, "rewrite_in_place", False),
    ("grant line written, op open", authorized, "rewrite_in_place", True),
    ("grant activation", grants, "activate", False),
    ("grant activated, step not recorded", grants, "activate", True),
]


@pytest.mark.parametrize("side", ["A", "B"])
@pytest.mark.parametrize(("label", "target", "name", "after"), CRASH_POINTS, ids=[p[0] for p in CRASH_POINTS])
def test_a_crash_inside_any_write_is_repaired_by_pairing_again(
    devices, monkeypatch, side, label, target, name, after
):
    crash_on(monkeypatch, target, name, side, after=after)
    if side == "A":
        with pytest.raises(RuntimeError):
            devices.pair()
    else:
        assert devices.pair_error() in GONE
        assert devices.b_result == {"error": "RuntimeError"}
    devices.pair()  # the same command again; no recover
    _paired(devices)


@pytest.mark.parametrize("after", [False, True], ids=["before completion", "after completion"])
def test_b_crashing_around_its_completion_is_repaired(devices, monkeypatch, after):
    crash_on(monkeypatch, pair_state, "finish", "B", after=after)
    assert devices.pair_error() in GONE
    assert bool(devices.open_intents(devices.b)) is not after
    devices.pair()
    _paired(devices)


def test_b_crashing_after_completion_before_bye_is_repaired(devices):
    devices.b_crash_send = "bye"
    assert devices.pair_error() in GONE
    assert devices.open_intents(devices.b) == []
    devices.b_crash_send = None
    devices.pair()
    _paired(devices)


def test_a_crash_after_a_records_completion_needs_no_repair(devices, monkeypatch):
    crash_on(monkeypatch, pair_state, "finish", "A", after=True)
    with pytest.raises(RuntimeError):
        devices.pair()
    _paired(devices)


# --- grants, probes and pairing ids -------------------------------------------------------------


def test_a_grant_is_never_downgraded_by_a_failed_repair(devices):
    devices.pair()
    devices.drop_to_b.add("ready")
    devices.pair_error()
    assert devices.grant(devices.a, "mac") == "active" and devices.grant(devices.b, "win") == "active"


def test_a_failed_probe_leaves_the_route_unverified_and_a_failed_reprobe_clears_it(devices):
    devices.failing_probe.add("mac")
    assert devices.pair()["verified"] is False
    assert devices.route(devices.a, "mac") == ("192.0.2.9", 0)
    devices.failing_probe.clear()
    devices.pair()
    assert devices.route(devices.a, "mac") == ("192.0.2.9", 1)
    as_b(lambda: enroll.set_enabled(devices.b, False))
    assert devices.pair(enable=False)["verified"] is False
    assert devices.route(devices.a, "mac") == ("192.0.2.9", 0)


def test_a_second_pairing_is_refused_while_another_is_open_unless_restarted(devices):
    devices.drop_to_b.add("done")
    devices.pair_error()
    (a_open,) = devices.open_intents(devices.a)
    recover(devices.a, a_open.op_id, "abandon")  # A forgets; B still holds the old pair_id
    assert devices.pair_error() == "pair-in-progress"
    devices.pair(restart=True)
    _paired(devices)
    # The refused attempt wrote nothing on B; the restart kept the old record as superseded.
    assert sorted(i.state for i in _all_intents(devices.b, "accept")) == ["done", "superseded"]


def _all_intents(root: Path, op: str) -> list[intents.Intent]:
    conn = enroll.store.connect(root)
    try:
        rows = conn.execute("SELECT op_id FROM intents WHERE op=?", (op,)).fetchall()
        return [i for i in (intents.get(conn, r[0]) for r in rows) if i is not None]
    finally:
        conn.close()


# --- the owner's denial during a stalled pairing -----------------------------------------------


def test_a_revoke_while_the_pairing_stalls_is_not_reversed_by_resuming(devices):
    # A stalls on ack; meanwhile the owner revokes A on B. The guard is free, so it runs at once.
    devices.a_on_recv["ack"] = lambda: as_b(lambda: enroll.revoke(devices.b, "win"))
    assert devices.pair_error() == "pair-denied"
    assert devices.grant(devices.b, "win") is None and devices.route(devices.b, "win") is None
    assert devices.pair_error() == "pair-denied"  # resuming the same pairing
    assert devices.grant(devices.b, "win") is None and devices.route(devices.b, "win") is None
    devices.pair(restart=True)  # a new decision by the owner
    _paired(devices)


def test_an_off_before_the_pairing_enables_stops_it_from_turning_on(devices):
    devices.a_on_recv["offer"] = lambda: enroll.set_enabled(devices.a, False)
    assert devices.pair_error() == "pair-denied"
    assert not devices.enabled(devices.a)
    assert devices.pair_error() == "pair-denied"  # resuming with --enable is still refused
    assert not devices.enabled(devices.a)
    devices.pair(enable=False)  # resuming without --enable completes, and leaves it off
    assert not devices.enabled(devices.a) and devices.open_intents(devices.a) == []
    devices.pair(restart=True)
    assert devices.enabled(devices.a)


def test_an_off_after_the_pairing_enabled_stays_off_when_it_resumes(devices):
    devices.b_on_recv["probe-back"] = lambda: enroll.set_enabled(devices.b, False)  # B's own thread
    devices.drop_to_a.add("bye")  # A's record stays open, so the next pair resumes it
    devices.pair_error()
    assert not devices.enabled(devices.b)
    devices.pair()
    assert not devices.enabled(devices.b)  # the pairing already turned it on once; never again
    assert devices.open_intents(devices.a) == [] and devices.open_intents(devices.b) == []


def test_a_revoke_during_a_restarted_pairing_is_not_reversed_by_resuming_it(devices):
    # Reproduced: the resumed hello still carried restart=True, and B
    # took each retry as a fresh restart, dropped the denial and granted A again.
    devices.a_on_recv["ack"] = lambda: as_b(lambda: enroll.revoke(devices.b, "win"))
    assert devices.pair_error(restart=True) == "pair-denied"
    assert devices.grant(devices.b, "win") is None
    assert devices.pair_error() == "pair-denied"  # no new restart choice: a resume
    assert devices.grant(devices.b, "win") is None and devices.route(devices.b, "win") is None
    devices.pair(restart=True)  # the owner's new, explicit restart
    _paired(devices)


def test_an_off_before_a_restarted_pairing_enables_stays_in_force(devices):
    devices.b_on_recv["ready"] = lambda: enroll.set_enabled(devices.b, False)  # before B enables
    assert devices.pair_error(restart=True) == "pair-denied"
    assert not devices.enabled(devices.b)
    assert devices.pair_error() == "pair-denied"
    assert not devices.enabled(devices.b)


def test_an_off_after_a_restarted_pairing_enabled_stays_off_when_it_resumes(devices):
    devices.b_on_recv["probe-back"] = lambda: enroll.set_enabled(devices.b, False)
    devices.drop_to_a.add("bye")
    devices.pair_error(restart=True)
    assert not devices.enabled(devices.b)
    devices.pair()
    assert not devices.enabled(devices.b)


# --- one deadline bounds the whole pairing ------------------------------------------------------


def test_every_write_and_probe_gets_at_most_the_remaining_budget(devices, monkeypatch):
    monkeypatch.setattr(pairing, "SESSION_TIMEOUT_S", 3.0)
    monkeypatch.setattr(pairing, "MESSAGE_TIMEOUT_S", 5.0)  # each step's own cap exceeds the budget
    devices.pair()
    sent = devices.a_end.sent_timeouts + devices.b_end.sent_timeouts
    assert sent and all(0 < t <= 3.0 for t in sent)
    assert devices.probe_timeouts and all(t is not None and 0 < t <= 3.0 for t in devices.probe_timeouts)


def test_no_transition_happens_after_the_deadline(devices, monkeypatch):
    monkeypatch.setattr(pairing, "SESSION_TIMEOUT_S", 0.5)
    monkeypatch.setattr(pairing, "MESSAGE_TIMEOUT_S", 2.0)
    devices.a_on_recv["ack"] = lambda: time.sleep(0.6)  # A stalls past its deadline
    assert devices.pair_error() == "pair-timeout"
    assert devices.probe_timeouts == []  # A never probed after the deadline
    assert devices.route(devices.a, "mac") == ("192.0.2.9", 0)
    assert devices.grant(devices.a, "mac") == "pending"  # and never activated B


def test_the_error_reply_is_short_and_skipped_after_the_deadline(devices, monkeypatch):
    channel = Script(_hello(a_user=None))
    with pytest.raises(PeerNetError):
        as_b(lambda: pairing.accept(devices.b, channel, env={"SSH_CONNECTION": "192.0.2.5 1 192.0.2.9 22"}))
    assert channel.timeouts == [pytest.approx(pairing.ERROR_REPLY_S, abs=0.1)]
    monkeypatch.setattr(pairing, "SESSION_TIMEOUT_S", 0.1)
    late = Script(_hello(), delay=0.2)
    with pytest.raises(PeerNetError) as caught:
        as_b(lambda: pairing.accept(devices.b, late, env={"SSH_CONNECTION": "192.0.2.5 1 192.0.2.9 22"}))
    assert caught.value.code == "pair-timeout"
    assert late.sent == [] and devices.open_intents(devices.b) == []


def test_a_slow_login_still_leaves_the_protocol_its_whole_budget(devices, monkeypatch):
    monkeypatch.setattr(pairing, "SESSION_TIMEOUT_S", 1.0)
    monkeypatch.setattr(pairing, "MESSAGE_TIMEOUT_S", 5.0)
    devices.login_s = 1.2  # longer than the protocol's own budget, within the login allowance
    assert devices.pair(login_timeout_s=10.0)["verified"] is True
    _paired(devices)


def test_a_fast_login_does_not_lend_its_unused_allowance_to_the_protocol(devices, monkeypatch):
    seen: list[float] = []

    class Spy(pairing.Clock):
        def cap(self, total_s: float) -> None:
            super().cap(total_s)
            seen.append(self.remaining())

    monkeypatch.setattr(pairing, "Clock", Spy)
    devices.pair(login_timeout_s=300.0)
    assert seen and seen[0] <= pairing.SESSION_TIMEOUT_S  # not 300 + 180 left after the offer


def test_a_slow_login_beyond_its_allowance_times_out(devices, monkeypatch):
    monkeypatch.setattr(pairing, "MESSAGE_TIMEOUT_S", 0.3)
    devices.login_s = 0.6
    assert devices.pair_error(login_timeout_s=0.3) == "pair-timeout"


def test_a_stalled_pairing_does_not_hold_the_guard(devices):
    seen = {}

    def owner_acts() -> None:
        seen["show"] = enroll.show(devices.a)["open_intents"]
        seen["recover"] = recover(devices.a)["open"]
        as_b(lambda: seen.setdefault("b", recover(devices.b)["open"]))

    devices.a_on_recv["probed"] = owner_acts
    devices.pair()
    assert [i["op"] for i in seen["show"]] == ["pair"]
    assert [i["op"] for i in seen["b"]] == ["accept"]


# --- validation at the protocol boundary ------------------------------------------------------


class Script:
    def __init__(self, *messages: dict, delay: float = 0.0) -> None:
        self.inbox = list(messages)
        self.sent: list[dict] = []
        self.timeouts: list[float] = []
        self.delay = delay

    def recv(self, timeout):
        time.sleep(self.delay)  # the message arrives late; recv itself does not police it
        if not self.inbox:
            raise PeerNetError("pair-closed", "closed")
        return self.inbox.pop(0)

    def send(self, message, timeout):
        self.sent.append(message)
        self.timeouts.append(timeout)

    def close(self):
        pass


def _hello(**over) -> dict:
    return {
        "v": 1,
        "phase": "hello",
        "pair_id": "c" * 32,
        "restart": False,
        "a_alias": "win",
        "a_user": "user",
        "a_port": 22,
        "a_peer_pub": host_line(30) + " agent-peer-net:win",
        "a_host_pub": host_line(21),
        **over,
    }


def test_a_malformed_hello_is_refused_reported_and_writes_nothing(devices):
    for over in (
        {"a_user": None},
        {"restart": "false"},
        {"pair_id": "short"},
        {"a_port": True},
        {"a_alias": "../x"},
    ):
        channel = Script(_hello(**over))
        with pytest.raises(PeerNetError) as caught:
            as_b(
                lambda c=channel: pairing.accept(
                    devices.b, c, env={"SSH_CONNECTION": "192.0.2.5 1 192.0.2.9 22"}
                )
            )
        assert caught.value.code == "pair-protocol", over
        assert channel.sent[-1]["phase"] == "error" and channel.sent[-1]["code"] == "pair-protocol"
        assert devices.open_intents(devices.b) == [] and devices.route(devices.b, "win") is None


def test_accept_outside_an_ssh_session_is_refused(devices):
    channel = Script(_hello())
    with pytest.raises(PeerNetError):
        as_b(lambda: pairing.accept(devices.b, channel, env={}))
    assert devices.route(devices.b, "win") is None


def test_b_refuses_to_offer_a_drifted_device_key(devices, tmp_path):
    pub = ssh.key_path("mac").with_name("agent_peer_mac.pub")
    pub.write_text(host_line(99) + " agent-peer-net:mac\n")
    assert devices.pair_error() == "key-drift"
    assert devices.route(devices.b, "win") is None and devices.grant(devices.b, "win") is None
    assert devices.open_intents(devices.b) == []


# --- recovery takes only the pin it wrote -----------------------------------------------------


def _stranded_route(devices, monkeypatch) -> tuple[intents.Intent, Path]:
    crash_on(monkeypatch, os, "replace", "A", after=True)
    with pytest.raises(RuntimeError):
        devices.pair()
    (route_op,) = [i for i in devices.open_intents(devices.a) if i.op == "pair-route"]
    return route_op, routes.ssh.pin_path(devices.a, "mac")


def test_a_changed_pin_is_a_conflict_that_neither_undo_nor_pairing_removes(devices, monkeypatch):
    route_op, pin = _stranded_route(devices, monkeypatch)
    pin.write_bytes(b"mac ssh-ed25519 SOMEONE-ELSES\n")
    report = recover(devices.a, route_op.op_id)
    assert report["status"] == "pin-conflict" and report["options"] == ["abandon"]
    with pytest.raises(PeerNetError) as caught:
        recover(devices.a, route_op.op_id, "undo")
    assert caught.value.code == "action-unavailable"
    assert devices.pair_error() == "pin-conflict"
    assert pin.read_bytes() == b"mac ssh-ed25519 SOMEONE-ELSES\n"


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs a privilege the Windows test user lacks")
def test_a_pin_replaced_by_a_link_is_never_followed_or_removed(devices, monkeypatch, tmp_path):
    route_op, pin = _stranded_route(devices, monkeypatch)
    foreign = tmp_path / "foreign"
    foreign.write_bytes(pin.read_bytes())  # even the planned bytes, behind a link
    pin.unlink()
    pin.symlink_to(foreign)
    assert recover(devices.a, route_op.op_id)["status"] == "pin-conflict"
    assert devices.pair_error() == "pin-conflict"
    assert pin.is_symlink() and foreign.exists()


def test_undo_removes_exactly_the_pin_it_wrote(devices, monkeypatch):
    route_op, pin = _stranded_route(devices, monkeypatch)
    assert recover(devices.a, route_op.op_id)["status"] == "route-unverified"
    recover(devices.a, route_op.op_id, "undo")
    assert not pin.exists()
