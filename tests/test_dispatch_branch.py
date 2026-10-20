"""A dispatch's branch name follows the repo's own convention.

The name comes from `dispatch.branch_template` in agent.yaml, defaulting to `feature/<slug>`,
so a worker's branch is shaped like the other branches in the repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_cli import dispatch, features, repos
from conftest import git
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401 — droot is a pytest fixture

pytestmark = pytest.mark.usefixtures("notes_remote")


def _template(root: Path, template: str | None, short: str = "alpha") -> None:
    reg = root / "agent.yaml"
    data = yaml.safe_load(reg.read_text(encoding="utf-8"))
    repo = next(r for r in data["repos"] if r["short"] == short)
    block = repo.setdefault("dispatch", {})
    if template is None:
        block.pop("branch_template", None)
    else:
        block["branch_template"] = template
    reg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _feature(root: Path, run) -> features.Feature:
    run(["feature", "new", "alpha-work", "--title", "Alpha work", "--ticket", "T-42", "--repo", "alpha"])
    return features.resolve(root, "1")


def test_default_template_names_the_branch_after_the_slug_and_machine(tmp_root: Path, run):
    """No template in agent.yaml: `feature/<slug>-<tag>-SS` (ADR-0036), the first dispatch
    included. The legacy untagged shape kept `-02` for a second worktree only. The number and
    the ticket are out of the default; a template can add them."""
    feat = _feature(tmp_root, run)
    repo = repos.load_registry(tmp_root).require("alpha")
    assert repo.branch_template == repos.DEFAULT_BRANCH_TEMPLATE
    assert dispatch.branch_name(feat, 1, repo, tag="mac") == "feature/alpha-work-mac-01"
    assert dispatch.branch_name(feat, 2, repo, tag="mac") == "feature/alpha-work-mac-02"
    assert dispatch.branch_name(feat, 1, repo, tag=None) == "feature/alpha-work"
    assert dispatch.branch_name(feat, 2, repo, tag=None) == "feature/alpha-work-02"


def test_repo_template_can_name_number_ticket_and_suffix(tmp_root: Path, run):
    """A repo template can use the ticket id or the feature number instead of the slug."""
    feat = _feature(tmp_root, run)
    _template(tmp_root, "feature/{ticket}{suffix}")
    repo = repos.load_registry(tmp_root).require("alpha")
    assert dispatch.branch_name(feat, 1, repo, tag="mac") == "feature/t-42-mac-01"
    assert dispatch.branch_name(feat, 3, repo, tag="mac") == "feature/t-42-mac-03"
    assert dispatch.branch_name(feat, 1, repo, tag=None) == "feature/t-42"
    assert dispatch.branch_name(feat, 3, repo, tag=None) == "feature/t-42-03"
    _template(tmp_root, "work/{number}-{slug}{suffix}")
    repo = repos.load_registry(tmp_root).require("alpha")
    assert dispatch.branch_name(feat, 1, repo, tag="mac") == "work/001-alpha-work-mac-01"


def test_ticket_is_empty_when_the_feature_has_none(tmp_root: Path, run):
    """`{ticket}` on a feature with no ticket renders empty rather than raising, so a template
    is safe on a feature opened without one."""
    run(["feature", "new", "no-ticket", "--title", "No ticket", "--repo", "alpha"])
    feat = features.resolve(tmp_root, "1")
    _template(tmp_root, "feature/{slug}{ticket}{suffix}")
    repo = repos.load_registry(tmp_root).require("alpha")
    assert dispatch.branch_name(feat, 1, repo, tag="mac") == "feature/no-ticket-mac-01"


def test_no_repo_falls_back_to_the_default(tmp_root: Path, run):
    """`branch_name` without a repo (a caller with no registry) uses the same default, so the
    name never silently changes shape."""
    feat = _feature(tmp_root, run)
    assert dispatch.branch_name(feat, 1, tag="mac") == "feature/alpha-work-mac-01"


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("feature/{repo}", "which a dispatch cannot supply"),
        ("feature/{slug} thing", "must not contain whitespace"),
        ("feature/one-branch", "must name at least one of"),
        ("feature/../{slug}", "not a valid git branch name"),
        ("", "non-empty format string"),
    ],
)
def test_doctor_rejects_a_template_it_cannot_render(tmp_root: Path, template: str, expected: str):
    """A bad template fails validation, not `git branch` after the worktree exists."""
    data = yaml.safe_load((tmp_root / "agent.yaml").read_text(encoding="utf-8"))
    repo = next(r for r in data["repos"] if r["short"] == "alpha")
    repo.setdefault("dispatch", {})["branch_template"] = template
    problems = repos.validate_registry_data(data)
    assert any(expected in p for p in problems), problems


def test_launch_uses_the_repos_template(droot: Path, fake_claude):
    """End to end: the record, the git branch and the work order all carry the repo's shape."""
    _template(droot, "work/{slug}{suffix}")
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    assert d["branch"] == "work/alpha-work-mac-01"
    assert "work/alpha-work-mac-01" in git(["branch", "--list"], droot / "repos/alpha-api")
    head = git(["rev-parse", "--abbrev-ref", "HEAD"], Path(d["worktree"])).strip()
    assert head == "work/alpha-work-mac-01"
