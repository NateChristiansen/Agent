from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path

import pytest

from agent_cli import frontmatter, journal, ops, repos
from agent_cli.config_file import registry_path
from agent_cli.core import AgentError
from agent_cli.hooks import journal_nag, journal_pending
from agent_cli.journal import pending as pending_mod
from agent_cli.mcp import specs_harness
from agent_cli.mcp.tools import Ctx
from usage_helpers import publish

pytestmark = pytest.mark.usefixtures("notes_remote")

DAY = date(2026, 9, 3)  # the fixed clock's today()
YESTERDAY = date(2026, 9, 2)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _config(root: Path, journal_machine: str | None, label: str | None) -> None:
    path = registry_path(root)
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not line.startswith(("journal:", "  machine:"))]
    if journal_machine is not None:
        kept += ["journal:", f"  machine: {journal_machine}"]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    local = root / "agent.local.yaml"
    old = local.read_text(encoding="utf-8") if local.exists() else ""
    rest = [line for line in old.splitlines() if not line.startswith("machine:")]
    local.write_text(
        "\n".join([*rest, f"machine: {label}" if label else "notes_sync: off"]) + "\n", encoding="utf-8"
    )


def _draft(root: Path) -> Path:
    draft = root / "draft.md"
    draft.write_text("- did the thing\n", encoding="utf-8")
    return draft


def _prompt() -> dict[str, object]:
    return {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "hello"}


def _harness(root: Path, action: specs_harness.HarnessAction, **kw):
    return specs_harness.harness(Ctx(root=root, profile="manager"), action=action, **kw)


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "value",
    [
        "mac",
        ["mac"],
        {"machine": "Mac"},
        {"machine": "9mac"},
        {"machine": "toolongname"},
        {"machine": None},
        {"machine": "mac", "extra": 1},
        {"machine": 3},
    ],
)
def test_a_bad_journal_value_is_refused(value):
    problems = repos.validate_registry_data({"repos": [], "journal": value})
    assert any("'journal' must be a mapping" in p for p in problems)


@pytest.mark.parametrize("value", [{"machine": "mac"}, {"machine": "win2"}, {}])
def test_a_good_journal_value_passes(value):
    assert repos.validate_registry_data({"repos": [], "journal": value}) == []


# ---------------------------------------------------------------- refusal, CLI and MCP


def test_another_machine_is_refused_on_every_note_write_through_the_cli(tmp_root: Path, run):
    _config(tmp_root, "mac", "win")
    draft = _draft(tmp_root)
    for argv in (
        ["journal", "write", DAY.isoformat(), "--file", str(draft)],
        ["journal", "collect", DAY.isoformat(), "--write"],
    ):
        r = run(["--json", *argv])
        assert r.code != 0, argv
        text = r.out + r.err
        assert "'mac'" in text and "'win'" in text and "journal.machine" in text and "agent.yaml" in text
    assert not journal.note_path(tmp_root, DAY).exists()
    assert not (tmp_root / ".agent" / "journal").exists()


def test_another_machine_is_refused_on_every_note_write_through_the_mcp_tool(tmp_root: Path):
    _config(tmp_root, "mac", "win")
    draft = _draft(tmp_root)
    day = DAY.isoformat()
    calls = [
        ("journal_write", {"date": day, "file": str(draft)}),
        ("journal_collect", {"date": day, "write": True}),
    ]
    for action, kw in calls:
        with pytest.raises(AgentError, match=r"'mac'.*'win'.*journal\.machine.*agent\.yaml|journal\.machine"):
            _harness(tmp_root, action, **kw)
    assert not journal.note_path(tmp_root, DAY).exists()


@pytest.mark.anyio
async def test_the_refusal_reaches_a_client_as_a_tool_error(tmp_root: Path):
    from mcp import Client

    from agent_cli.mcp import server as mcp_server

    _config(tmp_root, "mac", "win")
    draft = _draft(tmp_root)
    async with Client(mcp_server.build_server(tmp_root, "manager")) as client:
        r = await client.call_tool(
            "harness", {"action": "journal_write", "date": DAY.isoformat(), "file": str(draft)}
        )
    assert r.is_error is True
    assert "journal.machine" in getattr(r.content[0], "text", "")


@pytest.mark.parametrize("bad", ["Mac", "toolonglabel", "9mac"])
def test_an_invalid_journal_machine_refuses_every_note_write(tmp_root: Path, run, bad: str):
    _config(tmp_root, bad, "win")
    draft = _draft(tmp_root)
    for argv in (
        ["journal", "write", DAY.isoformat(), "--file", str(draft)],
        ["journal", "collect", DAY.isoformat(), "--write"],
    ):
        r = run(["--json", *argv])
        assert r.code != 0, argv
        assert repr(bad) in r.out + r.err and "journal.machine" in r.out + r.err
    assert not journal.note_path(tmp_root, DAY).exists()


def test_journal_missing_data_matches_its_text(tmp_root: Path, run):
    from agent_cli import ops

    op = ops.journal_missing(tmp_root)
    assert op.data == {"missing": [], "stale": []}
    assert op.text == "(none)"


def test_a_machine_with_no_valid_label_is_refused_with_its_problem(tmp_root: Path, run):
    _config(tmp_root, "mac", None)
    draft = _draft(tmp_root)
    r = run(["--json", "journal", "write", DAY.isoformat(), "--file", str(draft)])
    assert r.code != 0
    assert "no valid label" in r.out + r.err


def test_the_journal_machine_writes_and_so_does_an_unset_key(tmp_root: Path, run):
    draft = _draft(tmp_root)
    for journal_machine, label in (("mac", "mac"), (None, "win"), (None, None)):
        _config(tmp_root, journal_machine, label)
        journal.note_path(tmp_root, DAY).unlink(missing_ok=True)
        assert run(["--json", "journal", "write", DAY.isoformat(), "--file", str(draft)]).code == 0
        assert run(["--json", "journal", "collect", DAY.isoformat(), "--write"]).code == 0
        assert _harness(tmp_root, "journal_write", date=DAY.isoformat(), file=str(draft)).data["ok"] is True


# ---------------------------------------------------------------- nudges


def test_the_three_nudges_are_quiet_on_another_machine(tmp_root: Path, run, monkeypatch):
    run(["feature", "new", "x", "--title", "X"])
    run(["log", "1", "did a thing"])
    pending_mod.record(tmp_root, "feature-close", "016 final-audit", "done", day=DAY)
    _config(tmp_root, "mac", "mac")
    assert "016 final-audit" in journal_nag(tmp_root)
    assert journal_pending(tmp_root, _prompt()) != ""
    _config(tmp_root, "mac", "win")
    assert journal_nag(tmp_root) == ""
    assert journal_pending(tmp_root, _prompt()) == ""


def test_a_close_on_the_journal_machine_still_says_to_write_the_journal(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    _config(tmp_root, "mac", "mac")
    r = run(["--json", "feature", "close", "1"])
    assert r.code == 0, r.err
    assert json.loads(r.out)["next"] == "/journal today"


def test_the_close_result_on_another_machine_has_no_journal_step(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    _config(tmp_root, "mac", "win")
    r = run(["--json", "feature", "close", "1"])
    assert r.code == 0
    assert "next" not in json.loads(r.out)


def test_the_unset_key_nudges_as_before(tmp_root: Path, run):
    pending_mod.record(tmp_root, "feature-close", "016 final-audit", "done", day=DAY)
    _config(tmp_root, None, "win")
    assert "016 final-audit" in journal_nag(tmp_root)
    assert journal_pending(tmp_root, _prompt()) != ""


def test_the_nag_keeps_missing_days_but_drops_stale_numbers(tmp_root: Path, run, monkeypatch):
    stale_day, missing_day = date(2026, 9, 5), date(2026, 9, 6)
    for module in ("block", "usage_note"):
        monkeypatch.setattr(f"agent_cli.journal.{module}.today", lambda: date(2026, 9, 10))
    publish(tmp_root, stale_day, "mac")
    publish(tmp_root, stale_day, "windows", record=False, covered_through="2026-09-05T12:00:00-06:00")
    journal.write_generated(tmp_root, stale_day, "- the day\n")
    journal.ensure_note(tmp_root, missing_day)
    pending_mod.record(tmp_root, "feature-close", "x", "", day=missing_day)
    publish(tmp_root, stale_day, "windows", record={"dispatches": 1})
    listing = ops.journal_missing(tmp_root)
    assert listing.data["stale"] == [stale_day.isoformat()]
    assert "stale (run `/journal <date>`):" in listing.text
    _config(tmp_root, "mac", "mac")
    lines = [line for line in journal_nag(tmp_root).splitlines() if "journal" in line]
    # Audit 10 plan.md:118 (K6): stale numbers no longer nag on each compact.
    assert not any("stale for" in line for line in lines)
    missing_line = next(line for line in lines if "missing for" in line)
    assert "2026-09-06" in missing_line and "2026-09-05" not in missing_line


# ---------------------------------------------------------------- the lock


def test_two_writers_on_one_note_do_not_lose_each_other(tmp_root: Path, monkeypatch):
    """A write holds the note while it works. A second write waits for the lock and its
    block survives."""
    from agent_cli.journal import usage_note

    publish(tmp_root, YESTERDAY, record=False)
    journal.write_generated(tmp_root, YESTERDAY, "first block")
    inside, release = threading.Event(), threading.Event()
    real = usage_note.numbers_for
    calls: list[str] = []

    def slow(root, day, meta):
        calls.append(threading.current_thread().name)
        if threading.current_thread().name == "first":
            inside.set()
            assert release.wait(5)
        return real(root, day, meta)

    monkeypatch.setattr(usage_note, "numbers_for", slow)
    first = threading.Thread(
        target=journal.write_generated, args=(tmp_root, YESTERDAY, "first block"), name="first"
    )
    writer = threading.Thread(
        target=journal.write_generated, args=(tmp_root, YESTERDAY, "second block"), name="writer"
    )
    first.start()
    assert inside.wait(5)
    writer.start()
    writer.join(0.3)
    assert writer.is_alive(), "the writer waits for the note's lock"
    release.set()
    first.join(5)
    writer.join(5)
    text = journal.note_path(tmp_root, YESTERDAY).read_text(encoding="utf-8")
    assert journal.split_sections(text)[0] == "second block"
    assert calls == ["first", "writer"]
    meta, _ = frontmatter.split(text)
    assert "mac" in meta["usage_machines"]
