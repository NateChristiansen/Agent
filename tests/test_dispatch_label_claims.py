"""Real bare-remote label CAS, cached offline allocation, and refusal before record writes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent_cli import frontmatter, notes_claim_guard, notes_claims, notes_sync
from agent_cli.core import AgentError
from agent_cli.dispatch import label_claims, machine
from harness_root import NOTES_REMOTE
from seeded import writable_retry
from test_notes_claims import git, other_device


@pytest.fixture
def remote(tmp_root: Path, notes_remote: Path) -> Path:
    return tmp_root.parent / NOTES_REMOTE


def _records(remote: Path) -> list[dict]:
    return [json.loads(line) for line in git(remote, "show", f"main:{label_claims.LABELS_PATH}").splitlines()]


def test_first_claim_changes_only_label_ledger_and_caches_receipt(
    tmp_root: Path, notes_remote: Path, remote: Path
):
    head = git(notes_remote, "rev-parse", "HEAD")
    before = git(remote, "rev-parse", "main")
    claim = label_claims.require(tmp_root, "mac")
    after = git(remote, "rev-parse", "main")
    assert git(remote, "rev-list", "--parents", "-n", "1", after).split() == [after, before]
    assert git(remote, "diff-tree", "--no-commit-id", "--name-only", "-r", after) == label_claims.LABELS_PATH
    assert git(notes_remote, "rev-parse", "HEAD") == head
    assert not (notes_remote / label_claims.LABELS_PATH).exists()
    assert claim == label_claims.cached(tmp_root, "mac")
    install = machine.read_install(tmp_root)
    assert install is not None
    assert claim["install"] == install["token"]


def test_cached_owner_allocates_offline_but_new_install_refuses(tmp_root: Path, remote: Path, tmp_path: Path):
    receipt = label_claims.require(tmp_root, "mac")
    second = other_device(tmp_path, remote)
    shutil.rmtree(remote, onexc=writable_retry)
    assert label_claims.require(tmp_root, "mac") == receipt
    with pytest.raises(AgentError, match="remote unreachable"):
        label_claims.require(second, "win")
    assert label_claims.cached(second, "win") is None


@pytest.mark.parametrize("second_label", ["mac", "win"])
def test_two_installations_racing_have_one_label_owner_or_two_distinct_claims(
    tmp_root: Path,
    remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second_label: str,
):
    second = other_device(tmp_path, remote)
    real_push = notes_claims._push
    winner: list[dict] = []

    def race(repo, remote_name, commit, branch):
        if repo.path.name == "notes-b" and not winner:
            winner.append(label_claims.require(tmp_root, "mac"))
        return real_push(repo, remote_name, commit, branch)

    monkeypatch.setattr(notes_claims, "_push", race)
    if second_label == "mac":
        with pytest.raises(AgentError, match="belongs to another installation"):
            label_claims.require(second, second_label)
        assert label_claims.cached(second, second_label) is None
        assert [r["label"] for r in _records(remote)] == ["mac"]
    else:
        assert label_claims.require(second, second_label)["install"] != winner[0]["install"]
        assert [r["label"] for r in _records(remote)] == ["mac", "win"]


def test_unanswered_success_is_settled_without_duplicate_claim(
    tmp_root: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
):
    real_push = notes_claims._push
    calls = 0

    def push_without_answer(repo, remote_name, commit, branch):
        nonlocal calls
        calls += 1
        result = real_push(repo, remote_name, commit, branch)
        assert result is not None and result.returncode == 0
        return None

    monkeypatch.setattr(notes_claims, "_push", push_without_answer)
    claim = label_claims.require(tmp_root, "mac")
    assert calls == 1
    assert len(_records(remote)) == 1
    assert _records(remote)[0]["op"] == claim["op"]


def test_a_notes_repo_with_no_remote_reserves_locally_then_on_the_remote_once_added(
    tmp_root: Path, notes_remote: Path, remote: Path
):
    git(notes_remote, "remote", "remove", "origin")
    head = git(notes_remote, "rev-parse", "HEAD")
    receipt = label_claims.require(tmp_root, "mac")
    assert receipt["local"] is True and receipt == label_claims.cached(tmp_root, "mac")
    assert label_claims.require(tmp_root, "mac") == receipt
    assert label_claims.inspect(tmp_root, "mac")["source"] == "local"
    assert git(notes_remote, "rev-parse", "HEAD") == head

    git(notes_remote, "remote", "add", "origin", str(remote))
    git(notes_remote, "config", "branch.main.remote", "origin")
    git(notes_remote, "config", "branch.main.merge", "refs/heads/main")
    claimed = label_claims.require(tmp_root, "mac")
    assert "local" not in claimed
    assert [(r["label"], r["install"]) for r in _records(remote)] == [("mac", receipt["install"])]


def test_an_unreachable_upstream_still_refuses_a_first_label(tmp_root: Path, remote: Path):
    shutil.rmtree(remote, onexc=writable_retry)
    with pytest.raises(AgentError, match="remote unreachable"):
        label_claims.require(tmp_root, "mac")
    assert label_claims.cached(tmp_root, "mac") is None


def test_legacy_id_ledger_remains_compatible(tmp_root: Path, remote: Path):
    numeric = notes_claims.claim(tmp_root, "feature", "work")
    label_claims.require(tmp_root, "mac")
    assert notes_claims.claim(tmp_root, "initiative", "goal").id == 1
    text = git(remote, "show", f"main:{notes_claims.CLAIMS_PATH}")
    assert notes_claims._records(text, "main")[0]["op"] == numeric.op
    assert {r["kind"] for r in notes_claims._records(text, "main")} == {"feature", "initiative"}


@pytest.mark.parametrize("remote_only", [False, True])
def test_foreign_legacy_handoff_blocks_first_claim_even_if_remote_only(
    tmp_root: Path,
    notes_remote: Path,
    remote: Path,
    remote_only: bool,
):
    from agent_cli import features

    feature = features.new(tmp_root, "work", "Work")
    meta = {"id": "001-mac-01", "machine": "mac", "install": "anotherinstall00", "tags": ["handoff"]}
    path = feature.dir / "handoffs" / "mac-01-alpha.md"
    path.parent.mkdir()
    path.write_text(frontmatter.join(meta, "old\n"), encoding="utf-8")
    if remote_only:
        remote_file = notes_remote / "features" / feature.dir.name / "handoffs" / path.name
        remote_file.parent.mkdir(parents=True)
        remote_file.write_bytes(path.read_bytes())
        path.unlink()
        git(notes_remote, "add", "-A")
        git(notes_remote, "commit", "-q", "-m", "old handoff")
        git(notes_remote, "pull", "-q", "--rebase")
        git(notes_remote, "push", "-q")
    with pytest.raises(AgentError, match="legacy handoff"):
        label_claims.require(tmp_root, "mac")
    assert label_claims.cached(tmp_root, "mac") is None


def test_preview_never_mints_claim_or_changes_remote(tmp_root: Path, remote: Path):
    before = git(remote, "rev-parse", "main")
    assert label_claims.inspect(tmp_root, "mac")["verified"] is False
    assert machine.provenance(tmp_root, "mac", dry_run=True)["label_claim"]["verified"] is False
    assert not (tmp_root / machine.INSTALL_FILE).exists()
    assert git(remote, "rev-parse", "main") == before


def test_auto_sync_refuses_dirty_and_staged_label_ledger(tmp_root: Path, notes_remote: Path):
    (tmp_root / "agent.local.yaml").write_text("machine: mac\nnotes_sync: on\n", encoding="utf-8")
    ledger = notes_remote / label_claims.LABELS_PATH
    ledger.parent.mkdir()
    ledger.write_text("forged\n", encoding="utf-8")
    result = notes_sync.sync(tmp_root)
    assert result.status == "claims" and "labels.jsonl" in result.detail
    opened = notes_sync.open_repo(tmp_root, 5)
    assert not isinstance(opened, notes_sync.Result)
    repo, _gitdir, _common = opened
    assert notes_claim_guard.refusal(repo, "records/labels.jsonl") is not None


def test_corrupt_or_conflicting_ledger_refuses():
    base = {"kind": "label", "label": "mac", "install": "1111111111111111", "op": "one"}
    with pytest.raises(AgentError, match="conflicting owners"):
        label_claims.records(
            json.dumps(base) + "\n" + json.dumps({**base, "install": "2222222222222222", "op": "two"}), "head"
        )
    with pytest.raises(AgentError, match="not a label claim"):
        label_claims.records('{"kind":"feature","id":1}', "head")


def test_a_loaded_machine_still_claims_its_label(tmp_root: Path, remote: Path, monkeypatch):
    """About 15 tests failed at setup with "branch main has no upstream" while the
    upstream was configured: opening the repo and reading it shared a hook's 5 s budget, and a
    call that could not run read as a missing setting. Each git call here spends a second."""
    real = notes_sync.Repo.git

    def slow(self, *args, **kw):
        self.end -= 1.0
        return real(self, *args, **kw)

    monkeypatch.setattr(notes_sync.Repo, "git", slow)
    assert label_claims.require(tmp_root, "mac")["label"] == "mac"


def test_a_spent_budget_is_not_reported_as_a_missing_upstream(notes_remote: Path):
    assert notes_sync.upstream_of(notes_sync.Repo(notes_remote, 30.0)) == ("origin", "refs/heads/main")
    why = notes_sync.upstream_of(notes_sync.Repo(notes_remote, 0.0))
    assert isinstance(why, str) and "did not answer" in why and "no upstream" not in why
