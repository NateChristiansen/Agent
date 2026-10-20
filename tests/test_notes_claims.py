"""Ids are claimed on the notes remote as a compare-and-swap (ADR-0040).

Real git throughout: the `notes_remote` fixture's bare remote, this machine's notes clone linked
into `tmp_root`, and for the two-device cases a second clone behind a second harness root.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli import features, notes_claim_guard, notes_claims, notes_sync
from agent_cli.core import AgentError
from harness_root import NOTES_REMOTE, link_notes
from seeded import writable_retry


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def remote_claims(remote: Path) -> list[dict]:
    text = git(remote, "show", f"main:{notes_claims.CLAIMS_PATH}")
    return [json.loads(line) for line in text.splitlines()]


def other_device(tmp_path: Path, remote: Path, name: str = "b") -> Path:
    """A second machine: its own harness root and its own clone of the same notes remote."""
    clone = tmp_path / f"notes-{name}"
    git(tmp_path, "clone", "-q", str(remote), str(clone))
    git(clone, "config", "user.email", f"{name}@example.com")
    git(clone, "config", "user.name", name)
    root = tmp_path / f"harness-{name}"
    (root / ".agent").mkdir(parents=True)
    (root / ".agent" / "notes-root").write_text(str(clone), encoding="utf-8")
    return root


@pytest.fixture
def remote(tmp_root: Path, notes_remote: Path) -> Path:
    return tmp_root.parent / NOTES_REMOTE


def test_feature_new_is_one_claim_commit_on_the_fetched_head(
    tmp_root: Path, notes_remote: Path, remote: Path, run
):
    before = git(remote, "rev-parse", "main")
    local_head = git(notes_remote, "rev-parse", "HEAD")
    r = run(["--json", "feature", "new", "x", "--title", "X"])
    assert r.code == 0, r.err
    assert r.json["dir"] == "features/001-x"

    after = git(remote, "rev-parse", "main")
    assert git(remote, "rev-list", "--parents", "-n", "1", after).split() == [after, before]
    assert git(remote, "diff-tree", "--no-commit-id", "--name-only", "-r", after) == notes_claims.CLAIMS_PATH
    assert git(remote, "log", "-1", "--format=%s", after) == "notes: claim feature 001 x"
    [claim] = remote_claims(remote)
    assert {k: claim[k] for k in ("kind", "id", "slug")} == {"kind": "feature", "id": 1, "slug": "x"}
    assert claim["op"] in git(remote, "log", "-1", "--format=%b", after)
    assert git(notes_remote, "rev-parse", "HEAD") == local_head
    assert not (notes_remote / "records").exists()

    assert run(["--json", "feature", "new", "y", "--title", "Y"]).json["dir"] == "features/002-y"
    assert [c["id"] for c in remote_claims(remote)] == [1, 2]


def test_a_loaded_machine_still_claims_a_feature_id(tmp_root: Path, notes_remote: Path, monkeypatch):
    """Under load `feature new` ran out of a hook's 5 s budget before the claim
    started, refused, and tests failed later with "no feature with id 1". Each git call here
    spends a second of the budget."""
    real = notes_sync.Repo.git

    def slow(self, *args, **kw):
        self.end -= 1.0
        return real(self, *args, **kw)

    monkeypatch.setattr(notes_sync.Repo, "git", slow)
    assert notes_claims.claim(tmp_root, "feature", "x").id == 1


def test_no_notes_repo_refuses(tmp_root: Path, run):
    r = run(["feature", "new", "x", "--title", "X"])
    assert r.code == 2
    assert "no notes repo" in r.err
    assert not (tmp_root / "features" / "001-x").exists()


def test_an_unreachable_remote_refuses(tmp_root: Path, remote: Path, run):
    shutil.rmtree(remote, onexc=writable_retry)
    r = run(["feature", "new", "x", "--title", "X"])
    assert r.code == 2
    assert "remote is unreachable" in r.err
    assert not (tmp_root / "features" / "001-x").exists()


def test_a_notes_repo_with_no_remote_claims_locally(tmp_root: Path, notes_remote: Path, remote: Path, run):
    git(notes_remote, "remote", "remove", "origin")
    head = git(notes_remote, "rev-parse", "HEAD")
    r = run(["--json", "feature", "new", "x", "--title", "X"])
    assert r.code == 0, r.err
    assert r.json["dir"] == "features/001-x"
    assert run(["--json", "feature", "new", "y", "--title", "Y"]).json["dir"] == "features/002-y"
    assert notes_claims.claim(tmp_root, "initiative", "goal").id == 1
    assert git(notes_remote, "rev-parse", "HEAD") == head
    assert not (notes_remote / "records").exists()

    # A remote added later counts the ids taken without it, though no folder carries them.
    shutil.rmtree(tmp_root / "features" / "002-y")
    git(notes_remote, "remote", "add", "origin", str(remote))
    git(notes_remote, "config", "branch.main.remote", "origin")
    git(notes_remote, "config", "branch.main.merge", "refs/heads/main")
    assert notes_claims.claim(tmp_root, "feature", "z").id == 3
    assert [(c["kind"], c["id"]) for c in remote_claims(remote)] == [
        ("feature", 1),
        ("feature", 2),
        ("initiative", 1),
        ("feature", 3),
    ]
    # The carried claims are published once, not again with every later claim.
    assert notes_claims.claim(tmp_root, "feature", "w").id == 4
    assert [c["id"] for c in remote_claims(remote)] == [1, 2, 1, 3, 4]


def test_another_device_never_gets_an_id_taken_before_the_remote_existed(
    tmp_root: Path, notes_remote: Path, remote: Path, tmp_path: Path
):
    git(notes_remote, "remote", "remove", "origin")
    assert features.new(tmp_root, "x", "X").id == 1
    assert notes_claims.claim(tmp_root, "initiative", "goal").id == 1
    git(notes_remote, "remote", "add", "origin", str(remote))
    git(notes_remote, "config", "branch.main.remote", "origin")
    git(notes_remote, "config", "branch.main.merge", "refs/heads/main")
    assert notes_claims.claim(tmp_root, "initiative", "next").id == 2

    device_b = other_device(tmp_path, remote)
    assert not (device_b / "features").exists()
    assert notes_claims.claim(device_b, "feature", "other").id == 2
    assert notes_claims.claim(device_b, "initiative", "third").id == 3


def test_a_remote_with_no_upstream_still_refuses(tmp_root: Path, notes_remote: Path, run):
    git(notes_remote, "branch", "--unset-upstream")
    r = run(["feature", "new", "x", "--title", "X"])
    assert r.code == 2
    assert "has no upstream" in r.err
    assert not (tmp_root / "features" / "001-x").exists()


def test_two_devices_compete_for_one_id_and_exactly_one_wins(
    tmp_root: Path, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    device_b = other_device(tmp_path, remote)
    real_push = notes_claims._push
    pushed: list[tuple[str, str]] = []
    won: list[notes_claims.Claim] = []

    def racing_push(repo, remote_name, commit, branch):
        if repo.path.name == "notes-b" and not won:
            won.append(notes_claims.claim(tmp_root, "feature", "from-a"))
        pushed.append((repo.path.name, commit))
        return real_push(repo, remote_name, commit, branch)

    monkeypatch.setattr(notes_claims, "_push", racing_push)
    b = notes_claims.claim(device_b, "feature", "from-b")

    assert won[0].id == 1 and b.id == 2
    assert [(c["id"], c["slug"]) for c in remote_claims(remote)] == [(1, "from-a"), (2, "from-b")]
    losing = [commit for who, commit in pushed if who == "notes-b"][0]
    assert subprocess.run(["git", "cat-file", "-e", f"{losing}^{{commit}}"], cwd=remote).returncode != 0
    assert git(tmp_path / "notes-b", "branch", "--all", "--contains", losing) == ""
    assert len([who for who, _ in pushed if who == "notes-b"]) == 2


def test_a_lost_acknowledgement_replays_without_a_duplicate(
    tmp_root: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
):
    real_push = notes_claims._push
    calls: list[str] = []

    def lands_then_times_out(repo, remote_name, commit, branch):
        calls.append(commit)
        real_push(repo, remote_name, commit, branch)
        return None

    monkeypatch.setattr(notes_claims, "_push", lands_then_times_out)
    claim = notes_claims.claim(tmp_root, "feature", "x", op="op-1")
    assert (claim.id, claim.op) == (1, "op-1")
    assert len(calls) == 1
    assert [(c["op"], c["id"]) for c in remote_claims(remote)] == [("op-1", 1)]


def test_an_unanswered_push_that_never_landed_is_tried_again(
    tmp_root: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
):
    real_push = notes_claims._push
    calls: list[str] = []

    def first_one_lost(repo, remote_name, commit, branch):
        calls.append(commit)
        return None if len(calls) == 1 else real_push(repo, remote_name, commit, branch)

    monkeypatch.setattr(notes_claims, "_push", first_one_lost)
    assert notes_claims.claim(tmp_root, "feature", "x").id == 1
    assert len(calls) == 2
    assert [c["id"] for c in remote_claims(remote)] == [1]


def test_a_feature_another_device_synced_before_claims_existed_is_counted(
    tmp_root: Path, remote: Path, tmp_path: Path
):
    device_b = other_device(tmp_path, remote)
    clone_b = Path((device_b / ".agent" / "notes-root").read_text(encoding="utf-8"))
    (clone_b / "features" / "005-old").mkdir(parents=True)
    (clone_b / "features" / "005-old" / "plan.md").write_text("old\n", encoding="utf-8")
    git(clone_b, "add", "-A")
    git(clone_b, "commit", "-q", "-m", "old feature")
    git(clone_b, "push", "-q", "origin", "HEAD:main")

    assert notes_claims.claim(tmp_root, "feature", "x").id == 6


def test_initiative_ids_are_claimed_as_their_own_sequence(tmp_root: Path, remote: Path):
    assert notes_claims.claim(tmp_root, "feature", "x").id == 1
    assert notes_claims.claim(tmp_root, "initiative", "goal").id == 1
    assert notes_claims.claim(tmp_root, "initiative", "next").id == 2
    assert git(remote, "log", "-1", "--format=%s", "main") == "notes: claim initiative 02 next"


def test_a_busy_sync_lock_refuses(tmp_root: Path, notes_remote: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(notes_claims, "LOCK_WAIT_S", 0.1)
    common = Path(git(notes_remote, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    with notes_sync.repo_lock(common, 1.0), pytest.raises(AgentError, match="lock stayed busy"):
        notes_claims.claim(tmp_root, "feature", "x")


def test_auto_sync_never_commits_a_claims_file_and_publishes_on_top_of_claims(
    tmp_root: Path, notes_remote: Path, remote: Path, run
):
    (tmp_root / "agent.local.yaml").write_text("machine: mac\nnotes_sync: on\n", encoding="utf-8")
    link_notes(tmp_root)
    assert run(["feature", "new", "x", "--title", "X"]).code == 0

    stray = notes_remote / "initiatives" / "01-goal" / "records" / "claims.jsonl"
    stray.parent.mkdir(parents=True)
    stray.write_text('{"forged": true}\n', encoding="utf-8")
    head = git(notes_remote, "rev-parse", "HEAD")
    result = notes_sync.sync(tmp_root)
    assert result.status == "claims", result
    assert "initiatives/01-goal/records/claims.jsonl" in result.detail
    assert git(notes_remote, "rev-parse", "HEAD") == head

    shutil.rmtree(notes_remote / "initiatives")
    result = notes_sync.sync(tmp_root)
    assert result.status == "pushed", result
    assert git(remote, "rev-parse", "main") == git(notes_remote, "rev-parse", "HEAD")
    assert [c["id"] for c in remote_claims(remote)] == [1]
    assert git(remote, "show", "main:features/001-x/log.md").startswith("# Log")
    assert git(remote, "log", "-1", "--format=%s", "main") != "notes: claim feature 001 x"


def test_a_slug_another_device_claimed_is_refused_though_this_clone_has_not_pulled(
    tmp_root: Path, remote: Path, tmp_path: Path
):
    device_b = other_device(tmp_path, remote)
    features.new(tmp_root, "same", "Same")
    before = git(remote, "rev-parse", "main")

    with pytest.raises(AgentError, match="slug 'same' already claimed"):
        features.new(device_b, "same", "Same")

    assert not (device_b / "features").exists()
    assert git(remote, "rev-parse", "main") == before
    assert [(c["id"], c["slug"]) for c in remote_claims(remote)] == [(1, "same")]


def test_a_slug_taken_while_the_claim_retries_is_refused_on_the_retry(
    tmp_root: Path, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    device_b = other_device(tmp_path, remote)
    real_push = notes_claims._push
    pushes: list[str] = []

    def other_device_wins_first(repo, remote_name, commit, branch):
        pushes.append(repo.path.name)
        if repo.path.name == "notes-b" and len(pushes) == 1:
            features.new(tmp_root, "same", "Same")
        return real_push(repo, remote_name, commit, branch)

    monkeypatch.setattr(notes_claims, "_push", other_device_wins_first)
    with pytest.raises(AgentError, match="slug 'same' already claimed"):
        features.new(device_b, "same", "Same")

    assert pushes.count("notes-b") == 1
    assert not (device_b / "features").exists()
    assert [(c["id"], c["slug"]) for c in remote_claims(remote)] == [(1, "same")]


def test_a_slug_in_a_folder_on_the_remote_is_refused(tmp_root: Path, remote: Path, tmp_path: Path):
    device_b = other_device(tmp_path, remote)
    clone_b = Path((device_b / ".agent" / "notes-root").read_text(encoding="utf-8"))
    (clone_b / "initiatives" / "01-goal" / "features" / "005-old").mkdir(parents=True)
    (clone_b / "initiatives" / "01-goal" / "features" / "005-old" / "plan.md").write_text(
        "old\n", encoding="utf-8"
    )
    git(clone_b, "add", "-A")
    git(clone_b, "commit", "-q", "-m", "old feature")
    git(clone_b, "push", "-q", "origin", "HEAD:main")

    with pytest.raises(AgentError, match=r"slug 'old' already used by initiatives/01-goal/features/005-old"):
        notes_claims.claim(tmp_root, "feature", "old")


def test_replaying_an_operation_as_another_request_refuses_and_identical_replay_returns_the_receipt(
    tmp_root: Path, remote: Path, tmp_path: Path
):
    device_b = other_device(tmp_path, remote)
    first = notes_claims.claim(tmp_root, "feature", "x", op="persisted-op")

    with pytest.raises(AgentError, match="operation persisted-op already claimed feature 'x'"):
        notes_claims.claim(device_b, "initiative", "different", op="persisted-op")
    with pytest.raises(AgentError, match="operation persisted-op already claimed feature 'x'"):
        notes_claims.claim(device_b, "feature", "different", op="persisted-op")

    assert notes_claims.claim(device_b, "feature", "x", op="persisted-op") == first
    assert [(c["op"], c["kind"], c["id"]) for c in remote_claims(remote)] == [("persisted-op", "feature", 1)]


@pytest.fixture
def synced(tmp_root: Path, notes_remote: Path, remote: Path) -> Path:
    (tmp_root / "agent.local.yaml").write_text("machine: mac\nnotes_sync: on\n", encoding="utf-8")
    link_notes(tmp_root)
    (notes_remote / "records").mkdir()
    (notes_remote / "records" / "claims.jsonl").write_text("forged\n", encoding="utf-8")
    git(notes_remote, "add", "records/claims.jsonl")
    return notes_remote


def test_a_failed_claims_status_read_refuses_and_pushes_nothing(
    tmp_root: Path, synced: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
):
    before = git(remote, "rev-parse", "main")
    real = notes_sync.Repo.out

    def fail_claims_read(self, *args, cleanup=False):
        return None if args[-1] == notes_claim_guard.CLAIMS else real(self, *args, cleanup=cleanup)

    monkeypatch.setattr(notes_sync.Repo, "out", fail_claims_read)
    result = notes_sync.sync(tmp_root)
    assert result.status == "claims", result
    assert git(remote, "rev-parse", "main") == before
    assert git(synced, "rev-parse", "HEAD") == before


def test_a_claims_file_already_staged_never_reaches_a_sync_commit(
    tmp_root: Path, synced: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
):
    before = git(remote, "rev-parse", "main")
    real = notes_sync.Repo.out

    def claims_read_clean(self, *args, cleanup=False):
        return "" if args[-1] == notes_claim_guard.CLAIMS else real(self, *args, cleanup=cleanup)

    monkeypatch.setattr(notes_sync.Repo, "out", claims_read_clean)
    result = notes_sync.sync(tmp_root)
    assert result.status == "claims", result
    assert "records/claims.jsonl is staged" in result.detail
    assert git(remote, "rev-parse", "main") == before
    assert git(synced, "rev-parse", "HEAD") == before
