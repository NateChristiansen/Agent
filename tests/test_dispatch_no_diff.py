"""The automatic no-diff exemption: an unchanged dispatch takes no lane and runs no command, and
acceptance observes the predicate again rather than trusting what was published."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli.dispatch import lane, no_diff, verification
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.state import load, save
from dispatch_helpers import droot  # noqa: F401
from land_helpers import git as _git
from test_dispatch_verification import finish, ready  # noqa: F401


def _started(ready) -> tuple[Path, dict, Path]:
    root, d = ready
    wt = Path(d["worktree"])
    d["start_sha"] = _git(wt, "rev-parse", "HEAD")
    save(root, d)
    return root, load(root, d["id"]), wt


def _no_lane(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("an unchanged dispatch must not wait for a verification lane")

    monkeypatch.setattr(lane, "await_lane", refuse)
    monkeypatch.setattr(verification, "launch_prepared", refuse)


def test_an_unchanged_dispatch_takes_no_lane_and_runs_nothing(ready, monkeypatch):
    root, d, _ = _started(ready)
    _no_lane(monkeypatch)
    gate.execute(root, d["id"], d["attempt"])
    after = load(root, d["id"])
    assert after["verification"]["mode"] == "exempt"
    assert after["no_diff"] == {"start_sha": d["start_sha"], "relied_on": no_diff.NO_RECORD, "attempt": 1}
    result = finish(root, after)
    assert result["verification_result"]["status"] == "exempt"
    assert no_diff.NO_DIFF in " ".join(result["verification_result"]["reasons"])


def _commit(wt: Path) -> None:
    (wt / "new.txt").write_text("x\n")
    _git(wt, "add", "new.txt")
    _git(wt, "commit", "-q", "-m", "change")


def _empty_commit(wt: Path) -> None:
    _git(wt, "commit", "-q", "--allow-empty", "-m", "empty")


def _commit_and_revert(wt: Path) -> None:
    _commit(wt)
    _git(wt, "revert", "--no-edit", "HEAD")


def _staged(wt: Path) -> None:
    (wt / "staged.txt").write_text("x\n")
    _git(wt, "add", "staged.txt")


def _unstaged(wt: Path) -> None:
    tracked = _git(wt, "ls-files").splitlines()[0]
    (wt / tracked).write_text((wt / tracked).read_text() + "edit\n")


def _deleted(wt: Path) -> None:
    (wt / _git(wt, "ls-files").splitlines()[0]).unlink()


def _mode(wt: Path) -> None:
    path = wt / _git(wt, "ls-files").splitlines()[0]
    path.chmod(path.stat().st_mode | 0o111)


def _untracked(wt: Path) -> None:
    (wt / "scratch.txt").write_text("notes\n")


@pytest.mark.parametrize(
    "change",
    [
        _commit,
        _empty_commit,
        _commit_and_revert,
        _staged,
        _unstaged,
        _deleted,
        pytest.param(
            _mode,
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="Windows has no executable bit for chmod to change"
            ),
        ),
        _untracked,
    ],
)
def test_any_change_since_the_start_does_not_qualify(ready, change):
    _, d, wt = _started(ready)
    change(wt)
    assert no_diff.eligible(wt, d["start_sha"]) is None


def test_an_ignored_file_does_not_disqualify(ready):
    _, d, wt = _started(ready)
    (wt / ".gitignore").write_text("build/\n")
    _git(wt, "add", ".gitignore")
    _git(wt, "commit", "-q", "-m", "ignore build")
    d["start_sha"] = _git(wt, "rev-parse", "HEAD")
    (wt / "build").mkdir()
    (wt / "build" / "out.bin").write_bytes(b"\0")
    assert no_diff.eligible(wt, d["start_sha"]) is not None


def test_a_missing_start_or_failed_observation_never_qualifies(ready, monkeypatch):
    _, d, wt = _started(ready)
    assert no_diff.eligible(wt, None) is None

    def broken(*args, **kwargs):
        raise OSError("git vanished")

    monkeypatch.setattr(verification_process_module(), "observe_tree", broken)
    assert no_diff.eligible(wt, d["start_sha"]) is None


def verification_process_module():
    from agent_cli.dispatch import verification_process

    return verification_process


@pytest.mark.parametrize("local", [None, "false"])
def test_a_crlf_checkout_of_lf_blobs_reads_clean_by_the_platform_default(tmp_path: Path, local):
    """Git for Windows' system core.autocrlf=true is ignored by verification, so its named default
    stands in on Windows; a repository's own setting still decides."""
    wt = tmp_path / "crlf"
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    if local is not None:
        _git(wt, "config", "core.autocrlf", local)
    (wt / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    _git(wt, "-c", "core.autocrlf=true", "add", "a.txt")
    _git(wt, "commit", "-q", "-m", "lf blob")
    assert _git(wt, "cat-file", "blob", "HEAD:a.txt") == "one\ntwo"
    later = (wt / "a.txt").stat().st_mtime + 5
    os.utime(wt / "a.txt", (later, later))  # stat differs from the index, so Git compares content
    tree = verification.observe_tree(wt)
    assert tree.index_clean
    assert tree.tracked_clean is (sys.platform == "win32" and local is None)


def test_a_changed_dispatch_still_waits_for_the_lane(ready, monkeypatch):
    root, d, wt = _started(ready)
    _commit(wt)
    waited = []
    monkeypatch.setattr(lane, "await_lane", lambda root, ident, attempt: waited.append(ident))
    gate.execute(root, d["id"], d["attempt"])
    assert waited == [d["id"]]


@pytest.mark.parametrize("change", [_untracked, _unstaged, _commit])
def test_acceptance_observes_again_after_the_exemption(ready, monkeypatch, change):
    root, d, wt = _started(ready)
    _no_lane(monkeypatch)
    gate.execute(root, d["id"], d["attempt"])
    change(wt)
    result = finish(root, load(root, d["id"]))
    assert result["verification_result"]["status"] == "unverified"


def test_a_stored_no_diff_reason_grants_nothing_when_the_start_differs(ready, monkeypatch):
    root, d, _ = _started(ready)
    _no_lane(monkeypatch)
    gate.execute(root, d["id"], d["attempt"])
    forged = load(root, d["id"])
    forged["start_sha"] = "0" * 40
    save(root, forged)
    result = finish(root, load(root, d["id"]))
    assert result["verification_result"]["status"] == "unverified"


def test_an_explicit_no_checks_exemption_binds_head_only(droot: Path, monkeypatch):
    """An explicit exemption certifies nothing about the tree: a review reusing a dirty build
    worktree stays exempt."""
    from agent_cli.dispatch import lifecycle
    from dispatch_helpers import DEAD_PID, _new, envelope

    monkeypatch.setattr(lifecycle, "start", lambda root, d, watch: d)
    d = _new(droot, no_checks=True)
    ret = envelope()
    ret["session_id"] = d["session_id"]
    ret["structured_output"]["nonce"] = d["nonce"]
    d.update(state="running", supervisor_pid=DEAD_PID, stop_reason="result", exit_code=0, last_result=ret)
    save(droot, d)
    gate.execute(droot, d["id"], d["attempt"])
    _untracked(Path(d["worktree"]))
    assert finish(droot, load(droot, d["id"]))["verification_result"]["status"] == "exempt"
