"""`ask-guard` blocks a manager's Stop once when the reply ends asking the owner
something no question tool registered this turn.

These tests pin the detector against a labelled corpus of turn endings, the once-per-turn rule
and its turn identity, the peer-stop interaction, question coverage across routes, the Stop
group's order (`lease-yield` and `activity` see the block), worker scope and the telemetry.
"""

from __future__ import annotations

import importlib
import json
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from agent_cli import sessions, tab_drain
from agent_cli.hooks import ask_guard as guard
from agent_cli.hooks.attention import clear_question, raise_question
from agent_cli.hooks.groups import GROUPS
from agent_cli.leases import HOLDER_STOPPED_KEY
from agent_cli.ops import sessions as session_ops
from agent_cli.telemetry import recorder as recorder_mod
from agent_cli.telemetry.recorder import main_of
from agent_cli.telemetry.sink import Sink
from harness_root import FIXED_NOW, git, repoint_files

run_mod = importlib.import_module("agent_cli.hooks.run")
activity_mod = importlib.import_module("agent_cli.hooks.activity")
CORPUS = Path(__file__).parent / "fixtures" / "ask_guard_corpus.jsonl"
SID = "s-ask"
PUSH = "The relink landed at `ea3233b`.\n\nShould I push main now?"
DONE = "Landed 045 at `a1b2c3d`. Nothing needs you."


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _root_template: Path) -> Path:
    """The shared harness root without its clones, venv and Obsidian views: the guard and the
    Stop groups read only session records, raw capture and telemetry, and the full copy was
    most of each test's cost."""
    shutil.copytree(
        _root_template / "harness",
        tmp_path / "harness",
        symlinks=True,
        ignore=lambda d, _names: ["repos", ".venv", "obsidian"] if Path(d).name == "harness" else [],
    )
    repoint_files(tmp_path, _root_template, ("harness/agent.yaml",))
    root = tmp_path / "harness"
    monkeypatch.setenv("AGENT_ROOT", str(root))
    monkeypatch.setenv("AGENT_NOW", FIXED_NOW)
    monkeypatch.setenv("AGENT_NOTIFY", "off")
    for name in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(root)
    return root


@pytest.fixture(autouse=True)
def quiet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loss = tmp_path / "losstmp"
    loss.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(loss))
    monkeypatch.setattr(recorder_mod, "_process", None)
    monkeypatch.setattr(activity_mod, "refresh_tab", lambda *a, **k: None)
    monkeypatch.setattr(tab_drain, "request", lambda *_a, **_kw: {"started": True})
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)


def _prompt(root: Path, turn: str = "", codex: bool = False) -> None:
    payload: dict[str, Any] = {"session_id": SID, "hook_event_name": "UserPromptSubmit", "prompt": "go"}
    if turn:
        payload["turn_id" if codex else "prompt_id"] = turn
    assert guard.ask_guard(root, payload) == ""


def _stop(root: Path, text: str, turn: str = "", codex: bool = False, **extra: Any) -> str:
    payload: dict[str, Any] = {
        "session_id": SID,
        "hook_event_name": "Stop",
        "last_assistant_message": text,
        **extra,
    }
    if turn:
        payload["turn_id" if codex else "prompt_id"] = turn
    return guard.ask_guard(root, payload)


def _blocked(out: str) -> bool:
    return bool(out) and json.loads(out).get("decision") == "block"


def _fires(root: Path) -> list[dict[str, Any]]:
    events = Sink.for_main(main_of(root)).events
    lines = [line for p in sorted(events.glob("*.jsonl")) for line in p.read_text().splitlines()]
    return [e for e in map(json.loads, lines) if e["kind"] == "workflow.ask_guard"]


# --- the detector -----------------------------------------------------------------------------


def _corpus() -> list[dict[str, Any]]:
    return [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_corpus_is_labelled_and_covers_each_kind():
    rows = _corpus()
    assert len(rows) >= 40
    kinds = {r["kind"] for r in rows}
    assert {"rhetorical", "quoted", "code", "status", "ask", "decision"} <= kinds
    assert all("miss" in r for r in rows if r["kind"] == "miss")


@pytest.mark.parametrize("row", _corpus(), ids=lambda r: f"{r['kind']}:{r['text'][:40]}")
def test_the_detector_never_fires_on_a_non_ask_and_misses_only_with_a_reason(row: dict[str, Any]):
    asks = guard.asks_in(row["text"])
    asked = asks[-1] if asks else ""
    if not row["ask"]:
        assert asked == "", asked
    elif "miss" in row:
        assert asked == "", f"a documented miss now fires; drop its reason: {row['miss']}"
    else:
        assert asked, row["text"]


def test_coverage_is_equality_after_normalising():
    """Strict since review round 3; since round 5 both sides go through one parser-based
    function (`comparable`): Markdown syntax that parses as markup (code-span backticks,
    emphasis, HTML tags), case, spacing, trailing punctuation and the asking phrase drop out;
    any other difference fires."""
    assert guard.covers("push main now", "Should I push main now?")
    assert guard.covers("Should I push `main`?", "Want me to push main?")
    assert guard.covers("Should I push <code>main</code>?", "Should I push main?")
    assert guard.covers("Should I push **main**?", "should i push main")
    assert guard.covers("Should I push release_candidate?", "Should I push `release_candidate`?")
    assert not guard.covers("Should I push releasecandidate?", "Should I push release_candidate?")
    assert not guard.covers("Should I deploy versionnext?", "Should I deploy version~next?")
    assert guard.covers('Should I deploy to "production"?', 'Do you want me to deploy to "production"')
    # Quote marks are prose, not markup: dropping them was regex stripping, removed in round 5.
    assert not guard.covers('Should I deploy to "production"?', "Do you want me to deploy to production")
    assert guard.covers("Which repo holds the config fix?", "which  repo holds the config fix")
    assert guard.covers(r"Should I push release\_candidate?", "Should I push release_candidate?")
    assert guard.covers("Should I use alpha&amp;beta?", "Should I use alpha&beta?")
    assert not guard.covers("Should I use the pattern `ab`?", "Should I use the pattern `a**b`?")
    assert guard.covers("Should I use a**b?", "Should I use a**b?")
    assert guard.comparable("Should I use a**b?") == "use a**b"
    assert not guard.covers("Should I push the docs branch?", "Should I push main now?")
    assert not guard.covers("push", "Should I push main now?")
    assert not guard.covers("", "Should I push?")
    assert not guard.covers("Should I push?", "")


def test_containment_no_longer_covers_in_either_direction():
    """A paraphrase or a part used to be covered; it now fires (review round 3)."""
    assert not guard.covers("push main", "Should I push main now?")  # was covered by word overlap
    assert not guard.covers("Should I push main to origin now?", "Should I push main now?")
    assert not guard.covers("Should I not delete the release branch?", "Should I delete the release branch?")


# --- once per logical turn --------------------------------------------------------------------


def test_two_successive_owner_turns_each_ending_in_an_ask_both_fire(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _blocked(_stop(tmp_root, PUSH, "p1"))
    assert _stop(tmp_root, PUSH, "p1", stop_hook_active=True) == ""  # the continuation ends
    _prompt(tmp_root, "p2")
    assert _blocked(_stop(tmp_root, PUSH, "p2"))


def test_repeated_stops_in_one_turn_fire_once(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "t1", codex=True)
    outs = [_stop(tmp_root, PUSH, "t1", codex=True, stop_hook_active=i > 0) for i in range(4)]
    assert [_blocked(o) for o in outs] == [True, False, False, False]
    assert len(_fires(tmp_root)) == 1


def test_without_a_client_turn_id_the_turn_is_counted_at_each_prompt(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root)
    assert _blocked(_stop(tmp_root, PUSH))
    assert _stop(tmp_root, PUSH, stop_hook_active=True) == ""
    _prompt(tmp_root)
    assert _blocked(_stop(tmp_root, PUSH))
    assert (sessions.get(tmp_root, SID) or {})[guard.STATE]["turn"] == 2


def test_a_reply_that_ends_without_an_ask_passes(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _stop(tmp_root, DONE, "p1") == ""
    assert _stop(tmp_root, "", "p1") == ""
    assert _fires(tmp_root) == []


# --- peer-stop interaction --------------------------------------------------------------------


def test_a_peer_stop_block_spends_nothing_and_the_next_stop_of_the_turn_fires(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _stop(tmp_root, DONE, "p1", _stop_blocked=True) == ""
    assert (sessions.get(tmp_root, SID) or {})[guard.STATE]["pending_turn"] == 1
    out = _stop(tmp_root, "The worker's result is in.\n\npush?", "p1", stop_hook_active=True)
    assert _blocked(out)
    assert _fires(tmp_root)[0]["data"] == {"client": "claude", "after_block": True}


def test_in_the_claude_stop_group_a_peer_stop_block_then_push_fires(tmp_root, monkeypatch):
    peer = iter([json.dumps({"decision": "block", "reason": "a peer message"}), ""])
    monkeypatch.setitem(run_mod.HANDLERS, "peer-stop", lambda _root, _payload: next(peer))
    sessions.set_record(tmp_root, SID, pid=4242, name="mgr-009-x")
    _prompt(tmp_root, "p1")
    stop = {
        "session_id": SID,
        "hook_event_name": "Stop",
        "prompt_id": "p1",
        "last_assistant_message": "push?",
    }
    first, _ = run_mod.run(tmp_root, "stop", json.dumps(stop))
    assert json.loads(first) == {"decision": "block", "reason": "a peer message"}
    second, _ = run_mod.run(tmp_root, "stop", json.dumps({**stop, "stop_hook_active": True}))
    assert "AskUserQuestion" in json.loads(second)["reason"]
    third, _ = run_mod.run(tmp_root, "stop", json.dumps({**stop, "stop_hook_active": True}))
    assert third == ""


# --- what registers a question ----------------------------------------------------------------


def _ask_tool(root: Path, question: str, turn: str) -> None:
    """Claude's AskUserQuestion as the pre-ask group sees it, then its answer."""
    raise_question(root, SID, "sync")  # `activity` at PreToolUse
    pre = {
        "session_id": SID,
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "prompt_id": turn,
        "tool_input": {"questions": [{"question": question, "options": []}]},
    }
    assert guard.ask_guard(root, pre) == ""
    clear_question(root, SID, "answered")  # `activity` at PostToolUse: the owner answered


def test_an_answered_tool_question_does_not_cover_a_different_final_question(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _ask_tool(tmp_root, "Which repo holds the config fix?", "p1")
    assert _blocked(_stop(tmp_root, PUSH, "p1"))


def test_the_tool_question_covers_a_final_line_that_restates_it(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _ask_tool(tmp_root, "Should I push main now?", "p1")
    assert _stop(tmp_root, PUSH, "p1") == ""


def test_a_question_registered_in_an_earlier_turn_covers_nothing_now(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _ask_tool(tmp_root, "Should I push main now?", "p1")
    _prompt(tmp_root, "p2")
    assert _blocked(_stop(tmp_root, PUSH, "p2"))


def test_codex_a_visible_question_followed_by_owner_question_passes(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "t1", codex=True)
    session_ops.owner_question(tmp_root, session_id=SID, summary="push main now")
    assert _stop(tmp_root, PUSH, "t1", codex=True) == ""


def test_codex_owner_question_without_a_summary_cannot_show_it_covers_the_ask(tmp_root):
    """When unsure, fire: the continuation re-raises it with the question as `summary`."""
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "t1", codex=True)
    session_ops.owner_question(tmp_root, session_id=SID)
    assert _blocked(_stop(tmp_root, PUSH, "t1", codex=True))


def test_codex_owner_question_about_something_else_does_not_cover_the_ask(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "t1", codex=True)
    session_ops.owner_question(tmp_root, session_id=SID, summary="which repo holds the config fix")
    out = _stop(tmp_root, PUSH, "t1", codex=True)
    assert _blocked(out)
    assert "owner_question" in json.loads(out)["reason"]


def test_codex_an_answered_request_user_input_does_not_cover_a_later_ask(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "t1", codex=True)
    raise_question(tmp_root, SID, "sync")
    clear_question(tmp_root, SID, "answered")
    assert _blocked(_stop(tmp_root, PUSH, "t1", codex=True))


def test_a_wake_turn_reasking_the_pending_hands_question_passes(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    session_ops.owner_question(tmp_root, session_id=SID, summary="push main now")
    _prompt(tmp_root, "p2")  # a machine wake: the hand stays up
    assert _stop(tmp_root, "Should I push main now?", "p2") == ""


def test_a_lead_in_before_the_reask_now_fires(tmp_root):
    """Covered until round 4 by dropping a lead-in before a colon; that could also drop an
    operand ("Production: should I deploy?"), so the comparison keeps it and fires once."""
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    session_ops.owner_question(tmp_root, session_id=SID, summary="push main now")
    _prompt(tmp_root, "p2")
    assert _blocked(_stop(tmp_root, "Still waiting on you: should I push main now?", "p2"))
    assert not guard.covers("Staging: should I deploy?", "Production: should I deploy?")


# --- the Stop group ---------------------------------------------------------------------------


def test_the_guard_runs_after_peer_stop_and_before_lease_yield_and_activity():
    assert [m.name for m in GROUPS["stop"].members] == [
        "capture",
        "peer-stop",
        "ask-guard",
        "record-guard",
        "lease-yield",
        "activity",
    ]
    assert [m.name for m in GROUPS["codex-stop"].members] == [
        "capture",
        "ask-guard",
        "record-guard",
        "lease-yield",
        "codex-activity",
    ]
    for name in ("prompt-submit", "codex-prompt-submit"):
        assert [m.name for m in GROUPS[name].members][:2] == ["capture", "ask-guard"]


def test_a_guard_block_leaves_the_session_working_and_its_leases_held(tmp_root):
    sessions.set_record(tmp_root, SID, pid=4242, name="mgr-009-x")
    sessions.set_field(tmp_root, SID, "activity", {"state": "working"})
    _prompt(tmp_root, "p1")
    stop = {"session_id": SID, "hook_event_name": "Stop", "prompt_id": "p1", "last_assistant_message": PUSH}
    out, _ = run_mod.run(tmp_root, "stop", json.dumps(stop))
    assert json.loads(out)["decision"] == "block"
    record = sessions.get(tmp_root, SID) or {}
    assert record["activity"]["state"] == "working"
    assert sessions.get_counter(tmp_root, SID, HOLDER_STOPPED_KEY) == 0
    out, _ = run_mod.run(tmp_root, "stop", json.dumps({**stop, "stop_hook_active": True}))
    assert out == ""
    assert (sessions.get(tmp_root, SID) or {})["activity"]["state"] == "idle"
    assert sessions.get_counter(tmp_root, SID, HOLDER_STOPPED_KEY) > 0


# --- scope and telemetry ----------------------------------------------------------------------


@pytest.mark.parametrize("where", ["agent_id", "dispatch_record", "dispatch_env"])
def test_never_in_a_worker_or_a_subagent(tmp_root, monkeypatch, where):
    sessions.set_record(tmp_root, SID, dispatch_id="001-mac-04" if where == "dispatch_record" else None)
    if where == "dispatch_env":
        monkeypatch.setenv("AGENT_DISPATCH_ID", "001-mac-04")
    extra: dict[str, Any] = {"agent_id": "a1"} if where == "agent_id" else {}
    assert _stop(tmp_root, PUSH, "p1", **extra) == ""


def test_a_fire_is_counted_as_a_detected_raw_text_ask_without_its_text(tmp_root):
    sessions.set_record(tmp_root, SID, feature={"id": 9, "slug": "public-launch", "dir": "features/x"})
    _prompt(tmp_root, "t1", codex=True)
    assert _blocked(_stop(tmp_root, PUSH, "t1", codex=True))
    (event,) = _fires(tmp_root)
    assert (event["session"], event["feature"]) == (SID, "009")
    assert event["data"] == {"client": "codex", "after_block": False}
    assert "push" not in json.dumps(event).lower()


def test_the_guard_can_be_switched_off():
    from agent_cli.hooks.toggles import TOGGLEABLE

    assert "ask-guard" in TOGGLEABLE


# --- Executed probes, kept as regressions ---


@pytest.mark.parametrize(
    ("first", "then"),
    [
        ({"prompt_id": "p1"}, {}),
        ({}, {"prompt_id": "p1"}),
        ({"turn_id": "t1"}, {"turn_id": "t2"}),
    ],
    ids=["claude-id-disappears", "claude-id-appears", "codex-id-changes"],
)
def test_a_changed_client_id_within_one_prompt_never_blocks_again(tmp_root, first, then):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root)
    assert _blocked(_stop(tmp_root, PUSH, **first))
    assert _stop(tmp_root, PUSH, stop_hook_active=True, **then) == ""


def test_six_changing_ids_in_one_prompt_give_one_block(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "t0", codex=True)
    outs = [_stop(tmp_root, PUSH, f"t{i}", codex=True, stop_hook_active=i > 0) for i in range(6)]
    assert sum(_blocked(o) for o in outs) == 1


def test_alternating_ids_cannot_reblock(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root)
    outs = [_stop(tmp_root, PUSH, f"t{i % 2}", codex=True, stop_hook_active=i > 0) for i in range(10)]
    assert sum(_blocked(o) for o in outs) == 1


def test_the_turn_survives_a_compaction_and_a_record_rewrite(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root)
    assert _blocked(_stop(tmp_root, PUSH))
    sessions.set_record(tmp_root, SID, name="resumed")
    guard.ask_guard(tmp_root, {"session_id": SID, "hook_event_name": "PreCompact"})
    assert _stop(tmp_root, PUSH, stop_hook_active=True) == ""


def test_concurrent_stops_of_one_turn_share_one_block(tmp_root, monkeypatch):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    barrier = threading.Barrier(2)
    seen = threading.local()
    original = sessions.get

    def read(root: Path, sid: str) -> dict[str, Any] | None:
        record = original(root, sid)
        if not getattr(seen, "read", False):  # both callers hold the same unfired read
            seen.read = True
            barrier.wait(timeout=5)
        return record

    monkeypatch.setattr(sessions, "get", read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outs = list(pool.map(lambda _: _stop(tmp_root, PUSH, "p1"), range(2)))
    assert sum(_blocked(o) for o in outs) == 1


def test_an_answered_related_question_does_not_cover_a_new_decision(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _ask_tool(tmp_root, "Should I push the docs branch?", "p1")
    assert _blocked(_stop(tmp_root, PUSH, "p1"))


def test_a_pending_related_question_from_an_earlier_turn_does_not_cover_a_new_decision(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    raise_question(tmp_root, SID, "manual", "Should I push the docs branch?")
    _prompt(tmp_root, "p2")
    assert _blocked(_stop(tmp_root, PUSH, "p2"))


CORRUPT = ["[]", "null", "7", '"a string"']


@pytest.mark.parametrize("content", CORRUPT)
def test_a_record_that_is_not_a_mapping_makes_the_guard_a_no_op(tmp_root, content):
    path = sessions.sessions_dir(tmp_root) / f"{SID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    for event in ("UserPromptSubmit", "Stop"):
        payload = {
            "session_id": SID,
            "hook_event_name": event,
            "prompt_id": "p1",
            "last_assistant_message": PUSH,
        }
        assert run_mod.run_one(tmp_root, "ask-guard", payload) == ""
    guard.register(tmp_root, SID, "manual", "push main")
    assert path.read_text() == content


@pytest.mark.parametrize("content", CORRUPT)
@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_a_record_that_is_not_a_mapping_blocks_neither_full_stop_group(tmp_root, content, codex):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "t1", codex=codex)
    (sessions.sessions_dir(tmp_root) / f"{SID}.json").write_text(content)
    payload = {
        "session_id": SID,
        "hook_event_name": "Stop",
        "last_assistant_message": PUSH,
        **({"turn_id": "t1"} if codex else {"prompt_id": "t1"}),
    }
    out, code = run_mod.run(tmp_root, "codex-stop" if codex else "stop", json.dumps(payload))
    assert code == 0
    assert not _blocked(out)


def test_a_failing_guard_leaves_the_later_stop_members_running(tmp_root, monkeypatch):
    visited: list[bool] = []

    def explode(*_a: Any) -> str:
        raise RuntimeError("probe")

    def later(_root: Path, payload: dict[str, Any]) -> str:
        visited.append(bool(payload.get("_stop_blocked")))
        return ""

    for name, handler in {
        "ask-guard": explode,
        "capture": lambda *_a: "",
        "peer-stop": lambda *_a: "",
        "lease-yield": later,
        "activity": later,
    }.items():
        monkeypatch.setitem(run_mod.HANDLERS, name, handler)
    out, code = run_mod.run(tmp_root, "stop", json.dumps({"session_id": SID, "hook_event_name": "Stop"}))
    assert (out, code, visited) == ("", 0, [False, False])


def test_a_subagent_type_alone_skips_the_guard(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _stop(tmp_root, PUSH, "p1", agent_type="reviewer") == ""


# --- Executed probes, kept as regressions ---


def _full_stop(root: Path, text: str, turn: str, codex: bool = False) -> str:
    payload = {
        "session_id": SID,
        "hook_event_name": "Stop",
        "last_assistant_message": text,
        "turn_id" if codex else "prompt_id": turn,
    }
    out, code = run_mod.run(root, "codex-stop" if codex else "stop", json.dumps(payload))
    assert code == 0
    return out


CONTAINERS = {
    "multiline-span": "The example is:\n\n`\nShould I push main?\n`",
    "list-fence": "- ```text\n  Should I push main?\n  ```",
    "lazy-quote": "> The worker asked:\nShould I push main?",
    "html-pre": "<pre>\nShould I push main?\n</pre>",
}


@pytest.mark.parametrize("case", sorted(CONTAINERS))
@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_a_question_inside_a_markdown_container_does_not_block(tmp_root, case, codex):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert not _blocked(_full_stop(tmp_root, CONTAINERS[case], "p1", codex))


def test_a_parser_failure_fails_open(tmp_root, monkeypatch):
    import markdown_it

    def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("parser")

    monkeypatch.setattr(markdown_it.MarkdownIt, "parse", broken)
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert guard.asks_in(PUSH) == []
    assert _stop(tmp_root, PUSH, "p1") == ""


@pytest.mark.parametrize("which", [0, 1])
def test_each_question_of_a_batched_tool_call_covers_its_own_restatement(tmp_root, which):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    questions = ["Should I push main to origin?", "Which release date works for you?"]
    pre: dict[str, Any] = {
        "session_id": SID,
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "prompt_id": "p1",
        "tool_input": {"questions": [{"question": q, "options": []} for q in questions]},
    }
    _, code = run_mod.run(tmp_root, "pre-ask", json.dumps(pre))
    assert code == 0
    clear_question(tmp_root, SID, "answered")
    assert not _blocked(_full_stop(tmp_root, questions[which], "p1"))


@pytest.mark.parametrize("shape", ["list", "prose"])
@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_a_covered_question_does_not_hide_another_ask_in_the_final_paragraph(tmp_root, shape, codex):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    covered = "Should I close feature 045?"
    _ask_tool(tmp_root, covered, "p1")
    ending = {
        "list": f"1. Should I push main now?\n2. {covered}",
        "prose": f"Should I push main now? {covered}",
    }
    out = _full_stop(tmp_root, ending[shape], "p1", codex)
    assert _blocked(out)
    reason = json.loads(out)["reason"]
    assert "push main" in reason and "close feature 045" not in reason


def test_every_ask_covered_passes(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _ask_tool(tmp_root, "Should I push main now?", "p1")
    _ask_tool(tmp_root, "Should I close feature 045?", "p1")
    assert _stop(tmp_root, "1. Should I push main now?\n2. Should I close feature 045?", "p1") == ""


DEPLOY = (
    "Should I deploy the reviewed release artifact from branch main using the approved workflow to {} now?"
)


@pytest.mark.parametrize("pending", [False, True], ids=["answered", "pending"])
@pytest.mark.parametrize(
    ("asked", "ending"),
    [
        (DEPLOY.format("staging"), DEPLOY.format("production")),
        ("Should I merge main into release?", "Should I merge release into main?"),
        ("Should I close feature 045?", "Should I close feature 046?"),
    ],
    ids=["release-target", "merge-direction", "feature-id"],
)
def test_a_changed_operand_does_not_share_coverage(tmp_root, pending, asked, ending):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    if pending:
        raise_question(tmp_root, SID, "manual", asked)
        _prompt(tmp_root, "p2")
    else:
        _ask_tool(tmp_root, asked, "p1")
    assert _blocked(_full_stop(tmp_root, ending, "p2" if pending else "p1"))


def test_order_and_operands_are_kept_by_strict_coverage():
    assert not guard.covers("Should I merge main into release?", "Should I merge release into main?")
    assert not guard.covers(DEPLOY.format("staging"), DEPLOY.format("production"))
    assert guard.covers("Want me to push main now?", "Should I push main now?")
    # An added "now" was covered under round-2 similarity; strict coverage fires on it.
    assert not guard.covers("Should I merge main into release?", "Should I merge main into release now?")


@pytest.mark.parametrize("case", ["changed-id", "no-id", "worker"])
def test_identity_and_worker_boundaries(tmp_root, case):
    sessions.set_record(tmp_root, SID, dispatch_id="worker-1" if case == "worker" else None)
    _prompt(tmp_root)
    first = _blocked(_stop(tmp_root, PUSH, "t1", codex=True))
    second = _blocked(_stop(tmp_root, PUSH, "t2" if case == "changed-id" else "", codex=True))
    assert (first, second) == ((False, False) if case == "worker" else (True, False))


def test_a_pending_exact_question_survives_a_wake(tmp_root):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    raise_question(tmp_root, SID, "manual", "Should I push main now?")
    _prompt(tmp_root, "p2")
    assert not _blocked(_stop(tmp_root, "Should I push main now?", "p2"))


def test_telemetry_carries_no_question_text(tmp_root, monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(recorder_mod, "record", lambda *_a, **kw: calls.append(kw))
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _blocked(_stop(tmp_root, "Should I push main now?", "p1"))
    assert len(calls) == 1
    assert calls[0]["data"] == {"client": "claude", "after_block": False}
    assert "push" not in json.dumps(calls)


# --- Executed probes, kept as regressions ---


def _route(root: Path, question: str, codex: bool) -> None:
    """Ask `question` the supported way: Codex's owner_question, or Claude's pre-ask group."""
    if codex:
        session_ops.owner_question(root, session_id=SID, summary=question)
        return
    pre = {
        "session_id": SID,
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "prompt_id": "p1",
        "tool_input": {"questions": [{"question": question, "options": []}]},
    }
    _, code = run_mod.run(root, "pre-ask", json.dumps(pre))
    assert code == 0


# An earlier pending question goes through owner_question for either client, so it runs once.
ROUTES = pytest.mark.parametrize(
    ("codex", "pending"),
    [(False, False), (True, False), (False, True)],
    ids=["claude-answered", "codex-answered", "earlier-pending"],
)
# The client changes only the turn key the guard reads, not how it reads the text: text-only
# cases run in Claude's Stop group, and the Codex group is covered by the cases that route.
TEXT_ONLY = pytest.mark.parametrize("codex", [False], ids=["claude"])

CHANGED_DECISIONS = [
    pytest.param("Should I push?", "Should I push `main`?", id="inline-target"),
    pytest.param("Should I deploy?", 'Should I deploy "production"?', id="quoted-target"),
    pytest.param(
        "Should I delete the release branch?", "Should I not delete the release branch?", id="negation"
    ),
    pytest.param(
        "Should I copy the release artifact to staging?",
        "Should I copy the release artifact from staging?",
        id="direction-word",
    ),
    pytest.param(
        "Should I push the reviewed changes on branch alpha after the approved checks pass?",
        "Should I push the reviewed changes on branch beta after the approved checks pass?",
        id="arbitrary-branch",
    ),
    pytest.param(
        "Should I build the reviewed release artifact for branch main using the approved workflow?",
        "Should I delete the reviewed release artifact for branch main using the approved workflow?",
        id="action-verb",
    ),
    pytest.param(
        "Should I not delete the release branch?",
        "Should I delete the release branch?",
        id="negation-dropped",
    ),
]


@pytest.mark.parametrize(("registered", "ending"), CHANGED_DECISIONS)
@ROUTES
def test_a_different_decision_is_not_covered(tmp_root, registered, ending, codex, pending):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert guard.asks_in(ending)
    if pending:
        session_ops.owner_question(tmp_root, session_id=SID, summary=registered)
        _prompt(tmp_root, "p2")
    else:
        _route(tmp_root, registered, codex)
        clear_question(tmp_root, SID, "answered")
    assert _blocked(_full_stop(tmp_root, ending, "p2" if pending else "p1", codex))


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Should I push `main`?", id="inline-name"),
        pytest.param("Should I close feature `045`?", id="inline-id"),
        pytest.param('Should I deploy to "production"?', id="quoted-name"),
    ],
)
def test_an_identical_routed_question_with_code_or_quotes_is_covered(tmp_root, codex, question):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route(tmp_root, question, codex)
    if not codex:
        clear_question(tmp_root, SID, "answered")
    assert not _blocked(_full_stop(tmp_root, question, "p1", codex))


@TEXT_ONLY
@pytest.mark.parametrize(
    "ending",
    [
        pytest.param("- # Should I push main?", id="list-heading"),
        pytest.param("- Status:\n  ## Should I push main?", id="nested-heading"),
        pytest.param("# Should I push main?", id="top-heading"),
        pytest.param("- ```text\n  Should I push main?\n  ```", id="list-code"),
        pytest.param("> A question:\nShould I push main?", id="lazy-blockquote"),
        pytest.param("<code>Should I push main?</code>", id="single-paragraph-code"),
    ],
)
def test_headings_code_and_quotes_never_ask(tmp_root, codex, ending):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert not _blocked(_full_stop(tmp_root, ending, "p1", codex))


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_several_uncovered_questions_block_once(tmp_root, codex):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    ending = "Should I push main? Should I close feature 045?"
    assert _blocked(_full_stop(tmp_root, ending, "p1", codex))
    assert not _blocked(_full_stop(tmp_root, ending, "p1", codex))


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_a_new_turn_still_blocks(tmp_root, codex):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _blocked(_full_stop(tmp_root, "Should I push main?", "p1", codex))
    _prompt(tmp_root, "p2")
    assert _blocked(_full_stop(tmp_root, "Should I push main?", "p2", codex))


# --- Executed probes, kept as regressions ---


def _route4(root: Path, question: str, codex: bool, pending: bool) -> None:
    """Route a question as the earlier probes did: Codex or an earlier pending question through
    owner_question, Claude through the pre-ask group; then answer it or start the next turn."""
    if codex or pending:
        session_ops.owner_question(root, session_id=SID, summary=question)
    else:
        _route(root, question, codex=False)
    if pending:
        _prompt(root, "p2")
    else:
        clear_question(root, SID, "answered")


OPERAND_LOSS = [
    pytest.param("Should I push releasecandidate?", "Should I push release_candidate?", id="bare-underscore"),
    pytest.param("Should I push?", "Should I push <code>main</code>?", id="html-code-operand"),
    pytest.param("Should I deploy versionnext?", "Should I deploy version~next?", id="literal-tilde"),
]


@pytest.mark.parametrize(("registered", "ending"), OPERAND_LOSS)
@ROUTES
def test_a_changed_literal_operand_is_uncovered(tmp_root, registered, ending, codex, pending):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert guard.asks_in(ending)
    assert not guard.covers(registered, ending)
    _route4(tmp_root, registered, codex, pending)
    assert _blocked(_full_stop(tmp_root, ending, "p2" if pending else "p1", codex))


@pytest.mark.parametrize(
    "question",
    [
        "Should I push release_candidate?",
        "Should I push <code>main</code>?",
        "Should I deploy version~next?",
        'Should I push "`main`"?',
        "Should I push `release_candidate`?",
        'Should I push "release_candidate"?',
        "Should I push main?",
    ],
)
@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_an_identical_routed_literal_question_is_covered_once_per_turn(tmp_root, question, codex):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route4(tmp_root, question, codex, pending=False)
    assert not _blocked(_full_stop(tmp_root, question, "p1", codex))
    _prompt(tmp_root, "p2")
    assert _blocked(_full_stop(tmp_root, "Should I close feature 046?", "p2", codex))
    assert not _blocked(_full_stop(tmp_root, "Should I close feature 046?", "changed-native", codex))


@pytest.mark.parametrize(
    "text",
    [
        'Should I push "`main`"?',
        "Should I push <code>`main`</code>?",
        'Should I push "a `b` and \\"c\\""?',
        "Is `x` ready? Should I push \"`main` to 'origin'\" or <kbd>release_candidate</kbd>?",
    ],
)
def test_no_placeholder_survives_in_an_extracted_ask(text):
    """Since round 6 an ask is a slice of the walk's `compare` text: rendered, with every code
    and quotation content in place and no mark."""
    asks = guard.asks_in(text)
    assert asks
    assert all(guard._MARK not in a for a in asks)  # pyright: ignore[reportPrivateUsage]
    assert "main" in asks[-1] or "release_candidate" in asks[-1] or "b" in asks[-1]


def test_no_corpus_ask_holds_a_placeholder():
    for row in _corpus():
        for ask in guard.asks_in(row["text"]):
            assert guard._MARK not in ask, row["text"]  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@pytest.mark.parametrize(
    "ending", ["- # Should I push main?", "`Should I push main?`", "> Should I push main?"]
)
def test_excluded_containers_still_do_not_block(tmp_root, ending, codex):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert not _blocked(_full_stop(tmp_root, ending, "p1", codex))


SECRET_ASK = "Should I push zebra_quartz_branch?"


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_no_written_event_or_log_holds_the_question_text(tmp_root, codex):
    """The block reason quotes the owner's questions to the model; nothing persisted may. The
    denial logger needs a git checkout, so the root is one. Raw captures (`.agent/raw`) and the
    session record keep the reply and the registered questions by design and are not logs."""
    git(["init"], tmp_root)
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    out = _full_stop(tmp_root, f"Landed.\n\n{SECRET_ASK}", "p1", codex)
    assert _blocked(out) and "zebra_quartz_branch" in json.loads(out)["reason"]
    events = [
        json.loads(line)
        for path in Sink.for_main(main_of(tmp_root)).events.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert {"workflow.ask_guard", "hook.denial"} <= {e["kind"] for e in events}
    denial = next(e for e in events if e["kind"] == "hook.denial" and e["data"]["hook"] == "ask-guard")
    assert denial["data"]["reason"] == "ask-guard: 1 uncovered asks"
    skipped = (tmp_root / ".agent" / "raw", sessions.sessions_dir(tmp_root))
    written = [
        p
        for p in tmp_root.rglob("*")
        if p.is_file() and ".git" not in p.parts and not any(p.is_relative_to(s) for s in skipped)
    ]
    assert written
    leaks = [p for p in written if "zebra_quartz_branch" in p.read_text(errors="replace")]
    assert leaks == []


# --- Executed probes, kept as regressions ---


@ROUTES
@pytest.mark.parametrize("run_length", [2, 3])
def test_a_changed_in_word_asterisk_operand_is_uncovered(tmp_root, codex, pending, run_length):
    ending = "Should I use the pattern `a" + "*" * run_length + "b`?"
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert guard.asks_in(ending)
    _route4(tmp_root, "Should I use the pattern `ab`?", codex, pending)
    assert _blocked(_full_stop(tmp_root, ending, "p2" if pending else "p1", codex))


@ROUTES
@pytest.mark.parametrize(
    "question",
    [
        pytest.param(r"Should I push release\_candidate?", id="escaped-underscore"),
        pytest.param(r"Should I deploy version\.next?", id="escaped-dot"),
        pytest.param("Should I use alpha&amp;beta?", id="entity"),
        pytest.param("Should I use alpha&#58;beta?", id="numeric-entity"),
        pytest.param('Should I push "`release__candidate`"?', id="nested"),
        pytest.param("Should I use <kbd>alpha_beta</kbd>?", id="html-code"),
        pytest.param("Should I use the pattern `a**b`?", id="same-asterisks"),
        pytest.param("Should I use a**b?", id="literal-asterisks"),
    ],
)
def test_an_identical_routed_markdown_question_is_covered(tmp_root, codex, pending, question):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert guard.asks_in(question)
    _route4(tmp_root, question, codex, pending)
    assert not _blocked(_full_stop(tmp_root, question, "p2" if pending else "p1", codex))


@ROUTES
@pytest.mark.parametrize("ending", ["Should I use the pattern `a*b`?", "Should I use the pattern `a__b`?"])
def test_other_changed_literal_operands_still_block(tmp_root, codex, pending, ending):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route4(tmp_root, "Should I use the pattern `ab`?", codex, pending)
    assert _blocked(_full_stop(tmp_root, ending, "p2" if pending else "p1", codex))


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_a_native_id_change_and_a_compaction_spend_no_second_block(tmp_root, codex):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _blocked(_full_stop(tmp_root, "Should I push main?", "p1", codex))
    out, code = run_mod.run(
        tmp_root, "precompact", json.dumps({"session_id": SID, "hook_event_name": "PreCompact"})
    )
    assert (out, code) == ("", 0)
    assert not _blocked(_full_stop(tmp_root, "Should I push main?", "continued", codex))
    _prompt(tmp_root, "p2")
    assert _blocked(_full_stop(tmp_root, "Should I push main?", "p2", codex))


def test_one_walk_builds_both_sides():
    """Round 6: an ask's comparison text is its sentence's `compare` slice from the reply's one
    token walk; a registration's is the same walk over its raw text (`comparable`). Identical
    Markdown therefore compares equal, with no slice parsed twice."""
    for question in (r"Push release\_candidate?", "Use alpha&#58;beta?", "Use `a**b`?", '"`x`"?'):
        ask = f"Should I {question[0].lower()}{question[1:]}"
        (sentence,) = guard._asks(ask)  # pyright: ignore[reportPrivateUsage]
        assert guard._normal(sentence.compare) == guard.comparable(ask)  # pyright: ignore[reportPrivateUsage]
        assert len(sentence.detect) == len(sentence.compare)
        assert guard.covers(ask, ask)


# --- Executed probes, kept as regressions ---

HTML_CASES = {
    "code-sentence": "Example: <code>Example sentence. Should I push main?</code>",
    "code-newline": "Example: <code>Example sentence.\nShould I push main?</code>",
    "kbd-sentence": "Example: <kbd>Example sentence. Should I push main?</kbd>",
    "samp-sentence": "Example: <samp>Example sentence. Should I push main?</samp>",
    "tt-sentence": "Example: <tt>Example sentence. Should I push main?</tt>",
    "code-middle": "Example: <code>Example. Should I push main? Example continues.</code>",
    "entity-quote": "Example: &quot;Example. Should I push main? Example continues.&quot;",
    "entity-curly": "Example: &#8220;Example. Should I push main? Example continues.&#8221;",
    "code-single": "Example: <code>Should I push main?</code>",
    "code-span": "Example: `Example sentence. Should I push main?`",
    "plain-quote": 'Example: "Example sentence. Should I push main?"',
    "block-quote": "> Example sentence. Should I push main?",
    "html-pre": "<pre>\nExample sentence. Should I push main?\n</pre>",
}


@TEXT_ONLY
@pytest.mark.parametrize("case", sorted(HTML_CASES))
def test_inline_code_and_quotations_survive_sentence_splitting(tmp_root, codex, case):
    assert guard.asks_in(HTML_CASES[case]) == []
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert not _blocked(_full_stop(tmp_root, HTML_CASES[case], "p1", codex))


QUESTIONS = ["Should I push main?", "Should I close feature 045?"]
STYLED = {
    "strong": "**Should I push main? Should I close feature 045?**",
    "emphasis": "*Should I push main? Should I close feature 045?*",
    "underscore-strong": "__Should I push main? Should I close feature 045?__",
    "underscore-emphasis": "_Should I push main? Should I close feature 045?_",
    "separate-strong": "**Should I push main?** **Should I close feature 045?**",
    "plain": "Should I push main? Should I close feature 045?",
}


def _route_all(root: Path, questions: list[str], codex: bool) -> None:
    if codex:
        for question in questions:
            session_ops.owner_question(root, session_id=SID, summary=question)
    else:
        pre = {
            "session_id": SID,
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "prompt_id": "p1",
            "tool_input": {"questions": [{"question": q, "options": []} for q in questions]},
        }
        _, code = run_mod.run(root, "pre-ask", json.dumps(pre))
        assert code == 0
    clear_question(root, SID, "answered")


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@pytest.mark.parametrize("case", sorted(STYLED))
def test_routed_questions_under_shared_or_separate_emphasis_are_covered(tmp_root, codex, case):
    assert guard.asks_in(STYLED[case]) == QUESTIONS
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route_all(tmp_root, QUESTIONS, codex)  # each owner_question call registers its own summary
    assert not _blocked(_full_stop(tmp_root, STYLED[case], "p1", codex))


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_an_unregistered_ask_blocks_once(tmp_root, codex):
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _blocked(_full_stop(tmp_root, QUESTIONS[0], "p1", codex))
    assert not _blocked(_full_stop(tmp_root, QUESTIONS[0], "p1", codex))


# --- Executed probes, kept as regressions ---

BREAKS = pytest.mark.parametrize("line_break", ["\n", "  \n"], ids=["softbreak", "hardbreak"])


@TEXT_ONLY
@pytest.mark.parametrize(
    "quotes", ['""', "“”", "«»", ("&quot;", "&quot;")], ids=["straight", "curly", "guillemets", "entity"]
)
@BREAKS
def test_a_quotation_wrapped_over_lines_does_not_ask(tmp_root, codex, quotes, line_break):
    text = (
        f"Example: {quotes[0]}Example sentence.{line_break}"
        f"Should I push main?{line_break}Example continues.{quotes[1]}"
    )
    assert guard.asks_in(text) == []
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert not _blocked(_full_stop(tmp_root, text, "p1", codex))


@TEXT_ONLY
@pytest.mark.parametrize("tag", ["code", "kbd", "samp", "tt", "pre"])
@pytest.mark.parametrize(
    "shape",
    [
        "Example: <{tag}>Example sentence.\n\nShould I push main?</{tag}>",
        "Example: <{tag}>Example sentence.\n\n- Should I push main?\n\n</{tag}>",
        "Example: <{tag}>Example sentence, never closed.\n\nShould I push main?",
    ],
    ids=["closed-later", "list-between", "never-closed"],
)
def test_an_inline_code_element_stays_open_across_blocks(tmp_root, codex, tag, shape):
    text = shape.format(tag=tag)
    assert guard.asks_in(text) == []
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert not _blocked(_full_stop(tmp_root, text, "p1", codex))


def test_a_closed_inline_code_element_ends_the_code():
    assert guard.asks_in("Example: <code>x</code>.\n\nShould I push main?") == ["Should I push main?"]
    assert guard.asks_in("<code>\nx\n</code>\n\nShould I push main?") == ["Should I push main?"]


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@BREAKS
def test_an_identical_wrapped_registered_question_is_covered(tmp_root, codex, line_break):
    text = f"Should I push main,{line_break}or should I close feature 045?"
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route4(tmp_root, text, codex, pending=False)
    assert not _blocked(_full_stop(tmp_root, text, "p1", codex))


@ROUTES
@BREAKS
@pytest.mark.parametrize(
    "prefix",
    ["Should I not", "Should I **not**", "Should I delete the release branch and"],
    ids=["negation", "emphasised-negation", "extra-action"],
)
def test_wrapping_cannot_drop_a_negation_or_an_action(tmp_root, codex, pending, line_break, prefix):
    ending = f"{prefix}{line_break}push main?"
    assert guard.asks_in(ending) == [f"{prefix.replace('**', '')} push main?"]
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route4(tmp_root, "Should I push main?", codex, pending)
    assert _blocked(_full_stop(tmp_root, ending, "p2" if pending else "p1", codex))


def test_a_line_break_never_ends_a_sentence_but_a_block_does():
    assert guard.asks_in("Should I push\nmain? Should I close\nfeature 045?") == [
        "Should I push main?",
        "Should I close feature 045?",
    ]
    assert guard.asks_in("Should I push main\n\nnow? Done.") == []


# --- Executed probes, kept as regressions ---

ASK = "Should I push main?"
CODE_TAGS = ["code", "kbd", "samp", "tt", "pre"]


@TEXT_ONLY
@pytest.mark.parametrize("tag", CODE_TAGS)
@pytest.mark.parametrize(
    "attribute",
    ['title="a > b"', "title='a > b'", 'title="example"'],
    ids=["double-with-gt", "single-with-gt", "simple"],
)
def test_a_code_element_with_any_valid_attribute_hides_its_content(tmp_root, codex, tag, attribute):
    text = f"Example: <{tag} {attribute}>{ASK}</{tag}>"
    assert guard.asks_in(text) == []
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert not _blocked(_full_stop(tmp_root, text, "p1", codex))


@TEXT_ONLY
@pytest.mark.parametrize("tag", CODE_TAGS)
def test_a_comment_cannot_close_a_code_element(tmp_root, codex, tag):
    text = f"Example: <{tag}>Example.\n\n<!-- </{tag}> -->\n\n{ASK}\n\n</{tag}>"
    assert guard.asks_in(text) == []
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert not _blocked(_full_stop(tmp_root, text, "p1", codex))


FAKE_OPENERS = {
    "comment": "<!-- <code> -->",
    "attribute": '<div title="<code>">\nExample.\n</div>',
    "script": '<script>const example = "<code>";</script>',
    "processing-instruction": "<?example <code> ?>",
    "inline-comment": "Example: <!-- <code> --> done.",
    "plain-comment": "<!-- Example. -->",
}


@TEXT_ONLY
@pytest.mark.parametrize("shape", sorted(FAKE_OPENERS))
def test_tag_shaped_text_cannot_hide_the_final_ask(tmp_root, codex, shape):
    text = f"{FAKE_OPENERS[shape]}\n\n{ASK}"
    assert guard.asks_in(text) == [ASK]
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _blocked(_full_stop(tmp_root, text, "p1", codex))


@TEXT_ONLY
@pytest.mark.parametrize("tag", CODE_TAGS)
def test_a_real_close_restores_prose(tmp_root, codex, tag):
    text = f"Example: <{tag}>Example.\n\nSecond code paragraph.\n\n</{tag}>\n\n{ASK}"
    assert guard.asks_in(text) == [ASK]
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _blocked(_full_stop(tmp_root, text, "p1", codex))


def test_a_tokenizer_failure_fails_toward_not_an_ask(monkeypatch):
    def broken(self: Any, data: str) -> None:
        raise RuntimeError("tokenizer")

    monkeypatch.setattr(guard._CodeDepth, "feed", broken)  # pyright: ignore[reportPrivateUsage]
    assert guard.asks_in(f"<!-- x -->\n\n{ASK}") == []
    assert guard.asks_in(ASK) == [ASK]  # no HTML, nothing fed


# --- Executed probes, kept as regressions ---


@TEXT_ONLY
@pytest.mark.parametrize("tag", CODE_TAGS)
@pytest.mark.parametrize("shape", ["inline", "block"])
def test_a_mismatched_end_tag_does_not_close_open_code(tmp_root, codex, tag, shape):
    closer = CODE_TAGS[(CODE_TAGS.index(tag) + 1) % len(CODE_TAGS)]
    separator = " " if shape == "inline" else "\n\n"
    text = f"Example: <{tag}>Example.{separator}</{closer}>{separator}{ASK}"
    assert guard.asks_in(text) == []
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert not _blocked(_full_stop(tmp_root, text, "p1", codex))


@TEXT_ONLY
@pytest.mark.parametrize("tag", CODE_TAGS)
def test_a_matching_close_restores_the_final_ask(tmp_root, codex, tag):
    text = f"Example: <{tag}>Example.</{tag}>\n\n{ASK}"
    assert guard.asks_in(text) == [ASK]
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _blocked(_full_stop(tmp_root, text, "p1", codex))


def test_an_end_tag_closes_its_own_element_and_those_opened_above_it():
    # </code> closes the open <kbd> inside it too (an implied close), then prose resumes.
    assert guard.asks_in(f"<code>a <kbd>b</code> done.\n\n{ASK}") == [ASK]
    # </kbd> with only <code> open closes nothing.
    assert guard.asks_in(f"Example: <code>a </kbd> {ASK}") == []
    # Nested same-name elements need both closes.
    assert guard.asks_in(f"Example: <code><code>a</code> {ASK}") == []


LOWER_PAIRS = {
    "addressed": ["Should I push main?", "should I close feature 045?"],
    "bare": ["push main?", "close feature 045?"],
    "choice": ["Should I push main?", "main or release?"],
}


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@pytest.mark.parametrize("shape", sorted(LOWER_PAIRS))
@pytest.mark.parametrize("capitalised", [False, True], ids=["as-written", "capitalised"])
def test_each_question_in_a_run_covers_itself_whatever_its_case(tmp_root, codex, shape, capitalised):
    questions = [q[0].upper() + q[1:] if capitalised else q for q in LOWER_PAIRS[shape]]
    assert guard.asks_in(" ".join(questions)) == questions
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route_all(tmp_root, questions, codex)
    assert not _blocked(_full_stop(tmp_root, " ".join(questions), "p1", codex))


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@pytest.mark.parametrize("trailer", ["if yes, I'll land it.", "If yes, I'll land it."])
def test_a_trailer_of_either_case_keeps_the_ask_separate(tmp_root, codex, trailer):
    text = f"{ASK} {trailer}"
    assert guard.asks_in(text) == [ASK]
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert _blocked(_full_stop(tmp_root, text, "p1", codex))


@pytest.mark.parametrize(
    ("text", "asks"),
    [
        (
            "Should I push main? should I close feature 045?",
            ["Should I push main?", "should I close feature 045?"],
        ),
        ("Should I push main?! should I wait?", ["Should I push main?!", "should I wait?"]),
        ("(Should I push main?) should I wait?", ["(Should I push main?)", "should I wait?"]),
        ("See e.g. the plan. should I push main?", ["should I push main?"]),
        ("Compare docs vs. code, i.e. the drift. should I push main?", ["should I push main?"]),
        ("I'd start with 1, then 2. Which do you want?", ["Which do you want?"]),
        ("Version 1.2. should I ship?", ["should I ship?"]),
        ("Landed. should I push main?", ["should I push main?"]),
    ],
)
def test_sentence_boundaries_ignore_case(text, asks):
    assert guard.asks_in(text) == asks


# --- Executed probes, kept as regressions ---

NEGATED = {
    "spaced": "Should I not ... push main?",
    "attached": "Should I not... push main?",
    "four-dots": "Should I not .... push main?",
    "soft-break": "Should I not ...\npush main?",
    "hard-break": "Should I not ...  \npush main?",
    "unicode": "Should I not … push main?",
}


@ROUTES
@pytest.mark.parametrize("case", sorted(NEGATED))
def test_an_ellipsis_never_splits_off_a_negation(tmp_root, codex, pending, case):
    (ask,) = guard.asks_in(NEGATED[case])
    assert "not" in ask and not guard.covers(ASK, NEGATED[case])
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route4(tmp_root, ASK, codex, pending)
    assert _blocked(_full_stop(tmp_root, NEGATED[case], "p2" if pending else "p1", codex))


ABBREVIATED_STATUS = {
    "duration": "The checks took one min.",
    "negative-answer": "The answer is no.",
    "country": "I checked the U.S.",
}


@ROUTES
def test_a_status_ending_in_a_plain_word_stays_its_own_sentence(tmp_root, codex, pending):
    ending = f"The checks took one minute. {ASK}"
    assert guard.asks_in(ending) == [ASK]
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route4(tmp_root, ASK, codex, pending)
    assert not _blocked(_full_stop(tmp_root, ending, "p2" if pending else "p1", codex))


@ROUTES
@pytest.mark.parametrize("case", sorted(ABBREVIATED_STATUS))
def test_a_status_ending_in_an_abbreviation_merges_and_nudges_once_by_design(tmp_root, codex, pending, case):
    """Round 11 flips round 10: an abbreviation's `.` never ends a sentence, because a wrong
    split can drop a negation or an action (a lost ask) while a wrong merge costs one nudge.
    So the status and the question merge into one ask the registration does not cover, and
    the guard blocks once; the next Stop of the turn passes."""
    ending = f"{ABBREVIATED_STATUS[case]} {ASK}"
    assert guard.asks_in(ending) == [ending]
    sessions.set_record(tmp_root, SID)
    turn = "p2" if pending else "p1"
    _prompt(tmp_root, "p1")
    _route4(tmp_root, ASK, codex, pending)
    assert _blocked(_full_stop(tmp_root, ending, turn, codex))
    assert not _blocked(_full_stop(tmp_root, ending, turn, codex))


R10_CONTROLS = {
    "unchanged": (ASK, ASK, False),
    "negation-without-ellipsis": (ASK, "Should I not push main?", True),
    "mid-sentence-abbreviation": ("Should I use e.g. main?", "Should I use e.g. main?", False),
}


@ROUTES
@pytest.mark.parametrize("case", sorted(R10_CONTROLS))
def test_round_ten_coverage_controls(tmp_root, codex, pending, case):
    registered, ending, expected = R10_CONTROLS[case]
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route4(tmp_root, registered, codex, pending)
    assert _blocked(_full_stop(tmp_root, ending, "p2" if pending else "p1", codex)) == expected


@pytest.mark.parametrize(
    ("text", "asks"),
    [
        ("Should I use e.g. main?", ["Should I use e.g. main?"]),
        ("Compare docs vs. release. should I push main?", ["should I push main?"]),
        ("We ship to the U.S. market. Should I push main?", ["Should I push main?"]),
        ("Item no. 4 is done. Should I push main?", ["Should I push main?"]),
        ("Wait... should I push main?", ["Wait... should I push main?"]),
    ],
)
def test_abbreviations_and_ellipses_at_the_boundary(text, asks):
    assert guard.asks_in(text) == asks


# --- Executed probes, kept as regressions ---

ABBREVIATION_CASES = [
    ("GitHub or GitLab?", "Should I not use e.g. GitHub or GitLab?"),
    ("GitHub or GitLab?", "Should I not choose e.g. GitHub or GitLab?"),
    ("Jane or John?", "Should I not contact Dr. Jane or John?"),
    ("Production or Staging?", "Should I not deploy to e.g. Production or Staging?"),
]


@pytest.mark.parametrize("case", range(len(ABBREVIATION_CASES)))
def test_a_capital_after_an_abbreviation_keeps_the_question_whole(case):
    registered, ending = ABBREVIATION_CASES[case]
    assert guard.asks_in(ending) == [ending]
    assert not guard.covers(registered, ending)


@ROUTES
@pytest.mark.parametrize("case", range(len(ABBREVIATION_CASES)))
def test_an_abbreviation_keeps_the_action_and_the_negation(tmp_root, codex, pending, case):
    registered, ending = ABBREVIATION_CASES[case]
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route4(tmp_root, registered, codex, pending)
    assert _blocked(_full_stop(tmp_root, ending, "p2" if pending else "p1", codex))


@ROUTES
@pytest.mark.parametrize("case", range(len(ABBREVIATION_CASES)))
def test_an_identical_registered_question_with_an_abbreviation_is_covered(tmp_root, codex, pending, case):
    _, ending = ABBREVIATION_CASES[case]
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route4(tmp_root, ending, codex, pending)
    assert not _blocked(_full_stop(tmp_root, ending, "p2" if pending else "p1", codex))


R11_CONTROLS = [
    ("Should I push main?", "Should I not ... push main?", True),
    ("Should I use e.g. main?", "Should I use e.g. main?", False),
    ("Should I push main?", "Should I push main? should I close feature 045?", True),
    ("Should I push main?", "Example: <code>Should I push main?</code>", False),
    ("Should I push main?", "Example: “Should I push main?”", False),
    ("Should I push main?", "The checks took one minute. Should I push main?", False),
]


@ROUTES
@pytest.mark.parametrize("case", range(len(R11_CONTROLS)))
def test_round_eleven_controls(tmp_root, codex, pending, case):
    registered, ending, expected = R11_CONTROLS[case]
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    _route4(tmp_root, registered, codex, pending)
    assert _blocked(_full_stop(tmp_root, ending, "p2" if pending else "p1", codex)) == expected


# --- Documented misses ---

MERGE_MISSES = [row for row in _corpus() if "abbreviation's dot never ends a sentence" in row.get("miss", "")]


def test_the_merge_misses_are_documented_in_the_corpus():
    assert {row["text"] for row in MERGE_MISSES} == {
        "The checks took one min. Push?",
        "The checks took one min. Land 045 now?",
        "You asked about the U.S. Should I push main?",
        "The owner asked about timing in min. Should I push main?",
    }


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@pytest.mark.parametrize("row", MERGE_MISSES, ids=lambda r: r["text"][:40])
def test_a_wrong_merge_after_an_abbreviation_is_a_known_miss(tmp_root, codex, row):
    """Known misses of the safe-side choice (`_boundaries`): the merged sentence loses the
    short-action start, or falls under the reported-speech exclusion. Pinned so a fix shows."""
    assert guard.asks_in(row["text"]) == []
    sessions.set_record(tmp_root, SID)
    _prompt(tmp_root, "p1")
    assert not _blocked(_full_stop(tmp_root, row["text"], "p1", codex))
