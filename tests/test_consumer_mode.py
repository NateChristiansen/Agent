"""A consumer install (harness.role: consumer, ADR-0043) runs upstream harness code and
contributes nothing back: no commit, land or push in the harness; lessons go to the local queue
like every install's; product repos are untouched."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_cli import harness_role, land, lessons, push_records
from agent_cli.core import AgentError
from agent_cli.hooks.commit_guard import commit_guard
from agent_cli.push_input import Update
from land_helpers import git, harness  # noqa: F401 -- pytest fixture


def _consumer(root: Path) -> None:
    (root / "agent.local.yaml").write_text("harness:\n  role: consumer\n")


def _decide(root: Path, command: str, cwd: Path, codex: bool = False) -> str:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    payload.update({"turn_id": "t1"} if codex else {"session_id": ""})
    out = commit_guard(root, payload)
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else ""


def test_the_role_is_source_unless_declared(harness: Path):
    assert harness_role.role(harness) == "source"
    _consumer(harness)
    assert harness_role.consumer(harness)
    (harness / "agent.local.yaml").write_text("harness:\n  role: maybe\n")
    assert harness_role.consumer(harness)  # a broken declaration stays a consumer


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git merge feature",
        "git rebase main",
        "git push origin main",
        "git pull",
        "git reset --hard HEAD~1",
    ],
)
def test_a_consumer_refuses_harness_changes_on_any_branch(harness: Path, command: str, codex: bool):
    _consumer(harness)
    git(harness, "switch", "-q", "feature")
    assert _decide(harness, command, harness, codex) == "deny", command


def test_a_consumer_may_fast_forward_to_its_upstream(harness: Path):
    _consumer(harness)
    assert _decide(harness, "git pull --ff-only", harness) == ""
    assert _decide(harness, "git merge --ff-only origin/main", harness) == ""
    assert _decide(harness, "git merge --ff-only feature", harness) == "deny"
    assert _decide(harness, "git status", harness) == ""


def test_a_consumer_never_lands_or_pushes(harness: Path):
    _consumer(harness)
    with pytest.raises(AgentError, match="consumer install"):
        land.land(harness, "feature", strategy="ff")
    head = git(harness, "rev-parse", "main")
    assert push_records.problems(harness, [Update(head, "0" * 40, "refs/heads/main", "refs/heads/main")])


@pytest.mark.parametrize("consumer", [False, True], ids=["source", "consumer"])
def test_every_install_queues_lessons_in_the_notes_queue(harness: Path, consumer: bool):
    if consumer:
        _consumer(harness)
    (harness / "harness" / "lessons" / "queue.md").write_text("# queue\n")
    legacy = (harness / "harness" / "lessons" / "queue.md").read_text()
    entry = lessons.add(
        harness, title="a hook misfired", scope="harness", source="", evidence="seen", home=""
    )
    assert entry.title == "a hook misfired"
    assert "a hook misfired" in (harness / "harness" / "owner" / "lessons-queue.md").read_text()
    assert (harness / "harness" / "lessons" / "queue.md").read_text() == legacy


def test_product_repos_are_unaffected(harness: Path, tmp_path: Path):
    _consumer(harness)
    product = tmp_path / "product"
    product.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=product, check=True)
    assert _decide(harness, "git commit --allow-empty -m x", product) == ""
    assert _decide(harness, "git push origin main", product) == ""
