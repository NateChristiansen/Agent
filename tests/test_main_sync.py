"""`repos_sync harness=true` (main_sync.py, ADR-0041 amendment): a machine that lands through
another fast-forwards the harness's main to the main pushed at the pinned `harness.sync_url`,
and nothing else. Each of Codex's P5 probes is a case here. Round 1: a forced fetch refspec, a
repointed origin, and a checkout switched after detection. Round 2: the private ref made a
symref to main, and a checkout attaching main just before the compare-and-swap."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_cli import land_journal, land_observe, land_recover, main_sync, ops
from agent_cli.core import AgentError


def _git(at: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=at,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _pin(root: Path, url: Path | str) -> None:
    (root / "agent.local.yaml").write_text(f"harness:\n  sync_url: '{url}'\n", encoding="utf-8")


@pytest.fixture(scope="module")
def _harness_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Main pushed to the pinned bare repo; the landing machine then pushes one more commit."""
    tmp_path = tmp_path_factory.mktemp("main-sync-template")
    root = tmp_path / "harness"
    root.mkdir()
    (root / "a.txt").write_text("a\n")
    (root / ".gitignore").write_text(".agent/\nagent.local.yaml\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    bare = tmp_path / "pinned.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "landing-machine"
    _git(tmp_path, "clone", "-q", str(bare), str(other))
    (other / "a.txt").write_text("landed on the other machine\n")
    _git(other, "commit", "-q", "-am", "landed elsewhere")
    _git(other, "push", "-q", "origin", "main")
    return tmp_path


@pytest.fixture
def harness(tmp_path: Path, _harness_template: Path) -> Path:
    for name in ("harness", "pinned.git", "landing-machine"):
        shutil.copytree(_harness_template / name, tmp_path / name, symlinks=True)
    root, bare = tmp_path / "harness", tmp_path / "pinned.git"
    _git(root, "remote", "set-url", "origin", str(bare))
    _pin(root, bare)
    return root


def _tip(at: Path, ref: str = "refs/heads/main") -> str:
    return _git(at, "rev-parse", ref)


def _pushed(root: Path) -> str:
    return _git(root.parent / "landing-machine", "rev-parse", "HEAD")


def _refs(root: Path) -> dict[str, str]:
    rows = _git(root, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
    return dict(row.split(" ", 1) for row in rows)


def test_main_fast_forwards_to_the_pinned_tip_and_nothing_else_moves(harness: Path):
    old = _tip(harness)
    before = _refs(harness)
    done = main_sync.fast_forward(harness)
    assert done["action"] == "fast-forwarded 1 commit(s)" and done["old"] == old
    assert _tip(harness) == _pushed(harness) == done["new"] == _tip(harness, main_sync.SYNC_REF)
    assert (harness / "a.txt").read_text() == "landed on the other machine\n"
    assert _git(harness, "reflog", "-1", "--format=%gs", "refs/heads/main").startswith("agent sync-main")
    after = _refs(harness)
    moved = {n for n in before.keys() | after.keys() if before.get(n) != after.get(n)}
    assert moved == {"refs/heads/main", main_sync.SYNC_REF}  # origin/main, tags: untouched
    assert not (harness / ".git" / "FETCH_HEAD").exists()
    assert main_sync.fast_forward(harness)["action"] == "already current"


def test_no_pinned_url_refuses_with_a_setup_hint(harness: Path):
    (harness / "agent.local.yaml").unlink()
    before = _tip(harness)
    with pytest.raises(AgentError, match="sync_url"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == before


def test_a_repointed_origin_is_never_read(harness: Path, tmp_path: Path):
    """Probe 3: origin points at a repo holding an unverified commit; the pinned URL decides."""
    alternate = tmp_path / "alternate.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(alternate))
    _git(harness, "switch", "-q", "-c", "unverified")
    _git(harness, "commit", "-q", "--allow-empty", "-m", "unverified alternative")
    _git(harness, "push", "-q", str(alternate), "HEAD:main")
    _git(harness, "switch", "-q", "main")
    _git(harness, "remote", "set-url", "origin", str(alternate))
    main_sync.fast_forward(harness)
    assert _tip(harness) == _pushed(harness)


@pytest.mark.parametrize("remote", ["origin", "url"])
def test_a_forced_fetch_refspec_cannot_touch_main(harness: Path, remote: str):
    """Probe 1: a configured `+refs/heads/main:refs/heads/main`, for origin or for the pinned URL
    itself, with a local-only main commit and a detached HEAD. `--refmap=` ignores it, main keeps
    its commit, and the sync refuses."""
    url = str(harness.parent / "pinned.git")
    _git(harness, "commit", "-q", "--allow-empty", "-m", "local only")
    local = _tip(harness)
    _git(harness, "switch", "-q", "--detach", "HEAD~1")
    name = "origin" if remote == "origin" else url
    _git(harness, "config", "--add", f"remote.{name}.fetch", "+refs/heads/main:refs/heads/main")
    with pytest.raises(AgentError, match="lacks"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == local


def test_a_url_rewrite_of_the_pinned_url_refuses(harness: Path, tmp_path: Path):
    url = str(harness.parent / "pinned.git")
    _git(harness, "config", f"url.{tmp_path / 'elsewhere.git'}.insteadOf", url)
    before = _tip(harness)
    with pytest.raises(AgentError, match="rewrites the pinned URL"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == before


def test_a_local_commit_is_never_lost(harness: Path):
    _git(harness, "commit", "-q", "--allow-empty", "-m", "local only")
    before = _tip(harness)
    with pytest.raises(AgentError, match="lacks"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == before


def test_a_dirty_tracked_tree_moves_nothing(harness: Path):
    before = _tip(harness)
    (harness / "a.txt").write_text("uncommitted\n")
    with pytest.raises(AgentError, match="uncommitted"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == before and (harness / "a.txt").read_text() == "uncommitted\n"


def test_a_switch_after_detection_is_refused(harness: Path, monkeypatch: pytest.MonkeyPatch):
    """Probe 4: the checkout switches to another branch right after it was found holding main.
    The sync refuses, and neither main nor the new branch moves."""
    base = _tip(harness)
    real = main_sync._holders  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def switched(main: Path) -> list[Path]:
        holders = real(main)
        if not calls:
            calls.append(1)
            [holder] = holders
            _git(holder, "switch", "-q", "-c", "side")
        return holders

    monkeypatch.setattr(main_sync, "_holders", switched)
    with pytest.raises(AgentError, match="no longer has main"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == base == _tip(harness, "refs/heads/side")


def test_a_switch_between_check_and_merge_is_reported_not_claimed(
    harness: Path, monkeypatch: pytest.MonkeyPatch
):
    """The same race one step later: the merge advances the other branch. The final check sees
    main did not move and says which branch did; it never reports success."""

    def racing(holder: Path, new: str) -> None:
        _git(holder, "switch", "-q", "-c", "side")
        _git(holder, "merge", "--ff-only", "--quiet", new)

    monkeypatch.setattr(main_sync, "_merge", racing)
    base = _tip(harness)
    with pytest.raises(AgentError, match="refs/heads/side checked out"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == base


def test_main_not_checked_out_moves_by_compare_and_swap(harness: Path):
    _git(harness, "switch", "-q", "-c", "side")
    main_sync.fast_forward(harness)
    assert _tip(harness) == _pushed(harness)
    assert _git(harness, "symbolic-ref", "HEAD") == "refs/heads/side"


def test_main_in_a_linked_worktree_is_moved_there(harness: Path, tmp_path: Path):
    _git(harness, "switch", "-q", "--detach")
    holder = tmp_path / "main-holder"
    _git(harness, "worktree", "add", "-q", str(holder), "main")
    main_sync.fast_forward(harness)
    assert _tip(holder) == _pushed(harness)
    assert (holder / "a.txt").read_text() == "landed on the other machine\n"


def test_an_unfinished_landing_refuses(harness: Path):
    land_journal.write(harness, {"id": "pending-1", "state": "prepared"})
    before = _tip(harness)
    with pytest.raises(AgentError, match="--recover pending-1"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == before


def test_the_ops_entry_takes_harness_alone(harness: Path):
    with pytest.raises(AgentError, match="no other option"):
        ops.repos_sync(harness, harness=True, pull=True)
    op: Any = ops.repos_sync(harness, harness=True)
    assert op.data["action"].startswith("fast-forwarded")


def test_only_the_sync_is_not_an_outside_move(harness: Path):
    """`outside_moves` skips a sync to a tip the private ref holds; a `git pull` never counts."""
    main_sync.fast_forward(harness)
    git = land_observe.Git(harness)
    tip = _tip(harness)
    synced = land_recover._synced  # pyright: ignore[reportPrivateUsage]
    assert synced(git, tip, "agent sync-main: Fast-forward")
    assert not synced(git, tip, "pull --ff-only: Fast-forward")
    _git(harness, "commit", "-q", "--allow-empty", "-m", "local only")
    assert not synced(git, _tip(harness), "agent sync-main: Fast-forward")


def _local_only_detached(harness: Path) -> str:
    _git(harness, "commit", "-q", "--allow-empty", "-m", "local only")
    local = _tip(harness)
    _git(harness, "switch", "-q", "--detach", "HEAD~1")
    return local


def test_a_symref_private_ref_cannot_rewind_main(harness: Path):
    """Round 2, probe 1: `refs/agent/sync/main` made a symbolic ref to main. The sync deletes the
    alias itself, fetches into a fresh ref, and main keeps its local-only commit."""
    local = _local_only_detached(harness)
    _git(harness, "symbolic-ref", main_sync.SYNC_REF, main_sync.MAIN)
    with pytest.raises(AgentError, match="lacks"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == local
    assert subprocess.run(["git", "symbolic-ref", "-q", main_sync.SYNC_REF], cwd=harness).returncode != 0
    assert _tip(harness, main_sync.SYNC_REF) == _pushed(harness)  # a plain ref now
    assert not [r for r in _refs(harness) if r.startswith(main_sync.SYNC_PREFIX) and r != main_sync.SYNC_REF]


def test_an_unfinished_landing_refuses_before_any_fetch(harness: Path):
    local = _local_only_detached(harness)
    _git(harness, "symbolic-ref", main_sync.SYNC_REF, main_sync.MAIN)
    land_journal.write(harness, {"id": "pending-2", "state": "prepared"})
    with pytest.raises(AgentError, match="--recover pending-2"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == local


def test_a_held_land_lock_refuses_before_any_fetch(harness: Path):
    local = _local_only_detached(harness)
    _git(harness, "symbolic-ref", main_sync.SYNC_REF, main_sync.MAIN)
    with land_journal.flock(harness), pytest.raises(AgentError, match="land lock"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == local


def test_a_fetch_that_moves_main_is_put_back(harness: Path, monkeypatch: pytest.MonkeyPatch):
    """Whatever way a fetch still found to write the fetched sha into main, it is compared with the
    snapshot taken under the lock and undone by compare-and-swap from that sha."""
    _git(harness, "switch", "-q", "-c", "side")
    base = _tip(harness)
    real = main_sync._git  # pyright: ignore[reportPrivateUsage]

    def moving(at: Path, *args: str, **kwargs: Any) -> tuple[int, str]:
        result = real(at, *args, **kwargs)
        if "fetch" in args:
            fresh = args[-1].split(":", 1)[1]
            _git(at, "update-ref", main_sync.MAIN, _tip(at, fresh))
        return result

    monkeypatch.setattr(main_sync, "_git", moving)
    with pytest.raises(AgentError, match="put back"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == base


def test_a_concurrent_local_commit_is_never_rewound(harness: Path, monkeypatch: pytest.MonkeyPatch):
    """Round 3, probe 1: a real compare-and-swap of main to a new local commit lands during the
    fetch. The sync refuses and leaves it: the local commit stays on main."""
    _git(harness, "switch", "-q", "-c", "side")
    old = _tip(harness)
    local = _git(harness, "commit-tree", f"{old}^{{tree}}", "-p", old, "-m", "concurrent local commit")
    real = main_sync._git  # pyright: ignore[reportPrivateUsage]
    fired: list[bool] = []

    def concurrent(at: Path, *args: str, **kwargs: Any) -> tuple[int, str]:
        if "fetch" in args and not fired:
            fired.append(True)
            _git(harness, "update-ref", main_sync.MAIN, local, old)
        return real(at, *args, **kwargs)

    monkeypatch.setattr(main_sync, "_git", concurrent)
    with pytest.raises(AgentError, match="left as is"):
        main_sync.fast_forward(harness)
    assert (
        subprocess.run(["git", "merge-base", "--is-ancestor", local, main_sync.MAIN], cwd=harness).returncode
        == 0
    )
    assert _tip(harness) == local


def test_main_checked_out_twice_refuses(harness: Path, tmp_path: Path):
    """Round 3, probe 3: `git worktree add --force` made a second checkout of main. A sync would
    leave one of them with old files, so it refuses and names both."""
    extra = tmp_path / "extra"
    _git(harness, "worktree", "add", "--force", "-q", str(extra), "main")
    base = _tip(harness)
    with pytest.raises(AgentError, match="checked out in 2 places") as raised:
        main_sync.fast_forward(harness)
    assert str(extra) in str(raised.value)
    assert _tip(harness) == base
    assert _git(extra, "status", "--porcelain", "--untracked-files=no") == ""


def test_a_failed_fetch_changes_no_ref(harness: Path, tmp_path: Path):
    _pin(harness, tmp_path / "missing.git")
    before = _refs(harness)
    with pytest.raises(AgentError, match="fetch"):
        main_sync.fast_forward(harness)
    assert _refs(harness) == before


@pytest.mark.parametrize("linked", [False, True], ids=["switch", "worktree-add"])
def test_a_checkout_attaching_before_the_swap_is_never_a_false_success(
    harness: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: bool
):
    """Round 2, probe 2: main is not checked out, and a `git switch main` (or a linked worktree
    of main) attaches just before the compare-and-swap. Git cannot lock that out, so the sync
    sees a checkout whose index is the old tree, puts main back and refuses; files and main agree."""
    _git(harness, "switch", "-q", "-c", "side")
    base = _tip(harness)
    holder = tmp_path / "new-holder" if linked else harness
    real = main_sync._git  # pyright: ignore[reportPrivateUsage]

    fired: list[bool] = []

    def racing(at: Path, *args: str, **kwargs: Any) -> tuple[int, str]:
        if args[:1] == ("update-ref",) and main_sync.MAIN in args and "-m" in args and not fired:
            fired.append(True)
            if linked:
                _git(harness, "worktree", "add", "-q", str(holder), "main")
            else:
                _git(harness, "switch", "-q", "main")
        return real(at, *args, **kwargs)

    monkeypatch.setattr(main_sync, "_git", racing)
    with pytest.raises(AgentError, match="checked out main during the sync"):
        main_sync.fast_forward(harness)
    assert _tip(harness) == base
    assert (holder / "a.txt").read_text() == "a\n"
    assert _git(holder, "status", "--porcelain", "--untracked-files=no") == ""


def test_a_checkout_attaching_after_the_swap_is_consistent(harness: Path, monkeypatch: pytest.MonkeyPatch):
    """Attached after the swap, the checkout has the new tree: that is a real success."""
    _git(harness, "switch", "-q", "-c", "side")
    real = main_sync._swap  # pyright: ignore[reportPrivateUsage]

    def then_attach(main: Path, old: str, new: str) -> None:
        real(main, old, new)
        _git(main, "switch", "-q", "main")

    monkeypatch.setattr(main_sync, "_swap", then_attach)
    assert main_sync.fast_forward(harness)["action"].startswith("fast-forwarded")
    assert (harness / "a.txt").read_text() == "landed on the other machine\n"
