"""Dispatch notice show and recovery: observed-generation show consumption and
the bounded reconciler behind restart and collect. Real records, guards and stores; a recording
fake transport stands in for the ring."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_cli import dispatch, sessions
from agent_cli.dispatch import notice_delivery, notice_producers, notices, telemetry_events
from agent_cli.dispatch.state import load, save
from agent_cli.dispatch.verification_gate import record_guard
from agent_cli.hooks import notice_inbox
from dispatch_helpers import droot  # noqa: F401
from notice_helpers import MGR, OTHER, entries, finish, queued, ready, wired  # noqa: F401

DONE = notices.Disposition(1, "terminal", "done", "verified", None, False, None)
BLOCKED = notices.Disposition(1, "terminal", "blocked", "unverified", None, False, None)


def _record(root: Path, n: int, *, to: str = MGR, legacy: bool = False) -> str:
    did = f"023-mac-{n:02d}"
    rec = {
        "id": did,
        "short": "alpha",
        "repo": "alpha",
        "feature": 23,
        "state": "done",
        "attempt": 1,
        "notify": ["chat"],
        "launched_by_session": to,
    }
    if not legacy:
        rec["notice_route"] = notices.route(notices.harness_identity(root), to)
    save(root, rec)
    return did


def _intent(
    root: Path, did: str, *, at: str | None = None, consumption: str = "none", disposition=DONE
) -> dict:
    with record_guard(root, did) as d:
        entry = notice_producers.record(root, d, disposition, consumption=consumption)
        assert entry is not None
        if at is not None:
            entry["recorded_at"] = at
        save(root, d)
    return entry


def _stamp(n: int) -> str:
    return f"2026-10-04T10:00:{n:02d}+00:00"


@contextmanager
def _held_elsewhere(root: Path, did: str):
    taken, release = threading.Event(), threading.Event()

    def hold():
        with record_guard(root, did):
            taken.set()
            release.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    taken.wait(5)
    try:
        yield
    finally:
        release.set()
        worker.join()


@pytest.fixture
def undelivered(wired, monkeypatch):
    """A finalize whose process died after the intent save, before its delivery."""

    @contextmanager
    def crashed(root, dispatch_id):
        yield []

    original = notice_producers.delivering
    monkeypatch.setattr(notice_producers, "delivering", crashed)
    d = ready(wired.root, monkeypatch)
    finish(wired.root, d)
    monkeypatch.setattr(notice_producers, "delivering", original)
    return d


# ------------------------------------------------------------------ show


def _shown(wired, monkeypatch, caller: str) -> tuple[dict, dict]:
    d = ready(wired.root, monkeypatch)
    finish(wired.root, d)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", caller)
    return d, dispatch.show(wired.root, dispatch_id=d["id"])


def test_show_by_the_recipient_consumes_the_generation_it_returned(wired, monkeypatch):
    d, shown = _shown(wired, monkeypatch, MGR)
    [entry] = entries(load(wired.root, d["id"]))
    assert shown["notice_show"] == {"caller": MGR, "consumed": [entry["notice_id"]], "failure": None}
    assert entry["consumption"] == "show-consumed" and shown["notice_outbox"][entry["notice_id"]]["audit"]
    assert notice_delivery.consumed_as(wired.root, MGR, entry["notice_id"]) == "show-consumed"
    assert load(wired.root, d["id"])["chat_pending"] is False and shown["chat_pending"] is False
    assert notice_inbox.Notices(wired.root, MGR).unread() == []


def test_show_by_another_session_consumes_nothing(wired, monkeypatch):
    d, shown = _shown(wired, monkeypatch, OTHER)
    [entry] = entries(load(wired.root, d["id"]))
    assert shown["notice_show"]["consumed"] == [] and entry["consumption"] == "none"
    assert load(wired.root, d["id"])["chat_pending"] is True
    assert notice_delivery.consumed_as(wired.root, MGR, entry["notice_id"]) is None


def test_show_observing_g_while_g_plus_one_commits_consumes_nothing_newer(wired, monkeypatch):
    d = ready(wired.root, monkeypatch)
    finish(wired.root, d)
    real = notice_delivery.bounded_record_guard

    @contextmanager
    def racing(root, did, timeout):
        with real(root, did, timeout) as current:
            notice_producers.record(root, current, BLOCKED)
            save(root, current)
            yield current

    monkeypatch.setattr(notice_delivery, "bounded_record_guard", racing)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", MGR)
    shown = dispatch.show(wired.root, dispatch_id=d["id"])

    old, new = entries(load(wired.root, d["id"]))
    assert shown["notice_show"]["consumed"] == []
    assert old["delivery"] == "superseded" and new["consumption"] == "none"
    assert load(wired.root, d["id"])["chat_pending"] is True


def test_show_of_a_notice_with_no_recipient_clears_the_flag_for_its_reader(wired, monkeypatch):
    """With no recipient the legacy clear still runs, but on a record from before `verdict_read`
    the flag is the read fact, so only a reader allowed to read it clears it: another live manager's show leaves it, the launcher's clears it."""
    did = _record(wired.root, 1)
    with record_guard(wired.root, did) as d:
        d["notice_route"] = None
        save(wired.root, d)
    entry = _intent(wired.root, did)
    assert entry["delivery"] == "fallback" and load(wired.root, did)["chat_pending"] is True
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OTHER)
    assert dispatch.show(wired.root, dispatch_id=did)["chat_pending"] is True
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", MGR)
    assert dispatch.show(wired.root, dispatch_id=did)["chat_pending"] is False
    assert load(wired.root, did)["chat_pending"] is False


@pytest.mark.parametrize("routed", [True, False])
def test_two_consecutive_shows_return_the_saved_record(wired, monkeypatch, routed):
    did = _record(wired.root, 1)
    if not routed:
        with record_guard(wired.root, did) as d:
            d["notice_route"] = None
            save(wired.root, d)
    _intent(wired.root, did)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", MGR)
    first = dispatch.show(wired.root, dispatch_id=did)
    second = dispatch.show(wired.root, dispatch_id=did)
    first.pop("notice_show", None)
    second.pop("notice_show", None)
    assert first == second
    assert first["record_revision"] == load(wired.root, did)["record_revision"]


def test_a_failed_source_save_consumes_nothing(wired, monkeypatch):
    d = ready(wired.root, monkeypatch)
    finish(wired.root, d)

    def broken(root, record):
        raise OSError("disk full")

    monkeypatch.setattr(notice_producers, "save", broken)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", MGR)
    shown = dispatch.show(wired.root, dispatch_id=d["id"])

    [entry] = entries(load(wired.root, d["id"]))
    assert shown["notice_show"] == {"caller": MGR, "consumed": [], "failure": "store-unavailable"}
    assert entry["consumption"] == "none" and load(wired.root, d["id"])["chat_pending"] is True
    assert notice_delivery.consumed_as(wired.root, MGR, entry["notice_id"]) is None


def test_a_crash_between_the_source_and_inbox_saves_replays_nothing(wired, monkeypatch):
    d = ready(wired.root, monkeypatch)
    finish(wired.root, d)
    [entry] = entries(load(wired.root, d["id"]))
    assert queued(wired.root) == [entry["notice_id"]]

    def crash(*args, **kwargs):
        raise OSError("killed before the tombstone")

    monkeypatch.setattr(notice_delivery, "tombstone_all", crash)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", MGR)
    dispatch.show(wired.root, dispatch_id=d["id"])

    assert entries(load(wired.root, d["id"]))[0]["consumption"] == "show-consumed"
    assert notice_inbox.Notices(wired.root, MGR).unread() == []
    assert notice_producers.reconcile(wired.root) == [] and len(wired.rings) == 1


def test_a_crash_before_delivery_is_delivered_once_by_collect(wired, undelivered, monkeypatch):
    terminals: list[str] = []
    monkeypatch.setattr(telemetry_events, "terminal", lambda root, d: terminals.append(d["id"]))
    assert wired.rings == []
    dispatch.collect(wired.root, dispatch_id=undelivered["id"])
    dispatch.collect(wired.root, dispatch_id=undelivered["id"])
    [entry] = entries(load(wired.root, undelivered["id"]))
    assert [r["envelope"]["notice_id"] for r in wired.rings] == [entry["notice_id"]]
    assert entry["delivery"] == "wake-accepted" and terminals == []


def test_supervisor_start_runs_the_reconciler(wired, undelivered, monkeypatch):
    seen: list[object] = []
    monkeypatch.setattr(dispatch.supervisor, "reconcile", lambda root: seen.append(root))
    with pytest.raises(Exception, match="not queued"):
        dispatch.supervisor.run(wired.root, dispatch_id=undelivered["id"])
    assert seen == [wired.root]


def test_a_busy_record_guard_returns_a_fixed_class_and_retry_delivers_once(wired, undelivered):
    did = undelivered["id"]
    [entry] = entries(load(wired.root, did))
    with _held_elsewhere(wired.root, did):
        outcome = notice_producers.deliver(wired.root, did, entry["notice_id"])
    assert outcome is not None and outcome.failure == "guard-timeout" and wired.rings == []
    assert entry["notice_id"] in notice_delivery.eligible(load(wired.root, did))

    first = notice_producers.reconcile(wired.root, [did])
    second = notice_producers.reconcile(wired.root, [did])
    assert [o.delivery for o in first] == ["wake-accepted"] and second == []
    assert len(wired.rings) == 1 and wired.rings[0]["session"] == MGR


def test_consumed_entries_never_starve_the_oldest_three(wired):
    for n in range(1, 4):
        _intent(wired.root, _record(wired.root, n, to=OTHER), at=_stamp(n), consumption="show-consumed")
    eligible = _intent(wired.root, _record(wired.root, 4, to=OTHER), at=_stamp(4))

    outcomes = notice_producers.reconcile(wired.root)
    assert [o.notice_id for o in outcomes] == [eligible["notice_id"]]
    assert [r["envelope"]["notice_id"] for r in wired.rings] == [eligible["notice_id"]]


def test_a_failed_reconcile_leaves_the_notice_eligible(wired):
    wired.answer = (False, "refused", "transport-refused")
    did = _record(wired.root, 1)
    entry = _intent(wired.root, did)
    assert [o.delivery for o in notice_producers.reconcile(wired.root, [did])] == ["wake-failed"]
    assert entry["notice_id"] in notice_delivery.eligible(load(wired.root, did))


def test_reconcile_takes_the_oldest_three(wired):
    nids = [_intent(wired.root, _record(wired.root, n), at=_stamp(n))["notice_id"] for n in range(1, 5)]
    assert [o.notice_id for o in notice_producers.reconcile(wired.root)] == nids[:3]


@pytest.mark.parametrize(("var", "value"), [("AGENT_NOTIFY", "off"), ("AGENT_NOW", "2026-10-04T10:00:00")])
def test_suppression_rings_nothing_on_reconcile(wired, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    did = _record(wired.root, 1)
    entry = _intent(wired.root, did)
    assert [o.failure for o in notice_producers.reconcile(wired.root, [did])] == ["suppressed"]
    assert wired.rings == [] and load(wired.root, did)["chat_pending"] is True
    assert entries(load(wired.root, did))[0]["notice_id"] == entry["notice_id"]


def test_reconcile_skips_superseded_and_acknowledged_ids(wired):
    did = _record(wired.root, 1)
    _intent(wired.root, did)
    _intent(wired.root, did, disposition=BLOCKED)
    with record_guard(wired.root, did) as d:
        notice_producers.acknowledge(d)
        save(wired.root, d)
    assert notice_producers.reconcile(wired.root, [did]) == [] and wired.rings == []


def test_a_legacy_record_with_an_exact_local_session_gets_a_route(wired):
    did = _record(wired.root, 1, legacy=True)
    entry = _intent(wired.root, did)
    assert entry["envelope"]["recipient"] == MGR and entry["delivery"] == "pending"
    assert "notice_route" not in load(wired.root, did)


@pytest.mark.parametrize("case", ["missing", "worker", "ended"])
def test_a_legacy_record_without_an_exact_local_manager_falls_back(wired, case):
    sid = "mgr-9"
    if case == "worker":
        sessions.set_record(wired.root, sid, dispatch_id="001-mac-77")
    elif case == "ended":
        sessions.set_record(wired.root, sid, name="gone")
        sessions.set_field(wired.root, sid, "activity", {"state": "ended"})
    did = _record(wired.root, 1, to=sid, legacy=True)
    entry = _intent(wired.root, did)
    assert (entry["envelope"], entry["delivery"], entry["failure"]) == (None, "fallback", "route-missing")
    assert load(wired.root, did)["chat_pending"] is True
