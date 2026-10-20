"""A landing finished by recovery records its integration with the tip the journal saved (Codex's
phase-1 review, round 3): a squash landing that crashed after moving main used to finish with no
`integration`, so its builder's worktree stayed held."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import land_recover
from agent_cli.dispatch import state
from land_helpers import (
    child,
    git,
    harness,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture
    only_journal,
    recorded,
)


@pytest.mark.parametrize("point", ["index_locked", "files_applied", "ref_moved"])
def test_a_recovered_squash_landing_records_its_integration(harness: Path, point: str):
    tip = git(harness, "rev-parse", "feature")
    state.save(
        harness,
        {"id": "001-mac-01", "repo": "Harness", "short": "harness", "branch": "feature", "state": "done"},
    )
    recorded(harness)
    assert child(harness, f"{point}:exit", "squash").wait(60) == 97
    journal = only_journal(harness)
    assert journal["tip"] == tip
    finished = land_recover.recover(harness, str(journal["id"]))
    assert finished["state"] == "done"
    integration = state.load(harness, "001-mac-01")["integration"]
    assert (
        integration["state"] == "landed"
        and integration["tip"] == tip
        and integration["journal"] == journal["id"]
    )
