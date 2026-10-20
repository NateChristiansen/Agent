"""Dispatch notice producers: each lifecycle transition records its intent in
the guarded save that exposes it and delivers outside every guard. Real records, guards and
transitions; a recording fake transport stands in for the ring."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from agent_cli import dispatch, repos
from agent_cli.dispatch import (
    codex_transport,
    notice_producers,
    notices,
    telemetry_events,
    terminal,
    verification,
)
from agent_cli.dispatch import reverify as reverify_mod
from agent_cli.dispatch.state import load, save
from dispatch_helpers import _new_fast, _set_alpha_dispatch, droot, envelope, one, says, turn  # noqa: F401
from notice_helpers import (  # noqa: F401
    MGR,
    OTHER,
    answer_at_hold,
    broken_hold,
    entries,
    finish,
    queued,
    ready,
    reservation,
    wired,
)


def test_new_persists_the_local_launch_route(wired, monkeypatch):
    d = ready(wired.root, monkeypatch)
    assert d["notice_route"] == notices.route(notices.harness_identity(wired.root), MGR)
    assert load(wired.root, d["id"])["notice_route"] == d["notice_route"]


def test_a_committed_done_records_one_terminal_intent_and_rings_after_lease_release(wired, monkeypatch):
    d = ready(wired.root, monkeypatch)
    after = finish(wired.root, d)

    [entry] = entries(after)
    env = entry["envelope"]
    assert (env["event_class"], env["state"], env["verification"]) == ("terminal", "done", "exempt")
    assert env["recipient"] == MGR and entry["delivery"] == "wake-accepted"
    [ring] = wired.rings
    assert ring["held"] == [] and ring["lease"] is None
    assert ring["record"]["terminal_committed"] and ring["record"]["state"] == "done"

    again = finish(wired.root, d)
    assert notices.outbox(again).keys() == notices.outbox(after).keys()
    assert len(wired.rings) == 1


@pytest.mark.parametrize("state", ["blocked", "failed", "question"])
def test_a_committed_stop_without_continuation_is_one_terminal_notice(wired, monkeypatch, state):
    d = ready(wired.root, monkeypatch)
    after = finish(wired.root, d, {"state": state, "error": "stopped", "result": None})
    [entry] = entries(after)
    assert (entry["envelope"]["event_class"], entry["envelope"]["state"]) == ("terminal", state)
    assert [r["envelope"]["notice_id"] for r in wired.rings] == [entry["notice_id"]]


def test_a_failed_delivery_changes_no_state_cost_evidence_lease_or_event(wired, monkeypatch):
    counts = {"terminal": 0, "verification": 0}
    for name in counts:
        original = getattr(telemetry_events, name)

        def counting(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(telemetry_events, name, counting)
    wired.answer = (False, "refused", "transport-refused")
    d = ready(wired.root, monkeypatch)
    after = finish(wired.root, d)

    [ring] = wired.rings
    at_ring = ring["record"]
    for key in (
        "state",
        "cost_usd",
        "verification_result",
        "result",
        "checks",
        "terminal_committed",
        "attempt",
    ):
        assert after.get(key) == at_ring.get(key), key
    assert after["state"] == "done" and ring["lease"] is None
    assert counts == {"terminal": 1, "verification": 1}
    [entry] = entries(after)
    assert (entry["delivery"], entry["failure"]) == ("wake-failed", "transport-refused")
    assert after["chat_pending"] is True


def test_cleanup_incomplete_rings_with_the_lease_held_then_a_commit_supersedes_it(wired, monkeypatch):
    d = ready(wired.root, monkeypatch)
    monkeypatch.setattr(terminal, "requires_lease", lambda root, current: True)
    first = finish(wired.root, d)
    again = finish(wired.root, d)

    [attention] = entries(again)
    assert notices.outbox(first).keys() == notices.outbox(again).keys()
    env = attention["envelope"]
    assert (env["event_class"], env["attention_reason"], env["verification"]) == (
        "attention",
        "cleanup-incomplete",
        "incomplete",
    )
    [ring] = wired.rings
    assert ring["lease"] is not None and ring["held"] == []

    monkeypatch.setattr(terminal, "requires_lease", lambda root, current: False)
    final = finish(wired.root, d)
    old, new = entries(final)
    assert old["notice_id"] == attention["notice_id"] and old["delivery"] == "superseded"
    assert old["superseded_by"] == new["notice_id"] and old["audit"]
    assert new["generation"] == 2 and new["envelope"]["event_class"] == "terminal"
    # the second arrival joins the first ring's accepted reservation instead of ringing again
    assert queued(wired.root) == [old["notice_id"], new["notice_id"]]
    assert new["delivery"] == "inbox-accepted" and len(wired.rings) == 1


# ------------------------------------------------------------------ question holds


def test_a_claude_question_rings_at_hold_start_and_an_answer_supersedes_it(wired, monkeypatch, fake_claude):
    monkeypatch.setattr(dispatch.state, "QUESTION_IDLE_SECONDS", 20.0)
    q = envelope("question", open_questions=["which port?"], summary="need the port")
    fake_claude.scenario([{"turns": [turn(q), turn(envelope(summary="used 8080"))]}])

    def answer(env):
        if env["attention_reason"] == "question-pending":
            dispatch.say(wired.root, dispatch_id=env["dispatch_id"], message="use 8080")

    wired.on_ring = answer
    d = _new_fast(wired.root, session_id=MGR)

    assert d["state"] == "done"
    question, done = entries(load(wired.root, d["id"]))
    hold = wired.rings[0]
    assert hold["envelope"]["notice_id"] == question["notice_id"]
    assert hold["record"]["state"] == "running" and hold["lease"] is not None
    assert "which port" not in json.dumps(question)
    assert question["superseded_by"] == "answer"
    assert done["envelope"]["state"] == "done"
    assert queued(wired.root) == [question["notice_id"], done["notice_id"]]


def test_an_answer_retires_its_notice_even_when_the_worker_returns_at_once(wired, monkeypatch, fake_claude):
    """The answer's notice retirement used to be submitted after the answer went out. A worker
    that returned at once stopped the attempt, which cancelled that call before any thread
    picked it up, and the terminal notice superseded the question instead (a loaded landing
    run). The ordering is forced: the retirement waits before reaching its thread."""
    monkeypatch.setattr(dispatch.state, "QUESTION_IDLE_SECONDS", 20.0)
    q = envelope("question", open_questions=["which port?"], summary="need the port")
    fake_claude.scenario([{"turns": [turn(q), turn(envelope(summary="used 8080"))]}])
    real = asyncio.to_thread

    async def slow_to_start(func, /, *args, **kwargs):
        if func is notice_producers.answered:
            await asyncio.sleep(0.5)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", slow_to_start)

    def answer(env):
        if env["attention_reason"] == "question-pending":
            dispatch.say(wired.root, dispatch_id=env["dispatch_id"], message="use 8080")

    wired.on_ring = answer
    d = _new_fast(wired.root, session_id=MGR)

    assert d["state"] == "done"
    question, _ = entries(load(wired.root, d["id"]))
    assert question["superseded_by"] == "answer"


def test_an_unanswered_claude_question_is_superseded_by_the_committed_question(wired, fake_claude):
    q = envelope("question", open_questions=["which port?"], summary="need the port")
    fake_claude.scenario([one(q)])
    d = _new_fast(wired.root, session_id=MGR)

    assert d["state"] == "question"
    attention, stop = entries(load(wired.root, d["id"]))
    assert attention["envelope"]["attention_reason"] == "question-pending"
    assert attention["superseded_by"] == stop["notice_id"]
    assert (stop["envelope"]["event_class"], stop["envelope"]["state"]) == ("terminal", "question")
    assert queued(wired.root) == [attention["notice_id"], stop["notice_id"]]
    assert wired.rings[0]["envelope"]["notice_id"] == attention["notice_id"]


class FakeCodex:
    """The app-server calls one attempt makes: a question turn, then an answered turn."""

    def __init__(self, returns: list[dict]) -> None:
        self.events: asyncio.Queue = asyncio.Queue()
        self.returns = returns
        self.process = None

    async def call(self, method: str, params: dict) -> dict:
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            ret = self.returns.pop(0)
            item = {"type": "agentMessage", "phase": "final_answer", "text": json.dumps(ret)}
            await self.events.put({"method": "item/completed", "params": {"item": item}})
            await self.events.put({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
            return {"turn": {"id": f"turn-{len(self.returns)}"}}
        return {}


def test_a_codex_question_rings_at_hold_start_and_an_answer_supersedes_it(wired, monkeypatch):
    monkeypatch.setattr(dispatch.state, "QUESTION_IDLE_SECONDS", 20.0)
    d = ready(wired.root, monkeypatch, provider="codex", model="gpt-6-luna")
    question = envelope("question", nonce=d["nonce"], open_questions=["which port?"])["structured_output"]
    done = envelope(nonce=d["nonce"])["structured_output"]
    rpc = FakeCodex([question, done])

    @asynccontextmanager
    async def server(*args, **kwargs):
        yield rpc, {}

    monkeypatch.setattr(codex_transport, "isolated_server", server)
    inbox = wired.root / "codex.inbox"

    def answer(env):
        inbox.write_text("use 8080\n", encoding="utf-8")

    wired.on_ring = answer
    repo = repos.load_registry(wired.root).require("alpha")
    outcome = codex_transport.run_codex(
        wired.root, d, repo, text="work", resume=False, mcp_servers={}, inbox=inbox, timeout=30
    )

    assert outcome["stop_reason"] == "result"
    [attention] = entries(load(wired.root, d["id"]))
    [ring] = wired.rings
    assert ring["envelope"]["attention_reason"] == "question-pending"
    assert ring["record"]["state"] == "running" and ring["lease"] is not None
    assert attention["superseded_by"] == "answer" and attention["delivery"] == "superseded"


def _held_without_a_notice(seen: dict) -> None:
    record = seen["record"]
    assert record["state"] == "running" and record["chat_pending"] is False
    assert not record.get(notices.OUTBOX_FIELD)


@pytest.mark.parametrize("how", ["guard", "save"])
def test_a_failed_claude_hold_intent_keeps_the_hold_running(wired, monkeypatch, fake_claude, how):
    monkeypatch.setattr(dispatch.state, "QUESTION_IDLE_SECONDS", 20.0)
    q = envelope("question", open_questions=["which port?"], summary="need the port")
    fake_claude.scenario([{"turns": [turn(q), turn(envelope(summary="used 8080"))]}])
    broken_hold(monkeypatch, how)
    thread, seen = answer_at_hold(
        wired.root,
        "001-mac-01",
        lambda: dispatch.say(wired.root, dispatch_id="001-mac-01", message="use 8080"),
    )
    d = _new_fast(wired.root, session_id=MGR)
    thread.join(timeout=5)

    _held_without_a_notice(seen)
    assert d["state"] == "done" and d["result"]["summary"] == "used 8080"
    assert all(e["envelope"]["attention_reason"] is None for e in entries(load(wired.root, d["id"])))


@pytest.mark.parametrize("how", ["guard", "save"])
def test_a_failed_codex_hold_intent_keeps_the_hold_running(wired, monkeypatch, how):
    monkeypatch.setattr(dispatch.state, "QUESTION_IDLE_SECONDS", 20.0)
    d = ready(wired.root, monkeypatch, provider="codex", model="gpt-6-luna")
    question = envelope("question", nonce=d["nonce"], open_questions=["which port?"])["structured_output"]
    rpc = FakeCodex([question, envelope(nonce=d["nonce"])["structured_output"]])

    @asynccontextmanager
    async def server(*args, **kwargs):
        yield rpc, {}

    monkeypatch.setattr(codex_transport, "isolated_server", server)
    broken_hold(monkeypatch, how)
    inbox = wired.root / "codex.inbox"
    thread, seen = answer_at_hold(
        wired.root, d["id"], lambda: inbox.write_text("use 8080\n", encoding="utf-8")
    )
    repo = repos.load_registry(wired.root).require("alpha")
    outcome = codex_transport.run_codex(
        wired.root, d, repo, text="work", resume=False, mcp_servers={}, inbox=inbox, timeout=30
    )
    thread.join(timeout=5)

    _held_without_a_notice(seen)
    assert outcome["stop_reason"] == "result" and wired.rings == []
    assert not load(wired.root, d["id"]).get(notices.OUTBOX_FIELD)


def test_a_raising_transport_is_a_refused_ring(wired, monkeypatch):
    counts = {"terminal": 0, "verification": 0}
    for name in counts:
        original = getattr(telemetry_events, name)

        def counting(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(telemetry_events, name, counting)

    def explode(env):
        raise RuntimeError("socket vanished")

    wired.on_ring = explode
    d = ready(wired.root, monkeypatch)
    after = finish(wired.root, d)

    [ring] = wired.rings
    [entry] = entries(after)
    assert (entry["delivery"], entry["failure"], entry["claim"]) == ("wake-failed", "transport-refused", None)
    assert reservation(wired.root)["state"] == "failed"
    for key in ("state", "cost_usd", "terminal_committed", "verification_result"):
        assert after.get(key) == ring["record"].get(key), key
    assert after["state"] == "done" and counts == {"terminal": 1, "verification": 1}


def test_cancel_supersedes_and_names_itself(wired, monkeypatch):
    d = ready(wired.root, monkeypatch)
    monkeypatch.setattr(terminal, "requires_lease", lambda root, current: True)
    finish(wired.root, d)
    monkeypatch.setattr(terminal, "requires_lease", lambda root, current: False)
    with dispatch.verification_gate.record_guard(wired.root, d["id"]) as current:
        current["state"] = "running"
        save(wired.root, current)
    monkeypatch.setattr(dispatch.recovery, "await_worker_stop", lambda root, did: True)
    after = dispatch.cancel(wired.root, dispatch_id=d["id"])

    assert after["state"] == "cancelled"
    [entry] = entries(load(wired.root, d["id"]))
    assert entry["superseded_by"] == "cancel" and entry["audit"]
    assert len(wired.rings) == 1


def test_abandon_supersedes_and_names_itself(wired, monkeypatch):
    d = ready(wired.root, monkeypatch)
    finish(wired.root, d, {"state": "blocked", "error": "stuck", "result": None})
    dispatch.abandon(wired.root, dispatch_id=d["id"], note="nobody will resume it")
    [entry] = entries(load(wired.root, d["id"]))
    assert entry["superseded_by"] == "abandon" and entry["delivery"] == "superseded"
    assert len(wired.rings) == 1


def test_ack_marks_the_entries_acknowledged(wired, monkeypatch):
    d = ready(wired.root, monkeypatch)
    finish(wired.root, d)
    dispatch.ack(wired.root, dispatch_id=d["id"])
    [entry] = entries(load(wired.root, d["id"]))
    assert entry["consumption"] == "acknowledged"


def test_a_timeout_auto_resume_records_nothing_and_an_explicit_resume_supersedes(
    wired, monkeypatch, fake_claude
):
    monkeypatch.setattr(dispatch.state, "WATCHDOG_SECONDS", 0.5)
    _set_alpha_dispatch(wired.root, timeout_minutes=0.01)
    fake_claude.scenario(
        [{"turns": [turn(says("still thinking"))]}, one(envelope(summary="second")), one(envelope())]
    )
    d = _new_fast(wired.root, session_id=MGR)
    assert d["auto_resumes"][0]["reason"] == "timeout" and d["state"] == "done"
    [stop] = entries(load(wired.root, d["id"]))
    assert stop["envelope"]["attempt"] == 2

    resumed = dispatch.resume(wired.root, dispatch_id=d["id"], answer="once more", watch=True)
    old, new = entries(load(wired.root, d["id"]))
    assert old["superseded_by"] == "resume" and new["envelope"]["attempt"] == 3
    assert resumed["notice_route"] == d["notice_route"]
    assert queued(wired.root) == [old["notice_id"], new["notice_id"]]


def _verified(wired, monkeypatch) -> dict:
    d = ready(wired.root, monkeypatch, no_checks=False)
    after = finish(wired.root, d)
    assert after["state"] == "done" and len(wired.rings) == 1
    return after


def test_reverify_by_the_launcher_is_show_consumed_and_by_another_session_is_delivered(wired, monkeypatch):
    d = _verified(wired, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", MGR)
    row = reverify_mod.reverify(wired.root, d["id"])

    terminal_entry, run = entries(load(wired.root, d["id"]))
    assert run["consumption"] == "show-consumed" and run["delivery"] == "pending"
    assert run["envelope"]["run_id"] == row["binding"]["run_id"]
    assert (run["envelope"]["event_class"], run["envelope"]["state"]) == ("verification-complete", "done")
    assert terminal_entry["superseded_by"] == run["notice_id"]
    assert len(wired.rings) == 1 and run["notice_id"] not in queued(wired.root)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OTHER)
    reverify_mod.reverify(wired.root, d["id"])

    run = entries(load(wired.root, d["id"]))[-1]
    assert run["consumption"] == "none" and run["delivery"] == "inbox-accepted"
    assert run["notice_id"] in queued(wired.root, MGR) and not queued(wired.root, OTHER)

    # A raised reverify by the launcher records its attention, self-consumed.
    rings = len(wired.rings)
    events: list[str] = []
    monkeypatch.setattr(telemetry_events, "reverify", lambda root, d, **kw: events.append(kw["status"]))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", MGR)

    def broken(handle):
        raise RuntimeError("verifier lost")

    monkeypatch.setattr(verification, "wait", broken)
    with pytest.raises(RuntimeError):
        reverify_mod.reverify(wired.root, d["id"])

    after = load(wired.root, d["id"])
    attention = entries(after)[-1]
    env = attention["envelope"]
    assert (env["event_class"], env["attention_reason"], env["verification"]) == (
        "attention",
        "verification-incomplete",
        "incomplete",
    )
    assert attention["consumption"] == "show-consumed" and after["chat_pending"] is True
    assert len(wired.rings) == rings and events == ["incomplete"]
