import json
from pathlib import Path

import pytest

from agent_cli import frontmatter

pytestmark = pytest.mark.usefixtures("notes_remote")


def _fixture(run, tmp_root: Path):
    run(["feature", "new", "alpha-work", "--title", "Alpha work", "--ticket", "T-42", "--repo", "alpha"])
    run(["feature", "new", "beta-work", "--title", "Beta work", "--status", "blocked", "--repo", "beta-web"])
    run(["feature", "new", "old", "--title", "Old"])
    (tmp_root / "features/003-old/retro.md").write_text("# Retro\n", encoding="utf-8")
    run(["feature", "close", "3"])
    plan = tmp_root / "features/001-alpha-work/plan.md"
    meta, body = frontmatter.load(plan)
    frontmatter.dump(plan, meta, body.replace("- [ ]\n", "- [ ] write the thing\n"))
    run(["log", "1", "first entry"])


def test_golden_render_and_every_generated_view(tmp_root: Path, run):
    _fixture(run, tmp_root)
    (tmp_root / "STATUS.md").write_text("retired generated table\n", encoding="utf-8")
    r = run(["--json", "status", "render"])
    digest = r.json["source_hash"]
    # Audit 10 plan.md:113 (K1) retires the generated feature table.
    assert not (tmp_root / "STATUS.md").exists()
    payload = json.loads((tmp_root / ".agent/status.json").read_text(encoding="utf-8"))
    assert payload["source_hash"] == digest
    f1 = payload["features"][0]
    assert f1["next_task"] == "write the thing"
    assert f1["open_tasks"] == 1
    assert f1["last_log"]["kind"] == "note"

    _render_is_idempotent(tmp_root, run)
    _handoffs_live_view_filters_on_the_live_flag(tmp_root)
    _no_base_expression_wraps_a_date_typed_property_in_date(tmp_root)
    _the_journal_week_is_a_view_script_not_a_second_base_view(tmp_root)
    _a_board_card_leads_with_its_feature_number(tmp_root)
    _index_is_rendered_and_generated(tmp_root)
    _handoffs_base_shows_the_rubric_columns(tmp_root)
    _docs_base_covers_the_long_form_notes(tmp_root)
    _bases_and_dashboard(tmp_root, run)  # last: it adds a feature and a journal note


def test_task_writes_refresh_canonical_counts_and_text(tmp_root: Path, run):
    from agent_cli import features, ops

    assert run(["feature", "new", "counts", "--title", "Counts"]).code == 0
    plan = tmp_root / "features/001-counts/plan.md"
    meta, body = frontmatter.load(plan)
    body = body.replace("- [ ]\n", "- [x] done\n- [-] superseded\n- [ ] open\n  - [ ] child\n- [ ]\n")
    frontmatter.dump(plan, meta, body)

    def check():
        payload = json.loads((tmp_root / ".agent/status.json").read_text(encoding="utf-8"))
        current = features.resolve(tmp_root, "1")
        expected = [
            {"index": t.index, "text": t.text} for t in features.list_tasks(current.body) if t.is_open
        ]
        row = payload["features"][0]
        assert row["open_task_items"] == expected
        assert row["open_tasks"] == len(expected)
        return expected

    ops.task_add(tmp_root, feature="1", text="new task")
    assert check() == [{"index": 3, "text": "open"}, {"index": 4, "text": "new task"}]
    ops.task_done(tmp_root, feature="1", task="3")
    assert check() == [{"index": 4, "text": "new task"}]
    ops.task_supersede(tmp_root, feature="1", task="4", reason="covered elsewhere", link="[[Dashboard]]")
    assert check() == []


def _bases_and_dashboard(tmp_root: Path, run):
    import yaml

    features_base = tmp_root / "bases/features.base"
    adrs_base = tmp_root / "bases/adrs.base"
    dashboard = tmp_root / "Dashboard.md"
    for path in (features_base, adrs_base, dashboard):
        assert path.exists(), path
    fb = yaml.safe_load(features_base.read_text(encoding="utf-8"))
    assert 'file.hasTag("feature")' in fb["filters"]["and"]
    assert [v["name"] for v in fb["views"]] == ["Open", "Board", "Closed", "All"]
    assert [v["type"] for v in fb["views"]] == ["table", "cards", "table", "table"]
    ab = yaml.safe_load(adrs_base.read_text(encoding="utf-8"))
    assert [v["name"] for v in ab["views"]] == ["Current", "All"]
    jb = yaml.safe_load((tmp_root / "bases/journal.base").read_text(encoding="utf-8"))
    assert 'file.hasTag("journal")' in jb["filters"]["and"]
    recent = next(v for v in jb["views"] if v["name"] == "Recent")
    assert recent["order"] == [
        "note.date",
        "file.name",
        "note.spend_usd",
        "note.spend_complete",
        # what the owner's own sessions cost, estimated on Stop; spend_usd is workers
        "note.manager_usd",
        "note.manager_complete",
        "note.dispatches",
        # the manager's own sessions and their subagents, which spend_usd never covered
        "note.session_tokens",
        "note.session_models",
    ]
    assert recent["sort"] == [{"property": "note.date", "direction": "DESC"}]
    hb = yaml.safe_load((tmp_root / "bases/handoffs.base").read_text(encoding="utf-8"))
    assert 'file.hasTag("handoff")' in hb["filters"]["and"]
    assert [v["name"] for v in hb["views"]] == ["Running", "Live", "Done", "All", "Rubric"]
    text = dashboard.read_text(encoding="utf-8")
    assert "generated: true" in text
    assert "# Dashboard" not in text  # inline title covers it
    assert 'dv.view("harness/obsidian/views/features")' not in text
    assert 'dv.view("harness/obsidian/views/tasks")' not in text
    assert "bases/features.base#" not in text
    # The pane owns features and tasks too; journal and log stay below its reading area.
    assert text.index("```agent-live\n```") < text.index("## Journal") < text.index("## Recent log")
    assert "## In flight" not in text
    assert 'dv.view("harness/obsidian/views/log", { hours: 24, limit: 20 })' in text
    # The live sections still read .agent/live.json; the note embeds one plugin pane.
    assert text.count("```agent-live\n```") == 1
    assert 'dv.view("harness/obsidian/views/live"' not in text
    assert 'dv.view("harness/obsidian/views/nav")' in text
    assert "managers:live" not in text
    assert "![[bases/handoffs.base#Live]]" not in text
    assert "## Decisions" not in text  # moved to Index.md
    assert "## Docs" not in text  # moved to Index.md
    assert 'dv.view("harness/obsidian/views/journal", { days: 7 })' in text
    assert "bases/journal.base" not in text  # the week is a view script now
    # the note carries no data, so a new feature or a new day leaves it unchanged
    run(["feature", "new", "gamma", "--title", "G"])
    nested = tmp_root / "journal/2026/09-September/03-Thursday.md"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("# today\n")
    run(["status", "render"])
    assert dashboard.read_text(encoding="utf-8") == text
    views = Path(__file__).resolve().parents[1] / "harness/obsidian/views"
    assert not (views / "live.js").exists()
    for name in ("nav", "tasks", "log", "worker", "feature"):
        assert (views / f"{name}.js").is_file(), name


def _handoffs_live_view_filters_on_the_live_flag(tmp_root: Path):
    """Needs answer was a 7-day window over question/blocked/failed, so probes and stale asks
    sat in it and a done dispatch waiting on a push never showed. Live filters on the `live`
    flag `status render` computes from the dispatch record."""
    import yaml

    hb = yaml.safe_load((tmp_root / "bases/handoffs.base").read_text(encoding="utf-8"))
    assert "Needs answer" not in [v["name"] for v in hb["views"]]
    live = next(v for v in hb["views"] if v["name"] == "Live")
    assert live["filters"] == {"and": ["live == true"]}
    assert "limit" not in live
    assert live["order"][:5] == ["note.id", "note.repo", "note.status", "note.goal", "note.ask"]


def _no_base_expression_wraps_a_date_typed_property_in_date(tmp_root: Path):
    """date() takes a string. `opened`, `closed` and a journal note's `date` are written
    unquoted, so Obsidian hands Bases a date object and the view dies with "Type error in
    date, parameter input. Expected String, given Object". Only the quoted handoff timestamps
    (`started`, `finished`) may be wrapped."""
    import re

    wrapped: list[str] = []
    for path in sorted((tmp_root / "bases").glob("*.base")):
        wrapped += [
            f"{path.name}: {m}" for m in re.findall(r"date\((\w+)\)", path.read_text(encoding="utf-8"))
        ]
    # No view wraps a date today (the Live view dropped Needs answer's 7-day window). The guard
    # stays for the next one that does.
    assert all(m.split(": ")[1] in {"started", "finished"} for m in wrapped), wrapped


def _the_journal_week_is_a_view_script_not_a_second_base_view(tmp_root: Path):
    """The Week view's six columns scrolled sideways past the readable line width
    on the Dashboard while every other section stayed inside it. Both home notes use the
    wrapping journal view with different date windows."""
    import yaml

    jb = yaml.safe_load((tmp_root / "bases/journal.base").read_text(encoding="utf-8"))
    assert [v["name"] for v in jb["views"]] == ["Recent"]
    dashboard = (tmp_root / "Dashboard.md").read_text(encoding="utf-8")
    assert 'await dv.view("harness/obsidian/views/journal", { days: 7 })' in dashboard
    assert "journal.base#Week" not in dashboard
    assert 'await dv.view("harness/obsidian/views/journal", { days: 30 })' in (
        tmp_root / "Index.md"
    ).read_text(encoding="utf-8")


def _a_board_card_leads_with_its_feature_number(tmp_root: Path):
    """The tiles carried the title only, so which feature a card was
    took a click. Bases has no padStart, hence the nested if."""
    import yaml

    fb = yaml.safe_load((tmp_root / "bases/features.base").read_text(encoding="utf-8"))
    board = next(v for v in fb["views"] if v["name"] == "Board")
    assert board["order"][0] == "formula.numbered"
    assert "note.title" not in board["order"]
    assert fb["formulas"]["numbered"].startswith("if(id,")


def _index_is_rendered_and_generated(tmp_root: Path):
    from agent_cli import vault

    index = tmp_root / "Index.md"
    assert index.exists()
    assert vault.INDEX in vault.GENERATED
    text = index.read_text(encoding="utf-8")
    assert "generated: true" in text
    assert 'dv.view("harness/obsidian/views/features")' in text
    for section in ("closed", "adrs", "handoffs"):
        assert f'dv.view("harness/obsidian/views/index", {{ section: "{section}" }})' in text
    for kind in ("retro", "design", "audit", "qa-plan"):
        assert f'dv.view("harness/obsidian/views/index", {{ section: "docs", kind: "{kind}" }})' in text
    assert 'dv.view("harness/obsidian/views/journal", { days: 30 })' in text
    assert "![[bases/" not in text
    assert "[[Dashboard|dashboard]]" in text


def _handoffs_base_shows_the_rubric_columns(tmp_root: Path):
    """Every handoff has carried model, effort and choice_reason since the rubric
    landed. No view showed them, so "which row was picked, and what did it cost" could not be
    asked in the vault."""
    import yaml

    hb = yaml.safe_load((tmp_root / "bases/handoffs.base").read_text(encoding="utf-8"))
    for prop in ("note.model", "note.effort", "note.choice_reason", "note.cost_usd"):
        assert prop in hb["properties"], prop
    rubric = next(v for v in hb["views"] if v["name"] == "Rubric")
    assert rubric["order"] == [
        "note.id",
        "note.repo",
        "note.model",
        "note.effort",
        "note.cost_usd",
        "note.budget_usd",
        "note.choice_reason",
        "note.finished",
    ]


def _docs_base_covers_the_long_form_notes(tmp_root: Path):
    """Eleven notes tagged retro, design, audit or qa-plan had no base at all. The only way
    to find one was to remember which feature folder it was in."""
    import yaml

    from agent_cli import vault

    path = tmp_root / "bases/docs.base"
    assert path.exists()
    assert vault.DOCS_BASE in vault.GENERATED
    db = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert db["filters"]["or"] == [
        'file.hasTag("retro")',
        'file.hasTag("design")',
        'file.hasTag("audit")',
        'file.hasTag("qa-plan")',
    ]
    for prop in ("file.tags", "note.feature", "note.date"):
        assert prop in db["properties"], prop
    assert [v["name"] for v in db["views"]] == ["Recent", "Retros", "Design", "Audits", "QA plans"]
    text = (tmp_root / "Index.md").read_text(encoding="utf-8")  # Docs moved off the dashboard onto Index
    assert "## Docs" in text
    assert 'dv.view("harness/obsidian/views/index", { section: "docs", kind: "retro" })' in text
    assert (
        text.index("## Decisions")
        < text.index("## Docs")
        < text.index("## Handoffs")
        < text.index("## Journal")
    )


def _render_is_idempotent(tmp_root: Path, run):
    paths = [
        tmp_root / "adrs/README.md",
        tmp_root / "Dashboard.md",
        tmp_root / "Index.md",
        tmp_root / "bases/features.base",
    ]
    before = {p: p.stat().st_mtime_ns for p in paths}
    run(["status", "render"])
    assert {p: p.stat().st_mtime_ns for p in paths} == before


def test_check_detects_hand_edits_and_the_hash_covers_tasks_and_unassigned(tmp_root: Path, run):
    import json as _json

    _fixture(run, tmp_root)
    assert run(["status", "check"]).code == 0
    plan = tmp_root / "features/001-alpha-work/plan.md"
    meta, body = frontmatter.load(plan)
    meta["tickets"] = ["T-43"]
    frontmatter.dump(plan, meta, body)
    r = run(["status", "check"])
    assert r.code == 1
    assert "stale" in r.out
    run(["status", "render"])
    assert run(["status", "check"]).code == 0
    meta, body = frontmatter.load(plan)
    frontmatter.dump(plan, meta, body.replace("- [ ] write the thing", "- [x] write the thing"))
    assert run(["status", "check"]).code == 1
    run(["status", "render"])
    assert run(["status", "check"]).code == 0
    # A new unassigned capture file is part of the hash, so the capture that creates one
    # re-renders: before, doctor failed minutes after a fresh setup (#60).
    run(
        ["capture", "--kind", "stop", "--stdin"],
        stdin=_json.dumps({"session_id": "zzz", "hook_event_name": "Stop"}),
    )
    assert run(["status", "check"]).code == 0
    assert json.loads((tmp_root / ".agent/status.json").read_text())["unassigned_raw"]["files"] == 1
    (tmp_root / ".agent/raw/unassigned/2026-09-01.jsonl").write_text("{}\n")  # not via capture
    assert run(["status", "check"]).code == 1

    # a string date does not crash the render a log triggers
    plan = tmp_root / "features/003-old/plan.md"
    text = plan.read_text(encoding="utf-8").replace("closed: 2026-09-03", "closed: '2026-09-01'")
    plan.write_text(text)
    r = run(["log", "1", "still fine"])
    assert r.code == 0, r.err
    assert "still fine" in (tmp_root / "features/001-alpha-work/log.md").read_text(encoding="utf-8")


def test_unassigned_captures_reported(tmp_root: Path, run):
    payload = json.dumps({"session_id": "zzz", "hook_event_name": "Stop"})
    run(["capture", "--kind", "stop", "--stdin"], stdin=payload)
    result = run(["--json", "status", "render"])
    assert result.json["unassigned_raw"] == {"files": 1, "latest": "2026-09-03"}
    assert not (tmp_root / "STATUS.md").exists()


# ------------------------------------------------------------------ one render_quietly (T4 item 3)


def test_render_quietly_reports_a_failure_instead_of_raising(tmp_root: Path, run, capsys):
    """`ops.render_views` and `dispatch.state.render` were the same function with two different
    stderr wordings. One implementation now, with ops' wording. Both names point at it."""
    from agent_cli import dispatch, ops, status

    assert ops.render_views is status.render_quietly
    assert dispatch.state.render is status.render_quietly

    run(["feature", "new", "a", "--title", "A"])
    run(["feature", "new", "b", "--title", "B"])
    (tmp_root / "features/002-b/plan.md").write_text("no frontmatter here\n")
    capsys.readouterr()
    status.render_quietly(tmp_root)  # must not raise
    err = capsys.readouterr().err
    assert "re-render failed" in err
    assert "bin/agent doctor" in err


def test_dispatch_no_longer_aliases_is_alive(tmp_root: Path):
    """`dispatch.state._alive` was `core.is_alive` under a second name. doctor reached for it
    through the package. One name now (T4 item 3)."""
    from agent_cli import dispatch

    assert not hasattr(dispatch.state, "_alive")
    assert not hasattr(dispatch, "_alive")
