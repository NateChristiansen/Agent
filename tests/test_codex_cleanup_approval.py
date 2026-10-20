"""Codex git cleanup approval (phase C).

A Codex worker cannot take the worktree's index lock inside its sandbox, so `git restore` needs
the host's approval; it used to be declined automatically.
Approval is limited to a worktree-only restore of literal, unprotected paths inside the dispatch's
declared writes (any unprotected path for a read-only review or an unknown-scope dispatch).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_cli.dispatch import codex_permissions
from agent_cli.dispatch.codex_permissions import git_metadata_refusal
from dispatch_helpers import droot  # noqa: F401 -- pytest fixture

_ERROR_PRIVILEGE_NOT_HELD = 1314


def _repo(where: Path) -> Path:
    """A worktree whose HEAD has the files these cases name."""
    import subprocess

    for rel in (
        "src/report/cost.py",
        "src/report/other.py",
        "src/agent_cli/hooks/guards.py",
        ".agents/skills/dispatch/SKILL.md",
        "tests/other.py",
        "bin/agent",
    ):
        (where / rel).parent.mkdir(parents=True, exist_ok=True)
        (where / rel).write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(where)], check=True)
    subprocess.run(["git", "-C", str(where), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(where), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"],
        check=True,
    )
    return where


POWERSHELL = r'"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Command '


def _approve(
    monkeypatch: pytest.MonkeyPatch, wt: Path, command: str, writes: list[str] | None, *, raw: str = ""
) -> bool:
    if not (wt / ".git").exists():
        _repo(wt)
    record: dict[str, Any] = {"worktree": str(wt), "writes": writes}
    monkeypatch.setattr(codex_permissions.state, "load", lambda *_a: record)
    monkeypatch.setattr(codex_permissions, "decision", lambda *_a: ("allow", "rule"))
    monkeypatch.setattr(
        "agent_cli.worktree_grants.protected_paths", lambda _root: ["src/agent_cli/hooks/**", "bin/agent"]
    )
    params = {"kind": "command", "cwd": str(wt), "command": raw or f"/bin/zsh -lc {command!r}"}
    return not git_metadata_refusal(Path("/unused"), "d", params)


@pytest.mark.parametrize(
    ("command", "writes"),
    [
        ("git restore -- src/report/cost.py", ["src/report"]),
        ("git restore --worktree -- src/report/cost.py", ["src/report/cost.py"]),
        ("git checkout -- src/report/cost.py", ["src/report"]),
        ("git restore -- src/report/cost.py", []),  # a read-only review restoring its probe
        ("git restore -- src/report/cost.py", None),
    ],
)
def test_a_worktree_restore_of_declared_paths_is_approved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, writes: list[str] | None
) -> None:
    assert _approve(monkeypatch, tmp_path, command, writes)


@pytest.mark.parametrize(
    "command",
    [
        "git restore -- tests/other.py",  # outside writes
        "git restore -- src/agent_cli/hooks/guards.py",  # the owner's protected path
        "git restore -- .agents/skills/dispatch/SKILL.md",  # Codex-protected
        "git restore -- .git/config",
        "git restore --staged -- src/report/cost.py",  # changes the index
        "git restore -S -- src/report/cost.py",
        "git restore --source HEAD~1 -- src/report/cost.py",
        "git restore --pathspec-from-file=list -- src/report/cost.py",
        "git restore -p -- src/report/cost.py",
        "git restore src/report/cost.py",  # no `--`
        "git restore -- 'src/report/*.py'",  # a pattern
        "git restore -- :/src/report/cost.py",  # pathspec magic
        "git restore -- ../escape.py",
        "git checkout main",  # branch checkout
        "git checkout -b new -- src/report/cost.py",
        "git checkout HEAD~1 -- src/report/cost.py",
        "git restore -- src/report/cost.py; rm -rf src",
        "git restore -- src",  # a directory restores protected files below it
        "git checkout -- src",
        "git restore -- .agents",
        "git restore -- src/report",
        "git restore -- 'src/${TARGET:-../tests/other.py}'",  # shell expansion
        "git restore -- src/report/~cost.py",
        "git restore -- src/report/new_file.py",  # not in HEAD
    ],
)
def test_other_cleanups_are_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str) -> None:
    writes = ["src/report", "src/agent_cli/hooks", ".agents/skills", ".git"]
    assert not _approve(monkeypatch, tmp_path, command, writes if "other" not in command else ["src/report"])


def test_add_and_commit_are_still_approved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _approve(monkeypatch, tmp_path, "git add src/report/cost.py", ["src/report"])
    assert _approve(monkeypatch, tmp_path, "git commit -m 'fix'", ["src/report"])


def test_a_windows_powershell_commit_is_approved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The exact request 043-windows-08 sent, which the host declined.
    raw = POWERSHELL + """'git commit -m "test: acceptance fixture"'"""
    assert _approve(monkeypatch, tmp_path, "", ["src/report"], raw=raw)
    assert _approve(
        monkeypatch, tmp_path, "", ["src/report"], raw=POWERSHELL + "'git add src/report/cost.py'"
    )
    pwsh = r'"C:\Program Files\PowerShell\7\pwsh.exe" -Command '
    assert _approve(monkeypatch, tmp_path, "", ["src/report"], raw=pwsh + "'git commit -m fix'")


@pytest.mark.parametrize(
    "raw",
    [
        POWERSHELL + """'git commit -m "$(Remove-Item src)"'""",  # PowerShell subexpression
        POWERSHELL + """'git commit -m "a`"; Remove-Item src; `""'""",  # PowerShell escape
        POWERSHELL + "'git commit -m fix; Remove-Item src'",
        POWERSHELL + "'Remove-Item src'",
        r'"C:\Windows\System32\cmd.exe" -Command ' + "'git commit -m fix'",
        r'"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -EncodedCommand abc',
        POWERSHELL + "'git commit -m fix' extra",
    ],
)
def test_other_powershell_requests_are_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    assert not _approve(monkeypatch, tmp_path, "", ["src/report"], raw=raw)


@pytest.mark.parametrize(
    "body",
    [
        # Each runs a second command, or writes a file, under PowerShell while POSIX parsing sees
        # one git command (review of 043-codex-powershell-commit).
        "git commit -m 'a’; calc; ‘b'",  # smart quotes end a PowerShell string
        'git commit -m "a”; calc; “b"',
        'git commit -m "a\\"; calc; \\""',  # PowerShell has no backslash escape
        "git commit -m (calc)",
        "git commit -m @(calc)",
        "git commit -m @x",  # splatting
        "git commit -m fix|calc",
        "git commit -m fix | calc",
        "git commit -m fix\rcalc",
        "git commit -m fix calc",
        "git commit -m fix\u0085calc",
        "git commit -m fix > ..\\out.txt",
        "git add src/report/cost.py>..\\out.txt",
        "git commit -m fix --% calc",
        "git commit -m {calc}",
        "git commit -m fix #; calc",
        "git commit -m %PATH%",
    ],
)
def test_powershell_bodies_outside_the_plain_set_are_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    import shlex

    assert not _approve(monkeypatch, tmp_path, "", ["src/report"], raw=POWERSHELL + shlex.quote(body))


def test_a_host_decline_is_recorded_as_the_hosts_not_the_owners(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from agent_cli import repos
    from agent_cli.core import AgentError
    from agent_cli.dispatch import codex_transport, state
    from dispatch_helpers import _new_fast, envelope, one

    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    d["model"] = "gpt-6-luna"
    repo = repos.load_registry(droot).require("alpha")
    answers: list[dict[str, Any]] = []

    class RPC:
        process = None

        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise AgentError("stop before inference")

        def stderr_tail(self) -> str:
            return ""

    @asynccontextmanager
    async def isolated(*_args: Any, **kwargs: Any) -> AsyncIterator[tuple[RPC, dict[str, Any]]]:
        answers.append(
            await kwargs["handle_request"](
                {
                    "id": 41,
                    "method": "item/fileChange/requestApproval",
                    "params": {"reason": "protected path"},
                }
            )
        )
        yield RPC(), {}

    monkeypatch.setattr(codex_transport, "isolated_server", isolated)
    asyncio.run(
        codex_transport._run_codex(
            droot, d, repo, text="x", resume=False, mcp_servers={}, inbox=droot / ".agent/x", timeout=5
        )
    )
    assert answers == [{"decision": "decline"}]
    events = state.load(droot, d["id"])["permission_events"]
    host = [e for e in events if e.get("origin") == "host"]
    assert len(host) == 1
    assert host[0]["kind"] == "item/fileChange/requestApproval"
    assert host[0]["request_id"] == 41
    assert "no owner saw it" in host[0]["reason"]


def test_a_symlink_to_an_allowed_path_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _repo(tmp_path)
    try:
        (tmp_path / "src/report/link.py").symlink_to(tmp_path / "src/agent_cli/hooks/guards.py")
    except OSError as error:
        if getattr(error, "winerror", None) != _ERROR_PRIVILEGE_NOT_HELD:
            raise
        pytest.skip("this Windows account lacks the privilege to create symlinks (WinError 1314)")
    assert not _approve(monkeypatch, tmp_path, "git restore -- src/report/link.py", ["src"])


# ---------------------------------------------------------------- MCP grants

GRANTED = "mcp__n3grants__read_nonce"
MCP_SERVERS = {
    "n3grants": {"command": "python", "args": ["server.py"]},
    "other": {"url": "https://mcp.example.test/mcp"},
}


def _transport(
    droot: Path, monkeypatch: pytest.MonkeyPatch, requests: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """The thread config the real transport sends, its answer to each request once the thread
    has started, and the dispatch id."""
    import asyncio
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from agent_cli import repos
    from agent_cli.core import AgentError
    from agent_cli.dispatch import codex_transport
    from dispatch_helpers import _new_fast

    d = _new_fast(droot)
    d["model"] = "gpt-6-luna"
    repo = repos.load_registry(droot).require("alpha")
    config: dict[str, Any] = {}
    answers: list[dict[str, Any]] = []
    handler: list[Any] = []

    class RPC:
        process = None

        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            config.update(params["config"])
            for request in requests:
                answers.append(await handler[0](request))
            raise AgentError("stop before inference")

        def stderr_tail(self) -> str:
            return ""

    @asynccontextmanager
    async def isolated(*_args: Any, **kwargs: Any) -> AsyncIterator[tuple[RPC, dict[str, Any]]]:
        handler.append(kwargs["handle_request"])
        yield RPC(), {}

    monkeypatch.setattr(codex_transport, "isolated_server", isolated)
    asyncio.run(
        codex_transport._run_codex(
            droot,
            d,
            repo,
            text="x",
            resume=False,
            mcp_servers=MCP_SERVERS,
            inbox=droot / ".agent/x",
            timeout=5,
        )
    )
    return config, answers, d["id"]


def test_the_thread_config_approves_exactly_the_granted_mcp_tools(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dispatch_helpers import _set_alpha_dispatch, envelope, one

    fake_claude.scenario([one(envelope())])
    _set_alpha_dispatch(droot, allowed_tools=[GRANTED])
    config, _answers, _id = _transport(droot, monkeypatch, [])
    servers = config["mcp_servers"]
    assert servers["n3grants"]["tools"] == {"read_nonce": {"approval_mode": "approve"}}
    assert "default_tools_approval_mode" not in servers["n3grants"]
    assert "tools" not in servers["other"]


def test_without_a_grant_the_thread_config_approves_no_mcp_tool(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dispatch_helpers import envelope, one

    fake_claude.scenario([one(envelope())])
    config, _answers, _id = _transport(droot, monkeypatch, [])
    assert not any("tools" in s for name, s in config["mcp_servers"].items() if name in MCP_SERVERS)


@pytest.mark.parametrize(
    ("allow", "deny", "approved"),
    [
        ([GRANTED], [], ["read_nonce"]),
        ([GRANTED, "mcp__n3grants__write_sentinel"], [], ["read_nonce", "write_sentinel"]),
        ([GRANTED, GRANTED], [], ["read_nonce"]),
        ([GRANTED], [GRANTED], []),  # deny wins
        ([GRANTED], ["mcp__n3grants__*"], []),
        (["mcp__n3grants__*"], [], []),  # names no tool, so the server keeps prompting
        (["mcp__n3grants"], [], []),  # bare: no tool to the Codex hook
        (["mcp__n3grants__read_*"], [], []),
        (["mcp__other__read_nonce", "Bash(git status)"], [], []),
        (["mcp__n3grants__read_nonce(x)"], [], []),
    ],
)
def test_only_exact_undenied_grants_are_approved(
    allow: list[str], deny: list[str], approved: list[str]
) -> None:
    from agent_cli.dispatch.codex_transport import approved_mcp_tools

    assert approved_mcp_tools("n3grants", allow, deny) == approved


def test_an_ambiguous_server_name_approves_nothing() -> None:
    from agent_cli.dispatch.codex_transport import approved_mcp_tools

    assert approved_mcp_tools("a__b", ["mcp__a__b__read"], []) == []
    assert approved_mcp_tools("a_", ["mcp__a___read"], []) == []


def test_every_approval_request_and_elicitation_is_declined(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.dispatch import state
    from dispatch_helpers import _set_alpha_dispatch, envelope, one

    fake_claude.scenario([one(envelope())])
    _set_alpha_dispatch(droot, allowed_tools=[GRANTED])
    elicitation = "mcpServer/elicitation/request"
    form = {
        "threadId": "thread-1",
        "serverName": "n3grants",
        "mode": "form",
        "requestedSchema": {"type": "object", "properties": {}},
    }
    requests = [
        # Codex's approval prompt for the granted tool's server and the server's own form look alike.
        {
            "id": 0,
            "method": elicitation,
            "params": {**form, "message": "Allow the n3grants MCP server to run tool?"},
        },
        {
            "id": 1,
            "method": elicitation,
            "params": {**form, "message": "Approve the deployment to production?"},
        },
        {
            "id": 2,
            "method": elicitation,
            "params": {**form, "serverName": "other", "message": "Run write_sentinel?"},
        },
        {"id": 3, "method": elicitation, "params": "malformed"},
        {"id": 4, "method": elicitation},
        {
            "id": 5,
            "method": "item/commandExecution/requestApproval",
            "params": {"kind": "command", "cwd": "/elsewhere", "command": "rm -rf src"},
        },
        {"id": 6, "method": "item/fileChange/requestApproval", "params": {"reason": "protected path"}},
        {"id": 7, "method": "item/permissions/requestApproval", "params": []},
    ]
    _config, answers, did = _transport(droot, monkeypatch, requests)
    assert answers == [{"decision": "decline"}] * len(requests)
    host = [e for e in state.load(droot, did)["permission_events"] if e.get("origin") == "host"]
    assert [(e["request_id"], e["decision"]) for e in host] == [(i, "decline") for i in range(len(requests))]
    assert "mode" in host[0]["request_fields"] and host[0]["meta_fields"] == []
    assert "request_fields" not in host[5]
