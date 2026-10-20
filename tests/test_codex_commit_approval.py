"""The dispatch host approves a Codex worker's escalated git add/commit on a fresh install: a
product repo registered by relative path outside the harness root, with or without a remote,
judged through the transport's own request handler with the real worker rules."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_cli import links, repos
from agent_cli.core import AgentError
from agent_cli.dispatch import codex_transport, state
from conftest import git
from dispatch_helpers import _new_fast, droot, envelope  # noqa: F401 -- pytest fixture

COMMIT = "/bin/zsh -lc 'git commit -m \"Add alpha probe README\"'"
ADD = "/bin/zsh -lc 'git add README.md'"


def _probe_repo(root: Path, run: Any, *, remote: bool) -> str:
    """Register `alpha-probe` the way the alpha runbook does: `../repos/alpha-probe`, worktrees
    beside it, one empty commit, and a remote only when asked."""
    short = "alpha-probe"
    clone = root.parent / "repos" / short
    clone.mkdir(parents=True)
    git(["init", "-q", "-b", "main"], clone)
    git(["config", "user.email", "owner@example.com"], clone)
    git(["config", "user.name", "Owner"], clone)
    git(["commit", "-q", "--allow-empty", "-m", "init"], clone)
    entry: dict[str, Any] = {
        "name": short,
        "short": short,
        "path": f"../repos/{short}",
        "default_branch": "main",
        "worktrees_dir": f"../repos/_worktrees/{short}",
        "harness": "none",
        "dispatch": {"model": None, "permission_mode": "acceptEdits", "max_budget_usd": 1, "extra_args": []},
    }
    if remote:
        bare = root.parent / "remotes" / f"{short}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        git(["init", "-q", "--bare", str(bare)], root.parent)
        git(["remote", "add", "origin", str(bare)], clone)
        git(["push", "-q", "origin", "main"], clone)
        git(["fetch", "-q", "origin"], clone)
        entry["remote"] = str(bare)
    reg = root / "agent.yaml"
    data = yaml.safe_load(reg.read_text(encoding="utf-8"))
    data["repos"].append(entry)
    reg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    r = run(["feature", "set", "1", "--add-repo", short])
    assert r.code == 0, r.err
    return short


def _codex_dispatch(root: Path, monkeypatch: pytest.MonkeyPatch, short: str) -> dict[str, Any]:
    def no_turn(_root: Path, record: dict[str, Any], _repo: Any, **_kw: Any) -> dict[str, Any]:
        final = envelope(status="question", nonce=record["nonce"])
        final["session_id"] = record["session_id"]
        return {
            "last_result": final,
            "session_established": True,
            "exit_code": 0,
            "timed_out": False,
            "stop_reason": "result",
            "resolved_model": record["model"],
        }

    monkeypatch.setattr(codex_transport, "run_codex", no_turn)
    d = _new_fast(
        root, repo=short, provider="codex", row="probe", writes=["README.md"], budget_reason="probe"
    )
    d = state.load(root, d["id"])
    (Path(d["worktree"]) / "README.md").write_text("alpha probe\n", encoding="utf-8")
    return d


def _answers(
    root: Path, monkeypatch: pytest.MonkeyPatch, d: dict[str, Any], requests: list[dict[str, Any]]
) -> list[str]:
    """Each request's answer from the transport's approval handler, as Codex would receive it."""
    answers: list[str] = []

    class RPC:
        process = None

        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise AgentError("stop before inference")

        def stderr_tail(self) -> str:
            return ""

    @asynccontextmanager
    async def isolated(*_args: Any, **kwargs: Any) -> AsyncIterator[tuple[RPC, dict[str, Any]]]:
        for n, params in enumerate(requests):
            message = {"id": n, "method": "item/commandExecution/requestApproval", "params": params}
            answers.append((await kwargs["handle_request"](message))["decision"])
        yield RPC(), {}

    monkeypatch.setattr(codex_transport, "isolated_server", isolated)
    repo = repos.load_registry(root).require(str(d["repo"]))
    asyncio.run(
        codex_transport._run_codex(  # pyright: ignore[reportPrivateUsage]
            root, d, repo, text="x", resume=False, mcp_servers={}, inbox=root / ".agent/x", timeout=5
        )
    )
    return answers


def _request(cwd: str, command: str = COMMIT) -> dict[str, Any]:
    return {
        "threadId": "t",
        "turnId": "u",
        "itemId": "i",
        "reason": "Codex protects git metadata",
        "networkApprovalContext": None,
        "command": command,
        "cwd": cwd,
    }


@pytest.mark.parametrize("remote", [False, True], ids=["local-only", "with-remote"])
def test_a_fresh_install_commit_is_approved_in_any_spelling_of_the_worktree(
    droot: Path, run: Any, monkeypatch: pytest.MonkeyPatch, remote: bool
) -> None:
    d = _codex_dispatch(droot, monkeypatch, _probe_repo(droot, run, remote=remote))
    wt = d["worktree"]
    link = droot.parent / "wt-link"
    links.make(link, Path(wt), is_dir=True)
    spellings = [wt, f"{wt}/", f"{wt}/.", str(link)]
    requests = [_request(cwd, command) for cwd in spellings for command in (ADD, COMMIT)]
    assert _answers(droot, monkeypatch, d, requests) == ["accept"] * len(requests)


def test_escalations_the_rule_refuses_stay_declined_with_the_refusing_check(
    droot: Path, run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    short = _probe_repo(droot, run, remote=False)
    d = _codex_dispatch(droot, monkeypatch, short)
    wt = d["worktree"]
    clone = str(droot.parent / "repos" / short)
    git(["add", "README.md"], Path(wt))
    git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"], Path(wt))
    (Path(wt) / "other.md").write_text("x\n", encoding="utf-8")
    git(["add", "other.md"], Path(wt))
    git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "other"], Path(wt))
    (Path(clone) / "child").mkdir()
    links.make(Path(wt) / "jump", Path(clone) / "child", is_dir=True)
    (Path(wt) / "sub").mkdir()
    clone_link = droot.parent / "clone-link"
    links.make(clone_link, Path(clone), is_dir=True)
    cwds = [_request(clone), _request(f"{wt}/.."), _request("relative/path"), {**_request(wt), "cwd": None}]
    if os.name != "nt":
        # Lexically the worktree, on disk the main clone. Windows drops `..` before it follows
        # any link, so there this cwd is the worktree.
        cwds.append(_request(f"{wt}/jump/.."))
    refused = {
        "cwd": [*cwds, _request(f"{wt}/sub"), _request(str(clone_link))],
        "verb": [
            _request(wt, "/bin/zsh -lc 'git push origin main'"),
            _request(wt, "/bin/zsh -lc 'rm README.md'"),
        ],
        "cleanup": [_request(wt, "/bin/zsh -lc 'git restore -- other.md'")],
        "chained": [_request(wt, "/bin/zsh -lc 'git add README.md; touch x'")],
    }
    requests = [req for reqs in refused.values() for req in reqs]
    assert set(_answers(droot, monkeypatch, d, requests)) == {"decline"}
    host = [e for e in state.load(droot, d["id"])["permission_events"] if e.get("origin") == "host"]
    assert [e["refused_by"] for e in host] == [check for check, reqs in refused.items() for _ in reqs]
    assert [e["request_cwd"] for e in host[1:4]] == ["<worktree>/..", "relative/path", "None"]
    if os.name != "nt":
        assert host[4]["request_cwd"] == "<worktree>/jump/.."
    assert host[len(refused["cwd"])]["request_cwd"] == "<worktree>"
    assert host[0]["request_kind"] == "None"
