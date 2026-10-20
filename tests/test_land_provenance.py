"""Disposable Git probes for portable, exact landing provenance."""

from pathlib import Path

import pytest

from agent_cli import land, land_journal, ops, tidy
from agent_cli.cli import parse
from agent_cli.core import AgentError
from agent_cli.mcp.specs_harness import harness as harness_tool
from agent_cli.mcp.tools import Ctx
from land_helpers import git
from land_helpers import harness as harness  # noqa: F401


def copied_branch(root: Path) -> tuple[str, str]:
    original = git(root, "rev-parse", "feature")
    (root / "b.txt").write_text("main advanced\n")
    git(root, "commit", "-am", "advance main before copying")
    git(root, "switch", "-c", "copy")
    git(root, "cherry-pick", original)
    copied = git(root, "rev-parse", "HEAD")
    git(root, "switch", "main")
    assert original != copied
    return original, copied


def advance_main(root: Path, *, edit_landed_path: bool = True) -> None:
    # An unrelated edit advances history; a later edit of the landed path defeats tree fallback.
    (root / "b.txt").write_text("unrelated later edit\n")
    git(root, "commit", "-am", "unrelated edit after landing")
    if edit_landed_path:
        (root / "a.txt").write_text("subsequent main work\n")
        git(root, "commit", "-am", "later edit of landed path")


def test_copy_without_origin_reproduces_cross_machine_unresolved(harness: Path):
    original, _ = copied_branch(harness)
    land.land(harness, "copy", strategy="squash")
    advance_main(harness)
    assert git(harness, "rev-parse", "feature") == original
    assert not tidy.resolved(harness, harness, "feature", records=[])
    assert tidy.ResolutionContext(harness, []).possibly_copied(harness, "feature", "main")


def test_original_copy_land_pull_and_moved_original(harness: Path, tmp_path: Path):
    """Plan D: original T0, distinct copy T1, portable origin evidence, then new work T2."""
    original, copied = copied_branch(harness)
    peer = tmp_path / "peer"
    git(tmp_path, "clone", "--no-local", "--branch", "main", str(harness), str(peer))
    git(peer, "branch", "original", "origin/feature")
    done = land.land(harness, "copy", strategy="squash", origins=[f"feature {original}"])
    assert done["origins"] == [{"branch": "feature", "tip": original}]
    message = git(harness, "show", "-s", "--format=%B", "main")
    assert f"Landed-tip: copy {copied}" in message
    assert f"Landed-tip: feature {original} origin" in message
    advance_main(harness, edit_landed_path=False)
    git(peer, "pull", "--ff-only")
    assert not land_journal.every(peer), "no journal or dispatch sync supplies this proof"
    assert tidy.resolved(peer, peer, "original", records=[])
    # Exact trailers also survive a subsequent edit that defeats per-path tree equality.
    (harness / "a.txt").write_text("subsequent main work\n")
    git(harness, "commit", "-am", "later edit of landed path")
    git(peer, "pull", "--ff-only")
    assert tidy.resolved(peer, peer, "original", records=[])
    git(peer, "switch", "original")
    (peer / "new-work.txt").write_text("unlanded work\n")
    git(peer, "add", "new-work.txt")
    git(peer, "commit", "-m", "original advanced to T2")
    assert not tidy.resolved(peer, peer, "original", records=[])


def test_indent_only_patch_match_is_a_hint_never_resolution(harness: Path):
    source = (
        "def result():\n    for i in range(3):\n        if i == 2:\n            return i\n        return 0\n"
    )
    git(harness, "switch", "-c", "original", "main")
    (harness / "behavior.py").write_text(source)
    git(harness, "add", "behavior.py")
    git(harness, "commit", "-m", "original behavior")
    git(harness, "switch", "-c", "copy", "main")
    (harness / "behavior.py").write_text(source.replace("        return 0", "    return 0"))
    git(harness, "add", "behavior.py")
    git(harness, "commit", "-m", "different indentation")
    git(harness, "switch", "main")
    land.land(harness, "copy", strategy="squash")
    assert git(harness, "cherry", "main", "original").startswith("- ")
    assert not tidy.resolved(harness, harness, "original", records=[])
    assert tidy.ResolutionContext(harness, []).possibly_copied(harness, "original", "main")


def test_multicommit_squash_resolves_via_member_trailer(harness: Path, tmp_path: Path):
    git(harness, "switch", "feature")
    (harness / "a.txt").write_text("second worker change\n")
    git(harness, "commit", "-am", "second")
    tip = git(harness, "rev-parse", "HEAD")
    git(harness, "switch", "main")
    done = land.land(harness, "feature")
    assert done["strategy"] == "squash"
    assert f"Landed-tip: feature {tip}" in git(harness, "show", "-s", "--format=%B", "main")
    advance_main(harness)
    peer = tmp_path / "peer"
    git(tmp_path, "clone", "--no-local", str(harness), str(peer))
    git(peer, "branch", "worker", "origin/feature")
    assert tidy.resolved(peer, peer, "worker", records=[])


@pytest.mark.parametrize(
    "origin", ["feature", "feature main", "feature abcd", "bad..branch {tip}", "feature {missing}"]
)
def test_origins_refuse_malformed_or_unavailable_commits(harness: Path, origin: str):
    before = git(harness, "rev-parse", "main")
    origin = origin.format(tip=git(harness, "rev-parse", "feature"), missing="f" * 40)
    with pytest.raises(AgentError, match="origin"):
        land.land(harness, "feature", origins=[origin])
    assert git(harness, "rev-parse", "main") == before


def test_origin_must_be_a_commit_and_may_be_a_fetched_tip(harness: Path, tmp_path: Path):
    tree = git(harness, "rev-parse", "feature^{tree}")
    with pytest.raises(AgentError, match="fetch the peer"):
        land.land(harness, "feature", origins=[f"original {tree}"])
    peer = tmp_path / "peer"
    git(tmp_path, "clone", "--no-local", str(harness), str(peer))
    (peer / "peer.txt").write_text("peer work\n")
    git(peer, "add", "peer.txt")
    git(peer, "commit", "-m", "peer only")
    tip = git(peer, "rev-parse", "HEAD")
    with pytest.raises(AgentError, match="fetch the peer"):
        land.land(harness, "feature", origins=[f"peer-branch {tip}"])
    git(harness, "fetch", str(peer), "main")
    assert land.checked_origins(land.obs.Git(harness), [f"peer-branch {tip}"]) == [("peer-branch", tip)]


def test_origins_flow_from_mcp_and_cli(harness: Path, monkeypatch):
    origin = f"original {git(harness, 'rev-parse', 'feature')}"
    assert parse(["land", "copy", "--origins", origin, "--origins", origin]).origins == [origin, origin]
    seen = {}

    def capture(root, **kwargs):
        seen.update(kwargs)
        return ops.Op({}, "queued")

    monkeypatch.setattr(ops, "land", capture)
    harness_tool(Ctx(harness, "probe"), action="land", ref="copy", origins=[origin])
    assert seen["origins"] == [origin] and seen["detach"] is True


def test_detached_origins_reach_child_argv(harness: Path, monkeypatch):
    import subprocess

    origin = f"original {git(harness, 'rev-parse', 'feature')}"
    seen = []
    popen = subprocess.Popen

    class Child:
        pid = 12345

    # Only the detached land is stubbed; git that `subprocess.run` starts on the way still runs.
    def spawn(argv, **kwargs):
        if "--op" not in argv:
            return popen(argv, **kwargs)
        seen.extend(argv)
        return Child()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    ops.land(harness, branch="copy", detach=True, origins=[origin])
    assert seen[seen.index("--origins") + 1] == origin
