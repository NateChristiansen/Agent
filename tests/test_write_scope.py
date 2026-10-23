"""Declared write scope (phase D): unknown, read-only review, integration paths.

A read-only review once declared no writes, which the board read as "may hold
any path", and it blocked builds until it was cancelled. `writes: []` now means a read-only
review that blocks nothing; unknown scope and real overlap still refuse; a read-only review that
leaves changes fails; a resume may narrow its paths, never widen them, and the change is recorded.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_cli.core import AgentError
from agent_cli.dispatch import lifecycle, write_scope
from agent_cli.dispatch.state import load, save

evaluate_mod = importlib.import_module("agent_cli.dispatch.evaluate")

from dispatch_helpers import (  # noqa: E402
    _new_fast as new_fast,  # pyright: ignore[reportPrivateUsage] -- shared test helper
)
from dispatch_helpers import (  # noqa: E402
    droot,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture, used as a parameter name
    envelope,
    one,
    settle,
)


def _active(root: Path, d: dict[str, Any], writes: list[str] | None) -> None:
    d = load(root, d["id"])
    d["state"] = "question"  # active on the board, with no live process to check
    d["writes"] = writes
    save(root, d)


def _launch(root: Path, repo: str, writes: list[str]) -> None:
    with write_scope.reserve(root, repo, writes):
        pass


def test_canonical_writes_keeps_read_only_apart_from_unknown() -> None:
    assert write_scope.canonical_writes(None) is None
    assert write_scope.canonical_writes([]) == []
    assert write_scope.scope(None) == "unknown"
    assert write_scope.scope([]) == write_scope.READ_ONLY
    assert write_scope.scope(["src/x.py"]) == "integration"


def test_unknown_scope_still_blocks_and_real_overlap_still_refuses(droot: Path, fake_claude: Any) -> None:
    fake_claude.scenario([one(envelope())])
    unknown = new_fast(droot)
    _active(droot, unknown, [])
    _launch(droot, unknown["repo"], ["src/independent.py"])  # a read-only review blocks nothing
    _active(droot, unknown, None)
    with pytest.raises(AgentError, match="declares no writable paths"):
        _launch(droot, unknown["repo"], ["src/independent.py"])
    _active(droot, unknown, ["src/shared.py"])
    with pytest.raises(AgentError, match="writes overlap"):
        _launch(droot, unknown["repo"], ["src/shared.py"])


def test_a_read_only_review_that_leaves_changes_fails(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_claude.scenario([one(envelope())])
    d = load(droot, new_fast(droot)["id"])
    d["writes"] = []
    worktree = Path(d["worktree"])
    assert write_scope.broken_read_only(d) is None  # a clean review keeps its scope
    probe = next(p for p in worktree.rglob("*") if p.is_file() and ".git" not in p.parts)
    original = probe.read_text(encoding="utf-8")
    probe.write_text(original + "\nmutated by a probe\n", encoding="utf-8")
    broken = write_scope.broken_read_only(d)
    assert broken is not None and str(probe.relative_to(worktree)) in broken
    probe.write_text(original, encoding="utf-8")  # a restored probe mutation is fine
    assert write_scope.broken_read_only(d) is None
    # Evaluation fails a read-only review with changes.
    monkeypatch.setattr(
        evaluate_mod, "_decide", lambda *_a, **_k: {"state": "done", "auto_resume": None, "error": None}
    )
    monkeypatch.setattr(write_scope, "git_files", lambda *_a: ["src/agent_cli/core.py"])
    out = evaluate_mod.evaluate(droot, d)
    assert out["state"] == "failed"
    assert "read-only review" in out["error"] and "src/agent_cli/core.py" in out["error"]
    d["writes"] = ["src/agent_cli/core.py"]  # an integration dispatch is not judged by this rule
    assert evaluate_mod.evaluate(droot, d)["state"] == "done"


def test_no_checks_grants_no_read_only_scope(droot: Path, fake_claude: Any) -> None:
    fake_claude.scenario([one(envelope())])
    d = load(droot, new_fast(droot, no_checks=True)["id"])
    assert d.get("writes") is None
    assert write_scope.scope(d.get("writes")) == "unknown"


def test_a_resume_may_narrow_its_paths_and_the_change_is_recorded() -> None:
    d: dict[str, Any] = {"id": "x", "attempt": 1, "writes": ["tests/selection.toml", "src/report"]}
    write_scope.narrow(d, ["src/report/cost.py"], 2)
    assert d["writes"] == ["src/report/cost.py"]
    assert d["writes_history"][0] | {"at": None} == {
        "attempt": 2,
        "from": ["tests/selection.toml", "src/report"],
        "to": ["src/report/cost.py"],
        "at": None,
    }
    write_scope.narrow(d, [], 3)  # down to read-only
    assert d["writes"] == [] and len(d["writes_history"]) == 2


@pytest.mark.parametrize("old", [["src/report"], []])
def test_a_resume_may_not_widen_its_paths(old: list[str]) -> None:
    d: dict[str, Any] = {"id": "x", "attempt": 1, "writes": old}
    with pytest.raises(AgentError, match="may only narrow"):
        write_scope.narrow(d, ["tests/selection.toml"], 2)
    assert d["writes"] == old and "writes_history" not in d


def test_unknown_scope_may_declare_paths_on_resume() -> None:
    d: dict[str, Any] = {"id": "x", "attempt": 1, "writes": None}
    write_scope.narrow(d, ["src/a.py"], 2)
    assert d["writes"] == ["src/a.py"]


def test_narrowing_on_resume_frees_the_paths_it_dropped(droot: Path, fake_claude: Any) -> None:
    """022 N1a could not resume while its own selection-table reservation overlapped N0."""
    fake_claude.scenario([one(envelope())])
    builder = new_fast(droot)
    settle(droot, builder)
    _running(droot, "099-mac-99", ["tests/selection.toml"], repo=builder["repo"], short=builder["short"])
    d = load(droot, builder["id"])
    d["writes"] = ["tests/selection.toml", "src/report/cost.py"]
    save(droot, d)
    with pytest.raises(AgentError, match="writes overlap"):
        lifecycle._prepare_resume(droot, dispatch_id=d["id"], answer="go on")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(AgentError, match="stop after the board check"):
        real = write_scope.check_resume

        def checked(root: Path, rec: dict[str, Any], narrow_to: list[str] | None = None) -> Any:
            undo = real(root, rec, narrow_to=narrow_to)
            undo()
            assert rec["writes"] == ["src/report/cost.py"]
            raise AgentError("stop after the board check")

        write_scope.check_resume = checked  # type: ignore[assignment]
        try:
            lifecycle._prepare_resume(  # pyright: ignore[reportPrivateUsage]
                droot, dispatch_id=d["id"], answer="go on", writes=["src/report/cost.py"]
            )
        finally:
            write_scope.check_resume = real  # type: ignore[assignment]


def test_git_files_sees_an_uncommitted_probe(tmp_path: Path) -> None:
    tmp_path = tmp_path / "repo"
    tmp_path.mkdir()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a"],
        check=True,
    )
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout
    d: dict[str, Any] = {"writes": [], "worktree": str(tmp_path), "base": base.strip()}
    assert write_scope.broken_read_only(d) is None
    (tmp_path / "a.txt").write_text("b", encoding="utf-8")
    assert "a.txt" in str(write_scope.broken_read_only(d))


def test_a_committed_then_reverted_change_still_breaks_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run([*run, "add", "a.txt"], check=True)
    subprocess.run([*run, "commit", "-qm", "a"], check=True)
    start = subprocess.run([*run, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "a.txt").write_text("b", encoding="utf-8")
    subprocess.run([*run, "commit", "-qam", "change"], check=True)
    subprocess.run([*run, "revert", "--no-edit", "HEAD"], check=True, capture_output=True)
    d: dict[str, Any] = {"writes": [], "worktree": str(repo), "base": start, "start_sha": start}
    assert "2 commit(s)" in str(write_scope.broken_read_only(d))


def test_read_only_boundary_requires_resume_evidence_and_survives_auto_retry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = ["git", "-C", str(repo)]
    subprocess.run([*run, "init", "-q", "-b", "worker"], check=True)
    subprocess.run([*run, "commit", "--allow-empty", "-qm", "base"], check=True)
    base = subprocess.run(
        [*run, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "outside.txt").write_text("implementation", encoding="utf-8")
    subprocess.run([*run, "add", "outside.txt"], check=True)
    subprocess.run([*run, "commit", "-qm", "implementation"], check=True)
    d: dict[str, Any] = {"writes": [], "worktree": str(repo), "branch": "worker", "base": base, "attempt": 2}
    assert "1 commit(s)" in str(write_scope.broken_scope(d))
    d["writes_history"] = [{"attempt": 2, "from": None, "to": []}]
    assert "1 commit(s)" in str(write_scope.broken_scope(d))
    write_scope.prepare_read_only_resume(d, 2, previous={**d, "writes": None})
    assert write_scope.broken_scope(d) is None
    d["attempt"] = 3
    assert "1 commit(s)" in str(write_scope.broken_scope(d))
    d["auto_resumes"] = [{"attempt": 2, "reason": "timeout"}]
    assert write_scope.broken_scope(d) is None
    subprocess.run([*run, "commit", "--allow-empty", "-qm", "new write"], check=True)
    assert "1 commit(s)" in str(write_scope.broken_scope(d))
    d["writes"] = ["allowed"]
    assert "outside.txt" in str(write_scope.broken_scope(d))
    d["writes"] = None
    assert write_scope.broken_scope(d) is None


@pytest.mark.parametrize("stage", ["committed", "staged", "unstaged"])
def test_declared_scope_checks_both_rename_paths_and_deleted_files(tmp_path: Path, stage: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*run, "init", "-q"], check=True)
    (repo / "allowed").mkdir()
    for name in ("outside source.txt", "deleted.txt", "allowed/moved.txt"):
        (repo / name).write_text(name, encoding="utf-8")
    subprocess.run([*run, "add", "."], check=True)
    subprocess.run([*run, "commit", "-qm", "base"], check=True)
    base = subprocess.run([*run, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "outside source.txt").rename(repo / "allowed/incoming.txt")
    (repo / "allowed/moved.txt").rename(repo / "outside destination.txt")
    (repo / "deleted.txt").unlink()
    if stage != "unstaged":
        subprocess.run([*run, "add", "-A"], check=True)
    if stage == "committed":
        subprocess.run([*run, "commit", "-qm", "changes"], check=True)
    (repo / "allowed/new.txt").write_text("fine", encoding="utf-8")
    unusual = 'outside\n"quoted".txt'
    (repo / unusual).write_text("untracked", encoding="utf-8")
    d: dict[str, Any] = {"writes": ["allowed/./"], "worktree": str(repo), "base": base}
    broken = write_scope.broken_scope(d)
    assert broken is not None
    for name in ("outside source.txt", "outside destination.txt", "deleted.txt", unusual):
        assert repr(name) in broken
    assert "allowed/" not in broken
    d["writes"] = None
    assert write_scope.broken_scope(d) is None
    d["writes"] = ["allowed", "outside source.txt", "outside destination.txt", "deleted.txt", unusual]
    assert write_scope.broken_scope(d) is None


def test_outside_scope_return_is_recorded_as_failed(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_claude.scenario([one(envelope())])
    decide = evaluate_mod._decide  # pyright: ignore[reportPrivateUsage]

    def leave_change(root: Path, d: dict[str, Any], rp: Any) -> dict[str, Any]:
        (Path(d["worktree"]) / "outside.txt").write_text("left behind", encoding="utf-8")
        return decide(root, d, rp)

    monkeypatch.setattr(evaluate_mod, "_decide", leave_change)
    recorded = new_fast(droot, writes=["allowed"])
    assert recorded["state"] == "failed"
    assert "outside.txt" in load(droot, recorded["id"])["error"]
    assert recorded["writes"] == ["allowed"]


# ------------------------------------------------------------------ the path board


@pytest.mark.parametrize(("first", "second"), [("src/x/", "src/./x"), ("src/x", "src//x/y")])
def test_path_spellings_cannot_hide_an_overlap(first: str, second: str) -> None:
    assert write_scope.overlaps([first], [second])
    assert not write_scope.overlaps(["src/x"], ["src/xy"])


@pytest.mark.parametrize("bad", ["../outside", "/abs/path", "", "src/../.."])
def test_a_path_outside_the_repo_is_refused(bad: str) -> None:
    with pytest.raises(AgentError, match="relative to the repo"):
        write_scope.canonical(bad)


def _running(root: Path, dispatch_id: str, writes: list[str] | None, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": dispatch_id,
        "repo": "Harness",
        "short": "harness",
        "state": "running",
        "attempt": 1,
        "writes": writes,
        "supervisor_pid": os.getpid(),
        **fields,
    }
    save(root, record)
    return record


def test_a_resume_checks_the_board_but_not_itself(tmp_path: Path) -> None:
    _running(tmp_path, "001-mac-01", ["src/shared/"])
    me: dict[str, Any] = {"id": "001-mac-02", "repo": "Harness", "attempt": 1, "writes": ["src/shared/x/"]}
    with pytest.raises(AgentError, match="not resumed: writes overlap 001-mac-01"):
        write_scope.check_resume(tmp_path, me)
    _running(tmp_path, "001-mac-02", ["src/mine/"])
    undo = write_scope.check_resume(
        tmp_path, {"id": "001-mac-02", "repo": "Harness", "writes": ["src/mine/"]}
    )
    undo()


def test_another_repo_s_paths_never_clash(tmp_path: Path) -> None:
    _running(tmp_path, "001-mac-01", None, repo="Other")
    _launch(tmp_path, "Harness", ["src/shared/"])


def test_a_stale_record_holds_nothing(tmp_path: Path) -> None:
    _running(tmp_path, "001-mac-01", None, supervisor_pid=2**22 + 9, worker_cleanup_confirmed=True)
    _launch(tmp_path, "Harness", ["src/z/"])


def test_a_dead_supervisor_s_unconfirmed_worker_still_holds_its_paths(tmp_path: Path) -> None:
    """Codex, round 3: a live worker lost its reservation when its supervisor died."""
    _running(tmp_path, "001-mac-01", ["src/z"], supervisor_pid=2**22 + 9, worker_cleanup_pending=True)
    with pytest.raises(AgentError, match="not launched: writes overlap 001-mac-01"):
        _launch(tmp_path, "Harness", ["src/z/"])


def test_a_second_resume_of_one_dispatch_waits_for_the_first(tmp_path: Path) -> None:
    """Codex, rounds 6-7: overlapping resumes shared one reservation name and one lease holder, so
    an older failure dropped the newer one's paths and holdings. One resume at a time."""
    d = {**_running(tmp_path, "001-mac-01", ["src/a/"]), "state": "blocked"}
    save(tmp_path, d)
    first = write_scope.check_resume(tmp_path, d)
    assert "001-mac-01" in write_scope.resuming(tmp_path)
    with pytest.raises(AgentError, match="already being resumed"):
        write_scope.check_resume(tmp_path, d)
    with pytest.raises(AgentError, match="not launched: writes overlap 001-mac-01"):
        _launch(tmp_path, "Harness", ["src/a/b"])  # the reservation holds its paths meanwhile
    first()
    write_scope.check_resume(tmp_path, d)()
