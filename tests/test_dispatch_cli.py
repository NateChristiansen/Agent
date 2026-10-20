"""CLI wiring for dispatch: the argparse layer over dispatch.py, the hooks, and the
handoffs protect rule. Behavior of the runner itself is covered in test_dispatch.py."""

import json
import shutil
from pathlib import Path

import pytest

from agent_cli import frontmatter, launcher

pytestmark = pytest.mark.usefixtures("notes_remote")


def _feature_with_alpha(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X", "--repo", "alpha"])
    run(["task", "add", "1", "do the thing"])
    contract = tmp_root / "harness" / "worker-contract.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text("# worker contract (test stub)\n")


def test_dispatch_preview_writes_order_without_allocating_worker(tmp_root: Path, run, fake_claude):
    _feature_with_alpha(tmp_root, run)
    r = run(
        [
            "--json",
            "dispatch",
            "new",
            "alpha",
            "--feature",
            "1",
            "--task",
            "1",
            "--prompt",
            "add a line",
            "--dry-run",
        ]
    )
    assert r.code == 0, r.err
    assert r.json["repo"] == "alpha-api"
    assert "branch" in r.json and "budget_usd" in r.json
    assert not list((tmp_root / ".agent/dispatches").glob("*.json"))
    assert not (tmp_root / "features/001-x/handoffs").exists()
    assert (tmp_root / r.json["work_order"]).read_text(encoding="utf-8") == r.json["prompt"]
    assert r.json["work_order_url"].startswith("obsidian://open?")
    r = run(["dispatch", "new", "alpha", "--feature", "1", "--prompt", "add a line", "--dry-run"])
    assert "dispatch preview" in r.out
    assert "  budget: $1.00 (repo default, dispatch.max_budget_usd)" in r.out


def test_dispatch_new_dry_run_cli_states_the_explicit_budget(tmp_root: Path, run, fake_claude):
    _feature_with_alpha(tmp_root, run)
    r = run(
        [
            "dispatch",
            "new",
            "alpha",
            "--feature",
            "1",
            "--prompt",
            "x",
            "--budget",
            "3",
            "--reason",
            "why",
            "--dry-run",
        ]
    )
    assert r.code == 0, r.err
    assert "  budget: $3.00 (explicit budget_usd; repo default $1.00; reason: why)" in r.out


def test_dispatch_resume_cli_records_owner_answer(tmp_root: Path, run, fake_claude):
    from agent_cli import dispatch as dispatch_mod
    from dispatch_helpers import envelope, one

    _feature_with_alpha(tmp_root, run)
    fake_claude.scenario([one(envelope("question", open_questions=["continue?"])), one(envelope())])
    initial = dispatch_mod.new(
        tmp_root,
        repo="alpha",
        feature="1",
        prompt="Original work order",
        watch=True,
        no_checks=True,
    )

    result = run(["dispatch", "resume", initial["id"], "--answer", "Owner answer", "--watch"])

    assert result.code == 0, result.err
    current = dispatch_mod.load(tmp_root, initial["id"])
    assert current["thread"][-2]["from"] == "owner"
    assert current["thread"][-2]["text"] == "Owner answer"
    assert "owner → worker" in (tmp_root / current["handoff"]).read_text(encoding="utf-8")
    assert len(fake_claude.calls()) == 2


def test_dispatch_new_dry_run_preview_reports_the_repo_lease(tmp_root: Path, run, fake_claude):
    """Item 4: the preview says whether `repo:alpha` is held, without taking it."""
    from agent_cli import leases

    _feature_with_alpha(tmp_root, run)
    r = run(["dispatch", "new", "alpha", "--feature", "1", "--prompt", "add a line", "--dry-run"])
    assert "repo lease: free" in r.out
    assert leases.get(tmp_root, "repo:alpha") is None
    import os

    leases.take(tmp_root, "repo:alpha", session_id="someone-else", pid=os.getpid())
    r = run(["dispatch", "new", "alpha", "--feature", "1", "--prompt", "add a line", "--dry-run"])
    assert "repo lease: held by someone-else (live)" in r.out


def test_dispatch_new_dry_run_preview_shows_the_prompt_options_and_preflight(
    tmp_root: Path, run, fake_claude
):
    """`_preview_text` used to read `prompt_lines`, a key `new()` never emitted (it writes
    `prompt_head`).

    So the work order text never showed up in a preview the owner approves (1.2/0). The fix then
    printed twelve lines, so what they approved was the nonce block and half the task. T5 item 5
    prints the whole composed order, first line to last. This test also covers the preflight
    table added alongside the fix, and the options block that replaced the argv dump.
    `cli_path` has to be visible: a worker running the SDK's bundled CLI instead of the PATH
    one is what ADR-0009 says must not happen.
    """
    _feature_with_alpha(tmp_root, run)
    prompt = "add a line\n\nRun this first:\n\n```sh\npnpm test\n```"
    r = run(["--json", "dispatch", "new", "alpha", "--feature", "1", "--prompt", prompt, "--dry-run"])
    assert r.code == 0, r.err
    assert r.json["preflight"] == [
        {"command": "pnpm test", "verdict": "no-rule", "rule": None, "source": "work-order"}
    ]
    assert r.json["options"]["cli_path"] == shutil.which("claude")
    assert r.json["options"]["mcp_servers"] == ["agent"]
    assert "nonce" in r.json["options"]["json_schema"]
    work_order, line_count = r.json["prompt"], r.json["prompt_lines"]
    assert line_count == len(work_order.splitlines()) > 12
    # Plan §A3 (plan.md:60-64): select the prepared identity to render the same artifact.
    r = run(
        [
            "dispatch",
            "new",
            "alpha",
            "--feature",
            "1",
            "--prompt",
            prompt,
            "--preview-id",
            r.json["preview_id"],
            "--dry-run",
        ]
    )
    assert r.code == 0, r.err
    assert "    add a line" in r.out, "the composed prompt text belongs in the preview"
    assert f"    {work_order.splitlines()[0]}" in r.out
    assert f"    {work_order.rstrip().splitlines()[-1]}" in r.out, "the LAST line too, not a head"
    assert r.out.count("─" * 68) == 2, "the work order is fenced by rule lines"
    assert f"{line_count} lines" in r.out
    assert "preflight" in r.out
    assert "no-rule" in r.out and "pnpm test" in r.out
    assert "cli_path:" in r.out


def test_dispatch_new_allow_unmatched_round_trip(tmp_root: Path, run, monkeypatch, fake_claude):
    """CLI wiring only: `--allow-unmatched` has to reach `dispatch.new()`. The refusal itself
    (and that it goes away with this flag) is covered in test_dispatch.py."""
    from agent_cli import dispatch as dispatch_mod

    _feature_with_alpha(tmp_root, run)
    captured: dict = {}
    original = dispatch_mod.new

    def spy(root, **kw):
        captured.update(kw)
        return original(root, **kw)

    monkeypatch.setattr(dispatch_mod, "new", spy)
    r = run(
        [
            "--json",
            "dispatch",
            "new",
            "alpha",
            "--feature",
            "1",
            "--prompt",
            "add a line",
            "--allow-unmatched",
            "--dry-run",
        ]
    )
    assert r.code == 0, r.err
    assert captured["allow_unmatched"] is True
    r = run(["--json", "dispatch", "new", "alpha", "--feature", "1", "--prompt", "add a line", "--dry-run"])
    assert r.code == 0, r.err
    assert captured["allow_unmatched"] is False


def test_dispatch_new_effort_and_choice_reason_round_trip(tmp_root: Path, run, fake_claude):
    _feature_with_alpha(tmp_root, run)
    r = run(
        [
            "--json",
            "dispatch",
            "new",
            "alpha",
            "--feature",
            "1",
            "--prompt",
            "add a line",
            "--effort",
            "high",
            "--choice-reason",
            "cross-cutting change, unfamiliar repo",
            "--dry-run",
        ]
    )
    assert r.code == 0, r.err
    assert r.json["effort"] == "high"
    assert r.json["choice_reason"] == "cross-cutting change, unfamiliar repo"
    r = run(
        [
            "dispatch",
            "new",
            "alpha",
            "--feature",
            "1",
            "--prompt",
            "add a line",
            "--effort",
            "vibes",
            "--dry-run",
        ]
    )
    assert r.code == 2, r.err  # argparse rejects an unlisted --effort choice


def test_dispatch_new_requires_repo_in_feature(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    r = run(["dispatch", "new", "alpha", "--feature", "1", "--prompt", "hi", "--dry-run"])
    assert r.code == 2
    assert "repos" in r.err


def test_dispatch_new_prompt_sources(tmp_root: Path, run, tmp_path: Path, fake_claude):
    _feature_with_alpha(tmp_root, run)
    pf = tmp_path / "order.md"
    pf.write_text("from a file")
    r = run(["--json", "dispatch", "new", "alpha", "--feature", "1", "--prompt-file", str(pf), "--dry-run"])
    assert r.code == 0 and r.json["goal"] == "from a file"
    r = run(
        ["--json", "dispatch", "new", "alpha", "--feature", "1", "--stdin", "--dry-run"], stdin="from stdin"
    )
    assert r.code == 0 and r.json["goal"] == "from stdin"
    r = run(["dispatch", "new", "alpha", "--feature", "1", "--dry-run"])
    assert r.code == 2  # no prompt at all


def test_dispatch_list_and_show_unknown(tmp_root: Path, run):
    r = run(["--json", "dispatch", "list"])
    assert r.code == 0 and r.json == []
    r = run(["dispatch", "show", "999-99"])
    assert r.code == 1


def test_hooks_registered_and_inbox_quiet_when_empty(tmp_root: Path, run):
    from agent_cli.hooks import NAMES

    assert "dispatch-inbox" in NAMES and "worker-stop" in NAMES
    r = run(["hook", "dispatch-inbox"], stdin=json.dumps({"session_id": "s", "prompt": "hi"}))
    assert r.code == 0 and r.out == ""
    r = run(["hook", "worker-stop"], stdin=json.dumps({"session_id": "nobody", "hook_event_name": "Stop"}))
    assert r.code == 0 and r.out == ""


def test_protect_denies_handoff_edits(tmp_root: Path, run):
    """2026-09-08 (plan 2.1): the native `Edit(/features/*/handoffs/**)` rule in
    .claude/settings.json now denies an Edit into handoffs/, not this hook. See
    test_hooks.py::test_native_rules_cover_generated_and_guardrail_paths. This hook still
    denies the Bash-based write that native rules can't express."""
    payload = json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": "features/001-x/handoffs/01-alpha.md"}}
    )
    r = run(["hook", "protect-generated"], stdin=payload)
    assert r.out == ""
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "echo x >> features/001-x/handoffs/01-alpha.md"}}
    )
    r = run(["hook", "protect-generated"], stdin=payload)
    assert json.loads(r.out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_feature_close_runs_cleanup(tmp_root: Path, run, monkeypatch):
    from agent_cli import dispatch as dispatch_mod

    calls = []
    monkeypatch.setattr(
        dispatch_mod,
        "cleanup",
        lambda root, *, feature, force, session_id: (
            calls.append((feature, force, session_id)) or {"removed": [], "skipped": []}
        ),
    )
    run(["feature", "new", "x", "--title", "X"])
    r = run(["feature", "close", "1", "--session-id", "closer"])
    assert r.code == 0
    assert calls == [("1", False, "closer")]
    meta, _ = frontmatter.load(tmp_root / "features/001-x/plan.md")
    assert meta["status"] == "done"


def test_worker_settings_allow_git_with_and_without_dash_c(tmp_root: Path):
    from agent_cli import dispatch as dispatch_mod
    from agent_cli import repos as repos_mod

    repo = repos_mod.load_registry(tmp_root).require("alpha")
    settings = dispatch_mod.build_settings(tmp_root, repo, worktree="/wt/alpha/001-x")
    allow = settings["permissions"]["allow"]
    assert "Bash(git status:*)" in allow and "Bash(git commit:*)" in allow and "Bash(git rm:*)" in allow
    assert "Bash(git -C /wt/alpha/001-x status:*)" in allow
    assert "Bash(git -C /wt/alpha/001-x commit:*)" in allow
    assert "Bash(git -C /wt/alpha/001-x push:*)" not in allow
    assert "Bash(python3:*)" in allow and "WebFetch" in allow


def test_worker_settings_allow_restore_and_checkout_dash_dash(tmp_root: Path):
    """Plain `git restore <path>` discards the worker's own uncommitted edits to that path,
    not just what is staged. That IS the intended use once GIT_VERBS covers `restore` (not
    `restore --staged`) and `checkout --` (1.7). `git checkout <branch>` stays unlisted: a
    headless worker cannot answer an ask, so unlisted is a deny."""
    from agent_cli import dispatch as dispatch_mod
    from agent_cli import repos as repos_mod

    repo = repos_mod.load_registry(tmp_root).require("alpha")
    settings = dispatch_mod.build_settings(tmp_root, repo, worktree="/wt/alpha/001-x")
    allow = settings["permissions"]["allow"]
    deny = settings["permissions"]["deny"]
    assert "Bash(git restore:*)" in allow
    assert "Bash(git checkout --:*)" in allow
    assert "Bash(git -C /wt/alpha/001-x restore:*)" in allow
    assert "Bash(git -C /wt/alpha/001-x checkout --:*)" in allow
    assert "Bash(git restore --staged:*)" not in allow
    assert "Bash(git checkout -- *)" not in deny


def test_worker_settings_wire_the_precompact_hook(tmp_root: Path):
    """T5 item 9: the hook that re-injects the nonce and the return shape has to be in the
    settings json the worker launches against.

    T7 item 4 left it the ONLY command hook there. Stop and SessionEnd moved to in-process SDK
    callbacks. PreCompact did not: nothing exercised a real compaction, and losing the nonce
    is terminal. Unlike Stop, it fires once per compaction, so there is no per-turn re-exec to
    save by moving it.
    """
    from agent_cli import dispatch as dispatch_mod
    from agent_cli import repos as repos_mod

    repo = repos_mod.load_registry(tmp_root).require("alpha")
    hooks = dispatch_mod.build_settings(tmp_root, repo, worktree="/wt/alpha/001-x")["hooks"]
    first, rest = launcher.command_and_args(tmp_root)
    expected = " ".join([f'"{first}"', *rest, "hook", "precompact"])
    assert [h["command"] for g in hooks["PreCompact"] for h in g["hooks"]] == [expected]
    assert set(hooks) == {"PreCompact"}


def test_mcp_manager_profile_has_one_dispatch_tool(tmp_root: Path):
    """2.2: the six `dispatch_*` tools collapsed into one `dispatch` tool, action-routed.
    Net five fewer always-loaded tools on the manager profile."""
    from agent_cli.mcp.server import MANAGER_TOOLS, WORKER_TOOLS

    assert "dispatch" in MANAGER_TOOLS
    for name in (
        "dispatch_new",
        "dispatch_list",
        "dispatch_show",
        "dispatch_resume",
        "dispatch_cancel",
        "dispatch_attach_info",
    ):
        assert name not in MANAGER_TOOLS
    assert not any(n.startswith("dispatch") for n in WORKER_TOOLS)


def test_worker_settings_allow_gh_reads_and_deny_gh_writes(tmp_root: Path):
    """A worker addressing PR feedback must read the thread. It must never answer it:
    replying, resolving, and retitling are the owner's."""
    from agent_cli import dispatch as dispatch_mod
    from agent_cli import repos as repos_mod

    repo = repos_mod.load_registry(tmp_root).require("alpha")
    settings = dispatch_mod.build_settings(tmp_root, repo, worktree="/wt/alpha/001-x")
    allow = settings["permissions"]["allow"]
    deny = settings["permissions"]["deny"]
    # `Bash(gh api repos/:*)` matched nothing: a prefix rule has to end on a token boundary
    for read in ("Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr checks:*)", "Bash(gh api:*)"):
        assert read in allow, read
    assert not any(rule.startswith("Bash(gh api repos/") for rule in allow)
    # a PR branch moves under the worker when someone updates it from main
    for catch_up in ("Bash(git fetch:*)", "Bash(git merge:*)"):
        assert catch_up in allow, catch_up
    assert "Bash(git -C /wt/alpha/001-x fetch:*)" in allow
    for write in (
        "Bash(gh pr create:*)",
        "Bash(gh pr merge:*)",
        "Bash(gh pr comment:*)",
        "Bash(gh pr review:*)",
        "Bash(gh pr edit:*)",
        "Bash(gh pr ready:*)",
        "Bash(gh auth token:*)",
        "Bash(gh api -X:*)",
        "Bash(gh api * --method *)",
        "Bash(gh api * -f *)",
        "Bash(gh api graphql:*)",
    ):
        assert write in deny, write
    assert not any(rule.startswith("Bash(gh api") and rule in deny for rule in allow)


def test_preview_names_the_feature(tmp_root: Path, run, fake_claude):
    """The preview header printed `feature None` for every dispatch: the dry-run payload
    never carried the feature it was for."""
    _feature_with_alpha(tmp_root, run)
    r = run(["dispatch", "new", "alpha", "--feature", "1", "--prompt", "do the thing", "--dry-run"])
    assert r.code == 0, r.err
    assert "feature None" not in r.out
    assert "for feature 001 x" in r.out
    j = run(
        ["--json", "dispatch", "new", "alpha", "--feature", "1", "--prompt", "do the thing", "--dry-run"]
    ).json
    assert j["feature"] == 1 and j["feature_slug"] == "x"


# ------------------------------------------------------------------ the preview lives in ops (T4 item 2)


def test_the_dry_run_preview_is_the_ops_text_both_front_ends_print(tmp_root: Path, run, fake_claude):
    """`_preview_text` used to live in cli.py. A Bash caller saw the table, but an MCP caller
    got the bare dict with a one-line "preview only" summary instead. It is `ops.Op.text` now:
    the CLI prints it, and the MCP tool's text content is the same string (ADR-0005)."""
    import asyncio

    from mcp import Client

    from agent_cli import ops
    from agent_cli.mcp import server as mcp_server

    _feature_with_alpha(tmp_root, run)
    op = ops.dispatch_new(tmp_root, repo="alpha", feature="1", prompt="add a line", dry_run=True)
    assert op.text.startswith("dispatch preview —")
    assert op.text == ops.dispatch_preview_text(op.data)

    r = run(["dispatch", "new", "alpha", "--feature", "1", "--prompt", "add a line", "--dry-run"])
    assert r.out.splitlines()[0] == op.text.splitlines()[0]
    assert "dispatch preview" in r.out

    async def _tool_text() -> str:
        server = mcp_server.build_server(tmp_root, "manager")
        async with Client(server) as client:
            result = await client.call_tool(
                "dispatch",
                {
                    "action": "new",
                    "repo": "alpha",
                    "feature": "1",
                    "prompt": "add a line",
                    "dry_run": True,
                    "session_id": "s1",
                },
            )
        return "\n".join(getattr(c, "text", "") for c in result.content)

    assert "dispatch preview —" in asyncio.run(_tool_text())
