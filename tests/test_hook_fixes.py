"""Hook false positives from the audit week, each with the refusal it must keep; hook switches; the
hook decision log."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest
from claude_agent_sdk import PreToolUseHookInput

from agent_cli.dispatch import readonly_policy
from agent_cli.hooks import decision_log, toggles
from agent_cli.hooks.run import run as run_hook
from agent_cli.hooks.worker_tests import full_suite


def _bash(command: str, **extra) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, **extra})


def _decision(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else ""


@pytest.mark.parametrize(
    "command",
    [
        "stat -f %z README.md",
        "shasum -a 256 a b",
        "sha256sum a",
        "realpath .",
        "jq -r .name package.json",
        "sed -n '10,40p' src/x.py",
        "sed -n '/def f/,/^$/p' src/x.py",
        "ls -la src",
        "codex --version",
        "unlisted-tool --version",
        "agent --help",
    ],
)
def test_the_read_only_class_admits_plain_reads(command: str):
    assert readonly_policy.reason(command)


@pytest.mark.parametrize(
    "command",
    [
        "file -C -m magic",
        "tree -o listing.txt",
        "sed -i s/a/b/ f",
        "sed -n 'w out' f",
        "sed -n '1p' f > out",
        "stat x | tee y",
        "jq --unknown-option f",
        "curl https://example.com",
        "python3 -c 'print(1)'",
        "python3 -c 'print(1)' --version",
        "rm sample.txt",
        "sed -n -i '1p' f",
        "agent --help dispatch new",
        "source --version",
        ". --version",
        # Audit 10 A6 round 2 R2 retires automatic version grants for executable paths.
        ".venv/bin/ruff --version",
        "./sample-tool --version",
        "/tmp/sample-tool --version",
        r"'.\sample-tool' --version",
    ],
)
def test_the_read_only_class_refuses_writes_execution_and_network(command: str):
    assert readonly_policy.reason(command) == ""


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n"], ids=["lf", "cr", "crlf"])
def test_the_read_only_class_refuses_unquoted_line_breaks(separator: str):
    assert readonly_policy.reason(f"ls{separator}cat README.md") == ""


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n"], ids=["lf", "cr", "crlf"])
@pytest.mark.parametrize("quote", ["'", '"'], ids=["single", "double"])
def test_the_read_only_class_keeps_quoted_line_breaks(separator: str, quote: str):
    assert readonly_policy.reason(f"rg -n {quote}first{separator}second{quote} src")


def test_both_providers_and_the_preflight_agree_on_the_read_only_class(tmp_root: Path):
    from agent_cli import repos
    from agent_cli.dispatch.rules import build_settings, decide

    preflight_mod = importlib.import_module("agent_cli.dispatch.preflight")

    repo = repos.load_registry(tmp_root).require("alpha")
    d = {"worktree": str(tmp_root / "repos" / "alpha-api"), "id": "001-mac-01"}
    perms = build_settings(tmp_root, repo, worktree=d["worktree"])["permissions"]
    for command, want in (
        ("sha256sum README.md", "allow"),
        ("tree -o out.txt", "deny"),
        ("nl -ba src/x.py | sed -n '1,190p'", "allow"),
        ("cat f | tee out", "deny"),
        ("unlisted-tool --version", "allow"),
        (".venv/bin/ruff --version", "deny"),
        ("./sample-tool --version", "deny"),
        ("/tmp/sample-tool --version", "deny"),
        (r"'.\sample-tool' --version", "deny"),
        ("stat README.md\ncat README.md", "deny"),
        ("stat README.md\rcat README.md", "deny"),
    ):
        verdict, _ = decide("Bash", {"command": command}, d, repo, tmp_root)
        assert verdict == want, command
        scored, _ = preflight_mod._score(command, perms["allow"], perms["deny"])
        assert (scored == "allowed") == (want == "allow"), command


@pytest.mark.parametrize(
    "command",
    [
        "rg -n example src",
        "ls -la src",
        "cat README.md",
        "sed -n '1,40p' README.md",
        "git check-ignore -v scratch/x.py tmp/x.py .scratch/x.py",
        "codex --version",
        "unlisted-tool --version",
        "agent --help",
        "rg -n 'first\nsecond' src",
        "git log -1 --format='first\nsecond'",
    ],
)
def test_worker_read_commands_pass_both_providers_and_preflight(tmp_root: Path, monkeypatch, command: str):
    from agent_cli import hooks, permissions, repos
    from agent_cli.dispatch import codex_permissions
    from agent_cli.dispatch.rules import build_settings, decide, sdk_hooks

    repo = repos.load_registry(tmp_root).require("alpha")
    d = {"repo": "alpha", "worktree": str(tmp_root / "repos" / "alpha-api"), "id": "001-mac-01"}
    perms = build_settings(tmp_root, repo, worktree=d["worktree"])["permissions"]
    # Claude's native rules run first; its callback decides only fall-throughs.
    verdict, _ = permissions.matches(command, perms["allow"], perms["deny"])
    if verdict == "no-rule":
        assert decide("Bash", {"command": command}, d, repo, tmp_root)[0] == "allow"
    else:
        assert verdict == "allowed"
    assert not hooks.is_chained(command)
    assert run_hook(tmp_root, "no-chaining", _bash(command))[0] == ""

    callback = sdk_hooks(tmp_root, Path(d["worktree"]))["PreToolUse"][0].hooks[0]
    payload: PreToolUseHookInput = {
        "hook_event_name": "PreToolUse",
        "session_id": "worker-read",
        "transcript_path": "",
        "cwd": d["worktree"],
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "read-command",
    }
    assert asyncio.run(callback(payload, None, {"signal": None})) == {}
    monkeypatch.setattr(codex_permissions.state, "load", lambda *_args: d)
    assert (
        codex_permissions.decision(
            tmp_root, d["id"], {"tool_name": "Bash", "tool_input": {"command": command}}
        )[0]
        == "allow"
    )
    preflight_mod = importlib.import_module("agent_cli.dispatch.preflight")
    assert preflight_mod._score(command, perms["allow"], perms["deny"])[0] == "allowed"


def test_worker_settings_grant_native_reads_and_check_ignore_twins(tmp_root: Path):
    from agent_cli import permissions, repos
    from agent_cli.dispatch.rules import build_settings

    repo = repos.load_registry(tmp_root).require("alpha")
    worktree = str(tmp_root / "repos" / "worker")
    perms = build_settings(tmp_root, repo, worktree=worktree)["permissions"]
    assert {"Read", "Glob"} <= set(perms["allow"])
    assert not {"Read", "Glob"} & set(perms["deny"])
    for target in (worktree, str(repo.abs_path(tmp_root))):
        assert (
            permissions.matches(
                f"git -C {target} check-ignore -v scratch/x.py", perms["allow"], perms["deny"]
            )[0]
            == "allowed"
        )


@pytest.mark.parametrize("command", ["sed -i s/a/b/ f", "rm -rf scratch", "python -c 'print(1)'"])
def test_read_fallback_does_not_grant_writes_or_inline_code(tmp_root: Path, command: str):
    from agent_cli import repos
    from agent_cli.dispatch.rules import decide

    repo = repos.load_registry(tmp_root).require("alpha")
    assert decide("Bash", {"command": command}, {}, repo, tmp_root)[0] == "deny"
    assert not readonly_policy.reason(command)


def test_a_checksum_of_a_worktree_file_is_a_read_and_writing_options_are_not(tmp_root: Path):
    """2026-10: `shasum` on an absolute worktree path was denied with "you are the manager"."""
    sibling = tmp_root.parent / "sibling" / "Gamma"
    sibling.mkdir(parents=True)
    reg = tmp_root / "agent.yaml"
    reg.write_text(
        reg.read_text()
        + "  - name: Gamma\n    short: gamma\n    path: ../sibling/Gamma\n    remote: git@github.com:example/Gamma.git\n"
    )
    for command, denied in (
        (f"shasum -a 256 {sibling}/a.py", False),
        (f"sha256sum {sibling}/a.py", False),
        (f"md5 -q {sibling}/a.py", False),
        (f"realpath {sibling}/a.py", False),
        (f"stat -f %z {sibling}/a.py", False),
        (f"tree -o {sibling}/listing.txt {sibling}", True),
        (f"file -C -m {sibling}/magic", True),
    ):
        out, _ = run_hook(tmp_root, "protect-generated", _bash(command))
        assert (_decision(out) == "deny") is denied, command


def test_the_find_exec_denial_names_a_read_only_form_both_layers_allow(tmp_root: Path):
    from agent_cli import permissions

    out, _ = run_hook(tmp_root, "protect-generated", _bash("find src -name '*.py' -exec wc -l {} +"))
    assert (
        _decision(out) == "deny"
        and "-print" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    )
    suggested = "find src -name '*.py' -print"
    assert run_hook(tmp_root, "protect-generated", _bash(suggested))[0] == ""
    block = json.loads((Path(__file__).resolve().parents[1] / ".claude" / "settings.json").read_text())
    perms = block["permissions"]
    assert permissions.matches(suggested, perms["allow"], perms["deny"])[0] == "allowed"
    assert permissions.matches("find src -exec wc -l {} +", perms["allow"], perms["deny"])[0] == "denied"


def test_pytest_version_and_help_are_not_a_full_suite():
    for command in ("uv run pytest --version", "uv run pytest -V", "uv run pytest --help"):
        assert full_suite(command) is None, command
    assert full_suite("uv run pytest") is not None


def test_a_switched_off_hook_does_not_run_and_the_safety_core_cannot_be_switched(tmp_root: Path):
    (tmp_root / "agent.local.yaml").write_text("hooks:\n  thrash-guard: false\n", encoding="utf-8")
    assert toggles.disabled(tmp_root, "thrash-guard")
    assert toggles.report(tmp_root) == (["thrash-guard"], [])
    (tmp_root / "agent.local.yaml").write_text("hooks:\n  no-chaining: false\n", encoding="utf-8")
    assert not toggles.disabled(tmp_root, "no-chaining")
    off, bad = toggles.report(tmp_root)
    assert off == [] and "cannot be switched" in bad[0]
    out, _ = run_hook(tmp_root, "no-chaining", _bash("ls; pwd"))
    assert _decision(out) == "deny"


def test_a_switch_for_a_retired_hook_is_ignored_not_an_error(tmp_root: Path):
    """An earlier change retired `plan-gate-stop` and `style-guard`. A leftover switch for one must not void
    the block, which would turn the owner's other switches back on."""
    (tmp_root / "agent.local.yaml").write_text(
        "hooks:\n  thrash-guard: false\n  plan-gate-stop: false\n  style-guard: false\n  probe-cwd-guard: false\n",
        encoding="utf-8",
    )
    assert toggles.report(tmp_root) == (["thrash-guard"], [])
    assert toggles.disabled(tmp_root, "thrash-guard")


def _events(sink: Path) -> list[dict]:
    files = sorted((sink / "events").glob("*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text().splitlines()]


def test_a_denial_is_logged_redacted_and_without_argument_values(tmp_path: Path, monkeypatch):
    from agent_cli import telemetry

    monkeypatch.setattr(telemetry, "sink_root", lambda root: tmp_path)
    payload = {
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "curl -H 'X: TOKEN=abc' https://x"},
    }
    out = json.dumps(
        {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "API_TOKEN=supersecret refused",
            }
        }
    )
    decision_log.denial(tmp_path, "push-guard", payload, out, 0.01)
    [event] = _events(tmp_path / ".agent" / "telemetry")
    assert event["kind"] == "hook.denial" and event["data"]["hook"] == "push-guard"
    assert "supersecret" not in json.dumps(event) and event["data"]["command"] == "curl -H"


def test_a_failing_handler_is_logged_and_still_fails_open(tmp_root: Path, monkeypatch):
    from agent_cli import telemetry

    run_mod = importlib.import_module("agent_cli.hooks.run")
    monkeypatch.setattr(telemetry, "sink_root", lambda root: tmp_root)

    def boom(root, payload):
        raise RuntimeError("handler broke")

    monkeypatch.setitem(run_mod.HANDLERS, "thrash-guard", boom)
    assert run_hook(tmp_root, "thrash-guard", "{}") == ("", 0)
    assert [e["kind"] for e in _events(tmp_root / ".agent" / "telemetry")] == ["hook.error"]


def test_a_denial_summary_keeps_no_credentials(tmp_path: Path, monkeypatch):
    """Codex review: assignments and attached option values reached the log."""
    from agent_cli import telemetry

    monkeypatch.setattr(telemetry, "sink_root", lambda root: tmp_path)
    command = (
        "API_TOKEN=example-secret curl -H'Authorization: Bearer example-secret' -pexample-secret https://x"
    )
    payload = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": command}}
    out = json.dumps(
        {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "refused: Authorization: Bearer example-secret",
            }
        }
    )
    decision_log.denial(tmp_path, "push-guard", payload, out, 0.01)
    [event] = _events(tmp_path / ".agent" / "telemetry")
    assert "example-secret" not in json.dumps(event)
    assert event["data"]["command"] == "curl -H -p"


# 21 of 29 Codex worker denials on 2026-10-07 were reads like these, refused because the
# read-only class rejected every pipeline and had no `nl`.
@pytest.mark.parametrize(
    "command",
    [
        "nl -ba src/agent_cli/report/cost.py | sed -n '1,190p'",
        "nl -ba tests/test_live_state_fence.py",
        "nl -ba src/x.py | tail -n +20 | head -n 40",
        "cat -n src/x.py | sed -n '5,9p'",
        "rg -n def src | head -n 20",
        "git_files_listing | wc -l".replace("git_files_listing", "cat list"),
        "pwd -P",
    ],
)
def test_read_only_pipelines_and_nl_are_in_the_class(command: str):
    assert readonly_policy.reason(command)


@pytest.mark.parametrize(
    "command",
    [
        "cat f | tee out",
        "cat f | xargs rm",
        "cat f | sh",
        "nl -ba f | sed -n '1p' > out",
        "cat $(ls) | head",
        "cat f | sort -o out",
        "rg --pre cat x | head",
        "rg -zi foo | head",
        "awk '{print}' f | head",
        "cat a b | uniq - out",
        "env | grep TOKEN",
        "nl f | head; rm f",
        "nl -ba input | sort --out=sort-output",  # an abbreviated --output
        "nl -ba input | sort -o out",
        "nl -ba input | uniq -- - -uniq-output",  # a second operand behind `--`
        "cat f | uniq --skip-chars=1 - out",
    ],
)
def test_a_pipeline_with_a_writing_or_executing_stage_stays_out(command: str):
    assert readonly_policy.reason(command) == ""
