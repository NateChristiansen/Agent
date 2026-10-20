"""Reject conflicting raw launch controls without guessing native precedence or leaking values."""

import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from agent_cli import dispatch, repos, repos_validate
from agent_cli.core import AgentError
from agent_cli.dispatch import mcp_policy
from agent_cli.launch_flags import parse_extra_args
from dispatch_helpers import (  # noqa: F401 -- shared pytest fixture
    _new,
    _set_alpha_dispatch,
    droot,
)

SECRET = "synthetic-credential-do-not-display"


MANAGED_FLAGS = [
    "--allowedTools",
    "--allowed-tools",
    "--disallowedTools",
    "--disallowed-tools",
    "--permission-mode",
    "--permission-prompts",
    "--permission-prompt-tool",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--tools",
    "--add-dir",
    "--settings",
    "--setting-sources",
    "--mcp-config",
    "--strict-mcp-config",
    "--plugin-dir",
    "--plugin-url",
    "--agent",
    "--agents",
    "--chrome",
    "--ide",
    "--brief",
    "--bare",
    "--restricted",
    "--safe-mode",
    "--disable-slash-commands",
    "--system-prompt",
    "--system-prompt-file",
    "--append-system-prompt",
    "--append-system-prompt-file",
    "--system-prompt-snapshot",
    "--exclude-dynamic-system-prompt-sections",
    "--session-id",
    "--name",
    "-n",
    "--continue",
    "-c",
    "--resume",
    "-r",
    "--fork-session",
    "--resume-session-at",
    "--resume-drops-turn",
    "--session-mirror",
    "--no-session-persistence",
    "--from-pr",
    "--teleport",
    "--worktree",
    "-w",
    "--tmux",
    "--bg",
    "--background",
    "--cloud",
    "--environment",
    "--remote-control",
    "--remote-control-session-name-prefix",
    "--input-format",
    "--output-format",
    "--json-schema",
    "--print",
    "-p",
    "--replay-user-messages",
    "--help",
    "-h",
    "--version",
    "-v",
    "--model",
    "--fallback-model",
    "--effort",
    "--max-budget-usd",
    "--max-turns",
    "--task-budget",
    "--thinking",
    "--max-thinking-tokens",
    "--thinking-display",
]


def test_documented_or_sdk_control_spelling_refuses_with_redacted_value():
    """Every harness-managed launch flag is refused, in both spellings, with its value redacted.

    `MANAGED_FLAGS` above is an INDEPENDENT inventory, deliberately not imported from
    `agent_cli.launch_flags.MANAGED_GROUPS`. Reading the list from production would make this
    circular: deleting a reserved flag from the source would delete it from the test's input
    too, and the regression would pass while the guard quietly stopped covering it.

    One test looping the list rather than 148 collected cases (2026-09-24). The coverage is
    identical — every flag, both the separated and `=` spellings — and a failure names the flag
    it failed on, because the loop asserts per flag.
    """
    for flag in MANAGED_FLAGS:
        for tokens in ([flag, SECRET], [f"{flag}={SECRET}"]):
            with pytest.raises(ValueError, match="conflicts with harness-managed") as exc:
                parse_extra_args(tokens)
            assert SECRET not in str(exc.value), flag


@pytest.mark.parametrize(
    "raw",
    [
        "--debug",
        {},
        ["--debug", 1],
        ["--debug", " "],
        ["--debug=" + SECRET + "\0"],
        ["-"],
        ["--"],
        ["---debug"],
        ["debug"],
        ["--debug", "filter", SECRET],
        ["--debug", "--"],
        ["--debug", "-1"],
        ["-dp"],
        ["-r" + SECRET],
        ["-x"],
        ["--" + SECRET + "?"],
        ["--" + SECRET + " flag"],
        ["--debug", "--debug"],
        ["-d", "--debug=filter"],
        ["--foo=value", "--foo=another"],
        ["--" + SECRET, "--" + SECRET],
        ["--verbose=false"],
        ["--verbose", SECRET],
        ["--debug-file"],
        ["--debug-file="],
        ["--debug-file", "--verbose"],
    ],
)
def test_malformed_or_ambiguous_argv_never_echoes_unknown_input(raw):
    with pytest.raises(ValueError) as exc:
        parse_extra_args(raw)
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, {}),
        ([], {}),
        (["--foo", "bar"], {"foo": "bar"}),
        (["--future-option"], {"future-option": None}),
        (["-d", "api,hooks"], {"debug": "api,hooks"}),
        (["-d=api"], {"debug": "api"}),
        (["--debug="], {"debug": ""}),
        (["--debug", "--verbose"], {"debug": None, "verbose": None}),
        (["--debug=--settings=" + SECRET], {"debug": "--settings=" + SECRET}),
        (["--foo=a=b"], {"foo": "a=b"}),
        (["--foo", "$HOME `literal`\nnewline"], {"foo": "$HOME `literal`\nnewline"}),
        (["--debug-file", "path with spaces"], {"debug-file": "path with spaces"}),
        (["--betas", "one,two", "--no-chrome"], {"betas": "one,two", "no-chrome": None}),
        (
            ["--include-hook-events", "--include-partial-messages"],
            {"include-hook-events": None, "include-partial-messages": None},
        ),
    ],
)
def test_nonconflicting_argv_is_represented_without_shell_splitting(raw, expected):
    assert parse_extra_args(raw) == expected


def record(root: Path) -> dict[str, Any]:
    return {
        "id": "001-01",
        "feature": 1,
        "feature_dir": "features/001-alpha-work",
        "worktree": str(root / "future-worktree"),
        "budget_usd": 1,
        "session_id": "00000000-0000-0000-0000-000000000000",
        "model": "sonnet",
        "effort": "medium",
    }


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    "tokens", [["--settings=" + SECRET], ["--name", SECRET], ["-r", SECRET], ["--output-format=text"]]
)
def test_initial_and_resume_refuse_before_other_configuration_reads(
    droot: Path,
    resume: bool,
    tokens: list[str],
    monkeypatch: pytest.MonkeyPatch,
):
    repo = repos.load_registry(droot).require("alpha")
    repo.dispatch["extra_args"] = tokens

    def forbidden(*args):
        raise AssertionError("MCP resolution ran before raw launch flag validation")

    monkeypatch.setattr(mcp_policy, "resolve", forbidden)
    with pytest.raises(AgentError, match="conflicts with harness-managed") as exc:
        dispatch.build_options(droot, record(droot), repo, resume=resume)
    assert SECRET not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.mark.parametrize("dry_run", [False, True])
def test_registry_refuses_before_dispatch_preflight_or_worker(
    droot: Path,
    fake_claude,
    dry_run: bool,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_alpha_dispatch(droot, extra_args=["--mcp-config=" + SECRET])

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight reached with conflicting raw launch flags")

    monkeypatch.setattr(dispatch.lifecycle, "preflight", forbidden)
    with pytest.raises(AgentError, match="conflicts with harness-managed") as exc:
        _new(droot, dry_run=dry_run)
    assert SECRET not in str(exc.value)
    assert not fake_claude.calls()
    assert not dispatch.file(droot, "001-01", ".json").exists()


def test_registry_validation_reports_conflict_without_changing_other_grants():
    problems = repos_validate.validate_dispatch({"extra_args": ["--allowedTools=" + SECRET]})
    assert len(problems) == 1 and "--allowed-tools" in problems[0] and SECRET not in problems[0]
    assert repos_validate.validate_dispatch({"extra_args": ["--foo", "bar"]}) == []


@pytest.mark.parametrize("resume", [False, True])
def test_sdk_serialization_preserves_nonconflicting_extras_and_managed_options(droot: Path, resume: bool):
    repo = repos.load_registry(droot).require("alpha")
    repo.dispatch["extra_args"] = ["-d=--settings=" + SECRET, "--betas", "fixture-beta", "--no-chrome"]
    d = record(droot)
    settings = dispatch.build_settings(droot, repo)
    options = dispatch.build_options(droot, d, repo, resume=resume, settings=settings)
    assert options.extra_args["debug"] == "--settings=" + SECRET
    assert options.extra_args["name"] == d["id"]
    assert options.allowed_tools == settings["permissions"]["allow"]
    assert options.disallowed_tools == settings["permissions"]["deny"]
    assert options.permission_mode == "acceptEdits" and options.strict_mcp_config is True
    assert options.model == "sonnet" and options.effort == "medium" and options.max_budget_usd == 1
    assert options.setting_sources is None and callable(options.can_use_tool)
    argv = SubprocessCLITransport(prompt="fixture", options=options)._build_command()  # noqa: SLF001
    assert "--debug=--settings=" + SECRET in argv
    assert argv.count("--settings") == 1
    assert argv[argv.index("--settings") + 1] == str(dispatch.file(droot, d["id"], ".settings.json"))
    assert argv.count("--mcp-config") == 1 and "--strict-mcp-config" in argv
    assert list(json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]) == ["agent"]
    assert "--no-chrome" in argv and argv[argv.index("--betas") + 1] == "fixture-beta"
    assert SECRET not in json.dumps(dispatch.options_preview(options))


def test_nonconflicting_flags_preserve_dry_run_preflight(droot: Path):
    _set_alpha_dispatch(droot, extra_args=["--debug", "fixture", "--no-chrome"])
    result = _new(droot, prompt="Run and report:\n\n```sh\ngit status\n```", dry_run=True)
    assert any(row["command"] == "git status" and row["verdict"] == "allowed" for row in result["preflight"])
    assert result["options"]["strict_mcp_config"] is True
