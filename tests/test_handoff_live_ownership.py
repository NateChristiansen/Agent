"""Who owns a handoff's live frontmatter once the notes repo syncs between machines (ADR-0034).

`sync_live_fields` rewrites `open`, `goal`, `ask` and `live` only from this machine's own
dispatch records, which live under the gitignored `.agent/` and never travel with the notes
repo. So the machine that ran a dispatch is the one writer of its handoff's live fields; a
render on the other machine, which has no record for it, leaves the synced file as it is.
"""

from __future__ import annotations

from pathlib import Path

from agent_cli import notes
from agent_cli.dispatch import handoffs as handoffs_mod

HANDOFF = "features/011-x/handoffs/011-01.md"
SYNCED = "---\nopen: obsidian://shell-commands/?vault=Agent&_dispatch=001-01\nlive: true\n---\n\nbody\n"


def test_a_machine_without_the_dispatch_record_never_rewrites_the_handoff(tmp_path: Path):
    other = tmp_path / "windows" / "Agent"
    (other / HANDOFF).parent.mkdir(parents=True)
    (other / HANDOFF).write_text(SYNCED, encoding="utf-8")
    assert handoffs_mod.sync_live_fields(other) == 0
    assert (other / HANDOFF).read_text(encoding="utf-8") == SYNCED


def test_dispatch_records_never_travel_with_the_notes_repo():
    assert ".agent/" in notes.GITIGNORE.splitlines()
