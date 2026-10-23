"""Bounded batches (K): several ready branches land as one exact tree, certified once, in
one journal. A batch that fails as a whole lands nothing and names its members; there is no
culprit isolation. Each member's dispatch gets its integration outcome, recovery included."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_cli import land, land_recover, verify_record
from agent_cli.core import AgentError
from agent_cli.dispatch import state
from land_helpers import REGISTRY, git, only_journal, recorded
from land_helpers import certified as certified  # fixtures, re-exported for pytest
from land_helpers import harness as harness


def _branch(root: Path, name: str, files: dict[str, str], base: str = "main") -> str:
    git(root, "switch", "-q", "-c", name, base)
    for path, text in files.items():
        (root / path).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"{name} work")
    git(root, "switch", "-q", "main")
    return git(root, "rev-parse", name)


def _prerecord(root: Path, files: dict[str, str]) -> None:
    """A pass record for main plus `files`, the tree a batch of them builds, so the landing under
    test reuses it instead of running the verifier."""
    _branch(root, "combined", files)
    recorded(root, "combined")
    git(root, "branch", "-q", "-D", "combined")


def _dispatch(root: Path, dispatch_id: str, branch: str) -> None:
    state.save(
        root, {"id": dispatch_id, "repo": "Harness", "short": "harness", "branch": branch, "state": "done"}
    )


def _records(root: Path) -> list[Path]:
    runs = root / ".agent" / "verify" / "runs"
    return sorted(runs.iterdir()) if runs.is_dir() else []


def test_two_disjoint_branches_land_as_one_certified_tree_in_one_journal(harness: Path):
    _branch(harness, "x", {"x.txt": "x\n"})
    _branch(harness, "y", {"y.txt": "y\n"})
    _dispatch(harness, "001-mac-01", "x")
    _dispatch(harness, "001-mac-02", "y")
    done = land.land(harness, "x", extra=["y"])
    assert done["state"] == "done" and [m["branch"] for m in done["members"]] == ["x", "y"]
    assert git(harness, "show", "main:x.txt") == "x" and git(harness, "show", "main:y.txt") == "y"
    assert only_journal(harness)["id"] == done["id"]
    assert len(_records(harness)) == 1, "one certificate for the combined tree"
    message = git(harness, "show", "-s", "--format=%B", "main")
    for member in done["members"]:
        assert f"Landed-tip: {member['branch']} {member['tip']}" in message
    for dispatch_id in ("001-mac-01", "001-mac-02"):
        landed: dict[str, Any] = state.load(harness, dispatch_id)["integration"]
        assert landed["state"] == "landed" and landed["journal"] == done["id"]


def test_an_interacting_pair_lands_nothing_and_names_both(harness: Path):
    cmd = "test ! -f x.txt -o ! -f y.txt && echo 3 passed in 0.01s"
    (harness / "agent.yaml").write_text(REGISTRY.format(cmd=cmd))
    _branch(harness, "x", {"x.txt": "x\n"})
    _branch(harness, "y", {"y.txt": "y\n"})
    before = git(harness, "rev-parse", "main")
    with pytest.raises(AgentError, match="the batch x, y did not pass together, so nothing landed"):
        land.land(harness, "x", extra=["y"])
    assert git(harness, "rev-parse", "main") == before
    [run] = _records(harness)
    assert any(run.iterdir()), "the failing combination's evidence is kept"


def test_a_member_stacked_on_another_lands_after_it(harness: Path, certified: None):
    _branch(harness, "lower", {"types.txt": "t1\n"})
    _branch(harness, "upper", {"use.txt": "uses t1\n"}, base="lower")
    land.land(harness, "lower", extra=["upper"])
    assert git(harness, "show", "main:types.txt") == "t1"
    assert git(harness, "show", "main:use.txt") == "uses t1"


def test_main_moving_under_the_batch_lands_nothing(harness: Path, monkeypatch: pytest.MonkeyPatch):
    _prerecord(harness, {"x.txt": "x\n", "y.txt": "y\n"})
    _branch(harness, "x", {"x.txt": "x\n"})
    _branch(harness, "y", {"y.txt": "y\n"})
    real = verify_record.record

    def record(root: Path, rev: str) -> dict[str, Any]:
        found = real(root, rev)
        (harness / "b.txt").write_text("b2\n")
        git(harness, "commit", "-q", "-am", "someone else moved main")
        return found

    monkeypatch.setattr(verify_record, "record", record)
    with pytest.raises(AgentError, match="main moved since the candidate was built"):
        land.land(harness, "x", extra=["y"])
    assert git(harness, "show", "main:x.txt", check=False) == ""


def test_a_batch_has_a_bound_and_a_strategy(harness: Path):
    names = [f"b{i}" for i in range(land.BATCH_MAX + 1)]
    for name in names:
        _branch(harness, name, {f"{name}.txt": "n\n"})
    with pytest.raises(AgentError, match=f"at most {land.BATCH_MAX}"):
        land.land(harness, names[0], extra=names[1:])
    with pytest.raises(AgentError, match="squash or merge"):
        land.land(harness, names[0], extra=[names[1]], strategy="ff")


def test_a_crash_mid_batch_recovers_and_records_every_member(harness: Path):
    point = "files_applied"
    _prerecord(harness, {"x.txt": "x\n", "y.txt": "y\n"})
    _branch(harness, "x", {"x.txt": "x\n"})
    _branch(harness, "y", {"y.txt": "y\n"})
    _dispatch(harness, "001-mac-01", "x")
    _dispatch(harness, "001-mac-02", "y")
    code = (
        "from pathlib import Path; from agent_cli import land\n"
        "land._review_gate = lambda *a, **k: []\n"
        f"land.land(Path({str(harness)!r}), 'x', extra=['y'])\n"
    )
    env = {**os.environ, land.SEAM: f"{point}:exit"}
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 97, proc.stderr
    land_recover.recover_all(harness, harness)
    assert git(harness, "show", "main:x.txt") == "x" and git(harness, "show", "main:y.txt") == "y"
    for dispatch_id in ("001-mac-01", "001-mac-02"):
        assert state.load(harness, dispatch_id)["integration"]["state"] == "landed"


def test_a_member_stacked_on_a_nonadjacent_one_squashes_cleanly(harness: Path, certified: None):
    """Codex, round 3: only the immediately preceding member was considered. Round 2: the default
    squash refused an upper member that re-edits its lower's line."""
    _branch(harness, "lower", {"a.txt": "lower\n"})
    _branch(harness, "beside", {"beside.txt": "b\n"})
    _branch(harness, "upper", {"a.txt": "upper\n"}, base="lower")
    land.land(harness, "lower", extra=["beside", "upper"])
    assert git(harness, "show", "main:a.txt") == "upper" and git(harness, "show", "main:beside.txt") == "b"


def test_landing_a_squash_landed_branch_again_is_refused(harness: Path, certified: None):
    """The phase-5 dogfood: a second landing of a squash-landed branch made an empty commit."""
    _branch(harness, "x", {"x.txt": "x\n"})
    land.land(harness, "x", strategy="squash")
    before = git(harness, "rev-parse", "main")
    with pytest.raises(AgentError, match="nothing to land"):
        land.land(harness, "x", strategy="squash")
    assert git(harness, "rev-parse", "main") == before
