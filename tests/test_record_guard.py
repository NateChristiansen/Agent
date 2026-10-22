"""`record-guard`: a manager owing reconciliation items is blocked once per marker (ask-guard
logical turn, owed set), and never loops.

Why the earlier Stop nag `done-unticked` was retired is unknown (its history survives only in a
squashed release), so these tests pin the two plausible causes instead: a repeat Stop always
passes, and every legitimate stop (waiting items, an owner question, workers, subagents, unmapped
or non-responsible sessions, the switch off) passes without a block.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli import features as features_mod
from agent_cli import reconcile, sessions
from agent_cli.dispatch import state, verdicts
from agent_cli.hooks import ask_guard, record_guard, toggles

run_mod = importlib.import_module("agent_cli.hooks.run")

SID = "s-rec"
OTHER = "s-other"


@pytest.fixture(autouse=True)
def manager(tmp_root: Path, notes_remote, run, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fixture feature with this session mapped to it as a live manager. The worker env is
    removed so a dispatched test run's own exemption cannot hide a missing block."""
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    run(["feature", "new", "x", "--title", "X"])
    sessions.set_record(tmp_root, SID, feature={"id": 1, "slug": "x", "dir": "features/001-x"})
    monkeypatch.setattr(verdicts, "live_managers", lambda root: _live(root, SID))
    return tmp_root


def _live(root: Path, *sids: str) -> dict[str, dict[str, Any]]:
    return {sid: sessions.get(root, sid) or {"session_id": sid} for sid in sids}


def _feature(root: Path) -> features_mod.Feature:
    return features_mod.resolve(root, "1")


def _owe(root: Path, *items: str) -> str:
    return reconcile.add_manual(root, _feature(root), list(items or ["finding"]))["source"]


def _prompt(root: Path, sid: str = SID) -> None:
    ask_guard.ask_guard(root, {"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": "go"})


def _stop(root: Path, sid: str = SID, **extra: Any) -> str:
    return record_guard.record_guard(root, {"session_id": sid, "hook_event_name": "Stop", **extra})


def _blocked(out: str) -> bool:
    return bool(out) and json.loads(out).get("decision") == "block"


# --- the bound --------------------------------------------------------------------------------


def test_owed_items_block_once_and_the_repeat_stop_passes(tmp_root: Path):
    src = _owe(tmp_root, "check the migration")
    _prompt(tmp_root)
    out = _stop(tmp_root)
    assert _blocked(out)
    reason = json.loads(out)["reason"]
    assert f"{src} check the migration" in reason and "`reconcile`" in reason
    for _ in range(5):
        assert _stop(tmp_root, stop_hook_active=True) == ""


def test_a_new_prompt_blocks_once_again(tmp_root: Path):
    _owe(tmp_root)
    _prompt(tmp_root)
    assert _blocked(_stop(tmp_root))
    assert _stop(tmp_root) == ""
    _prompt(tmp_root)
    assert _blocked(_stop(tmp_root))
    assert _stop(tmp_root) == ""


def test_a_new_item_blocks_once_in_the_same_turn(tmp_root: Path):
    _owe(tmp_root, "first")
    _prompt(tmp_root)
    assert _blocked(_stop(tmp_root))
    _owe(tmp_root, "second")
    assert _blocked(_stop(tmp_root))
    assert _stop(tmp_root) == ""


def test_markers_are_bounded_on_the_record(tmp_root: Path):
    for n in range(record_guard.KEEP + 3):
        _owe(tmp_root, f"item {n}")
        assert _blocked(_stop(tmp_root))
    markers = (sessions.get(tmp_root, SID) or {})[record_guard.STATE]["markers"]
    assert len(markers) == record_guard.KEEP


def test_an_earlier_members_block_spends_nothing(tmp_root: Path):
    _owe(tmp_root)
    _prompt(tmp_root)
    before = sessions.get(tmp_root, SID)
    assert _stop(tmp_root, _stop_blocked=True) == ""
    assert sessions.get(tmp_root, SID) == before
    assert _blocked(_stop(tmp_root))


def test_settling_the_items_lets_the_session_stop(tmp_root: Path):
    src = _owe(tmp_root)
    _prompt(tmp_root)
    reconcile.decide(tmp_root, 1, src, [{"item": "finding", "disposition": "no-task", "reason": "dup"}])
    assert _stop(tmp_root) == ""


def test_switched_off_ask_guard_falls_back_to_once_per_owed_set(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(toggles, "settings", lambda _root: {"ask-guard": False})
    _owe(tmp_root, "first")
    assert _blocked(_stop(tmp_root))
    _prompt(tmp_root)  # the turn would advance, but the marker ignores it
    assert _stop(tmp_root) == ""
    _owe(tmp_root, "second")
    assert _blocked(_stop(tmp_root))
    assert _stop(tmp_root) == ""


# --- legitimate stops pass --------------------------------------------------------------------


def test_waiting_items_pass(tmp_root: Path):
    src = _owe(tmp_root)
    _prompt(tmp_root)
    reconcile.decide(
        tmp_root,
        1,
        src,
        [{"item": "finding", "disposition": "waiting", "on": "owner", "until": "owner replies"}],
    )
    assert _stop(tmp_root) == ""


def test_a_registered_owner_question_passes(tmp_root: Path):
    _owe(tmp_root)
    _prompt(tmp_root)
    sessions.set_field(tmp_root, SID, "owner_question_pending", True)
    assert _stop(tmp_root) == ""
    sessions.set_field(tmp_root, SID, "owner_question_pending", False)
    assert _blocked(_stop(tmp_root))


@pytest.mark.parametrize("where", ["agent_id", "agent_type", "dispatch_record", "dispatch_env"])
def test_workers_and_subagents_pass(tmp_root: Path, monkeypatch, where: str):
    _owe(tmp_root)
    if where == "dispatch_record":
        sessions.set_record(tmp_root, SID, dispatch_id="001-mac-04")
    if where == "dispatch_env":
        monkeypatch.setenv("AGENT_DISPATCH_ID", "001-mac-04")
    extra = {where: "a1"} if where in ("agent_id", "agent_type") else {}
    assert _stop(tmp_root, **extra) == ""


def test_an_unmapped_session_passes(tmp_root: Path):
    _owe(tmp_root)
    sessions.set_record(tmp_root, SID, clear_feature=True)
    assert _stop(tmp_root) == ""
    assert _stop(tmp_root, "s-unknown") == ""


def test_nothing_owed_writes_nothing(tmp_root: Path):
    before = sessions.get(tmp_root, SID)
    assert _stop(tmp_root) == ""
    assert sessions.get(tmp_root, SID) == before


def test_the_switch_turns_it_off(tmp_root: Path, monkeypatch):
    _owe(tmp_root)
    monkeypatch.setattr(toggles, "settings", lambda _root: {"record-guard": False})
    payload = json.dumps({"session_id": SID, "hook_event_name": "Stop"})
    assert run_mod.run(tmp_root, "record-guard", payload) == ("", 0)
    assert run_mod.run(tmp_root, "stop", payload) == ("", 0)


# --- responsibility ---------------------------------------------------------------------------


def _dispatch(root: Path, launcher: str) -> str:
    d = state.save(
        root,
        {
            "id": "001-mac-01",
            "repo": "harness",
            "short": "mac",
            "feature": 1,
            "kind": "work",
            "attempt": 1,
            "state": "done",
            "launched_by_session": launcher,
            "result": {"status": "done", "summary": "s"},
        },
    )
    pending = reconcile.dispatch_source(d)
    assert pending is not None
    reconcile.append_source(_feature(root), pending)
    return d["id"]


def test_a_live_launcher_owns_its_dispatch_and_the_other_manager_passes(tmp_root: Path, monkeypatch):
    sessions.set_record(tmp_root, OTHER, feature={"id": 1, "slug": "x", "dir": "features/001-x"})
    monkeypatch.setattr(verdicts, "live_managers", lambda root: _live(root, SID, OTHER))
    _dispatch(tmp_root, launcher=OTHER)
    assert _stop(tmp_root) == ""
    assert _blocked(_stop(tmp_root, OTHER))


def test_an_ended_launchers_dispatch_is_adopted_by_the_mapped_manager(tmp_root: Path):
    _dispatch(tmp_root, launcher="s-gone")
    assert _blocked(_stop(tmp_root))


def test_a_source_with_no_session_belongs_to_the_most_recent_mapped_manager(tmp_root: Path, monkeypatch):
    sessions.set_record(tmp_root, OTHER, feature={"id": 1, "slug": "x", "dir": "features/001-x"})
    sessions.set_field(tmp_root, OTHER, "activity", {"state": "working", "at": "2999-01-01T00:00:00+00:00"})
    monkeypatch.setattr(verdicts, "live_managers", lambda root: _live(root, SID, OTHER))
    _owe(tmp_root)
    assert _stop(tmp_root) == ""
    assert _blocked(_stop(tmp_root, OTHER))


# --- in its groups ----------------------------------------------------------------------------


@pytest.mark.parametrize(("group", "native"), [("stop", "prompt_id"), ("codex-stop", "turn_id")])
def test_each_client_group_blocks_once_per_turn(tmp_root: Path, group: str, native: str):
    _owe(tmp_root)
    _prompt(tmp_root)
    payload = json.dumps({"session_id": SID, "hook_event_name": "Stop", native: "t1"})
    out, code = run_mod.run(tmp_root, group, payload)
    assert code == 0 and _blocked(out)
    assert json.loads(out)["reason"].startswith("record-guard:")
    assert run_mod.run(tmp_root, group, payload) == ("", 0)


def test_an_ask_guard_block_in_the_group_leaves_the_marker_unspent(tmp_root: Path):
    _owe(tmp_root)
    _prompt(tmp_root)
    ask = {
        "session_id": SID,
        "hook_event_name": "Stop",
        "last_assistant_message": "Landed it.\n\nShould I push main now?",
    }
    out, _ = run_mod.run(tmp_root, "stop", json.dumps(ask))
    reason = json.loads(out)["reason"]
    assert reason.startswith("ask-guard:") and "record-guard:" not in reason
    out, _ = run_mod.run(tmp_root, "stop", json.dumps({**ask, "last_assistant_message": "Done."}))
    assert json.loads(out)["reason"].startswith("record-guard:")
    assert run_mod.run(tmp_root, "stop", json.dumps({**ask, "last_assistant_message": "Done."})) == ("", 0)
