"""Two machines appending to one feature log at once (ADR-0034). On 2026-10-03 the first real
case stopped the MacBook's sync on a rebase conflict; with the notes repo's union rule both
sides' entries are kept and the push goes through. Any other file still stops on a conflict."""

from __future__ import annotations

from pathlib import Path

from agent_cli import notes, notes_sync
from test_notes_sync import Pair, git, pair  # noqa: F401 -- reuse the repo pair fixture

LOG = "features/011-x/log.md"


def _seed(p: Pair, attributes: bool) -> None:
    (p.a / "features" / "011-x").mkdir(parents=True)
    (p.a / LOG).write_text("# log\n", encoding="utf-8")
    if attributes:
        (p.a / ".gitattributes").write_text(notes.GITATTRIBUTES, encoding="utf-8")
    git(p.a, "add", "-A")
    git(p.a, "commit", "-q", "-m", "seed log")
    git(p.a, "push", "-q")
    git(p.b, "pull", "-q")


def _append(clone: Path, text: str) -> None:
    with (clone / LOG).open("a", encoding="utf-8") as fh:
        fh.write(text)


def test_concurrent_log_appends_keep_both_sides(pair: Pair):  # noqa: F811
    _seed(pair, attributes=True)
    _append(pair.b, "- from windows\n")
    git(pair.b, "commit", "-q", "-am", "b entry")
    git(pair.b, "push", "-q")
    _append(pair.a, "- from mac\n")
    assert notes_sync.sync(pair.harness).status == "pushed"
    text = git(pair.remote, "show", f"main:{LOG}")
    assert "- from windows" in text and "- from mac" in text and "<<<<<<<" not in text


def test_without_the_rule_the_same_appends_conflict(pair: Pair):  # noqa: F811
    _seed(pair, attributes=False)
    _append(pair.b, "- from windows\n")
    git(pair.b, "commit", "-q", "-am", "b entry")
    git(pair.b, "push", "-q")
    _append(pair.a, "- from mac\n")
    assert notes_sync.sync(pair.harness).status == "conflict"
