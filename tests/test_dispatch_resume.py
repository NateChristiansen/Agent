"""Failure modes and the one kept auto-resume (timeout, item 1.1), cancel, and resuming a
question or a budget hit with the owner's own answer.


"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from agent_cli import dispatch
from agent_cli.core import AgentError
from dispatch_helpers import (  # noqa: F401 -- droot is a pytest fixture, used only as a parameter name
    _log,
    _new_fast,
    _options,
    _set_alpha_dispatch,
    droot,
    envelope,
    one,
)

# ------------------------------------------------------------------ failure modes and auto-resume


def test_nonce_mismatch_is_failed(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope(nonce="001-mac-01-nope"))])
    d = _new_fast(droot)
    assert d["state"] == "failed"
    assert "nonce mismatch" in d["error"]
    assert len(fake_claude.calls()) == 1  # no auto-resume for a bad return
    assert "> handoff 001-mac-01 ← alpha: failed" in _log(droot) and "worktree kept at" in _log(droot)
    assert dispatch.parse_handoff(droot / d["handoff"])[0]["status"] == "failed"


# A budget hit with no valid return: the result carries a terminal_reason/subtype but no
# structured_output, same as claude does (reproduced against the SDK
# in a cap probe). Never keyed on exit code.
def _budget_envelope(**over) -> dict:
    env = {
        "type": "result",
        "subtype": "error_max_budget_usd",
        "is_error": True,
        "duration_ms": 12,
        "duration_api_ms": 9,
        "session_id": "{{SESSION}}",
        "total_cost_usd": 1.0,
        "num_turns": 4,
        "result": "",
        "terminal_reason": "budget_exhausted",
        "structured_output": None,
    }
    env.update(over)
    return env


def test_budget_hit_is_blocked_with_a_resume_hint_and_resumes_at_exactly_the_owners_budget(
    droot: Path, fake_claude
):
    """A budget hit never auto-resumes. It always reads as `blocked`, even when the return
    would otherwise have been valid. The old auto-resume raised the budget behind the owner's back
    twice in six days, and once answered a real design question for them.
    The error names the exact command to resume with (1.1).

    No more ×1.5 guessing. A blocked budget hit only moves when the owner resumes it by hand.
    It lands at exactly the number they gave (1.1).

    The budget is the dispatch's total across attempts. A resume after the whole budget is
    spent would hit the provider cap at once, so without a new budget it is refused before
    anything launches."""
    fake_claude.scenario([one(_budget_envelope(), commit="T-42 partial"), one(envelope(cost=0.3))])
    d = _new_fast(droot)
    assert d["state"] == "blocked" and d["attempt"] == 1
    assert d["auto_resumes"] == []
    assert len(fake_claude.calls()) == 1  # no second attempt
    assert d["budget_usd"] == 1.0  # unchanged
    assert "budget $1.00 reached; resume 001-mac-01" in d["error"]
    assert d["result"]["status"] == "blocked" and d["result"]["summary"].startswith(
        "synthesized by the supervisor"
    )
    assert "1 commit(s) on" in d["result"]["summary"]
    assert [c["subject"] for c in d["git"]["commits"]] == ["T-42 partial"]
    assert "> handoff 001-mac-01 ← alpha: blocked (attempt 1" in _log(droot)
    assert Path(d["worktree"]).is_dir()
    assert d["cost_usd"] == 1.0
    with pytest.raises(AgentError, match=r"has spent \$1\.00 of its \$1\.00 budget"):
        dispatch.resume(droot, dispatch_id="001-mac-01", answer="carry on")
    assert len(fake_claude.calls()) == 1
    r = dispatch.resume(
        droot,
        dispatch_id="001-mac-01",
        answer="more budget",
        budget=2.0,
        budget_reason="more work needed",
        watch=True,
    )
    assert r["state"] == "done" and r["attempt"] == 2 and r["budget_usd"] == 2.0
    assert len(fake_claude.calls()) == 2
    assert _options(fake_claude, 1).resume == d["session_id"]
    assert _options(fake_claude, 1).max_budget_usd == 1.0


@pytest.mark.parametrize("spent", [1.0, 1.2])
def test_exhausted_budget_never_builds_sdk_options(droot: Path, fake_claude, monkeypatch, spent):
    from agent_cli import repos
    from agent_cli.dispatch import options

    fake_claude.scenario([one(envelope("question", cost=spent))])
    d = _new_fast(droot)
    calls = []
    monkeypatch.setattr(options, "_WorkerOptions", lambda **kwargs: calls.append(kwargs))
    repo = repos.load_registry(droot).require(d["repo"])
    with pytest.raises(AgentError, match="has spent"):
        dispatch.build_options(droot, d, repo, resume=True)
    assert calls == []
    with pytest.raises(AgentError, match="has spent"):
        dispatch.resume(droot, dispatch_id=d["id"], answer="continue", watch=True)
    assert len(fake_claude.calls()) == 1


def test_resume_timeout_replaces_the_launch_clock_and_the_registry_does_not(droot: Path, fake_claude):
    """Raising `agent.yaml` never reached a resumed worker, because the clock is stamped at
    launch. `--timeout` is the explicit way to lengthen it, recorded like `--budget`. A
    non-positive or non-finite one is refused before launching: `float("inf") <= 0` is false,
    so infinity and NaN used to pass."""
    _set_alpha_dispatch(droot, timeout_minutes=45)
    q = envelope("question", open_questions=["keep going?"])
    fake_claude.scenario([one(q), one(q), one(envelope())])
    d = _new_fast(droot)
    assert d["timeout_minutes"] == 45
    for bad in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(AgentError, match="timeout_minutes must be"):
            dispatch.resume(droot, dispatch_id="001-mac-01", answer="go", timeout_minutes=bad)
    assert len(fake_claude.calls()) == 1
    assert dispatch.load(droot, "001-mac-01")["attempt"] == 1
    _set_alpha_dispatch(droot, timeout_minutes=240)
    d = dispatch.resume(droot, dispatch_id="001-mac-01", answer="yes", watch=True)
    assert d["timeout_minutes"] == 45, "a registry change alone must not move a running line of work"
    d = dispatch.resume(droot, dispatch_id="001-mac-01", answer="yes", timeout_minutes=120, watch=True)
    assert d["state"] == "done" and d["timeout_minutes"] == 120
    assert d["timeout_source"] == "passed"
    assert dispatch.load(droot, "001-mac-01")["timeout_minutes"] == 120


def test_a_question_caused_by_a_denied_command_is_never_auto_resumed(droot: Path, fake_claude):
    """A denial is a harness fault. Re-reading the plan cannot fix it. Auto-resuming one tells
    a worker that correctly stopped to carry on without its tools. That turned a
    right stop into a blind build of unverified files."""
    q = envelope("question", summary="pnpm is denied; nothing can be verified")
    q["permission_denials"] = [
        {"tool_name": "Bash", "tool_input": {"command": "pnpm --filter web lint"}},
        {"tool_name": "Bash", "tool_input": {"command": "pnpm --version"}},
    ]
    fake_claude.scenario([one(q), one(envelope())])
    d = _new_fast(droot)

    assert d["state"] == "blocked", "a denied command must not be talked into proceeding"
    assert d["attempt"] == 1, "it must not have been resumed"
    assert d["auto_resumes"] == []
    assert len(fake_claude.calls()) == 1, "the second scenario entry must never be reached"
    assert "2 denied command(s)" in d["error"]
    assert "allowed_tools" in d["error"], "the error should name the fix, in agent.yaml"


def test_a_worker_that_returns_nothing_is_failed(droot: Path, fake_claude):
    """The stream ends with no result at all: a crashed CLI, a transport that died. There are
    no recovery passes left to try. ADR-0009 deleted all three: the stdout envelope scrape,
    the JSON-in-result-text dig, and the last-assistant-message of the transcript. The
    supervisor says what it has instead of guessing at a return. With no session to resume and
    a changed worktree, a fresh start is refused."""
    fake_claude.scenario([one(end=True)])
    d = _new_fast(droot)
    assert d["state"] == "failed" and d["exit_code"] == 1 and d["attempt"] == 1
    assert d["last_result"] is None
    assert "no result" in d["error"]
    assert d["stop_reason"] == "stream-end"
    assert Path(d["worktree"]).is_dir()
    assert d["session_established"] is False
    (Path(d["worktree"]) / "partial.txt").write_text("prior work\n", encoding="utf-8")
    with pytest.raises(AgentError, match="changed its worktree"):
        dispatch.resume(droot, dispatch_id="001-mac-01", answer="try again", watch=True)
    assert dispatch.load(droot, "001-mac-01")["attempt"] == 1
    assert len(fake_claude.calls()) == 1


def test_a_result_with_no_structured_output_is_failed(droot: Path, fake_claude):
    """The other half of the same gap, and the one the spike hit. A result arrives but
    `structured_output` is null (an interrupt, an error subtype). `_synthesize` covers the
    timeout and budget paths. A bare error is `failed`, and the reason names the subtype."""
    broken = envelope()
    broken["structured_output"] = None
    broken["subtype"] = "error_during_execution"
    broken["is_error"] = True
    fake_claude.scenario([one(broken)])
    d = _new_fast(droot)
    assert d["state"] == "failed"
    assert "error_during_execution" in d["error"] and "no structured output" in d["error"]


def test_failed_startup_restarts_full_work_order_instead_of_resuming_missing_session(
    droot: Path, fake_claude, monkeypatch
):
    """A version-probe failure left no CLI session, then --resume failed again."""
    from agent_cli.dispatch import supervisor

    fake_claude.scenario([{}, one(envelope())])

    def factory(options):
        transport = fake_claude.factory(options)
        if len(fake_claude.transports) == 1:
            transport.startup_stage = "version probe"

            async def fail_before_session():
                raise TimeoutError()

            transport.connect = fail_before_session
        return transport

    monkeypatch.setattr(supervisor, "transport_factory", factory)
    first = _new_fast(droot)
    assert first["state"] == "failed" and first["session_established"] is False
    err = (droot / ".agent/dispatches/001-mac-01.err").read_text(encoding="utf-8")
    assert "version probe: TimeoutError" in err

    second = dispatch.resume(droot, dispatch_id="001-mac-01", answer="Start from the top", watch=True)
    assert second["state"] == "done" and second["session_established"] is True
    assert _options(fake_claude, 1).resume is None
    assert "fresh start: previous attempt established no observable session" in (
        droot / ".agent/dispatches/001-mac-01.err"
    ).read_text(encoding="utf-8")
    prompt = fake_claude.calls()[1]["prompt"]
    assert "Start from the top" in prompt
    assert (droot / ".agent/dispatches/001-mac-01.prompt.md").read_text(encoding="utf-8") in prompt


def test_timeout_interrupts_and_auto_resumes(droot: Path, fake_claude):
    """The one auto-resume 1.1 kept: a timeout, at the SAME budget. It did not run out of
    anything. It needs the clock reset. The kill became `client.interrupt()`. The decision
    table did not change."""
    _set_alpha_dispatch(droot, timeout_minutes=0.01)
    fake_claude.scenario([one(envelope(), sleep=20), one(envelope())])
    t0 = time.monotonic()
    d = _new_fast(droot)
    assert time.monotonic() - t0 < 15
    assert d["state"] == "done" and d["attempt"] == 2
    assert d["auto_resumes"][0]["reason"] == "timeout"
    assert d["budget_usd"] == 1.0  # unchanged; only a valid `resume --budget` moves it now
    assert d["auto_resumes"][0]["from_budget"] == d["auto_resumes"][0]["to_budget"] == 1.0
    calls = fake_claude.calls()
    assert len(calls) == 2 and "time limit was hit" in calls[1]["prompt"]
    assert calls[0]["interrupts"] == 1, "the turn in flight is interrupted, not abandoned"
    assert "timeout after" in (droot / ".agent/dispatches/001-mac-01.err").read_text(encoding="utf-8")
    # The second attempt's return is the record's one copy.
    assert d["last_result"]["return_in"] == "result" and "structured_output" not in d["last_result"]
    assert d["result"]["status"] == "done"


def test_cancel_while_running(droot: Path, fake_claude):
    """`cancel` runs in another process. It cannot signal the worker any more. The connection
    belongs to this supervisor. The record is the channel: `cancel` writes `cancelled`, and
    the supervisor's poller interrupts the turn and stops."""
    fake_claude.scenario([one(envelope(), sleep=30)])
    result: dict = {}

    def runner():
        try:
            result["d"] = _new_fast(droot)
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc

    th = threading.Thread(target=runner)
    th.start()
    path = droot / ".agent/dispatches/001-mac-01.json"
    for _ in range(400):
        if (
            path.exists()
            and json.loads(path.read_text(encoding="utf-8")).get("state") == "running"
            and fake_claude.transports
        ):
            break
        time.sleep(0.02)
    else:
        pytest.fail("worker never started")
    t0 = time.monotonic()
    c = dispatch.cancel(droot, dispatch_id="001-mac-01")
    th.join(timeout=15)
    assert not th.is_alive() and time.monotonic() - t0 < 12
    assert c["state"] == "cancelled" and result["d"]["state"] == "cancelled"
    assert fake_claude.transports[0].interrupts == 1
    final = dispatch.load(droot, "001-mac-01")
    assert (
        final["state"] == "cancelled" and final["claude_pid"] is None and final["stop_reason"] == "cancelled"
    )
    assert dispatch.parse_handoff(droot / final["handoff"])[0]["status"] == "cancelled"
    assert "> handoff 001-mac-01 ← alpha: cancelled" in _log(droot)
    assert Path(final["worktree"]).is_dir()
    with pytest.raises(AgentError, match="nothing to cancel"):
        dispatch.cancel(droot, dispatch_id="001-mac-01")


def test_abandon_closes_a_finished_dispatch(droot: Path, fake_claude):
    """a stale ask leaves the Workers view with a reason on the record, and the
    worktree stays."""
    fake_claude.scenario([one(envelope("blocked", summary="no port"))])
    d = _new_fast(droot)
    assert d["state"] == "blocked" and dispatch.is_live(d)
    dispatch.save(droot, {**dispatch.load(droot, "001-mac-01"), "state": "running"})
    for call in (
        lambda: dispatch.abandon(droot, dispatch_id="001-mac-01", note="x"),
        lambda: dispatch.ack(droot, dispatch_id="001-mac-01"),
    ):
        with pytest.raises(AgentError, match="cancel it first"):
            call()
    dispatch.save(droot, {**dispatch.load(droot, "001-mac-01"), "state": "blocked"})
    with pytest.raises(AgentError, match="needs a --note"):
        dispatch.abandon(droot, dispatch_id="001-mac-01", note="  ")
    a = dispatch.abandon(droot, dispatch_id="001-mac-01", note="superseded by 001-mac-02")
    assert a["state"] == "abandoned" and a["abandoned_from"] == "blocked"
    assert a["actioned"] == {"action": "abandoned", "note": "superseded by 001-mac-02"}
    assert not dispatch.is_live(a) and a["chat_pending"] is False
    assert dispatch.parse_handoff(droot / a["handoff"])[0]["status"] == "abandoned"
    assert "abandoned from blocked: superseded by 001-mac-02" in _log(droot)
    assert (
        next(row for row in dispatch.list_(droot, all_=True) if row["id"] == a["id"])["state"] == "abandoned"
    )
    assert Path(a["worktree"]).is_dir()
    assert "001-mac-01" not in dispatch.pending_block(droot)
    with pytest.raises(AgentError, match="already abandoned"):
        dispatch.abandon(droot, dispatch_id="001-mac-01", note="again")
    with pytest.raises(AgentError, match="nothing waits"):
        dispatch.ack(droot, dispatch_id="001-mac-01")


def test_ack_holds_until_the_next_finish(droot: Path, fake_claude, monkeypatch):
    """`ack` keeps the state and drops the row. A resume that finishes later than the ack
    brings the row back, with nothing to reset."""
    monkeypatch.setenv("AGENT_NOW", "2026-09-16T10:00:00")
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    assert dispatch.is_live(d) and not dispatch.is_actioned(d)
    k = dispatch.ack(droot, dispatch_id="001-mac-01")
    assert k["state"] == "done" and dispatch.is_actioned(k) and not dispatch.is_live(k)
    assert k["thread"][-1]["text"] == "ack"
    monkeypatch.setenv("AGENT_NOW", "2026-09-16T11:00:00")
    fake_claude.scenario([one(envelope())])
    r = dispatch.resume(droot, dispatch_id="001-mac-01", answer="one more pass", watch=True)
    assert r["state"] == "done" and dispatch.is_live(r)


def test_a_probe_never_waits_on_the_owner(droot: Path, fake_claude):
    """an eval probe's question is the result, not an ask. An escalation keeps
    the kind, and an unknown kind is refused before anything is created."""
    q = envelope("question", open_questions=["pnpm --version was denied"])
    fake_claude.scenario([one(q), one(q)])
    p = _new_fast(droot, kind="probe")
    assert p["kind"] == "probe" and p["state"] == "question"
    assert not dispatch.is_live(p) and "001-mac-01" not in dispatch.pending_block(droot)
    assert dispatch.list_(droot) == []
    e = _new_fast(droot, escalate_from="001-mac-01")
    assert e["kind"] == "probe"
    with pytest.raises(AgentError, match="kind must be"):
        _new_fast(droot, kind="demo")


def test_question_never_auto_resumes_and_resumes_with_the_owners_answer(droot: Path, fake_claude):
    """A plain `question` (no permission denials) used to get one free auto-resumed re-read of
    the plan. That is exactly the guessing this harness's own top rule (never make assumptions)
    forbids everywhere else. It answered a real open design question for the owner and
    raised the budget while doing it. It goes to the owner every time now (1.1), so the
    owner's manual resume here is attempt 2, not 3.

    An answer is a work order; an unmatched command refuses resume, as `new` does."""
    q = envelope("question", open_questions=["which port?"], summary="need the port")
    fake_claude.scenario([one(q), one(envelope(cost=0.1))])
    d = _new_fast(droot)
    assert d["state"] == "question" and d["attempt"] == 1
    assert d["stop_reason"] == "idle", "held open for an answer first, then finalized"
    assert d["auto_resumes"] == []
    assert len(fake_claude.calls()) == 1
    log = _log(droot)
    assert "> asks: which port?" in log
    assert "action resume, dispatch_id 001-mac-01" in log
    assert d["result"]["open_questions"] == ["which port?"]
    with pytest.raises(AgentError, match="--reason"):
        dispatch.resume(droot, dispatch_id="001-mac-01", answer="x", budget=9)
    answer = "use port 8080\n\n```sh\ngit rebase origin/main\n```"
    with pytest.raises(AgentError, match="no matching rule: git rebase origin/main"):
        dispatch.resume(droot, dispatch_id="001-mac-01", answer=answer)
    held = dispatch.load(droot, "001-mac-01")
    assert held["state"] == "question" and held["attempt"] == 1 and len(held["thread"]) == 2
    r = dispatch.resume(droot, dispatch_id="001-mac-01", answer=answer, watch=True, allow_unmatched=True)
    assert r["state"] == "done" and r["attempt"] == 2 and r["max_attempts"] == 3
    assert r["cost_usd"] == pytest.approx(0.42 + 0.1)
    call = fake_claude.calls()[1]
    assert _options(fake_claude, 1).resume == d["session_id"]
    assert _options(fake_claude, 1).max_budget_usd == pytest.approx(0.58)
    assert "### 2026-09-03T14:05:00-07:00 owner → worker\n\nuse port 8080" in call["prompt"]
    assert "(attempt 2)" in call["prompt"]
    assert (droot / ".agent/dispatches/001-mac-01.prompt.2.md").read_text(encoding="utf-8") == call["prompt"]
    assert "owner → worker" in dispatch.parse_handoff(droot / r["handoff"])[1]["Thread"]
    assert dispatch.parse_handoff(droot / r["handoff"])[0]["status"] == "done"
    assert next(row for row in dispatch.list_(droot, all_=True) if row["id"] == r["id"])["state"] == "done"


def test_resume_carries_a_real_verdicts_finding_and_no_finding_for_a_verified_attempt(
    droot: Path, fake_claude
):
    fake_claude.scenario(
        [
            one(envelope("question", open_questions=["which port?"])),
            one(envelope()),
            one(envelope()),
            one(envelope()),
        ]
    )
    d = _new_fast(droot)
    for n, verdict in enumerate(
        (
            {"status": "verified", "reasons": []},
            {"status": "unverified", "reasons": ["checks red"]},
            {"status": "failed", "reasons": ["checks red"]},
        ),
        start=1,
    ):
        current = dispatch.load(droot, d["id"])
        current["verification_result"] = verdict
        dispatch.save(droot, current)
        dispatch.resume(droot, dispatch_id=d["id"], answer="use port 8080", watch=True, again=n == 3)
        prompt = fake_claude.calls()[n]["prompt"]
        if verdict["status"] == "verified":
            assert "- verification" not in prompt
        else:
            assert f"- verification {verdict['status']}: checks red" in prompt
        assert "new attempt" not in prompt


def test_resume_answer_becomes_escalation_prefix_without_rewriting_work_order(
    droot: Path, fake_claude, monkeypatch
):
    from agent_cli import ops
    from agent_cli.dispatch import lifecycle

    fake_claude.scenario(
        [one(envelope("question", open_questions=["which port?"])), one(envelope()), one(envelope())]
    )
    original = _new_fast(droot, prompt="Original work order")
    answer = "Manager's latest instruction"
    ops.dispatch_resume(droot, dispatch_id=original["id"], answer=answer, answer_by="manager", watch=True)
    resumed = dispatch.load(droot, original["id"])
    manager_text = [entry["text"] for entry in resumed["thread"] if entry["from"] == "manager"]
    assert manager_text == ["Original work order", "Manager's latest instruction"]
    monkeypatch.setattr(lifecycle, "start", lambda root, d, watch: d)
    escalated = dispatch.new(
        droot,
        repo="alpha",
        feature="1",
        prompt="Escalated work",
        escalate_from=original["id"],
        no_checks=True,
        provider="codex",
        model="gpt-6-astra",
        effort="high",
    )
    prompt = dispatch.file(droot, escalated["id"], ".prompt.md").read_text(encoding="utf-8")
    assert "Manager's latest instruction" in prompt
    assert "Original work order" in dispatch.load(droot, original["id"])["thread"][0]["text"]


def test_mcp_resume_answer_is_recorded_and_rendered_as_manager(droot: Path, fake_claude, monkeypatch):
    import anyio
    from mcp import Client

    from agent_cli.dispatch import lifecycle
    from agent_cli.mcp import server as mcp_server

    fake_claude.scenario([one(envelope("question", open_questions=["continue?"]))])
    original = _new_fast(droot)
    monkeypatch.setattr(lifecycle, "start", lambda root, d, watch: d)

    async def call_resume():
        async with Client(mcp_server.build_server(droot, "manager")) as client:
            return await client.call_tool(
                "dispatch", {"action": "resume", "dispatch_id": original["id"], "answer": "Manager direction"}
            )

    result = anyio.run(call_resume)
    current = dispatch.load(droot, original["id"])
    assert result.is_error is False and current["thread"][-1]["from"] == "manager"
    assert current["thread"][-1]["text"] == "Manager direction"
    assert "manager → worker" in (droot / current["handoff"]).read_text(encoding="utf-8")
