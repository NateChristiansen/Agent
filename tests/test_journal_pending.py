"""Milestones accumulate, and a nudge asks whether they are worth a note.

The owner's shape: a feature closing, a worker returning, an ADR landing or a PR moving should make
the harness LOOK at the journal, not write one. `/journal` only ever ran when they typed it,
which is how 2026-09-15 — six retros, a repo split, a CI fix — ended with an empty block.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from agent_cli import hooks
from agent_cli.journal import pending as pending_mod

pytestmark = pytest.mark.usefixtures("notes_remote")

DAY = date(2026, 9, 3)  # the fixed clock's today()


def test_a_feature_close_leaves_a_marker(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    assert pending_mod.read(tmp_root, DAY) == []
    run(["feature", "close", "1"])
    items = pending_mod.read(tmp_root, DAY)
    assert [i["kind"] for i in items] == ["feature-close"]
    assert items[0]["ref"].endswith("x")


def test_an_adr_leaves_a_marker(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    run(["adr", "new", "arch", "--title", "Arch", "--feature", "1", "--scope", "harness"])
    items = pending_mod.read(tmp_root, DAY)
    assert [i["kind"] for i in items] == ["adr"]
    assert items[0]["ref"] == "ADR-0001"
    assert items[0]["detail"] == "Arch"


def test_summary_is_empty_with_nothing_pending(tmp_root: Path, run):
    assert pending_mod.summary(tmp_root, DAY) == ""


def test_summary_groups_by_kind_and_instructs(tmp_root: Path, run):
    pending_mod.record(tmp_root, "feature-close", "016 final-audit", "done", day=DAY)
    pending_mod.record(tmp_root, "dispatch-return", "001-03 done", "read the file", day=DAY)
    pending_mod.record(tmp_root, "dispatch-return", "001-04 question", "denied", day=DAY)
    line = pending_mod.summary(tmp_root, DAY)
    assert "feature-close: 016 final-audit" in line
    assert "dispatch-return: 001-03 done, 001-04 question" in line
    # "decide whether ... a thin day is not worth one" read as permission to defer.
    assert "worth" not in line and "why not" not in line
    assert f"Write it now with `/journal {DAY.isoformat()}`" in line


def test_a_repeated_ref_is_listed_once(tmp_root: Path, run):
    pending_mod.record(tmp_root, "adr", "ADR-0012", "two repos", day=DAY)
    pending_mod.record(tmp_root, "adr", "ADR-0012", "two repos", day=DAY)
    assert pending_mod.summary(tmp_root, DAY).count("ADR-0012") == 1


def test_an_unknown_kind_is_dropped(tmp_root: Path, run):
    pending_mod.record(tmp_root, "lunch", "sandwich", day=DAY)
    assert pending_mod.read(tmp_root, DAY) == []


def test_writing_the_note_clears_the_day(tmp_root: Path, run):
    """What was pending is now in the note. Nudging again about the same milestones is noise."""
    from agent_cli import journal as journal_mod

    pending_mod.record(tmp_root, "feature-close", "016 final-audit", "done", day=DAY)
    draft = tmp_root / "draft.md"
    draft.write_text("- closed the final audit\n", encoding="utf-8")
    journal_mod.write_generated(tmp_root, DAY, draft.read_text())
    assert pending_mod.read(tmp_root, DAY) == []
    assert pending_mod.summary(tmp_root, DAY) == ""


# ---------------------------------------------------------------- the prompt nudge


def _prompt(session_id: str = "s1") -> dict[str, object]:
    return {"hook_event_name": "UserPromptSubmit", "session_id": session_id, "prompt": "hello"}


def test_prompt_nudges_once_per_session_per_day(tmp_root: Path, run):
    """The journal nudge reaches both clients on the next prompt, once per session per day."""
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    pending_mod.record(tmp_root, "feature-close", "016 final-audit", "done", day=DAY)

    first = hooks.journal_pending(tmp_root, _prompt())
    output = json.loads(first)["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert "016 final-audit" in output["additionalContext"]
    assert hooks.journal_pending(tmp_root, _prompt()) == "", "second prompt in the same session is silent"
    # a different session still gets told
    assert hooks.journal_pending(tmp_root, _prompt("s2")) != ""


def test_a_new_milestone_after_a_nudge_nudges_again_in_every_session(tmp_root: Path, run):
    """One nudge per day hid the afternoon's landings from a session
    nudged in the morning, and a count could not see "cleared, then one new milestone"."""
    from agent_cli import journal as journal_mod

    pending_mod.record(tmp_root, "feature-close", "016 final-audit", "done", day=DAY)
    assert hooks.journal_pending(tmp_root, _prompt("s1")) != ""
    assert hooks.journal_pending(tmp_root, _prompt("s2")) != ""
    assert hooks.journal_pending(tmp_root, _prompt("s1")) == ""
    pending_mod.record(tmp_root, "dispatch-return", "001-05 done", "", day=DAY)
    assert "001-05 done" in hooks.journal_pending(tmp_root, _prompt("s1"))

    journal_mod.write_generated(tmp_root, DAY, "- the day\n")  # clears the pending list
    assert hooks.journal_pending(tmp_root, _prompt("s1")) == "", "cleared with no new work: quiet"
    assert hooks.journal_pending(tmp_root, _prompt("s2")) == ""
    pending_mod.record(tmp_root, "dispatch-return", "001-06 done", "", day=DAY)
    assert "001-06 done" in hooks.journal_pending(tmp_root, _prompt("s1"))
    assert "001-06 done" in hooks.journal_pending(tmp_root, _prompt("s2")), "an already-nudged session too"


def test_the_missing_days_list_shows_the_last_week(tmp_root: Path, run, monkeypatch):
    from datetime import date, timedelta

    from agent_cli import journal as journal_mod

    days = [date(2026, 9, 3) + timedelta(days=i) for i in range(10)]
    monkeypatch.setattr(journal_mod, "missing_days", lambda root: days)
    line = hooks.journal_nag(tmp_root)
    assert "2026-09-03" not in line and "2026-09-12" in line and "2026-09-06" in line
    assert "and 3 older" in line


def test_prompt_is_silent_with_nothing_pending(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    assert hooks.journal_pending(tmp_root, _prompt()) == ""


def test_prompt_never_fires_inside_a_subagent_or_worker(tmp_root: Path, run):
    pending_mod.record(tmp_root, "feature-close", "016 final-audit", "done", day=DAY)
    assert hooks.journal_pending(tmp_root, {**_prompt(), "hook_event_name": "Stop"}) == ""
    assert hooks.journal_pending(tmp_root, {**_prompt(), "agent_type": "retro-reader"}) == ""
    assert hooks.journal_pending(tmp_root, {**_prompt(), "agent_id": "a1"}) == ""


def test_session_start_carries_the_pending_line(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    pending_mod.record(tmp_root, "dispatch-return", "001-03 done", "read the file", day=DAY)
    assert "001-03 done" in hooks.journal_nag(tmp_root)
    startup = hooks.session_start(tmp_root, {"session_id": "s1", "source": "startup"})
    assert "001-03 done" in json.loads(startup)["hookSpecificOutput"]["additionalContext"]
    assert hooks.journal_pending(tmp_root, _prompt()) == "", "startup already delivered today's nudge"
    assert hooks.journal_pending(tmp_root, _prompt("s2")) != "", (
        "another session still sees pending milestones"
    )


def test_journal_hook_is_wired_to_prompt_in_both_clients():
    """The 2026-09-24 Codex Stop failure came from wiring a prompt-shaped nudge to Stop."""
    from agent_cli.hooks.groups import wired

    root = Path(__file__).resolve().parents[1]
    for path in (root / ".claude/settings.json", root / ".codex/hooks.json"):
        config = json.loads(path.read_text(encoding="utf-8"))
        for event in ("UserPromptSubmit", "Stop"):
            assert ("journal-pending" in wired(config, event)) == (event == "UserPromptSubmit")
