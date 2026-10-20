"""Bounds on the processes one operation starts, so the suite's per-process cost cannot creep back."""

from __future__ import annotations

import collections
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_cli import land, notes_sync
from agent_cli import land_journal as jn
from agent_cli import land_observe as obs
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401
from land_helpers import git, harness, recorded  # noqa: F401


class Spawns:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.stacks: list[str] = []

    def git(self) -> list[list[str]]:
        return [a for a in self.argv if a and os.path.basename(a[0]).lower() in ("git", "git.exe")]

    def by_command(self) -> collections.Counter[str]:
        out: collections.Counter[str] = collections.Counter()
        for argv in self.git():
            i = 1
            while i < len(argv) and argv[i] in ("-c", "-C"):
                i += 2
            out[argv[i] if i < len(argv) else ""] += 1
        return out


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> Spawns:
    seen = Spawns()
    real = subprocess.Popen.__init__

    def counting(self: subprocess.Popen[Any], args: Any, *a: Any, **kw: Any) -> None:
        seen.argv.append([str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)])
        frame, chain = sys._getframe(1), []
        while frame is not None:
            name = Path(frame.f_code.co_filename)
            if "agent_cli" in name.parts:
                chain.append(f"{name.name}:{frame.f_code.co_name}")
            frame = frame.f_back
        seen.stacks.append(" < ".join(chain))
        real(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    return seen


def test_an_ff_landing_with_a_reused_record_stays_under_its_git_call_bound(harness: Path, spawns: Spawns):  # noqa: F811
    recorded(harness)
    spawns.argv.clear()
    journal = land.land(harness, "feature", strategy="ff")
    assert journal["state"] == "done" and journal["record_reused"] is True
    calls = spawns.by_command()
    # 74 before each tree, the object format, the config and the git directory were read once.
    # Windows has no os.getuid, so git_discovery defers each main-checkout lookup to git; it
    # also asks which scope sets core.autocrlf and runs `git --exec-path` to find Git Bash.
    common = sum(a[-1] == "--git-common-dir" for a in spawns.git())
    if sys.platform == "win32":
        assert common <= 25, calls
        assert len(spawns.git()) - common <= 59, calls
    else:
        assert len(spawns.git()) <= 56, calls
    assert calls["ls-tree"] <= 3 and calls["config"] <= (6 if sys.platform == "win32" else 5), calls
    assert sum(a[1:3] == ["rev-parse", "--absolute-git-dir"] for a in spawns.git()) == 1, calls


def test_a_dispatch_stays_under_its_git_call_bound(droot: Path, fake_claude, spawns: Spawns):  # noqa: F811
    fake_claude.scenario([one(envelope())])
    spawns.argv.clear()
    d = _new_fast(droot)
    assert d["state"] == "done"
    # 47 before the launch read the notes label ledger once instead of twice, and 43 before a
    # launch stopped inspecting the claim that `allocation.claim` replaces.
    assert len(spawns.git()) <= 39, spawns.by_command()
    assert sum("label_claims.py:inspect" in s for s in spawns.stacks) == 0


def test_a_no_diff_observation_reads_the_git_config_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_cli.dispatch import no_diff, verification_process

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "start"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv], cwd=repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout
    spawns, real = Spawns(), verification_process.capture

    def counting(argv: list[str], *a: Any, **kw: Any) -> Any:
        spawns.argv.append([str(x) for x in argv])
        return real(argv, *a, **kw)

    monkeypatch.setattr(verification_process, "capture", counting)
    seen = no_diff.observe(repo, head.strip())
    assert seen.clean and seen.head == head.strip()
    # 9 before: the untracked listing read the filter config again, and an unchanged HEAD
    # listed the launch base's tree a second time.
    assert len(spawns.git()) == 7, spawns.argv
    assert sum("config" in a for a in spawns.argv) == 1 and sum("ls-tree" in a for a in spawns.argv) == 1


def test_the_git_directory_is_read_once_per_scope_and_again_outside_it(harness: Path, spawns: Spawns):  # noqa: F811
    with jn.git_dir_scope():
        assert jn.index_file(harness) == jn.lock_file(harness).with_name("index")
    assert jn.git_dir(harness) == jn.git_dir(harness)
    assert len(spawns.git()) == 3


def test_a_hook_reads_the_standing_notes_failure_with_one_git_call(droot: Path, spawns: Spawns):  # noqa: F811
    assert notes_sync.standing_failure(droot) is None
    # 2 before: opening the repo for a remote also asked for core.sshCommand.
    assert len(spawns.git()) == 1, spawns.argv


def test_a_hook_process_does_not_import_the_dispatch_package():
    """Measured as a module set, not a time: under load a timing bound only flakes. The dispatch
    package was about 40 ms of a 130 ms `agent hook pre-bash`."""
    code = (
        "import sys; from agent_cli import entry; from agent_cli.hooks import groups, run; "
        "print(sorted(m for m in sys.modules if m.startswith('agent_cli.dispatch')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_one_observer_reads_a_commit_tree_once_but_a_ref_every_time(harness: Path, spawns: Spawns):  # noqa: F811
    g = obs.Git(harness)
    sha = git(harness, "rev-parse", "feature")
    spawns.argv.clear()
    first = obs.tree_entries(g, sha)
    first.clear()
    assert obs.tree_entries(g, sha) and obs.tree_entries(g, "feature") and obs.tree_entries(g, "feature")
    assert spawns.by_command()["ls-tree"] == 3
    assert obs.tree_entries(obs.Git(harness), sha) == obs.tree_entries(g, sha)
    assert land.object_format(g) == land.object_format(g) == "sha1"
