"""The seven same-tree duplicate pairs of 2026-10-06, replayed (C6).

Codex found seven pairs where a dispatch's supervisor ran the full suite and `agent land` then
ran it again for the identical Git tree: 5,515 seconds, 91.9 minutes
(seven pairs of identical trees). A landing's candidate has
the branch head's tree whenever main is an ancestor of the branch, whether it lands by ff, squash
or a merge commit. A full
attempt is now certified as a pass record keyed by tree, so the landing reuses it: one run each.

One pair per strategy replays the mechanism; the seven identical pairs it once looped over cost
about a minute of suite time and proved nothing more (verify speed, phase 3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_cli import land, verify_record, verify_run
from land_helpers import git
from land_helpers import harness as harness

PAIRS = 1


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = verify_run.run

    def run(main: Path, repo: Any, sha: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(sha)
        return real(main, repo, sha, *args, **kwargs)

    monkeypatch.setattr(verify_run, "run", run)
    return seen


@pytest.mark.parametrize("strategy", ["ff", "squash", "merge"])
def test_a_certified_head_lands_with_no_second_run(harness: Path, runs: list[str], strategy: str):
    for pair in range(PAIRS):
        branch = f"pair-{pair}"
        git(harness, "switch", "-q", "-c", branch, "main")
        (harness / f"work-{pair}.txt").write_text(f"{pair}\n")
        git(harness, "add", "-A")
        git(harness, "commit", "-q", "-m", f"pair {pair}")
        git(harness, "switch", "-q", "main")
        head_tree = git(harness, "rev-parse", f"{branch}^{{tree}}")
        certified = verify_record.record(harness, branch)  # the supervisor's full attempt
        landed = land.land(harness, branch, strategy=strategy)
        assert git(harness, "rev-parse", "main^{tree}") == head_tree
        assert landed["record"] == certified["run_id"] and landed["record_reused"]
    assert len(runs) == PAIRS, "one full run per pair, not two"
