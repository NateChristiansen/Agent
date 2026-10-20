"""Inventory parity with the earlier resolver and measured git cost on disposable repositories."""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest

from agent_cli import land_journal, land_observe, leases, repos, sessions, tidy
from agent_cli.dispatch import state
from agent_cli.doctor.checks_repos import closed_leftovers
from agent_cli.records.narrow import as_dict, dig
from conftest import git
from test_tidy import _repo_template as _repo_template  # noqa: F401 (fixture)
from test_tidy import close, squash, tree
from test_tidy import repo as repo  # noqa: F401 (fixture)


@pytest.mark.parametrize("alias", [False, True])
def test_open_feature_terminal_tree_is_listed_and_removable(repo: Path, monkeypatch, alias: bool):
    """Audit 10 plan.md:63 (§A4) retires 'an open feature keeps every finished checkout'."""
    wt = tree(repo)
    recorded = wt
    if alias:
        from agent_cli import links

        recorded = wt.parent / "alias"
        links.make(recorded, wt, is_dir=True)
    records = [{"feature": 1, "state": "done", "worktree": str(recorded), "branch": "agent/001-cleanup"}]
    monkeypatch.setattr(state, "load_all", lambda root: records)
    dry = tidy.run(repo)
    assert dry["trees"][0]["holds"] == []
    assert dry["removed"] == [] and wt.exists()
    assert closed_leftovers(repo)[0]["check"] == "finished worktree"
    assert tidy.run(repo, apply=True)["removed"] == [str(wt)]
    assert not wt.exists()


@pytest.mark.parametrize("hold", ["dirty", "unlanded", "lease", "session", "cwd", "uncollected", "verifier"])
def test_open_feature_terminal_tree_keeps_existing_holds(repo: Path, monkeypatch, hold: str):
    wt = tree(repo)
    record: dict[str, Any] = {
        "feature": 1,
        "state": "done",
        "worktree": str(wt),
        "branch": "agent/001-cleanup",
    }
    monkeypatch.setattr(state, "load_all", lambda root: [record])
    if hold == "dirty":
        (wt / "tracked.txt").write_text("real edit\n")
    elif hold == "unlanded":
        commit_file(wt, "new.txt", "unlanded\n")
    elif hold == "lease":
        leases.take_marked(
            repo, "worktree:harness/tree", session_id="holder", pid=os.getpid(), worktree_key=str(wt)
        )
    elif hold == "session":
        sessions.set_record(repo, "holder", cwd=str(wt), pid=os.getpid())
    elif hold == "cwd":
        monkeypatch.setattr(tidy, "process_cwds", lambda: [(123, str(wt))])
    elif hold == "verifier":
        record["worker_cleanup_pending"] = True
    else:
        (wt / "draft.md").write_text("uncollected work\n")
    result = tidy.run(repo, apply=True)
    assert result["trees"][0]["holds"]
    assert result["removed"] == [] and wt.exists()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_open_feature_active_dispatch_is_not_a_terminal_candidate(repo: Path, monkeypatch, status: str):
    wt = tree(repo)
    monkeypatch.setattr(
        state,
        "load_all",
        lambda root: [{"feature": 1, "state": status, "worktree": str(wt), "branch": "agent/001-cleanup"}],
    )
    assert tidy.run(repo, apply=True)["trees"] == []
    assert wt.exists()


@pytest.mark.parametrize("status", ["question", "blocked"])
def test_owner_waiting_tree_and_branch_are_held(repo: Path, monkeypatch, status: str):
    wt = tree(repo)
    registry = repos.Registry(1, [repos.Repo("Harness", "harness", str(repo), "unused")])
    monkeypatch.setattr(repos, "load_registry", lambda root: registry)
    record = {
        "feature": 1,
        "repo": "Harness",
        "state": status,
        "worktree": str(wt),
        "branch": "agent/001-cleanup",
    }
    monkeypatch.setattr(state, "load_all", lambda root: [record])
    result = tidy.run(repo, apply=True)
    assert result["trees"][0]["holds"] == ["waiting on the owner"]
    assert result["removed"] == [] and wt.exists()
    git(["worktree", "remove", str(wt)], repo)
    result = tidy.run(repo, apply=True)
    assert result["branches"][0]["holds"] == ["waiting on the owner"]
    assert result["branches_deleted"] == []


def test_only_the_latest_dispatch_owner_wait_holds(repo: Path, monkeypatch):
    wt = tree(repo)
    records: list[dict[str, Any]] = [
        {"state": "done", "created": "2026-10-19T02:00:00Z"},
        {"state": "question", "created": "2026-10-19T01:00:00Z"},
    ]
    for record in records:
        record.update(feature=1, worktree=str(wt), branch="agent/001-cleanup")
    monkeypatch.setattr(state, "load_all", lambda root: records)
    assert tidy.run(repo, apply=True)["removed"] == [str(wt)]


def test_doctor_summarizes_resolved_branches_without_checkouts(repo: Path):
    tree(repo)
    for branch in ("agent/001-first", "agent/001-second"):
        git(["branch", branch], repo)
    close(repo)
    checks = closed_leftovers(repo)
    assert len(checks) == 2
    summary = next(c for c in checks if c["check"] == "resolved branches")
    assert summary["level"] == "warn"
    assert "2 resolved branches without checkouts" in summary["detail"]


@pytest.mark.parametrize("branch", ["agent/001-landed", "agent/landed"])
@pytest.mark.parametrize("known_feature", [False, True])
def test_landed_branch_without_checkout_is_removable_in_dry_run(
    repo: Path, monkeypatch, branch: str, known_feature: bool
):
    if not known_feature:
        monkeypatch.setattr(tidy.features, "load_all", lambda root: [])
    git(["branch", branch], repo)
    dry = tidy.run(repo)
    assert dry["branches"][0]["resolved"] is True
    assert f"{branch}: resolved; safe to remove" in tidy.format_result(dry)
    assert branch in git(["branch", "--list"], repo)
    assert tidy.run(repo, apply=True)["branches_deleted"] == [branch]


@pytest.mark.parametrize("draft", [False, True])
def test_open_product_tree_holds_ignored_draft_but_exempts_caches(repo: Path, monkeypatch, draft: bool):
    product = repo.parent / "product"
    git(["clone", "--no-local", str(repo), str(product)], repo)
    registry = repos.Registry(1, [repos.Repo("Product", "product", str(product), "unused")])
    monkeypatch.setattr(repos, "load_registry", lambda root: registry)
    wt = tree(product, "001-product")
    monkeypatch.setattr(
        state,
        "load_all",
        lambda root: [
            {
                "feature": 1,
                "repo": "Product",
                "state": "done",
                "worktree": str(wt),
                "branch": "agent/001-product",
            }
        ],
    )
    ignored = [".agent/scratch/draft.md"] if draft else []
    ignored += [
        ".venv/lib/cache.py",
        "node_modules/pkg/index.js",
        ".pytest-tmp/out.txt",
        "pytest-temproot/out.txt",
        ".ruff_cache/version/hash",
        ".mypy_cache/version/hash",
        ".pyright/cache/index",
    ]
    (product / ".git/info/exclude").write_text(
        ".venv/\nnode_modules/\n.pytest-tmp/\npytest-temproot/\n.ruff_cache/\n.mypy_cache/\n.pyright/\n"
    )
    for name in ignored:
        path = wt / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserve draft; caches disposable\n")
    assert git(["status", "--porcelain"], wt) == ""
    result = tidy.run(repo, apply=True)
    if draft:
        assert result["removed"] == [] and wt.exists()
        assert ".agent/scratch/draft.md" in result["trees"][0]["holds"][0]
    else:
        assert result["removed"] == [str(wt)] and not wt.exists()


def legacy_journal_landed(main, journal, branch, tip, tree):
    """Frozen pre-cache decision, including the literal journal target `main`."""
    if journal.get("state") != "done":
        return False
    candidate = str(journal.get("cand") or "")
    if not candidate or repos.git_or_none(["merge-base", "--is-ancestor", candidate, "main"], main) is None:
        return False
    members = journal.get("members") or [{"branch": journal.get("branch"), "tip": journal.get("tip")}]
    for raw in members:
        member = as_dict(raw)
        if str(member.get("branch") or "").removeprefix("refs/heads/") == branch:
            if member.get("tip") == tip:
                return True
            if tree and tree == repos.git_or_none(["rev-parse", f"{candidate}^{{tree}}"], main):
                return True
    return False


def legacy_resolved(root, main, branch, default="main", records=None, **_kwargs):
    """Frozen earlier resolver; inventory and holds only change how it receives a context."""
    tip = repos.git_or_none(["rev-parse", "--verify", f"{branch}^{{commit}}"], main)
    if not tip:
        return False
    tip = tip.strip()
    if repos.git_or_none(["merge-base", "--is-ancestor", tip, f"refs/heads/{default}"], main) is not None:
        return True
    tree = repos.git_or_none(["rev-parse", f"{tip}^{{tree}}"], main)
    for d in state.load_all(root) if records is None else records:
        if d.get("branch") != branch:
            continue
        integration = as_dict(d.get("integration"))
        if integration.get("state") == "landed" and integration.get("tip") == tip:
            return True
        if state.is_actioned(d) and dig(d, "actioned", "action") == "landed":
            ident = dig(d, "actioned", "note")
            for journal in land_journal.every(main):
                if journal.get("id") == ident and legacy_journal_landed(main, journal, branch, tip, tree):
                    return True
    if any(legacy_journal_landed(main, j, branch, tip, tree) for j in land_journal.every(main)):
        return True
    base = repos.git_or_none(["merge-base", f"refs/heads/{default}", tip], main)
    if not base:
        return False
    observed = land_observe.Git(main)
    paths = land_observe.changed_paths(observed, base.strip(), tip)
    branch_entries = land_observe.tree_entries(observed, tip)
    main_entries = land_observe.tree_entries(observed, f"refs/heads/{default}")
    return all(branch_entries.get(path) == main_entries.get(path) for path in paths)


def commit_file(checkout: Path, name: str, content: str) -> str:
    (checkout / name).write_text(content)
    git(["add", name], checkout)
    git(["commit", "-m", name], checkout)
    return git(["rev-parse", "HEAD"], checkout).strip()


def measure_inventory(root: Path, monkeypatch: pytest.MonkeyPatch, *, legacy: bool = False):
    """Count each wrapper once: git_or_none delegates to run_git, Git.run is separate."""
    calls: Counter[tuple[str, ...]] = Counter()
    run_git, observed_run = repos.run_git, land_observe.Git.run

    def counted_git(args, cwd):
        calls[tuple(args)] += 1
        return run_git(args, cwd)

    def counted_observed(self, *args, **kwargs):
        calls[args] += 1
        return observed_run(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(repos, "run_git", counted_git)
        patch.setattr(land_observe.Git, "run", counted_observed)
        if legacy:
            patch.setattr(tidy, "resolved", legacy_resolved)
            patch.setattr(tidy.ResolutionContext, "refresh", lambda self, main, default: None)
        start = perf_counter()
        listing = tidy.inventory(root)
        elapsed = perf_counter() - start
    return listing, calls, elapsed


def test_inventory_matches_legacy_landing_and_hold_decisions(repo: Path, monkeypatch):
    landed = tree(repo, "001-landed")
    commit_file(landed, "landed.txt", "ancestry\n")
    git(["merge", "--ff-only", "agent/001-landed"], repo)
    squashed = tree(repo, "001-squashed")
    commit_file(squashed, "squashed.txt", "squash\n")
    git(["merge", "--squash", "agent/001-squashed"], repo)
    git(["commit", "-m", "squash"], repo)
    records = []
    for name in ("journal", "tree-match", "legacy", "record"):
        branch = f"agent/001-{name}"
        wt = tree(repo, f"001-{name}")
        tip = commit_file(wt, "tracked.txt", name + "\n")
        git(["merge", "--squash", branch], repo)
        git(["commit", "-m", name], repo)
        candidate = git(["rev-parse", "HEAD"], repo).strip()
        if name != "record":
            journal: dict[str, Any] = {"id": name, "state": "done", "cand": candidate}
            if name == "journal":
                journal["members"] = [{"branch": f"refs/heads/{branch}", "tip": tip}]
            else:
                journal.update(branch=branch, tip=tip)
            land_journal.write(repo, journal)
        if name == "tree-match":
            git(["commit", "--amend", "-m", "rewritten tip"], wt)
            assert git(["rev-parse", "HEAD"], wt).strip() != tip
        elif name == "legacy":
            records.append(
                {
                    "branch": branch,
                    "actioned_at": "2026-10-15",
                    "actioned": {"action": "landed", "note": name},
                }
            )
        elif name == "record":
            records.append({"branch": branch, "integration": {"state": "landed", "tip": tip}})
    commit_file(repo, "tracked.txt", "later main edit\n")
    unresolved = tree(repo, "001-unresolved")
    commit_file(unresolved, "tracked.txt", "unlanded\n")
    (unresolved / "dirty.txt").write_text("real work\n")
    # Neither an unfinished journal nor an unreachable candidate proves a landing.
    for name, candidate, status in (
        ("unfinished", "main", "prepared"),
        ("unreachable", "agent/001-unresolved", "done"),
    ):
        land_journal.write(
            repo,
            {
                "id": name,
                "state": status,
                "cand": candidate,
                "branch": "agent/001-unresolved",
                "tip": git(["rev-parse", "agent/001-unresolved"], repo).strip(),
            },
        )
    close(repo)
    monkeypatch.setattr(state, "load_all", lambda root: records)
    before, _, _ = measure_inventory(repo, monkeypatch, legacy=True)
    after, _, _ = measure_inventory(repo, monkeypatch)
    assert after == before
    assert {row["branch"]: row["resolved"] for row in after["branches"]} == {
        f"agent/001-{name}": name != "unresolved"
        for name in ("landed", "squashed", "journal", "tree-match", "legacy", "record", "unresolved")
    }
    for row in after["trees"]:
        assert row["holds"] == (
            ["uncommitted changes: dirty.txt", tidy.UNRESOLVED_BRANCH]
            if row["branch"].endswith("unresolved")
            else []
        )


@pytest.mark.parametrize("n,m", [(1, 1), (4, 3)])
def test_inventory_git_cost_is_linear_in_branches_and_journals(repo: Path, monkeypatch, n: int, m: int):
    candidates = []
    for i in range(m):
        git(["commit", "--allow-empty", "-m", f"candidate {i}"], repo)
        candidates.append(git(["rev-parse", "HEAD"], repo).strip())
        land_journal.write(
            repo,
            {
                "id": f"candidate-{i}",
                "state": "done",
                "cand": candidates[-1],
                "members": [{"branch": f"agent/001-cost-{j}", "tip": "old"} for j in range(n)],
            },
        )
    for i in range(n):
        wt = tree(repo, f"001-cost-{i}")
        commit_file(wt, "tracked.txt", f"unresolved {i}\n")
    close(repo)
    records_read, journals_read = [], []
    load_all, every = state.load_all, land_journal.every

    def load_records(root):
        records_read.append(root)
        return load_all(root)

    def load_journals(main):
        journals_read.append(main)
        return every(main)

    before, old_calls, old_time = measure_inventory(repo, monkeypatch, legacy=True)
    monkeypatch.setattr(state, "load_all", load_records)
    monkeypatch.setattr(land_journal, "every", load_journals)
    after, calls, elapsed = measure_inventory(repo, monkeypatch)
    assert after == before
    assert records_read == [repo]
    assert journals_read == [repo]
    assert calls[("ls-tree", "-r", "-z", "refs/heads/main")] == 1
    # The hardened git reads its filter config once per checkout, not once per branch.
    assert calls[("config", "-z", "--list")] == 1
    for candidate in candidates:
        assert calls[("merge-base", "--is-ancestor", candidate, "main")] == 1
        assert calls[("rev-parse", f"{candidate}^{{tree}}")] == 1
    # Each tree and branch gets a tip read and fallback plumbing; candidates are shared.
    # The old resolver exceeds this bound even on these small inputs.
    total, old_total = calls.total(), old_calls.total()
    if sys.platform == "win32":
        # Windows asks git for each main checkout (git_discovery has no os.getuid there) and for
        # the scope that sets core.autocrlf; those are bounded on their own.
        common, scope = (
            ("rev-parse", "--git-common-dir"),
            ("config", "--show-scope", "--get", "core.autocrlf"),
        )
        assert calls[common] <= n + 3 and calls[scope] == 1
        total -= calls[common] + calls[scope]
        old_total -= old_calls[common] + old_calls[scope]
    assert total <= 16 * n + 2 * m + 8
    assert old_total > 16 * n + 2 * m + 8
    print(
        f"N={n} M={m}: before={old_time:.3f}s/{old_calls.total()} git; after={elapsed:.3f}s/{calls.total()} git"
    )


def test_apply_rechecks_branch_tips_with_the_shared_context(repo: Path, monkeypatch):
    wt = tree(repo)
    close(repo)
    inventory = tidy.inventory

    def inspect(*args, **kwargs):
        listing = inventory(*args, **kwargs)
        assert listing["trees"][0]["holds"] == []
        commit_file(wt, "tracked.txt", "commit after inventory\n")
        return listing

    monkeypatch.setattr(tidy, "inventory", inspect)
    result = tidy.run(repo, apply=True)
    assert result["removed"] == []
    assert result["branches_deleted"] == []
    assert result["trees"][0]["holds"] == [tidy.UNRESOLVED_BRANCH]


@pytest.mark.parametrize("landing", ["ancestry", "squash"])
def test_apply_rechecks_main_after_inventory(repo: Path, monkeypatch, landing: str):
    base = git(["rev-parse", "main"], repo).strip()
    wt = tree(repo)
    commit_file(wt, "tracked.txt", "branch\n")
    git(["merge", "--ff-only" if landing == "ancestry" else "--squash", "agent/001-cleanup"], repo)
    if landing == "squash":
        git(["commit", "-m", "squash"], repo)
    close(repo)
    inventory = tidy.inventory

    def inspect(*args, **kwargs):
        listing = inventory(*args, **kwargs)
        assert listing["trees"][0]["holds"] == []
        git(["reset", "--hard", base], repo)
        return listing

    monkeypatch.setattr(tidy, "inventory", inspect)
    result = tidy.run(repo, apply=True)
    assert result["removed"] == []
    assert result["branches_deleted"] == []
    assert result["trees"][0]["holds"] == [tidy.UNRESOLVED_BRANCH]


def test_apply_shares_records_and_journals_through_removal_and_deletion(repo: Path, monkeypatch):
    wt = tree(repo)
    squash(repo, wt)
    close(repo)
    calls = Counter()
    load_all, every = state.load_all, land_journal.every

    def records(root):
        calls["records"] += 1
        return load_all(root)

    def journals(main):
        calls[("journals", main)] += 1
        return every(main)

    monkeypatch.setattr(state, "load_all", records)
    monkeypatch.setattr(land_journal, "every", journals)
    result = tidy.run(repo, apply=True)
    assert result["removed"] == [str(wt)]
    assert result["branches_deleted"] == ["agent/001-cleanup"]
    assert calls == Counter({"records": 1, ("journals", repo): 1})


def test_inventory_keeps_each_checkouts_journals_and_default_tree_separate(repo: Path, monkeypatch):
    product = repo.parent / "product"
    product.mkdir()
    git(["init", "-b", "trunk"], product)
    git(["config", "user.name", "Fixture"], product)
    git(["config", "user.email", "fixture@example.invalid"], product)
    commit_file(product, "tracked.txt", "base\n")
    # Keep `main` behind trunk: journals have always required ancestry to literal main.
    git(["branch", "main"], product)
    wt = tree(product, "001-product")
    commit_file(wt, "tracked.txt", "product\n")
    git(["merge", "--squash", "agent/001-product"], product)
    git(["commit", "-m", "squash"], product)
    candidate = git(["rev-parse", "trunk"], product).strip()
    land_journal.write(
        product,
        {
            "id": "product",
            "state": "done",
            "cand": candidate,
            "branch": "agent/001-product",
            "tip": git(["rev-parse", "agent/001-product"], product).strip(),
        },
    )
    registry = repos.Registry(
        1, [repos.Repo("Product", "product", str(product), "unused", default_branch="trunk")]
    )
    monkeypatch.setattr(repos, "load_registry", lambda root: registry)
    harness_wt = tree(repo)
    commit_file(harness_wt, "tracked.txt", "unresolved harness\n")
    close(repo)
    before, _, _ = measure_inventory(repo, monkeypatch, legacy=True)
    after, _, _ = measure_inventory(repo, monkeypatch)
    assert after == before
    assert {(row["main"], row["resolved"]) for row in after["branches"]} == {
        (str(repo), False),
        (str(product), True),
    }
    # Main's later edit makes the fallback fail; a trunk-only journal must not override it.
    commit_file(product, "tracked.txt", "later product\n")
    before, _, _ = measure_inventory(repo, monkeypatch, legacy=True)
    after, _, _ = measure_inventory(repo, monkeypatch)
    assert after == before
    assert not any(row["resolved"] for row in after["branches"])


def test_resolved_positional_records_do_not_load_global_records(repo: Path, monkeypatch):
    wt = tree(repo)
    tip = commit_file(wt, "tracked.txt", "branch\n")

    def unexpected_read(root):
        pytest.fail("explicit records must not load global dispatch records")

    monkeypatch.setattr(state, "load_all", unexpected_read)
    assert tidy.resolved(
        repo,
        repo,
        "agent/001-cleanup",
        "main",
        [{"branch": "agent/001-cleanup", "integration": {"state": "landed", "tip": tip}}],
    )
    assert not tidy.resolved(repo, repo, "agent/001-cleanup", "main", [])


def test_dispatch_owned_product_refs_only_and_all_cleanup_holds(repo: Path, monkeypatch):
    product = repo.parent / "product"
    product.mkdir()
    git(["init", "-b", "trunk"], product)
    git(["config", "user.name", "Fixture"], product)
    git(["config", "user.email", "fixture@example.invalid"], product)
    commit_file(product, "source.txt", "base\n")
    registry = repos.Registry(
        1, [repos.Repo("Product", "product", str(product), "unused", default_branch="trunk")]
    )
    monkeypatch.setattr(repos, "load_registry", lambda root: registry)
    records = []
    for branch in ("feat/landed", "review/landed", "feat/unresolved", "feat/dirty", "review/live"):
        git(["branch", branch], product)
        wt = product.parent / branch.replace("/", "-")
        records.append({"repo": "Product", "branch": branch, "feature": 1, "worktree": str(wt)})
        if branch not in ("feat/landed", "review/landed"):
            git(["worktree", "add", str(wt), branch], product)
            if branch == "feat/unresolved":
                commit_file(wt, "source.txt", "unlanded work\n")
            elif branch == "feat/dirty":
                (wt / "source.txt").write_text("uncommitted work\n")
    for branch in ("feat/unrelated", "review/unrelated", "feat/other-repo", "feat/open-feature"):
        git(["branch", branch], product)
    records += [
        {"repo": "Elsewhere", "branch": "feat/other-repo", "feature": 1},
        {"repo": "Product", "branch": "feat/open-feature", "feature": 2},
    ]
    # The same branch name in the harness is not owned by the product's dispatch.
    git(["branch", "feat/landed"], repo)
    monkeypatch.setattr(state, "load_all", lambda root: records)
    live = product.parent / "review-live"
    monkeypatch.setattr(tidy, "process_cwds", lambda: [(123, str(live))])
    close(repo)
    dry = tidy.run(repo)
    assert {r["branch"] for r in dry["branches"]} == {
        "feat/landed",
        "review/landed",
        "feat/unresolved",
        "feat/dirty",
        "review/live",
    }
    assert {r["main"] for r in dry["branches"]} == {str(product)}
    holds = {r["branch"]: r["holds"] for r in dry["trees"]}
    assert holds["feat/unresolved"] == [tidy.UNRESOLVED_BRANCH]
    assert holds["feat/dirty"] == ["uncommitted changes: source.txt"]
    assert holds["review/live"] == ["process cwd 123"]
    result = tidy.run(repo, apply=True)
    assert set(result["branches_deleted"]) == {"feat/landed", "review/landed"}
    assert result["removed"] == []
    remaining = git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], product)
    for branch in (
        "feat/unrelated",
        "review/unrelated",
        "feat/other-repo",
        "feat/open-feature",
        "feat/unresolved",
    ):
        assert branch in remaining.splitlines()


def test_trailer_cache_rechecks_default_and_only_reads_reachable_trailers(repo: Path):
    wt = tree(repo)
    tip = commit_file(wt, "tracked.txt", "worker\n")
    context = tidy.ResolutionContext(repo, [])
    context.refresh(repo, "main")
    assert not tidy.resolved(repo, repo, "agent/001-cleanup", context=context)
    before = git(["rev-parse", "main"], repo).strip()
    git(["commit", "--allow-empty", "-m", f"landing\n\nLanded-tip: worker {tip}"], repo)
    context.refresh(repo, "main")
    assert tidy.resolved(repo, repo, "agent/001-cleanup", context=context)
    git(["reset", "--hard", before], repo)
    context.refresh(repo, "main")
    assert not tidy.resolved(repo, repo, "agent/001-cleanup", context=context)


def test_unrecorded_copy_hint_is_shown_but_never_deleted(repo: Path):
    wt = tree(repo)
    commit_file(wt, "tracked.txt", "worker\n")
    git(["merge", "--squash", "agent/001-cleanup"], repo)
    git(["commit", "-m", "copy without origins"], repo)
    commit_file(repo, "tracked.txt", "later main\n")
    close(repo)
    result = tidy.run(repo, apply=True)
    assert result["removed"] == [] and result["branches_deleted"] == []
    assert result["branches"][0]["hint"] == tidy.COPY_HINT
    assert tidy.COPY_HINT in tidy.format_result(result)
