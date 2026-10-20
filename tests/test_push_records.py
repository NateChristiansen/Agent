"""The pre-push record gate, through a real `git push` to a bare remote with the installed hook:
every pushed tip needs a full pass record; the export-only gate still refuses."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli import gitgate, links
from land_helpers import git, harness, recorded  # noqa: F401 -- pytest fixture

VENV = Path(__file__).resolve().parents[1] / ".venv"
AGENT_BIN = VENV / "Scripts" / "agent.exe" if sys.platform == "win32" else VENV / "bin" / "agent"


@pytest.fixture
def pushable(harness: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git(harness, "remote", "add", "origin", str(bare))
    links.make(harness / ".venv", VENV, is_dir=True)
    with (harness / ".gitignore").open("a") as fh:
        fh.write(".venv\n")
    git(harness, "commit", "-qam", "ignore the venv link")
    gitgate.install(harness)
    return harness


def _push(root: Path, *refspecs: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "push", "-q", "origin", *refspecs],
        cwd=root,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )


def _problems(root: Path, rev: str, ref: str) -> list[str]:
    """What the hook's gate says about pushing `rev` to `ref`, in process."""
    from agent_cli import push_records
    from agent_cli.push_input import Update

    return push_records.problems(root, [Update(git(root, "rev-parse", rev), "0" * 40, ref, ref)])


def test_branches_head_to_main_and_tags_each_need_their_tip_recorded(pushable: Path):
    """One real push through the hook; the other refs ask the gate directly. The export-only test
    pushes a recorded main through the hook."""
    refused = _push(pushable, "main")
    assert refused.returncode != 0 and "agent verify record" in refused.stderr
    recorded(pushable, "main")
    assert _problems(pushable, "main", "refs/heads/main") == []
    assert _problems(pushable, "feature", "refs/heads/feature")
    assert _problems(pushable, "feature", "refs/heads/main")
    recorded(pushable, "feature")
    assert _problems(pushable, "feature", "refs/heads/feature") == []
    assert _problems(pushable, "feature", "refs/heads/main") == []
    git(pushable, "switch", "-q", "-c", "topic", "main")
    (pushable / "b.txt").write_text("unrecorded\n")
    git(pushable, "commit", "-qam", "unrecorded tip")
    assert _problems(pushable, "HEAD", "refs/heads/dispatch")
    git(pushable, "tag", "-a", "-m", "release", "v1", "HEAD")
    assert _problems(pushable, "v1", "refs/tags/v1")
    git(pushable, "tag", "-a", "-m", "release", "v0", "main")
    assert _problems(pushable, "v0", "refs/tags/v0") == []


def test_deleting_main_is_refused_and_other_deletions_need_nothing(pushable: Path):
    # The remote branches to delete; the hook is not under test here.
    assert _push(pushable, "--no-verify", "main", "feature").returncode == 0
    from agent_cli import push_records
    from agent_cli.push_input import Update

    gone = Update("0" * 40, git(pushable, "rev-parse", "feature"), "(delete)", "refs/heads/feature")
    assert push_records.problems(pushable, [gone]) == []
    refused = _push(pushable, ":refs/heads/main")
    assert refused.returncode != 0 and "delete main" in refused.stderr


def test_the_export_only_gate_still_refuses_a_recorded_push(pushable: Path):
    recorded(pushable, "main")
    git(pushable, "config", "agent.exportOnly", "true")
    refused = _push(pushable, "main")
    assert refused.returncode != 0 and "agent verify record" not in refused.stderr
    git(pushable, "config", "--unset", "agent.exportOnly")
    assert _push(pushable, "main").returncode == 0


def test_malformed_input_fails_closed(pushable: Path):
    proc = subprocess.run(
        [str(AGENT_BIN), "--root", str(pushable), "git", "pre-push", "origin", "x"],
        input="not four fields\n",
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0 and "malformed" in proc.stderr


def test_an_unreadable_registry_refuses_the_push(pushable: Path):
    """Codex review: a registry that does not parse used to read as a public clone and push freely."""
    from agent_cli import push_records
    from agent_cli.push_input import Update

    (pushable / "agent.yaml").write_text("repos: [\n")
    head = git(pushable, "rev-parse", "main")
    assert push_records.problems(pushable, [Update(head, "0" * 40, "refs/heads/main", "refs/heads/main")])
    refused = _push(pushable, "main")
    assert refused.returncode != 0 and "registry could not be read" in refused.stderr
    update = [Update(head, "0" * 40, "refs/heads/main", "refs/heads/main")]
    (pushable / "agent.yaml").unlink()
    assert push_records.problems(pushable, update) == [], "no registry at all: an unconfigured clone"


def test_a_dangling_registry_link_refuses_the_push(pushable: Path):
    from agent_cli import push_records
    from agent_cli.push_input import Update

    head = git(pushable, "rev-parse", "main")
    update = [Update(head, "0" * 40, "refs/heads/main", "refs/heads/main")]
    (pushable / "agent.yaml").unlink()
    try:
        (pushable / "agent.yaml").symlink_to(pushable / "moved-away.yaml")
    except OSError:
        if sys.platform != "win32":
            raise
        pytest.skip("this Windows account cannot make file symlinks (no Developer Mode)")
    assert push_records.problems(pushable, update), "a link to nothing is unreadable, not absent"
