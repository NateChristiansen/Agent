"""Manager cost frontmatter from the usage records."""

import json
from datetime import date
from pathlib import Path

from agent_cli import frontmatter, journal
from usage_helpers import publish


def test_an_old_notes_cost_is_kept_until_the_records_cover_the_day(tmp_root: Path):
    """Refreshing an old note once overwrote $12.34 with $0.0; the cost stays across writes."""
    day = date(2026, 9, 1)
    path = journal.ensure_note(tmp_root, day)
    path.write_text(
        path.read_text(encoding="utf-8").replace("manager_usd: 0.0", "manager_usd: 12.34"),
        encoding="utf-8",
    )

    for text in ("refreshed old note", "again"):
        journal.write_generated(tmp_root, day, text)
        meta, _ = frontmatter.split(path.read_text(encoding="utf-8"))
        assert meta["manager_usd"] == 12.34

    publish(tmp_root, day, record={"manager_usd": 0.0, "manager_complete": True})
    journal.write_generated(tmp_root, day, "recorded zero")
    meta, _ = frontmatter.split(path.read_text(encoding="utf-8"))
    assert meta["manager_usd"] == 0.0
    assert meta["manager_complete"] is True


def test_a_record_with_unverified_manager_cost_marks_the_note_incomplete(tmp_root: Path):
    day = date(2026, 9, 1)
    publish(tmp_root, day, record={"manager_usd": 0.0, "manager_complete": False})
    path = journal.write_generated(tmp_root, day, "unknown cost")
    meta, _ = frontmatter.split(path.read_text(encoding="utf-8"))
    assert meta["manager_usd"] == 0.0
    assert meta["manager_complete"] is False


def test_known_cost_survives_an_unverified_dispatch_and_manager_record(tmp_root: Path, monkeypatch):
    day = date(2026, 9, 22)
    dispatch_dir = tmp_root / ".agent" / "dispatches"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "id": "001-01",
            "feature": 1,
            "state": "done",
            "attempt": 1,
            "cost_usd": 14.99,
            "last_result": {"type": "result", "total_cost_usd": 14.99},
            "finished": "2026-09-22T12:00:00-06:00",
        },
        {
            "id": "001-02",
            "feature": 1,
            "state": "done",
            "attempt": 2,
            "cost_usd": 3.25,
            "finished": "2026-09-22T13:00:00-06:00",
        },
    ]
    for record in records:
        (dispatch_dir / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")
    publish(
        tmp_root,
        day,
        record={
            "dispatch_usd": 18.24,
            "spend_complete": False,
            "manager_usd": 1.5,
            "manager_complete": False,
        },
    )

    path = journal.write_generated(tmp_root, day, "Done today.")
    meta, _ = frontmatter.split(path.read_text(encoding="utf-8"))
    assert (meta["spend_usd"], meta["spend_complete"]) == (18.24, False)
    assert (meta["manager_usd"], meta["manager_complete"]) == (1.5, False)

    path.write_text(
        path.read_text(encoding="utf-8").replace("spend_usd: 18.24", "spend_usd: null"), encoding="utf-8"
    )
    monkeypatch.setattr("agent_cli.journal.block.today", lambda: date(2026, 9, 24))
    assert day in journal.missing_days(tmp_root, since=day)
