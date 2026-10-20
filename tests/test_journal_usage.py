from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from agent_cli import frontmatter, journal
from usage_helpers import publish, sample_record

pytestmark = pytest.mark.usefixtures("notes_remote")

DAY = date(2026, 9, 2)  # the day before the fixed clock's today()
EARLY = "2026-09-02T12:00:00-06:00"  # a checkpoint that stops before the day ends
LATER = "2026-09-04T08:00:00-06:00"

ZERO = {
    "total_tokens": 0,
    "requests": 0,
    "tokens_manager": 0,
    "tokens_subagent": 0,
    "tokens_worker": 0,
    "tokens_other": 0,
    "models": {},
    "dispatches": 0,
    "dispatch_usd": 0.0,
    "manager_usd": 0.0,
    "sources": {
        "claude": {"status": "complete", "files": 0, "requests": 0},
        "codex": {"status": "complete", "files": 0, "requests": 0},
        "costs": {"status": "complete"},
    },
}


def _write_path(root: Path) -> Path:
    return journal.write_generated(root, DAY, "- the day\n")


def _write(root: Path) -> dict:
    return frontmatter.split(_write_path(root).read_text(encoding="utf-8"))[0]


def _old_note(root: Path) -> Path:
    path = journal.ensure_note(root, DAY)
    meta, body = frontmatter.split(path.read_text(encoding="utf-8"))
    meta.update(
        spend_usd=42.5, dispatches=7, manager_usd=3.0, session_tokens=9000, session_models="opus-5 9k"
    )
    path.write_text(frontmatter.join(meta, body), encoding="utf-8")
    return path


def test_two_machines_are_summed_into_one_note(tmp_root: Path):
    publish(tmp_root, DAY, "mac")
    publish(tmp_root, DAY, "windows", record={"dispatch_usd": 8.59, "dispatches": 1})
    meta = _write(tmp_root)
    assert meta["session_tokens"] == 3000
    assert meta["dispatches"] == 3
    assert meta["spend_usd"] == 11.84
    assert meta["manager_usd"] == 9.0
    assert meta["tokens_complete"] is True
    assert set(meta["usage_machines"]) == {"mac", "windows"}
    assert {m: row["status"] for m, row in meta["usage_machines"].items()} == {
        "mac": "complete",
        "windows": "complete",
    }
    assert "gpt-6-sol" in meta["session_models"] and "opus-5-5" in meta["session_models"]


def test_the_frontmatter_for_two_sample_machines(tmp_root: Path, capsys):
    publish(tmp_root, DAY, "mac")
    publish(tmp_root, DAY, "windows", record={"dispatch_usd": 8.59, "dispatches": 1})
    text = journal.write_generated(tmp_root, DAY, "- the day\n").read_text(encoding="utf-8")
    head = text.split("---\n")[1]
    assert head.splitlines()[0] == "tags:"
    assert list(frontmatter.split(text)[0])[:9] == [
        "tags",
        "date",
        "spend_usd",
        "spend_complete",
        "manager_usd",
        "manager_complete",
        "dispatches",
        "session_tokens",
        "session_models",
    ]
    with capsys.disabled():
        print("\n---\n" + head + "---")


def test_a_machine_without_a_record_or_a_covering_checkpoint_is_missing(tmp_root: Path):
    publish(tmp_root, DAY, "mac")
    publish(tmp_root, DAY, "windows", record=False, covered_through=EARLY)
    meta = _write(tmp_root)
    assert meta["usage_machines"]["windows"] == {"status": "missing"}
    assert meta["tokens_complete"] is False
    assert meta["session_tokens"] == 1500, "a lower bound, flagged by tokens_complete"


def test_a_covering_checkpoint_with_no_record_is_inactive(tmp_root: Path):
    publish(tmp_root, DAY, "mac")
    publish(tmp_root, DAY, "windows", record=False)
    meta = _write(tmp_root)
    assert meta["usage_machines"]["windows"]["status"] == "inactive"
    assert meta["tokens_complete"] is True
    assert meta["session_tokens"] == 1500


def test_a_corrupt_record_counts_as_missing(tmp_root: Path):
    publish(tmp_root, DAY, "mac")
    publish(tmp_root, DAY, "windows", record={"total_tokens": 7})  # the classes no longer sum to it
    meta = _write(tmp_root)
    assert meta["usage_machines"]["windows"]["status"] == "missing"
    assert meta["session_tokens"] == 1500
    assert meta["tokens_complete"] is False


def test_a_zero_usage_day_is_complete_not_missing(tmp_root: Path):
    publish(tmp_root, DAY, "mac", record=ZERO)
    meta = _write(tmp_root)
    assert meta["usage_machines"]["mac"]["status"] == "complete"
    assert meta["tokens_complete"] is True
    assert (meta["session_tokens"], meta["dispatches"], meta["spend_usd"]) == (0, 0, 0.0)


def test_an_empty_usage_folder_expects_no_machine_and_still_writes(tmp_root: Path):
    meta = _write(tmp_root)
    assert meta["usage_machines"] == {}
    assert meta["tokens_complete"] is False
    assert meta["session_tokens"] == 0


def test_an_empty_usage_folder_keeps_an_old_notes_figures(tmp_root: Path):
    _old_note(tmp_root)
    meta = _write(tmp_root)
    assert (meta["spend_usd"], meta["session_tokens"]) == (42.5, 9000)
    assert meta["tokens_complete"] is False


def test_an_unparseable_record_is_missing_not_inactive(tmp_root: Path):
    from agent_cli.usage import files

    publish(tmp_root, DAY, "mac")
    publish(tmp_root, DAY, "windows")
    path = files.base(tmp_root) / files.record_rel(DAY, "windows")
    path.write_text("---\ntags: [usage]\nmodels: [broken\n---\n", encoding="utf-8")
    meta = _write(tmp_root)
    assert meta["usage_machines"]["windows"]["status"] == "missing"
    assert meta["tokens_complete"] is False
    assert meta["session_tokens"] == 1500


def test_a_stale_record_is_found_and_journal_write_updates_it(tmp_root: Path):
    # Audit 10 plan.md:115 (K3): journal_write replaces standalone number refresh.
    publish(tmp_root, DAY, "mac")
    publish(tmp_root, DAY, "windows", record=False, covered_through=EARLY)
    _write(tmp_root)
    assert journal.stale_days(tmp_root, DAY) == []
    publish(tmp_root, DAY, "windows", record={"dispatch_usd": 8.59, "dispatches": 1})
    assert journal.stale_days(tmp_root, DAY) == [DAY]
    path = journal.note_path(tmp_root, DAY)
    path.write_text(
        path.read_text(encoding="utf-8").replace("## Notes\n", "## Notes\n\nmine\n", 1), encoding="utf-8"
    )
    before = journal.split_sections(path.read_text(encoding="utf-8"))

    _write(tmp_root)

    meta, _ = frontmatter.split(path.read_text(encoding="utf-8"))
    assert meta["session_tokens"] == 3000 and meta["tokens_complete"] is True
    assert meta["usage_machines"]["windows"]["status"] == "complete"
    assert journal.split_sections(path.read_text(encoding="utf-8")) == before
    assert journal.stale_days(tmp_root, DAY) == []


def test_a_newer_updated_stamp_makes_a_note_stale(tmp_root: Path):
    publish(tmp_root, DAY, "mac")
    _write(tmp_root)
    publish(tmp_root, DAY, "mac", record={"updated": LATER, "total_tokens": 1500})
    assert journal.stale_days(tmp_root, DAY) == [DAY]


def test_missing_days_keeps_its_meaning(tmp_root: Path):
    publish(tmp_root, DAY, "mac")
    _write(tmp_root)
    publish(tmp_root, DAY, "windows", record=False, covered_through=EARLY)
    assert DAY not in journal.missing_days(tmp_root, since=DAY)
    assert journal.stale_days(tmp_root, DAY) == [DAY]


def test_an_old_note_keeps_its_figures_until_every_machine_is_counted(tmp_root: Path):
    path = journal.ensure_note(tmp_root, DAY)
    meta, body = frontmatter.split(path.read_text(encoding="utf-8"))
    meta.update(
        spend_usd=42.5, dispatches=7, manager_usd=3.0, session_tokens=999, session_models="opus-5 999"
    )
    path.write_text(frontmatter.join(meta, body), encoding="utf-8")
    publish(tmp_root, DAY, "mac")
    publish(tmp_root, DAY, "windows", record=False, covered_through=EARLY)

    kept = _write(tmp_root)
    assert (kept["spend_usd"], kept["dispatches"], kept["manager_usd"], kept["session_tokens"]) == (
        42.5,
        7,
        3.0,
        999,
    )
    assert kept["session_models"] == "opus-5 999"
    assert kept["tokens_complete"] is False
    assert kept["usage_machines"]["windows"] == {"status": "missing"}

    for _ in range(2):
        again = _write(tmp_root)
        assert (again["spend_usd"], again["dispatches"], again["session_tokens"]) == (42.5, 7, 999)
    assert frontmatter.split(path.read_text(encoding="utf-8"))[0]["session_tokens"] == 999

    publish(tmp_root, DAY, "windows", record={"dispatch_usd": 8.59, "dispatches": 1})
    _write(tmp_root)
    meta, _ = frontmatter.split(path.read_text(encoding="utf-8"))
    assert meta["session_tokens"] == 3000 and meta["dispatches"] == 3
    assert "usage_held" not in meta


def test_sample_record_is_the_shape_the_journal_reads():
    assert sample_record()["schema"] == 1
