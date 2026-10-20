import json
from datetime import date
from pathlib import Path

import pytest

from agent_cli import frontmatter, journal
from agent_cli.core import AgentError
from usage_helpers import publish

pytestmark = pytest.mark.usefixtures("notes_remote")


def test_collect_filters_by_date_and_reports_git(tmp_root: Path, run, monkeypatch):
    run(["feature", "new", "a", "--title", "A"])
    run(["feature", "new", "b", "--title", "B"])
    run(["log", "1", "today a"])
    monkeypatch.setenv("AGENT_NOW", "2026-09-04T09:00:00-07:00")
    run(["log", "2", "tomorrow b"])
    monkeypatch.setenv("AGENT_NOW", "2026-09-03T15:00:00-07:00")
    payload = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "please do the thing"}
    run(["feature", "use", "1", "--session-id", "s1"])
    run(["capture", "--kind", "prompt", "--stdin"], stdin=json.dumps(payload))
    run(["adr", "new", "d", "--title", "D", "--scope", "harness"])

    r = run(["--json", "journal", "collect", "2026-09-03"])
    assert r.code == 0
    b = r.json
    assert b["date"] == "2026-09-03"
    ids = [f["id"] for f in b["features"]]
    assert ids == [1, 2]  # A feature opened today even though its only log entry is tomorrow
    f1 = next(f for f in b["features"] if f["id"] == 1)
    assert [e["text"] for e in f1["log_entries"]] == ["today a"]
    assert f1["raw"]["prompts"] == 1
    assert f1["raw"]["prompt_heads"] == ["please do the thing"]
    f2 = next(f for f in b["features"] if f["id"] == 2)
    assert f2["log_entries"] == []
    assert [a["id"] for a in b["adrs_today"]] == [1]
    git = {g["repo"]: g for g in b["git"]}
    assert git["beta-web"]["not_cloned"] is True
    alpha = git["alpha-api"]
    assert alpha["branch"] == "main"
    assert [c["subject"] for c in alpha["commits"]] == ["T-1 initial"]
    assert alpha["dirty_files"] == 0


def test_markdown_and_write(tmp_root: Path, run):
    run(["feature", "new", "a", "--title", "A"])
    run(["log", "1", "--kind", "surprise", "it worked"])
    r = run(["journal", "collect"])
    assert "# Journal collect — 2026-09-03" in r.out
    assert "**14:05 surprise** — it worked" in r.out
    assert "(not cloned)" in r.out
    # a stale bundle left over from before the move to `.agent/journal/` is cleaned up
    stale_dir = tmp_root / "journal"
    stale_dir.mkdir(parents=True, exist_ok=True)
    stale_json = stale_dir / "2026-09-03.collect.json"
    stale_json.write_text("{}", encoding="utf-8")
    r = run(["journal", "collect", "--write"])
    assert (tmp_root / ".agent/journal/2026-09-03.collect.md").exists()
    assert not (tmp_root / "journal/2026-09-03.collect.md").exists()
    assert not stale_json.exists()
    r = run(["--json", "journal", "collect", "--write"])
    assert r.json["written"] == ".agent/journal/2026-09-03.collect.json"
    assert run(["journal", "collect", "nope"]).code == 2


# ------------------------------------------------------------------ the note writer


def test_skeleton_round_trips_through_split_sections(tmp_root: Path):
    day = date(2026, 9, 9)
    text = journal.skeleton(day)
    assert text.startswith(
        "---\ntags:\n- journal\ndate: 2026-09-09\nspend_usd: 0.0\nspend_complete: true\n"
        "manager_usd: 0.0\nmanager_complete: true\ndispatches: 0\n"
        "session_tokens: 0\nsession_models: ''\n---\n"
    )
    assert "# 2026-09-09" in text
    assert journal.GENERATED_START in text and journal.GENERATED_END in text
    assert "## Notes" in text
    generated, notes_body = journal.split_sections(text)
    assert (generated, notes_body) == ("", "")
    assert journal.render_note(day, generated, notes_body) == text


def test_split_sections_raises_on_missing_markers_or_notes_heading():
    ok = journal.skeleton(date(2026, 9, 9))
    with pytest.raises(AgentError):
        journal.split_sections(ok.replace(journal.GENERATED_END, ""))
    with pytest.raises(AgentError):
        journal.split_sections(ok.replace("## Notes", "## Other"))


def test_note_rel_and_note_path_nest_by_year_named_month_weekday(tmp_root: Path):
    monday = date(2026, 9, 14)
    assert journal.note_rel(monday) == "journal/2026/09-September/14-Monday.md"
    assert journal.note_path(tmp_root, monday) == tmp_root / "journal/2026/09-September/14-Monday.md"
    sunday = date(2026, 9, 13)
    assert journal.note_rel(sunday) == "journal/2026/09-September/13-Sunday.md"
    assert journal.note_rel(date(2027, 1, 1)) == "journal/2027/01-January/01-Friday.md"
    assert journal.note_rel(date(2026, 12, 31)) == "journal/2026/12-December/31-Thursday.md"


def test_ensure_note_creates_the_skeleton_once(tmp_root: Path):
    day = date(2026, 9, 9)  # a Wednesday
    path = journal.ensure_note(tmp_root, day)
    assert path == tmp_root / "journal/2026/09-September/09-Wednesday.md"
    assert path.read_text(encoding="utf-8") == journal.skeleton(day)
    # a second call never touches an existing note
    path.write_text(path.read_text(encoding="utf-8") + "extra", encoding="utf-8")
    journal.ensure_note(tmp_root, day)
    assert path.read_text(encoding="utf-8").endswith("extra")


def test_ensure_note_creates_parent_dirs(tmp_root: Path):
    day = date(2026, 9, 14)
    assert not (tmp_root / "journal/2026").exists()
    path = journal.ensure_note(tmp_root, day)
    assert path.parent.is_dir()
    assert path.exists()


def test_missing_days_is_satisfied_by_a_filled_note(tmp_root: Path, run, monkeypatch):
    run(["feature", "new", "a", "--title", "A"])
    monkeypatch.setenv("AGENT_NOW", "2026-09-05T09:00:00-07:00")  # a Saturday
    run(["log", "1", "did something on the 5th"])
    monkeypatch.setenv("AGENT_NOW", "2026-09-10T09:00:00-07:00")
    day = date(2026, 9, 5)
    assert day in journal.missing_days(tmp_root)  # no note at all yet
    journal.write_generated(tmp_root, day, "something happened")
    assert day not in journal.missing_days(tmp_root)


def test_write_generated_preserves_hand_typed_notes_byte_for_byte(tmp_root: Path):
    day = date(2026, 9, 9)
    journal.write_generated(tmp_root, day, "first pass")
    path = journal.note_path(tmp_root, day)
    notes_paragraph = "The owner's own paragraph.\n\nA second paragraph -- with an em dash."
    text = path.read_text(encoding="utf-8").replace("## Notes\n", f"## Notes\n\n{notes_paragraph}\n", 1)
    path.write_text(text, encoding="utf-8")
    journal.write_generated(tmp_root, day, "**005** — Done: shipped it.")
    new_text = path.read_text(encoding="utf-8")
    assert "shipped it." in new_text
    assert notes_paragraph in new_text
    generated, notes_body = journal.split_sections(new_text)
    assert generated == "**005** — Done: shipped it."
    assert notes_body == notes_paragraph
    # a rerun that writes the identical block changes nothing about Notes either
    journal.write_generated(tmp_root, day, "**005** — Done: shipped it.")
    assert path.read_text(encoding="utf-8") == new_text


def test_write_generated_sets_spend_and_dispatches_and_keeps_a_hand_added_key(tmp_root: Path):
    day = date(2026, 9, 9)
    journal.write_generated(tmp_root, day, "first")
    meta, _ = frontmatter.split(journal.note_path(tmp_root, day).read_text(encoding="utf-8"))
    assert meta["spend_usd"] == 0.0
    assert meta["dispatches"] == 0
    path = journal.note_path(tmp_root, day)
    text = path.read_text(encoding="utf-8").replace("dispatches: 0\n", "dispatches: 0\nmood: good\n", 1)
    path.write_text(text, encoding="utf-8")
    publish(tmp_root, day, record={"dispatch_usd": 9.5, "dispatches": 2})
    journal.write_generated(tmp_root, day, "second")
    new_text = path.read_text(encoding="utf-8")
    meta, _ = frontmatter.split(new_text)
    assert meta["spend_usd"] == 9.5
    assert meta["dispatches"] == 2
    assert meta["mood"] == "good"
    assert "probe_spend_usd" not in meta  # only on a day that had probe spend
    assert journal.split_sections(new_text)[0] == "second"
    # an eval probe counts apart: not in spend_usd or dispatches
    publish(tmp_root, day, record={"dispatch_usd": 9.5, "dispatches": 2, "probe_usd": 0.4})
    journal.write_generated(tmp_root, day, "third")
    meta, _ = frontmatter.split(path.read_text(encoding="utf-8"))
    assert (meta["spend_usd"], meta["dispatches"], meta["probe_spend_usd"]) == (9.5, 2, 0.4)


def test_the_obsidian_daily_template_matches_the_base_and_is_adopted(tmp_root: Path):
    """The template was the one journal surface that disagreed with the rest.
    It had a QUOTED `date:`, so Obsidian wrote a string where the Base wants a date. It also
    had no `spend_usd`/`dispatches`, so a hand-made note was missing two of the Base's three
    columns. Read the real template, fill its placeholders the way Obsidian does, and put the
    result through `meta_for` and `split_sections`."""
    template = (
        Path(__file__).resolve().parents[1] / "harness" / "obsidian" / "templates" / "daily.md"
    ).read_text(encoding="utf-8")
    assert "date: {{date:YYYY-MM-DD}}" in template  # unquoted
    day = date(2026, 9, 9)
    rendered = template.replace("{{date:YYYY-MM-DD}}", day.isoformat())
    meta, _ = frontmatter.split(rendered)
    assert meta == {
        "tags": ["journal"],
        "date": day,
        "spend_usd": 0,
        "spend_complete": True,
        "manager_usd": 0,
        "manager_complete": True,
        "dispatches": 0,
        "session_tokens": 0,
        "session_models": "",
    }
    assert journal.split_sections(rendered) == ("", "")
    adopted = journal.meta_for(day, meta)
    assert adopted == {
        "tags": ["journal"],
        "date": day,
        "spend_usd": 0.0,
        "spend_complete": True,
        "manager_usd": 0.0,
        "manager_complete": True,
        "dispatches": 0,
        "session_tokens": 0,
        "session_models": "",
    }


def test_old_shape_note_with_log_section_drops_the_log_keeps_notes(tmp_root: Path):
    day = date(2026, 9, 9)
    old_text = (
        "---\ntags:\n- journal\ndate: 2026-09-09\n---\n"
        "# 2026-09-09\n\n"
        "<!-- journal:generated -->\nold block\n<!-- /journal:generated -->\n\n"
        "## Log\n- old log line\n\n"
        "## Notes\n\nkeep me\n"
    )
    path = journal.note_path(tmp_root, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(old_text, encoding="utf-8")
    journal.write_generated(tmp_root, day, "new block")
    text = path.read_text(encoding="utf-8")
    assert "old log line" not in text
    assert "## Log" not in text
    generated, notes_body = journal.split_sections(text)
    assert generated == "new block"
    assert notes_body == "keep me"


def test_first_sentence():
    assert journal.first_sentence("Did the thing. More detail here.") == "Did the thing."
    assert journal.first_sentence("Question? Yes.") == "Question?"
    assert journal.first_sentence("Wow! Great.") == "Wow!"
    assert journal.first_sentence("line one\nline two") == "line one"
    assert journal.first_sentence("no terminator at all") == "no terminator at all"
    assert journal.first_sentence("") == ""
    assert journal.first_sentence("  spaced  ") == "spaced"
    cut = journal.first_sentence("x" * 130)
    assert len(cut) == 120 and cut.endswith("…")


def test_missing_days(tmp_root: Path, run, monkeypatch):
    run(["feature", "new", "a", "--title", "A"])
    monkeypatch.setenv("AGENT_NOW", "2026-09-05T09:00:00-07:00")
    run(["log", "1", "did something on the 5th"])
    monkeypatch.setenv("AGENT_NOW", "2026-09-10T09:00:00-07:00")
    missing = journal.missing_days(tmp_root)
    assert date(2026, 9, 5) in missing
    assert date(2026, 9, 10) not in missing  # today is excluded even with activity
    assert journal.missing_days(tmp_root, since=date(2026, 9, 6)) == [
        d for d in missing if d >= date(2026, 9, 6)
    ]
    journal.write_generated(tmp_root, date(2026, 9, 5), "something happened")
    assert date(2026, 9, 5) not in journal.missing_days(tmp_root)


def test_journal_write_replaces_only_the_block(tmp_root: Path, run):
    run(["feature", "new", "a", "--title", "A"])
    block = tmp_root / "block.md"
    block.write_text("**001 A** — Done: opened it.\nSpend: $0.00, no dispatches.\n", encoding="utf-8")
    r = run(["--json", "journal", "write", "2026-09-03", "--file", str(block)])
    assert r.code == 0, r.err
    assert r.json["words"] == 10  # the em dash counts. It is what `wc -w` would say too
    assert r.json["spend_usd"] == 0.0
    assert r.json["dispatches"] == 0
    assert r.json["path"] == "journal/2026/09-September/03-Thursday.md"  # 2026-09-03 is a Thursday
    assert r.json["path"] == journal.note_rel(date(2026, 9, 3))
    text = journal.note_path(tmp_root, date(2026, 9, 3)).read_text(encoding="utf-8")
    generated, notes_body = journal.split_sections(text)
    assert generated == "**001 A** — Done: opened it.\nSpend: $0.00, no dispatches."
    assert notes_body == ""
    assert run(["journal", "write", "2026-09-03", "--file", str(tmp_root / "nope.md")]).code != 0
    (tmp_root / "empty.md").write_text("\n", encoding="utf-8")
    assert run(["journal", "write", "2026-09-03", "--file", str(tmp_root / "empty.md")]).code != 0


def test_collect_gains_dispatches_retros_and_harness_git(tmp_root: Path, run):
    run(["feature", "new", "a", "--title", "A"])
    ddir = tmp_root / ".agent" / "dispatches"
    ddir.mkdir(parents=True)
    record = {
        "id": "001-01",
        "feature": 1,
        "short": "alpha",
        "state": "done",
        "attempt": 1,
        "cost_usd": 1.23,
        "model": None,
        "effort": None,
        "finished": "2026-09-03T10:00:00-07:00",
        "result": {"summary": "did the thing. more detail"},
        "error": None,
        "handoff": "features/001-a/handoffs/01-alpha.md",
    }
    (ddir / "001-01.json").write_text(json.dumps(record), encoding="utf-8")
    (tmp_root / "features/001-a/retro.md").write_text(
        "---\ntags:\n- retro\nfeature: 1\ndate: 2026-09-03\n---\n# 001 retro: it went fine\n",
        encoding="utf-8",
    )
    b = journal.collect(tmp_root, date(2026, 9, 3))
    assert [d["id"] for d in b["dispatches"]] == ["001-01"]
    assert b["dispatches"][0]["summary"] == "did the thing."
    assert b["retros"] == [
        {"feature": 1, "path": "features/001-a/retro.md", "headline": "001 retro: it went fine"}
    ]
    assert b["harness_git"] == []  # tmp_root itself is not a git repo
    md = journal.render_markdown(b)
    assert "## Dispatches" in md and "001-01 alpha [done]" in md
    assert "## Retros" in md and "001 retro: it went fine" in md
    assert "## Harness git" in md
