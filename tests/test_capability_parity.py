"""What preflight promises a worker can do matches what the worker can do at run time (C5).

A work order wrote `> /tmp/out.json`: preflight scored it allowed and
the runtime redirect guard refused it. Codex reviewers could not run
`uv` at all, because the sandbox refuses uv's cache under the home directory."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch
from agent_cli import repos as repos_mod
from agent_cli.dispatch import codex_transport
from dispatch_helpers import (
    _set_alpha_dispatch as set_alpha_dispatch,  # pyright: ignore[reportPrivateUsage] -- shared test helper
)

COMMAND = "uv run pyright --outputjson > {target}"


def _rows(root: Path, target: str) -> list[dict[str, Any]]:
    worktree = root / "wt"
    worktree.mkdir(exist_ok=True)
    set_alpha_dispatch(root, allowed_tools=["Bash(uv:*)"])
    repo = repos_mod.load_registry(root).require("alpha")
    d: dict[str, Any] = {
        "id": "999-99",
        "worktree": str(worktree),
        "thread": [{"from": "manager", "text": f"```sh\n{COMMAND.format(target=target)}\n```"}],
    }
    return dispatch.preflight(root, d, repo)


def test_a_redirect_outside_the_worktree_is_denied_at_preflight(tmp_root: Path):
    [row] = _rows(tmp_root, "/tmp/034-peer-net-strict.json")
    assert row["verdict"] == "denied"
    assert "escapes the dispatch worktree" in str(row["rule"])


def test_a_redirect_inside_the_worktree_stays_allowed(tmp_root: Path):
    [row] = _rows(tmp_root, "strict.json")
    assert row["verdict"] == "allowed" and row["rule"] == "Bash(uv:*)"


def test_codex_workers_get_a_uv_cache_inside_their_writable_roots(tmp_path: Path):
    env: dict[str, str] = {}
    roots = [str(tmp_path / "wt"), str(tmp_path / "repo" / ".git")]
    codex_transport.sandbox_caches(env, roots)
    assert env["UV_CACHE_DIR"] == str(tmp_path / "repo" / ".git" / "agent-uv-cache")


def test_a_repos_own_uv_cache_wins(tmp_path: Path):
    env = {"UV_CACHE_DIR": "/custom/cache"}
    codex_transport.sandbox_caches(env, [str(tmp_path / "wt"), str(tmp_path / ".git")])
    assert env["UV_CACHE_DIR"] == "/custom/cache"


def _git_config(env: dict[str, str], cwd: Path) -> str:
    out = subprocess.run(
        ["git", "config", "--get-all", "safe.directory"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout


def test_codex_workers_trust_their_own_worktree_through_git(tmp_path: Path):
    worktree = tmp_path / "it's wt"
    worktree.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
    env["GIT_CONFIG_PARAMETERS"] = "'core.autocrlf=false'"
    codex_transport.trust_worktree(env, worktree)
    assert not any(k.startswith("GIT_CONFIG_KEY") for k in env)
    assert worktree.resolve().as_posix() in _git_config(env, tmp_path).splitlines()
    assert "core.autocrlf=false" in env["GIT_CONFIG_PARAMETERS"]


def test_a_claude_workers_own_git_call_passes_the_ownership_check(tmp_path: Path):
    """`trust_worktree` covers Codex, whose Windows sandbox runs as
    another account. A Claude worker runs git as the owner who created the worktree, so it needs
    no entry. `GIT_TEST_ASSUME_DIFFERENT_OWNER` shows the check is live in this git, so the pass
    is the owner match and not a check that never ran."""
    from types import SimpleNamespace

    from agent_cli.dispatch import options

    clone, worktree = tmp_path / "clone", tmp_path / "wt"
    for args in (
        ["init", "-q", str(clone)],
        ["-C", str(clone), "commit", "-q", "--allow-empty", "-m", "base"],
    ):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True)
    subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", str(worktree)], check=True)
    repo: Any = SimpleNamespace(env_allow=[], dispatch={})
    env = options.worker_env(tmp_path, {"id": "001-01", "feature": 1}, repo)
    assert "safe.directory" not in env.get("GIT_CONFIG_PARAMETERS", "")

    def status(extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "status", "--short"], cwd=worktree, env={**env, **extra}, capture_output=True, text=True
        )

    assert status({}).returncode == 0, status({}).stderr
    refused = status({"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"})
    assert refused.returncode != 0 and "dubious ownership" in refused.stderr


def _scored(root: Path, command: str) -> dict[str, Any]:
    set_alpha_dispatch(root, allowed_tools=["Bash(uv:*)"])
    repo = repos_mod.load_registry(root).require("alpha")
    d: dict[str, Any] = {
        "id": "999-99",
        "worktree": None,
        "thread": [{"from": "manager", "text": f"```sh\n{command}\n```"}],
    }
    [row] = dispatch.preflight(root, d, repo)
    return row


def test_a_target_less_test_run_is_denied_at_preflight(tmp_root: Path):
    """Work orders listed `uv run pytest tests`, which the worker hook refuses."""
    assert _scored(tmp_root, "uv run pytest tests")["verdict"] == "denied"
    assert _scored(tmp_root, "uv run pytest tests/test_x.py")["verdict"] == "allowed"


def test_a_resume_answer_is_scored_against_the_runtime_guards_too(tmp_root: Path):
    """Codex's phase-1 review: answer preflight still allowed what the worker refuses."""
    worktree = tmp_root / "wt"
    worktree.mkdir(exist_ok=True)
    set_alpha_dispatch(tmp_root, allowed_tools=["Bash(uv:*)"])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    d: dict[str, Any] = {
        "id": "999-99",
        "feature": 999,
        "attempt": 1,
        "worktree": str(worktree),
        "thread": [],
    }
    answer = "```sh\nuv run pyright > /tmp/out.json\nuv run pytest tests\n```"
    rows = dispatch.answer_preflight(tmp_root, d, repo, answer)
    assert [r["verdict"] for r in rows] == ["denied", "denied"]


def test_a_push_spelled_with_git_options_is_denied_at_preflight(tmp_root: Path):
    set_alpha_dispatch(tmp_root, allowed_tools=["Bash(git:*)"])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    d: dict[str, Any] = {
        "id": "999-99",
        "worktree": None,
        "thread": [{"from": "manager", "text": "```sh\ngit -C x push origin main\n```"}],
    }
    [row] = dispatch.preflight(tmp_root, d, repo)
    assert row["verdict"] == "denied" and "never pushes" in str(row["rule"])


def test_a_setup_command_is_scored_against_the_worktree_too(tmp_root: Path):
    """Codex's round-2 review: setup rows skipped the redirect check the worker enforces."""
    worktree = tmp_root / "wt"
    worktree.mkdir(exist_ok=True)
    set_alpha_dispatch(tmp_root, allowed_tools=["Bash(uv:*)"], setup=["uv run pyright > /tmp/out.json"])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    d: dict[str, Any] = {"id": "999-99", "worktree": str(worktree), "thread": []}
    [row] = dispatch.preflight(tmp_root, d, repo)
    assert row["source"] == "setup" and row["verdict"] == "denied"


def test_shared_chaining_guard_matches_preflight_and_both_worker_clients(tmp_root: Path, monkeypatch):
    from agent_cli.dispatch import codex_permissions
    from agent_cli.dispatch.rules import decide as claude_decide

    worktree = tmp_root / "wt"
    worktree.mkdir(exist_ok=True)
    set_alpha_dispatch(
        tmp_root,
        allowed_tools=["Bash(git:*)", "Bash(uv:*)", "Bash(python:*)"],
    )
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    d: dict[str, Any] = {"id": "999-99", "repo": "alpha", "worktree": str(worktree), "thread": []}
    monkeypatch.setattr(codex_permissions.state, "load", lambda root, dispatch_id: d)

    commands = (
        "git status && git log -1",
        "uv run bash -c 'git status; git log -1'",
        'python -c \'import subprocess; subprocess.run(["git", "status"])\'',
        "printf first && printf second",
    )
    for command in commands:
        d["thread"] = [{"from": "manager", "text": f"```sh\n{command}\n```"}]
        [row] = dispatch.preflight(tmp_root, d, repo)
        assert row["verdict"] == "denied" and "chained" in str(row["rule"]).lower()
        assert claude_decide("Bash", {"command": command}, d, repo, tmp_root)[0] == "deny"
        assert (
            codex_permissions._decision(
                tmp_root, "999-99", {"tool_name": "Bash", "tool_input": {"command": command}}
            )[0]
            == "deny"
        )

    set_alpha_dispatch(tmp_root, setup=[commands[0]])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    d["thread"] = []
    [setup_row] = dispatch.preflight(tmp_root, d, repo)
    assert setup_row["source"] == "setup" and setup_row["verdict"] == "denied"
    d.update(feature=999, attempt=1)
    answer = f"```sh\n{commands[1]}\n```"
    [answer_row] = dispatch.answer_preflight(tmp_root, d, repo, answer)
    assert answer_row["source"] == "answer" and answer_row["verdict"] == "denied"


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_portable_sha256_command_is_allowed_by_worker_runtime_policy(
    tmp_root: Path, monkeypatch, client: str
):
    import hashlib

    from agent_cli.dispatch import codex_permissions

    command = (
        'uv run python -c "import hashlib, pathlib, sys; '
        'print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())" spec.md'
    )
    worktree = tmp_root / "wt"
    worktree.mkdir(exist_ok=True)
    fixture = worktree / "spec.md"
    fixture.write_text("portable hash fixture\n", encoding="utf-8")
    set_alpha_dispatch(tmp_root, allowed_tools=["Bash(uv:*)"])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    d: dict[str, Any] = {"id": "999-99", "repo": "alpha", "worktree": str(worktree), "thread": []}
    if client == "codex":
        monkeypatch.setattr(codex_permissions.state, "load", lambda root, dispatch_id: d)
        verdict, _reason = codex_permissions._decision(
            tmp_root, "999-99", {"tool_name": "Bash", "tool_input": {"command": command}}
        )
        assert verdict == "allow"
    else:
        from agent_cli import permissions
        from agent_cli.dispatch.rules import build_settings
        from agent_cli.hooks import no_chaining

        perms = build_settings(tmp_root, repo, worktree=str(worktree))["permissions"]
        verdict, _rule = permissions.matches(command, perms["allow"], perms["deny"])
        assert verdict == "allowed"
        assert no_chaining(tmp_root, {"tool_input": {"command": command}}) == ""
    argv = shlex.split(command)
    result = subprocess.run(argv, cwd=worktree, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == hashlib.sha256(fixture.read_bytes()).hexdigest()
