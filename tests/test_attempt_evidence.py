"""Attempt evidence (phase E): kept streams, question and timeout boundaries, UTC
observation times, honest failure text, ack attribution and denial classes."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli.dispatch import notice_producers, state, streams, telemetry_events
from agent_cli.dispatch.terminal import blocked_reason
from agent_cli.dispatch.verification_evidence import Binding, CommandOutcome, Evidence, TreeState
from agent_cli.dispatch.verification_store import OBSERVED, Store
from agent_cli.hooks.decision_log import denial_class

evaluate_mod = importlib.import_module("agent_cli.dispatch.evaluate")

from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: E402,F401 -- pytest fixture

# Streams ---------------------------------------------------------------------------------------


def test_a_new_attempt_keeps_the_previous_stream_and_stderr(droot: Path, fake_claude: Any) -> None:
    fake_claude.scenario([one(envelope())])
    d = state.load(droot, _new_fast(droot)["id"])
    out, err = state.file(droot, d["id"], ".out"), state.file(droot, d["id"], ".err")
    err.write_text("before\n", encoding="utf-8")
    first = err.stat().st_size
    out.write_text('{"attempt one": true}\n', encoding="utf-8")
    with err.open("a", encoding="utf-8") as stream:
        stream.write("attempt one stderr\n")
    d.update(attempt=2, err_offsets={"1": first, "2": err.stat().st_size})
    kept = streams.keep_previous(droot, d)
    assert kept == {"attempt": 1, "path": f"{d['id']}.attempt-1.out", "stderr": f"{d['id']}.attempt-1.err"}
    assert (
        state.file(droot, d["id"], ".attempt-1.out").read_text(encoding="utf-8") == '{"attempt one": true}\n'
    )
    assert state.file(droot, d["id"], ".attempt-1.err").read_text(encoding="utf-8") == "attempt one stderr\n"
    assert not out.exists()
    assert state.load(droot, d["id"])["transcripts"] == [kept]


def test_an_empty_attempt_is_kept_and_nothing_is_overwritten(droot: Path, fake_claude: Any) -> None:
    fake_claude.scenario([one(envelope())])
    d = state.load(droot, _new_fast(droot)["id"])
    out = state.file(droot, d["id"], ".out")
    out.write_text("", encoding="utf-8")  # a zero-turn failed resume
    d["attempt"] = 3
    kept = streams.keep_previous(droot, d)
    assert kept is not None and kept["path"] == f"{d['id']}.attempt-2.out"
    out.write_text("a fresh start reusing attempt 3\n", encoding="utf-8")
    again = streams.keep_previous(droot, d)
    assert again is not None and again["path"] == f"{d['id']}.attempt-2-2.out"
    assert state.file(droot, d["id"], ".attempt-2.out").read_text(encoding="utf-8") == ""


def test_a_first_attempt_keeps_nothing(droot: Path, fake_claude: Any) -> None:
    fake_claude.scenario([one(envelope())])
    d = state.load(droot, _new_fast(droot)["id"])
    d["attempt"] = 1
    assert streams.keep_previous(droot, d) is None


# Lifecycle boundaries ----------------------------------------------------------------------------


def test_a_question_hold_and_its_answer_are_correlated_transitions(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_claude.scenario([one(envelope())])
    d = state.load(droot, _new_fast(droot)["id"])
    d.update(state="running", terminal_committed=False)
    state.save(droot, d)
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        telemetry_events, "transition", lambda _r, _d, phase, **data: seen.append((phase, data))
    )
    monkeypatch.setattr(notice_producers, "deliver", lambda *_a: None)
    notice_producers.question_hold(droot, d["id"])
    notice_producers.answered(droot, d["id"])
    question = f"{d['attempt']}-q1"
    assert seen == [
        ("awaiting-answer", {"question_id": question}),
        ("model-active", {"question_id": question}),
    ]
    record = state.load(droot, d["id"])
    assert "open_question" not in record
    assert [q["id"] for q in record["questions_held"]] == [question]


def test_the_writer_accepts_both_phase_names() -> None:
    assert {"awaiting-answer", "model-active", "ended"} <= set(telemetry_events.PHASES)


def test_an_auto_resume_closes_the_attempt_it_replaces(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_claude.scenario([one(envelope())])
    d = state.load(droot, _new_fast(droot)["id"])
    ended: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        telemetry_events, "terminal", lambda _r, rec: ended.append(("terminal", rec["attempt"], rec["state"]))
    )
    monkeypatch.setattr(
        telemetry_events,
        "transition",
        lambda _r, rec, phase, **_k: ended.append((phase, rec["attempt"], rec["state"])),
    )
    d.update(state="running", attempt=1, auto_resumes=[], timeout_minutes=45)
    outcome = {"state": "blocked", "error": "timed out", "auto_resume": "timeout", "cost_usd": 0.1}
    queued = evaluate_mod.queue_auto_resume(droot, d, None, outcome, "timeout")  # pyright: ignore[reportArgumentType]
    assert ended == [("terminal", 1, "blocked"), ("ended", 1, "blocked")]
    assert queued["attempt"] == 2 and queued["state"] == "queued"


# UTC observation sidecar ---------------------------------------------------------------------------


def _evidence(
    binding: Binding, state_: str, before: TreeState | None, commands: tuple[CommandOutcome, ...]
) -> Evidence:
    return Evidence(1, binding, state_, before, before if state_ != "running" else None, commands, 1.0)  # pyright: ignore[reportArgumentType]


def test_a_run_records_utc_start_and_end_beside_unchanged_evidence(tmp_path: Path) -> None:
    binding = Binding("001-01", 1, "a" * 40, "b" * 64, "c" * 32)
    store = Store(tmp_path, binding, create=True)
    try:
        tree = TreeState("a" * 40, True, True)
        store.evidence(_evidence(binding, "running", None, ()))
        started = json.loads((store.path / OBSERVED).read_text(encoding="utf-8"))
        assert started["run_id"] == "c" * 32 and started["started_utc"] and started["ended_utc"] is None
        store.evidence(_evidence(binding, "complete", tree, (CommandOutcome(0, "exited", 0, 1.0),)))
        observed = json.loads((store.path / OBSERVED).read_text(encoding="utf-8"))
        assert observed["started_utc"] == started["started_utc"] and observed["ended_utc"]
        terminal = store.read("terminal.json")
        assert terminal is not None and Evidence.from_dict(terminal).state == "complete"  # still parses
        assert "started_utc" not in terminal
    finally:
        store.close()


# Failure text -----------------------------------------------------------------------------------------


def test_a_blocked_reason_names_the_failing_test_first_and_shortens_the_command() -> None:
    command = "uv run pytest " + " ".join(f"tests/test_{i}.py" for i in range(120))
    reason = blocked_reason(
        [f"`{command}` exited 1 (run abc)"], ["FAILED tests/test_7.py::test_effort - AssertionError"]
    )
    assert reason.startswith("FAILED tests/test_7.py::test_effort")
    assert "`uv run pytest tests/test_0.py … (+119 more arguments)` exited 1 (run abc)" in reason
    assert len(reason) < 300


def test_a_codex_failure_is_labelled_codex_and_shows_only_this_attempts_stderr(tmp_path: Path) -> None:
    err = tmp_path / "x.err"
    err.write_text("agent [done] verification: verified\n", encoding="utf-8")
    offset = err.stat().st_size
    with err.open("a", encoding="utf-8") as stream:
        stream.write("Codex transport error: frame over limit\n")
    assert evaluate_mod._tail(err, 600, since=offset) == "Codex transport error: frame over limit"  # pyright: ignore[reportPrivateUsage]
    assert evaluate_mod._worker({"provider": "codex"}) == "the Codex worker"  # pyright: ignore[reportPrivateUsage]
    assert evaluate_mod._worker({"provider": "claude"}) == "claude"  # pyright: ignore[reportPrivateUsage]


def test_a_synthesized_return_counts_commits_since_the_dispatch_started(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_claude.scenario([one(envelope())])
    d = state.load(droot, _new_fast(droot)["id"])
    seen: list[str] = []
    monkeypatch.setattr(evaluate_mod, "git_commits", lambda _wt, since: seen.append(since) or [])
    monkeypatch.setattr(evaluate_mod, "git_files", lambda _wt, since: [])
    d.update(start_sha="d" * 40, base="origin/main")
    evaluate_mod._synthesize(d, "the time limit was hit")  # pyright: ignore[reportPrivateUsage]
    assert seen == ["d" * 40]


# Attribution and classes ------------------------------------------------------------------------------


def test_a_manager_ack_is_attributed_to_the_manager(droot: Path, fake_claude: Any) -> None:
    from agent_cli import ops

    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    ops.dispatch_ack(droot, dispatch_id=d["id"], by="manager")
    assert state.load(droot, d["id"])["thread"][-1]["from"] == "manager"


@pytest.mark.parametrize(
    ("hook", "reason", "want"),
    [
        ("codex-prompt", "Peer wake already delivered.", "wake-suppression"),
        ("manager-prompt", "Dispatch wake already delivered.", "wake-suppression"),
        ("peer-stop", "## Messages from other sessions (1)", "continuation"),
        ("host-decline", "declined automatically by the dispatch host", "host-decline"),
        ("worker-policy", "no rule covers this command", "policy"),
        ("no-chaining", "chained commands are not allowed", "policy"),
    ],
)
def test_denials_are_classified(hook: str, reason: str, want: str) -> None:
    assert denial_class(hook, reason) == want


def test_a_question_answered_by_resume_is_correlated_on_the_next_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_cli.dispatch import worktrees

    d: dict[str, Any] = {
        "id": "x",
        "attempt": 1,
        "thread": [],
        "open_question": "1-q1",
        "setup_pending": True,
    }
    notice_producers.record_answer(d, "manager", "use cost.py")
    assert "open_question" not in d and d["answered_question"] == {"id": "1-q1", "attempt": 2}
    d["attempt"] = 2
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(telemetry_events, "launch", lambda *_a: None)
    monkeypatch.setattr(
        telemetry_events, "transition", lambda _r, _d, phase, **data: seen.append((phase, data))
    )
    worktrees.AttemptClock(Path("/unused"), d)
    assert seen == [("model-active", {"question_id": "1-q1"})]
    d["attempt"] = 3  # a later attempt does not claim the old answer
    seen.clear()
    worktrees.AttemptClock(Path("/unused"), d)
    assert seen == [("model-active", {})]
