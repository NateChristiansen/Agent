from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agent_cli import lessons
from agent_cli.core import AgentError

EVIDENCE = "a worker returned a question in 15 seconds and the run looked hung for 10 minutes"
LOCAL = "harness/owner/lessons-queue.md"
LEGACY = "harness/lessons/queue.md"


def _add(root: Path, **kw) -> lessons.Entry:
    kw.setdefault("title", "check the question hold before calling a run stuck")
    kw.setdefault("source", "none")
    kw.setdefault("evidence", EVIDENCE)
    kw.setdefault("home", "dispatch skill references")
    return lessons.add(root, **kw)


def test_add_list_resolve_round_trip_in_the_local_queue(tmp_root: Path):
    first = _add(tmp_root, scope="harness")
    second = _add(tmp_root, scope="private", kind="practice", title="name who builds and when")
    third = _add(tmp_root, title="no scope at all")
    assert [first.id, second.id, third.id] == ["L-20260903-mac-01", "L-20260903-mac-02", "L-20260903-mac-03"]
    assert (tmp_root / LOCAL).exists()
    assert not (tmp_root / LEGACY).exists(), "nothing writes the retired tracked queue"
    assert "scope" not in first.as_dict()
    assert [e.id for e in lessons.open_entries(tmp_root)] == [first.id, second.id, third.id]

    done = lessons.resolve(tmp_root, entry_id=first.id, disposition="prose", note="dispatch lessons.md")
    assert done.fields["disposition"] == "prose: dispatch lessons.md"
    assert [e.id for e in lessons.open_entries(tmp_root)] == [second.id, third.id]
    reloaded = {e.id: e for e in lessons.load(tmp_root)}[first.id]
    assert reloaded.resolved and reloaded.fields["resolved"] == "2026-09-03"
    with pytest.raises(AgentError, match="already resolved"):
        lessons.resolve(tmp_root, entry_id=first.id, disposition="dropped", note="again")


@pytest.mark.parametrize(
    ("disposition", "note"),
    [
        ("upstream", "https://github.com/owner/agent/issues/12"),
        ("config", "agent.yaml dispatch block for the api repo"),
        ("repo", "the api repo's CLAUDE.md"),
    ],
)
def test_the_triage_dispositions(tmp_root: Path, disposition: str, note: str):
    entry = _add(tmp_root)
    done = lessons.resolve(tmp_root, entry_id=entry.id, disposition=disposition, note=note)
    assert done.fields["disposition"] == f"{disposition}: {note}"
    assert lessons.load(tmp_root)[0].resolved


def test_a_missing_queue_is_an_empty_queue(tmp_root: Path):
    """An existing notes repo never receives a new example file from `notes link`, so nothing
    may depend on the file existing before the first add."""
    assert not (tmp_root / LOCAL).exists()
    assert lessons.open_entries(tmp_root) == []
    assert lessons.nudge(tmp_root) is None


def test_resolve_needs_a_note_and_a_known_id(tmp_root: Path):
    entry = _add(tmp_root)
    with pytest.raises(AgentError, match="--note is required"):
        lessons.resolve(tmp_root, entry_id=entry.id, disposition="dropped", note="  ")
    with pytest.raises(AgentError, match="no lesson"):
        lessons.resolve(tmp_root, entry_id="L-20260903-99", disposition="dropped", note="x")
    with pytest.raises(AgentError, match="disposition must be one of"):
        lessons.resolve(tmp_root, entry_id=entry.id, disposition="filed", note="x")


def test_two_machines_mint_distinct_ids_and_an_ambiguous_id_refuses(tmp_root: Path):
    """Two machines sharing one notes repo once minted the same id on the same day."""
    local = tmp_root / "agent.local.yaml"
    local.write_text("machine: mac\n", encoding="utf-8")
    mac = _add(tmp_root, title="from the mac")
    local.write_text("machine: pc\n", encoding="utf-8")
    pc = _add(tmp_root, title="from the pc")
    assert [mac.id, pc.id] == ["L-20260903-mac-01", "L-20260903-pc-01"]
    assert [e.id for e in lessons.open_entries(tmp_root)] == [mac.id, pc.id]

    path = tmp_root / LOCAL
    legacy = "### L-20260901-01 — {}\n- kind: lesson\n\n"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("## Open\n", "## Open\n" + legacy.format("one") + legacy.format("two")))
    before = path.read_text(encoding="utf-8")
    with pytest.raises(AgentError, match="names 2 entries"):
        lessons.resolve(tmp_root, entry_id="L-20260901-01", disposition="dropped", note="x")
    assert path.read_text(encoding="utf-8") == before
    done = lessons.resolve(tmp_root, entry_id=pc.id, disposition="dropped", note="x")
    assert done.resolved and done.title == "from the pc"


def test_without_a_label_ids_fall_back_to_the_legacy_form(tmp_root: Path):
    (tmp_root / "agent.local.yaml").write_text("{}\n", encoding="utf-8")
    assert _add(tmp_root).id == "L-20260903-01"


def test_concurrent_adds_get_distinct_ids_and_all_land(tmp_root: Path):
    ids: list[str] = []
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            ids.append(_add(tmp_root, title=f"finding {n}").id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(ids)) == 6
    assert len(lessons.open_entries(tmp_root)) == 6


def test_hand_edits_survive_the_next_add_and_resolve(tmp_root: Path):
    """The review of the first draft: `_write` rebuilt the file from parsed fields, so the next
    add silently deleted anything the owner had written by hand."""
    entry = _add(tmp_root)
    path = tmp_root / LOCAL
    text = path.read_text(encoding="utf-8")
    text = text.replace("## Open", "Owner: review these on Fridays.\n\n## Open")
    text = text.replace(
        "- home: dispatch skill references", "- home: dispatch skill references\nkeep this: owner note"
    )
    path.write_text(text, encoding="utf-8")

    _add(tmp_root, title="second finding")
    lessons.resolve(tmp_root, entry_id=entry.id, disposition="dropped", note="covered elsewhere")
    after = path.read_text(encoding="utf-8")
    assert "Owner: review these on Fridays." in after
    assert "keep this: owner note" in after
    by_id = {e.id: e for e in lessons.load(tmp_root)}
    assert by_id[entry.id].extra == ["keep this: owner note"]
    assert all(not e.extra for i, e in by_id.items() if i != entry.id)
    # a second write leaves the file byte-identical: no blank lines accumulate
    _add(tmp_root, title="third")
    lessons.resolve(tmp_root, entry_id="L-20260903-mac-03", disposition="dropped", note="x")
    once = path.read_text(encoding="utf-8")
    lessons._write(tmp_root, *lessons._load_for_write(tmp_root))
    assert path.read_text(encoding="utf-8") == once


def test_text_the_writer_cannot_keep_refuses_the_mutation(tmp_root: Path):
    _add(tmp_root)
    path = tmp_root / LOCAL
    path.write_text(
        path.read_text(encoding="utf-8").replace("## Open\n", "## Open\nstray line\n"), encoding="utf-8"
    )
    before = path.read_text(encoding="utf-8")
    with pytest.raises(AgentError, match="cannot keep"):
        _add(tmp_root, title="another")
    assert path.read_text(encoding="utf-8") == before


def test_a_failing_queue_does_not_hide_the_retro_nag(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_cli.hooks import session_blocks

    monkeypatch.setattr(session_blocks, "dispatches_since_last_retro", lambda root: 99)

    def boom(root):
        raise RuntimeError("malformed queue")

    monkeypatch.setattr(lessons, "nudge", boom)
    assert "99 dispatches since the last retro" in session_blocks.lesson_nag(tmp_root)


def test_the_nudge_fires_at_ten_open(tmp_root: Path):
    from agent_cli.hooks.session_blocks import lesson_nag

    for n in range(9):
        _add(tmp_root, title=f"finding {n}")
    tracked = tmp_root / LEGACY
    tracked.parent.mkdir(parents=True, exist_ok=True)
    old = "".join(f"### L-20260901-{n:02d} — old {n}\n- kind: lesson\n\n" for n in range(1, 9))
    tracked.write_text(f"# Lesson queue: harness\n\n## Open\n\n{old}## Resolved\n", encoding="utf-8")
    assert lessons.nudge(tmp_root) is None
    assert "lesson queue" not in lesson_nag(tmp_root)
    _add(tmp_root, title="finding 9")
    line = "lesson queue: 10 open — run /lesson review"
    assert lessons.nudge(tmp_root) == line
    assert line in lesson_nag(tmp_root)


def test_add_and_resolve_rewrite_the_dashboard_count(tmp_root: Path, run):
    """The Dashboard reads the queue when the snapshot is built, so a change writes it at once
    instead of waiting for the next hook."""
    import json

    from agent_cli.vault import live

    def count() -> int:
        rows = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["health"]["rows"]
        return next(r for r in rows if r["signal"] == "lessons")["count"]

    r = run(["--json", "lesson", "add", "a finding", "--evidence", EVIDENCE])
    assert r.code == 0, r
    assert count() == 1
    r = run(["--json", "lesson", "resolve", r.json["id"], "--disposition", "dropped", "--note", "covered"])
    assert r.code == 0, r
    assert count() == 0


def test_cli_round_trip(tmp_root: Path, run):
    r = run(["--json", "lesson", "add", "a finding", "--scope", "harness", "--evidence", EVIDENCE])
    assert r.code == 0, r
    assert r.json["id"] == "L-20260903-mac-01"
    r = run(["--json", "lesson", "add", "another", "--evidence", EVIDENCE])
    assert r.code == 0, r
    r = run(["--json", "lesson", "list", "--scope", "private"])
    assert r.json["count"] == 2
    r = run(
        [
            "--json",
            "lesson",
            "resolve",
            "L-20260903-mac-01",
            "--disposition",
            "upstream",
            "--note",
            "https://x/1",
        ]
    )
    assert r.code == 0 and r.json["state"] == "resolved" and r.json["resolved"] == "2026-09-03"
    assert run(["--json", "lesson", "list"]).json["count"] == 1
