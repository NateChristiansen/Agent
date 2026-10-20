"""The command preflight (1.2): candidate extraction, scoring against the worker's rules, the
precedents warning, and the agent.yaml validations that back them.


"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import dispatch, permissions
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.dispatch.rules import build_settings
from dispatch_helpers import (  # noqa: F401 -- droot is a pytest fixture, used only as a parameter name
    PROMPT,
    _new,
    _set_alpha_dispatch,
    droot,
    envelope,
    one,
)

# ------------------------------------------------------------------ permission preflight (1.2)


def test_a_repo_extra_git_rule_matches_the_git_dash_c_spelling_in_its_own_worktree_only(tmp_root: Path):
    """A worker had `Bash(git reset --soft:*)` in its settings and was still denied
    `git -C <worktree> reset --soft <sha>`, because extras got no `-C` twin."""
    _set_alpha_dispatch(tmp_root, allowed_tools=["Bash(git reset --soft:*)", "Bash(pnpm:*)"])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    wt = "/tmp/worktrees/alpha/001-x"
    perms = build_settings(tmp_root, repo, worktree=wt)["permissions"]
    assert f"Bash(git -C {wt} reset --soft:*)" in perms["allow"]
    assert not any(r.startswith("Bash(git -C ") and "pnpm" in r for r in perms["allow"])
    verdict, _ = permissions.matches(f"git -C {wt} reset --soft 282cc5534", perms["allow"], perms["deny"])
    assert verdict == "allowed"
    verdict, _ = permissions.matches(
        "git -C /elsewhere reset --soft 282cc5534", perms["allow"], perms["deny"]
    )
    assert verdict != "allowed"


def test_extract_candidate_commands_from_a_work_order():
    text = (
        "Run the checks:\n"
        "\n"
        "```\n"
        "pnpm test\n"
        "$ pnpm build\n"
        "```\n"
        "\n"
        "Outside a fence:\n"
        "$ git status\n"
        "\n"
        "See `README.md` first.\n"
        "```shell\npnpm --filter x lint\n```\n"
    )
    assert dispatch.extract_candidate_commands(text) == [
        "pnpm test",
        "pnpm build",
        "git status",
        "pnpm --filter x lint",
    ]


def test_pytest_imports_agent_cli_from_the_candidate_checkout():
    import agent_cli

    candidate_src = Path(__file__).resolve().parents[1] / "src"
    assert Path(agent_cli.__file__).resolve().is_relative_to(candidate_src)


def test_extract_candidate_commands_dedupes_preserving_order():
    text = "$ git status\n\n```sh\ngit status\n```\n\n$ git status\n"
    assert dispatch.extract_candidate_commands(text) == ["git status"]


def test_configured_probes_are_scored_for_any_tool_without_execution(tmp_root: Path):
    sentinel = tmp_root / "probe-was-executed"
    _set_alpha_dispatch(
        tmp_root,
        probes=[
            "teleporter --version",
            "second-tool --version",
            "second-tool --unknown",
            f"touch {sentinel}",
        ],
        allowed_tools=["Bash(teleporter:*)"],
    )
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    d = {"id": "999-99", "thread": [], "worktree": None, "provider": "codex"}
    rows = dispatch.preflight(tmp_root, d, repo)
    assert [r["source"] for r in rows] == ["probe", "probe", "probe", "probe"]
    # Version queries qualify as reads; other ungranted commands still have no rule.
    assert [r["verdict"] for r in rows] == ["allowed", "allowed", "no-rule", "allowed"]
    assert not sentinel.exists()


def test_probe_configuration_defaults_to_no_candidates(tmp_root: Path):
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    assert dispatch.preflight(tmp_root, {"id": "999-99", "thread": [], "worktree": None}, repo) == []


def test_extract_candidate_commands_skips_non_shell_fences():
    """A fence tagged python/json/tsx is data the worker reads, not commands. Untagged and
    shell-tagged fences still count."""
    text = (
        "```python\ndef alpha():\n    return 1\n```\n"
        '```json\n{"a": 1}\n```\n'
        "```bash\n$ npm run lint\n```\n"
        "```\ngit status\n```\n"
    )
    assert dispatch.extract_candidate_commands(text) == ["npm run lint", "git status"]


def test_extraction_ignores_descriptive_mentions_and_comments_but_keeps_command_order():
    text = (
        "For example, `git log -1` is mentioned here.\n"
        "```python\nprint('uv run pytest tests')\n```\n"
        "```sh\n"
        "# git push origin main\n"
        "$ # git push origin main\n"
        "git status\n"
        "uv run pytest tests/test_x.py\n"
        "git status\n"
        "```\n"
        "$ # rm -rf x\n"
        "$ git diff --check\n"
    )
    assert dispatch.extract_candidate_commands(text) == [
        "git status",
        "uv run pytest tests/test_x.py",
        "git diff --check",
    ]


def test_preflight_accepts_the_portable_sha256_instruction(tmp_root: Path):
    import hashlib
    import subprocess

    command = (
        'uv run python -c "import hashlib, pathlib, sys; '
        'print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())" spec.md'
    )
    fixture = tmp_root / "spec.md"
    fixture.write_text("portable hash fixture\n", encoding="utf-8")
    d = {"id": "999-99", "worktree": None, "thread": [{"from": "manager", "text": f"```sh\n{command}\n```"}]}
    _set_alpha_dispatch(tmp_root, allowed_tools=["Bash(uv:*)"])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    [row] = dispatch.preflight(tmp_root, d, repo)
    assert row["command"] == command and row["verdict"] == "allowed"
    result = subprocess.run(command, shell=True, cwd=tmp_root, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == hashlib.sha256(fixture.read_bytes()).hexdigest()


def test_explicit_shell_tags_are_scanned_and_untagged_fences_stay_supported():
    text = "```shell-session\n$ git status\n```\n~~~\nuv run pytest tests/test_x.py\n~~~"
    assert dispatch.extract_candidate_commands(text) == ["git status", "uv run pytest tests/test_x.py"]


def test_preflight_flags_a_dead_prefix_and_accepts_the_wider_rule(tmp_root: Path):
    """`Bash(pnpm lint:*)` reads as a rule for `pnpm --filter x lint` but never matches it.
    That failure is caught here instead of by a headless worker."""
    d = {
        "id": "999-99",
        "worktree": None,
        "thread": [{"from": "manager", "text": "```sh\npnpm --filter x lint\n```"}],
    }
    _set_alpha_dispatch(tmp_root, allowed_tools=["Bash(pnpm lint:*)"])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    rows = dispatch.preflight(tmp_root, d, repo)
    assert rows == [
        {"command": "pnpm --filter x lint", "verdict": "no-rule", "rule": None, "source": "work-order"}
    ]

    _set_alpha_dispatch(tmp_root, allowed_tools=["Bash(pnpm:*)"])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    rows = dispatch.preflight(tmp_root, d, repo)
    assert rows == [
        {
            "command": "pnpm --filter x lint",
            "verdict": "allowed",
            "rule": "Bash(pnpm:*)",
            "source": "work-order",
        }
    ]


def test_preflight_also_scores_the_repo_setup_commands(tmp_root: Path):
    _set_alpha_dispatch(tmp_root, setup=["pnpm install"])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    d = {"id": "999-99", "worktree": None, "thread": []}
    rows = dispatch.preflight(tmp_root, d, repo)
    assert rows == [{"command": "pnpm install", "verdict": "no-rule", "rule": None, "source": "setup"}]


def test_new_refuses_to_launch_on_a_preflight_no_rule_command(droot: Path, fake_claude):
    """A real launch is the one place a dead or missing rule cannot be undone once the worker
    is headless (ADR-0008). Refuse instead of dispatching against it."""
    prompt = PROMPT + "\n\nRun this before committing:\n\n```sh\npnpm --filter x lint\n```"
    with pytest.raises(AgentError, match="preflight refused this launch"):
        _new(droot, prompt=prompt)
    assert not (droot / ".agent/dispatches").exists() or not any((droot / ".agent/dispatches").iterdir())


def test_new_allow_unmatched_overrides_the_preflight_refusal(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    prompt = PROMPT + "\n\nRun this before committing:\n\n```sh\npnpm --filter x lint\n```"
    d = _new(droot, prompt=prompt, allow_unmatched=True)
    assert d["state"] == "done"


def test_dry_run_preflight_does_not_refuse_and_lists_findings(droot: Path, fake_claude):
    """A `--dry-run` shows the owner the same table a real launch would refuse on, but never
    raises. That is the whole point of a preview."""
    prompt = PROMPT + "\n\nRun this before committing:\n\n```sh\npnpm --filter x lint\n```"
    p = _new(droot, prompt=prompt, dry_run=True)
    assert p["preflight"] == [
        {"command": "pnpm --filter x lint", "verdict": "no-rule", "rule": None, "source": "work-order"}
    ]


# ------------------------------------------------------------------ per-commit claim


def _claim_d(text: str) -> dict:
    return {"id": "999-98", "worktree": None, "thread": [{"from": "manager", "text": text}]}


def test_per_commit_check_refuses_a_claim_no_named_command_can_answer(tmp_root: Path):
    """Two dispatches on a five-commit stack were asked to verify the branch green at
    every commit, were denied `git checkout <sha>`, and returned a tip-only check as if the bar
    had been met. The ask is refused now, because the permissions deliberately stay as they
    are: a worker that can move HEAD can commit into a detached head and orphan the work."""
    d = _claim_d("build the stack and confirm the suite is green at every commit")
    rows = dispatch.per_commit_check(tmp_root, d, [])
    assert len(rows) == 1
    assert rows[0]["verdict"] == "unverifiable"
    assert rows[0]["source"] == "per-commit-claim"
    assert "per-commit or per-attempt claim" in rows[0]["warning"]


def test_per_commit_check_accepts_a_claim_an_allowed_history_read_can_answer(tmp_root: Path):
    """The escape is naming the command, not widening the rules. `git -C <path> show` is the
    spelling a worker reaches for far more often than the bare one, so it has to count."""
    d = _claim_d("for each commit run:\n\n```sh\ngit show <sha> --stat\n```\n\nReport the paths touched")
    allowed = [
        {
            "command": "git show <sha> --stat",
            "verdict": "allowed",
            "rule": "Bash(git show:*)",
            "source": "work-order",
        }
    ]
    assert dispatch.per_commit_check(tmp_root, d, allowed) == []
    dash_c = [
        {
            "command": "git -C repo show <sha>",
            "verdict": "allowed",
            "rule": "Bash(git -C:*)",
            "source": "work-order",
        }
    ]
    assert dispatch.per_commit_check(tmp_root, d, dash_c) == []


def test_per_commit_check_ignores_a_command_the_rules_do_not_allow(tmp_root: Path):
    """A denied command answers the claim no better than no command at all — that is the whole
    incident: the ask launched, the command it needed was denied, and the claim came back anyway."""
    d = _claim_d("confirm the suite is green at each commit")
    denied = [
        {
            "command": "git checkout <sha>",
            "verdict": "denied",
            "rule": "Bash(git checkout:*)",
            "source": "work-order",
        }
    ]
    assert len(dispatch.per_commit_check(tmp_root, d, denied)) == 1
    setup = [
        {"command": "git log --oneline", "verdict": "allowed", "rule": "Bash(git log:*)", "source": "setup"}
    ]
    assert len(dispatch.per_commit_check(tmp_root, d, setup)) == 1


def test_per_commit_check_is_quiet_on_a_work_order_that_makes_no_such_claim(tmp_root: Path):
    assert dispatch.per_commit_check(tmp_root, _claim_d("fix the flaky login test"), []) == []
    assert dispatch.per_commit_check(tmp_root, _claim_d("commit the fix and push"), []) == []


def test_new_refuses_to_launch_on_an_unanswerable_per_commit_claim(droot: Path, fake_claude):
    """The refusal is the mechanism, so it has to hold at a real launch, not just in the table."""
    prompt = PROMPT + "\n\nConfirm the suite is green at every commit before you push."
    with pytest.raises(AgentError, match="per-commit or per-attempt claim"):
        _new(droot, prompt=prompt)


def test_allow_unmatched_overrides_the_per_commit_refusal(droot: Path, fake_claude):
    """One override, the same one an unmatched command uses, and it shows in the preview."""
    fake_claude.scenario([one(envelope())])
    prompt = PROMPT + "\n\nConfirm the suite is green at every commit before you push."
    assert _new(droot, prompt=prompt, allow_unmatched=True)["state"] == "done"


def test_dry_run_prints_the_per_commit_refusal_under_warnings(droot: Path, fake_claude):
    """It carries a sentence, not a verdict on a binary, so it renders where the precedents
    warning does and never inflates the command count."""
    from agent_cli import ops

    prompt = PROMPT + "\n\nConfirm the suite is green at every commit before you push."
    text = ops.dispatch_preview_text(_new(droot, prompt=prompt, dry_run=True))
    assert "  warnings:" in text
    assert "per-commit or per-attempt claim" in text
    assert "preflight (0 command(s) found):" in text


# ------------------------------------------------------------------ precedents warning


def _precedents_d(tmp_root: Path, text: str, reason: str) -> dict:
    return {
        "id": "999-99",
        "worktree": None,
        "choice_reason": reason,
        "thread": [{"from": "manager", "text": text}],
    }


def test_precedents_check_warns_on_a_build_row_naming_no_sibling(tmp_root: Path):
    """A `build` row that names no file to mirror gets the advisory warning."""
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    d = _precedents_d(tmp_root, "drain the connection server-side and paint the page complete", "build row")
    rows = dispatch.precedents_check(tmp_root, d, repo)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "warning"
    assert rows[0]["source"] == "precedents"
    assert "precedents list" in rows[0]["warning"]
    assert "not a refusal" in rows[0]["warning"].lower()


def test_precedents_check_is_quiet_when_a_real_repo_path_is_named(tmp_root: Path):
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    (repo.abs_path(tmp_root) / "sub").mkdir()
    (repo.abs_path(tmp_root) / "sub" / "README.md").write_text("x\n", encoding="utf-8")
    # paths are named relative to the repo, which is where the check resolves them
    d = _precedents_d(tmp_root, "mirror the sibling at `sub/README.md`, same shape", "build row")
    assert dispatch.precedents_check(tmp_root, d, repo) == []
    # a slash-separated token that resolves to nothing is not a precedent
    d = _precedents_d(tmp_root, "mirror `sub/Nothing.md` and https://example.com/x", "build row")
    assert len(dispatch.precedents_check(tmp_root, d, repo)) == 1


def test_precedents_check_only_looks_at_the_build_row(tmp_root: Path):
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    text = "read the branch and report findings"
    assert dispatch.precedents_check(tmp_root, _precedents_d(tmp_root, text, "review row"), repo) == []
    assert dispatch.precedents_check(tmp_root, _precedents_d(tmp_root, text, ""), repo) == []


def test_a_precedents_warning_never_refuses_a_launch(droot: Path, fake_claude):
    """The warning is advisory by design. `check_preflight` skips it, so a build dispatch with
    no precedents still launches. It is a nudge at the preview, not a gate."""
    fake_claude.scenario([one(envelope())])
    d = _new(droot, prompt=PROMPT, choice_reason="build row: a readme line")
    assert d["state"] == "done"


def test_dry_run_preview_prints_the_precedents_warning_under_warnings(droot: Path, fake_claude):
    from agent_cli import ops

    p = _new(droot, prompt=PROMPT, choice_reason="build row: a readme line", dry_run=True)
    text = ops.dispatch_preview_text(p)
    assert "  warnings:" in text
    assert "precedents list" in text
    # a warning is not a command, so it never lands in the command table or its count
    assert "preflight (0 command(s) found):" in text
    assert "extraction is best-effort" in text


def test_registry_validates_allowed_tools_and_timeout(droot: Path):
    base = {
        "version": 1,
        "repos": [{"name": "x", "path": "../x", "remote": "r", "dispatch": {"allowed_tools": "Bash(x)"}}],
    }
    assert any("allowed_tools" in p for p in repos_mod.validate_registry_data(base))
    base["repos"][0]["dispatch"] = {"allowed_tools": ["Bash(uv run pytest:*)"], "timeout_minutes": -3}
    assert any("timeout_minutes" in p for p in repos_mod.validate_registry_data(base))
    base["repos"][0]["dispatch"] = {"allowed_tools": ["Bash(uv run pytest:*)"], "timeout_minutes": 30}
    assert repos_mod.validate_registry_data(base) == []
    repo = repos_mod.load_registry(droot).require("alpha")
    assert repo.timeout_minutes == 45.0 and repo.allowed_tools == []


def test_registry_validates_effort_and_model(droot: Path):
    """Model and effort were never chosen on any of the first nine dispatches
    audit). This is the gate on both the repo-declared default and the per-dispatch flag."""
    base = {
        "version": 1,
        "repos": [{"name": "x", "path": "../x", "remote": "r", "dispatch": {"effort": "extreme"}}],
    }
    assert any("effort" in p for p in repos_mod.validate_registry_data(base))
    base["repos"][0]["dispatch"] = {"model": ""}
    assert any("model" in p for p in repos_mod.validate_registry_data(base)), (
        "an empty-string model is not null"
    )
    base["repos"][0]["dispatch"] = {"effort": "high", "model": "claude-opus-4-8"}
    assert repos_mod.validate_registry_data(base) == []
