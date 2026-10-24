"""The MCP server over the SDK's in-memory client: tool shape, the ops behind them, session
resolution, the worker profile, and the --tools filter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import Client

import seeded as seeding_helpers
from agent_cli.core import AgentError
from agent_cli.mcp import server as mcp_server
from agent_cli.mcp.tools import Ctx, Op, ToolSpec

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("notes_remote")]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def _mcp_template(
    tmp_path_factory: pytest.TempPathFactory, _root_template: Path, _notes_template: Path
) -> Path:
    """The seeded root, built once per module and worker and copied per test."""
    base = tmp_path_factory.mktemp("mcp-template")
    root = seeding_helpers.base_root(base, _root_template, _notes_template)
    with seeding_helpers.seeding(root) as call:
        call(["feature", "new", "x", "--title", "X"])
        call(["task", "add", "1", "write the parser"])
        call(["task", "add", "1", "write the tests"])
        call(["feature", "use", "1", "--session-id", "s1"])
    return base


@pytest.fixture
def seeded(tmp_root: Path, _mcp_template: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    seeding_helpers.install(_mcp_template, tmp_root.parent)
    monkeypatch.chdir(tmp_root)
    return tmp_root


def _text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


# ---------------------------------------------------------------- 1. shape


async def test_list_tools_shape_and_enums(seeded: Path):
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == set(mcp_server.MANAGER_TOOLS)
    # The manager's tool budget is ~35 (ADR-0007). Every slot has to earn its context.
    # `journal_collect` was dropped: it hardcoded the markdown rendering.
    # `/journal` reads the bundle through `bin/agent journal collect --json` instead.
    # No transcript ever called the tool. The integration tools left: an integration is an
    # MCP server the owner configures, not a slot in this server. `lesson` joined: one routed tool
    # for the lesson queue, which /lesson and /retro write from both clients. `peer` joined in
    # One routed tool for manager-to-manager messages and paired plan review, after a
    # manager with no peer tool reached for SendMessage. `harness` joined: one
    # routed tool for the CLI-only commands a model runs, so models never run `bin/agent`.
    # `owner_question` joined: no event marks a plain-text question to the owner.
    # `raw_adopt` left: `feature_use` already adopts the calling session's captures.
    # `adr_list` left with it: no transcript called it; adrs/README.md and decisions/README.md list them.
    # `report` and `workflow_record` left  phase 6b: report was called 7 times, and
    # workflow_record only fed it. `dispatch_read` joined: the read-only dispatch
    # actions, annotated read-only, so a Codex manager's auto-review stops asking about polls.
    # `task_supersede` and `task_check` joined  P4: the only writer of `- [-]`, and the
    # read-only pickup check that `feature_use` and dispatch also run.
    # A plan adds strict config validation through the read-only repos_validate tool.
    # Audit 10 plan.md:113,115 (K1/K3) retire status_show and feature_current.
    # `request` joined: one routed tool over request records in the feature store.
    # `reconcile` joined: the typed ledger of results and findings that need a decision.
    assert len(tools) == 30, sorted(tools)
    assert {"status_show", "feature_current"}.isdisjoint(tools)
    gone = {
        "raw_adopt",
        "adr_list",
        "report",
        "workflow_record",
    }
    assert not gone & set(tools)
    assert "journal_collect" not in tools
    assert "feature_default" not in tools
    assert "dispatch" in tools and "handoff_note" not in tools
    log = tools["log"]
    props = log.input_schema["properties"]
    assert props["kind"]["enum"] == [
        "note",
        "decision",
        "deviation",
        "surprise",
        "measurement",
        "dead-end",
        "blocker",
    ]
    assert props["feature"]["default"] == "current"
    assert "ctx" not in props
    assert set(log.input_schema["required"]) == {"kind", "text"}
    assert log.description is not None and "bin/agent" not in log.description
    assert log.description is not None and "newlines are safe" in log.description
    task_done_description = tools["task_done"].description
    assert task_done_description is not None and "before telling the owner" in task_done_description
    for name, t in tools.items():
        assert t.output_schema is not None, name
        assert t.output_schema.get("type") == "object", name
        assert t.title == mcp_server.MANAGER_TOOLS[name].title
    feature_list_annotations = tools["feature_list"].annotations
    assert feature_list_annotations is not None and feature_list_annotations.read_only_hint is True
    feature_new_annotations = tools["feature_new"].annotations
    assert feature_new_annotations is not None and feature_new_annotations.read_only_hint is False
    task_done_annotations = tools["task_done"].annotations
    assert task_done_annotations is not None and task_done_annotations.idempotent_hint is True
    assert tools["feature_close"].input_schema["properties"]["status"]["enum"] == ["done", "abandoned"]
    assert tools["feature_new"].input_schema["properties"]["use"]["default"] is True


# ---------------------------------------------------------------- 2. feature_new + log


async def test_feature_new_and_log_with_hostile_text(seeded: Path):
    server = mcp_server.build_server(seeded, "manager")
    text = 'it "works" `now`\nline two; $HOME && rm -rf / || true\n[!note] not a callout'
    async with Client(server) as client:
        r = await client.call_tool(
            "feature_new", {"slug": "mcp-up", "title": "MCP is up", "session_id": "s7", "repos": ["alpha"]}
        )
        assert r.is_error is False, _text(r)
        assert r.structured_content["id"] == 2
        assert r.structured_content["repos"] == ["alpha-api"]
        assert r.structured_content["session_id"] == "s7"
        assert r.structured_content["summary"] == "created features/002-mcp-up (session s7 mapped)"
        assert r.structured_content["opened"] == "2026-09-03"
        r = await client.call_tool("log", {"kind": "surprise", "text": text, "session_id": "s7"})
        assert r.is_error is False, _text(r)
        assert r.structured_content["feature"] == 2
        assert r.structured_content["text"] == text
        assert r.structured_content["summary"] == "002 2026-09-03 14:05 — surprise"
    log_md = (seeded / "features/002-mcp-up/log.md").read_text()
    assert "> [!surprise] 2026-09-03 14:05" in log_md
    assert '> it "works" `now`' in log_md
    assert "> line two; $HOME && rm -rf / || true" in log_md
    assert "> \\[!note] not a callout" in log_md
    record = json.loads((seeded / ".agent/sessions/s7.json").read_text())
    assert record["feature"]["id"] == 2


async def test_list_outputs_become_items_and_count(seeded: Path):
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool("feature_list", {})
        assert r.structured_content["count"] == 1
        assert r.structured_content["items"][0]["slug"] == "x"
        assert "x" in r.structured_content["summary"]
        r = await client.call_tool("repos_list", {})
        assert [i["short"] for i in r.structured_content["items"]] == ["alpha", "beta"]


# ---------------------------------------------------------------- 3. task_add / task_done


async def test_task_add_and_done_idempotent(seeded: Path):
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool("task_add", {"text": "write the docs", "feature": "1"})
        assert r.structured_content["task"] == 3
        r = await client.call_tool("task_done", {"task": "docs", "feature": "1", "note": "abc123"})
        assert r.structured_content["changed"] is True
        r = await client.call_tool("task_done", {"task": "3", "feature": "1"})
        assert r.structured_content["changed"] is False
        assert "already done" in r.structured_content["summary"]
        r = await client.call_tool("task_list", {"feature": "1", "open_only": True})
        assert [t["index"] for t in r.structured_content["tasks"]] == [1, 2]
    assert "- [x] write the docs (abc123)" in (seeded / "features/001-x/plan.md").read_text()


async def test_raw_preserve_copies_referenced_transcripts(seeded: Path, run):
    """`raw_preserve` (plan 3.5, exposed standalone) runs the same copy `feature_close` already
    runs on its own. This proves the MCP tool reaches the same `ops.raw_preserve`."""
    transcript = seeded.parent / "t.jsonl"
    transcript.write_text("hi\n", encoding="utf-8")
    run(
        ["capture", "--kind", "stop", "--stdin"],
        stdin=json.dumps({"session_id": "s1", "hook_event_name": "Stop", "transcript_path": str(transcript)}),
    )
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool("raw_preserve", {"feature": "1"})
        assert r.structured_content["copied"] == [".agent/raw/001-x/transcripts/t.jsonl"]
    assert (seeded / ".agent/raw/001-x/transcripts/t.jsonl").exists()


# ---------------------------------------------------------------- 4. errors


async def test_unknown_feature_is_a_tool_error(seeded: Path):
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool("feature_show", {"feature": "nope"})
        assert r.is_error is True
        assert "no feature matching 'nope'" in _text(r)
        r = await client.call_tool("log", {"kind": "vibes", "text": "x", "feature": "1"})
        assert r.is_error is True  # enum enforced by the schema
        r = await client.call_tool("task_done", {"task": "write", "feature": "1"})
        assert r.is_error is True
        assert "matches 2 tasks" in _text(r)


# ---------------------------------------------------------------- 5. session resolution


async def test_session_resolution(seeded: Path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool("feature_show", {})
        assert r.is_error is True
        assert "session_id" in _text(r)
        r = await client.call_tool("feature_use", {"feature": "1"})
        assert r.is_error is True
        assert "session_id" in _text(r)
        # a named feature never needs a session
        r = await client.call_tool("task_list", {"feature": "1"})
        assert r.is_error is False
        # explicit wins
        r = await client.call_tool("feature_show", {"session_id": "s1"})
        assert r.structured_content["slug"] == "x"
    # the by-pid file the session-start hook writes
    monkeypatch.setenv("CLAUDE_PID", "4242")
    (seeded / ".agent/sessions/by-pid").mkdir(parents=True, exist_ok=True)
    (seeded / ".agent/sessions/by-pid/4242").write_text("s1\n")
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool("feature_show", {})
        assert r.is_error is False, _text(r)
        assert r.structured_content["slug"] == "x"
        r = await client.call_tool("session_get", {})
        assert r.structured_content["session_id"] == "s1"
    # env var is the next fallback
    monkeypatch.delenv("CLAUDE_PID")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool("feature_show", {})
        assert r.structured_content["slug"] == "x"


async def test_current_mcp_requires_a_mapped_session_despite_a_stale_default_file(
    seeded: Path, run, monkeypatch
):
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    (seeded / ".agent/default-feature").write_text("1\n")
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool("task_list", {})
        assert r.is_error is True
        assert "name the feature explicitly" in _text(r)
        r = await client.call_tool("task_list", {"feature": "1"})
        assert r.is_error is False, _text(r)
        assert r.structured_content["feature"] == 1

    run(["session", "set", "--session-id", "unmapped"])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "unmapped")
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool("task_list", {})
        assert r.is_error is True
        assert "name a feature explicitly" in _text(r)


# ---------------------------------------------------------------- 5b. lease tool


async def test_lease_tool_four_actions(seeded: Path):
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool(
            "lease", {"action": "take", "area": "vault:src", "note": "mcp test", "session_id": "s1"}
        )
        assert r.is_error is False, _text(r)
        assert r.structured_content["taken"] is True
        assert r.structured_content["lease"]["session_id"] == "s1"
        assert r.structured_content["lease"]["note"] == "mcp test"
        r = await client.call_tool("lease", {"action": "list"})
        assert r.is_error is False, _text(r)
        areas = {i["area"]: i for i in r.structured_content["items"]}
        assert areas["vault:src"]["status"] == "live" and areas["vault:src"]["session_id"] == "s1"
        r = await client.call_tool(
            "lease", {"action": "wait", "area": "vault:src", "timeout": 1, "session_id": "s1"}
        )
        assert r.is_error is False, _text(r)
        assert r.structured_content["taken"] is True  # already held by this session: returns at once
        r = await client.call_tool("lease", {"action": "release", "area": "vault:src", "session_id": "s1"})
        assert r.is_error is False, _text(r)
        assert r.structured_content["released"] is True
        r = await client.call_tool("lease", {"action": "release", "area": "vault:src", "session_id": "s1"})
        assert r.structured_content["released"] is False


async def test_lease_tool_requires_area_and_session(seeded: Path):
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool("lease", {"action": "take"})
        assert r.is_error is True
        assert "needs `area`" in _text(r)
        r = await client.call_tool("lease", {"action": "take", "area": "vault:src"})
        assert r.is_error is True
        assert "session_id" in _text(r)


# ---------------------------------------------------------------- 6. worker profile


@pytest.fixture
def worker_env(seeded: Path, monkeypatch):
    monkeypatch.setenv("AGENT_FEATURE", "1")
    monkeypatch.setenv("AGENT_DISPATCH_ID", "d-1")
    monkeypatch.setenv("AGENT_DISPATCH_TASKS", "2")
    return seeded


async def test_worker_profile(worker_env: Path):
    server = mcp_server.build_server(worker_env, "worker")
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"log"}  # a worker never ticks a manager-owned task
        assert tools["log"].input_schema["properties"]["kind"]["enum"] == [
            "dead-end",
            "surprise",
            "measurement",
            "blocker",
        ]
        assert "feature" not in tools["log"].input_schema["properties"]
        assert "session_id" not in tools["log"].input_schema["properties"]
        r = await client.call_tool("log", {"kind": "surprise", "text": "the build needs node 22"})
        assert r.is_error is False, _text(r)
        assert r.structured_content["text"] == "the build needs node 22 (dispatch d-1)"
        r = await client.call_tool("log", {"kind": "note", "text": "x"})
        assert r.is_error is True
        r = await client.call_tool("task_done", {"task": "1"})
        assert r.is_error is True
    plan = (worker_env / "features/001-x/plan.md").read_text()
    assert "- [ ] write the parser" in plan and "- [ ] write the tests" in plan
    assert "(dispatch d-1)" in (worker_env / "features/001-x/log.md").read_text()


def test_worker_refuses_to_build_half_configured(seeded: Path, monkeypatch, run):
    monkeypatch.delenv("AGENT_FEATURE", raising=False)
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    with pytest.raises(AgentError, match="AGENT_FEATURE, AGENT_DISPATCH_ID"):
        mcp_server.build_server(seeded, "worker")
    monkeypatch.setenv("AGENT_FEATURE", "1")
    monkeypatch.setenv("AGENT_DISPATCH_ID", "d-1")
    monkeypatch.setenv("AGENT_DISPATCH_TASKS", "two")
    with pytest.raises(AgentError, match="AGENT_DISPATCH_TASKS"):
        mcp_server.build_server(seeded, "worker")
    monkeypatch.setenv("AGENT_DISPATCH_TASKS", "1, 2")
    mcp_server.build_server(seeded, "worker")
    run(["task", "done", "1", "1"])
    run(["task", "done", "1", "2"])  # a feature with open tasks cannot close
    assert run(["feature", "close", "1"]).code == 0
    with pytest.raises(AgentError, match="is done"):
        mcp_server.build_server(seeded, "worker")


# ---------------------------------------------------------------- 7. --tools filter


async def test_tools_filter(seeded: Path):
    server = mcp_server.build_server(seeded, "manager", ["log", "task_done"])
    async with Client(server) as client:
        assert {t.name for t in (await client.list_tools()).tools} == {"log", "task_done"}
    with pytest.raises(AgentError, match="unknown tool"):
        mcp_server.build_server(seeded, "manager", ["log", "bogus"])
    with pytest.raises(AgentError, match="profile"):
        mcp_server.build_server(seeded, "auditor")


def test_registration_tables_are_extensible(seeded: Path, monkeypatch):
    def ping(ctx: Ctx, word: str = "pong") -> Op:
        return Op({"word": word}, f"said {word}")

    monkeypatch.setitem(mcp_server.MANAGER_TOOLS, "ping", ToolSpec(ping, "Ping", "test tool", read_only=True))
    server = mcp_server.build_server(seeded, "manager", ["ping"])

    async def go():
        async with Client(server) as client:
            r = await client.call_tool("ping", {})
            assert r.structured_content == {"word": "pong", "summary": "said pong"}

    import anyio

    anyio.run(go)


# ---------------------------------------------------------------- dispatch: branch/base/collect


async def test_dispatch_tool_takes_branch_and_base_like_the_cli(seeded: Path, run, fake_claude):
    """T5 item 6: the CLI's `dispatch new` has had `--branch`/`--base` all along, but the MCP
    tool did not. An MCP caller could not name either, and silently got the derived
    `agent/<feature>` branch off `origin/<default>`."""
    run(["feature", "set", "1", "--add-repo", "alpha"])
    (seeded / "harness").mkdir(exist_ok=True)
    (seeded / "harness" / "worker-contract.md").write_text("# contract\n", encoding="utf-8")
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["dispatch"]
        props = tool.input_schema["properties"]
        assert "branch" in props and "base" in props
        assert "collect" in props["action"]["enum"]
        r = await client.call_tool(
            "dispatch",
            {
                "action": "new",
                "repo": "alpha",
                "feature": "1",
                "prompt": "add a line",
                "branch": "agent/hand-picked",
                "base": "origin/main",
                "dry_run": True,
                "session_id": "s1",
            },
        )
    assert r.is_error is False, _text(r)
    assert r.structured_content["branch"] == "agent/hand-picked"
    assert r.structured_content["base"] == "origin/main"


async def test_mcp_dispatch_launch_requires_manager_session(seeded: Path, monkeypatch):
    """A Codex manager launched through this tool with an explicit feature and no
    `session_id`. The record got `launched_by_session: null`, `refresh_tab` found no worker for
    the tab, and the owner closed a tab that looked idle while its worker ran. `new` now needs a
    manager session whether or not the feature is named."""
    from agent_cli import ops

    seen: list[str | None] = []

    def fake_new(root, **kw):
        seen.append(kw.get("session_id"))
        return Op({}, "launched")

    monkeypatch.setattr(ops, "dispatch_new", fake_new)
    server = mcp_server.build_server(seeded, "manager")
    args = {"action": "new", "repo": "alpha", "feature": "1", "prompt": "add a line"}
    async with Client(server) as client:
        refused = await client.call_tool("dispatch", args)
        passed = await client.call_tool("dispatch", {**args, "session_id": "s1"})
    assert refused.is_error is True
    assert "no session id" in _text(refused)
    assert passed.is_error is False, _text(passed)
    assert seen == ["s1"]


async def test_dispatch_tool_collects(seeded: Path, run, fake_claude, monkeypatch):
    """The other half of T5 item 6: `ops.dispatch_collect` has existed since T4, but the tool
    had no action that reached it. An MCP-only caller could not reconcile a dispatch whose
    supervisor died. That is the exact situation `collect` exists for."""
    from agent_cli import dispatch as dispatch_mod
    from dispatch_helpers import _set_alpha_dispatch

    run(["feature", "set", "1", "--add-repo", "alpha"])
    _set_alpha_dispatch(
        seeded,
        verify=[{"command": "git diff --exit-code HEAD --", "timeout_seconds": 3}],
        verify_timeout_seconds=5,
    )
    (seeded / "harness").mkdir(exist_ok=True)
    (seeded / "harness" / "worker-contract.md").write_text("# contract\n", encoding="utf-8")
    monkeypatch.setattr(
        dispatch_mod.supervisor,
        "popen",
        type("P", (), {"pid": 2**22, "__init__": lambda self, *a, **k: None}),
    )
    dispatch_mod.new(seeded, repo="alpha", feature="1", prompt="add a line", watch=False)
    server = mcp_server.build_server(seeded, "manager")
    async with Client(server) as client:
        r = await client.call_tool("dispatch", {"action": "collect", "dispatch_id": "001-mac-01"})
    assert r.is_error is False, _text(r)
    assert [i["id"] for i in r.structured_content["items"]] == ["001-mac-01"]
    assert r.structured_content["items"][0]["state"] == "failed"
    assert "no result" in dispatch_mod.load(seeded, "001-mac-01")["error"]


async def test_dispatch_resume_dry_run_refuses_before_read_or_operation(
    seeded: Path, run, fake_claude, monkeypatch
):
    from agent_cli import __file__ as agent_cli_file
    from agent_cli import dispatch as dispatch_mod
    from agent_cli import ops
    from agent_cli.dispatch import lifecycle
    from dispatch_helpers import _new_fast

    assert Path(agent_cli_file).resolve().is_relative_to(Path(__file__).resolve().parents[1])
    run(["feature", "set", "1", "--add-repo", "alpha"])
    contract = seeded / "harness" / "worker-contract.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text("# contract\n", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "start", lambda root, d, watch: d)
    original = _new_fast(seeded)
    dispatch_mod.save(seeded, {**original, "state": "question"})
    before_record = dispatch_mod.load(seeded, original["id"])
    before_prompts = {path: path.read_bytes() for path in (seeded / ".agent/dispatches").glob("*.prompt*")}
    before_handoff = (seeded / original["handoff"]).read_bytes()
    monkeypatch.setattr(
        ops, "dispatch_resume", lambda *args, **kwargs: pytest.fail("resume operation invoked")
    )
    monkeypatch.setattr(lifecycle, "load", lambda *args, **kwargs: pytest.fail("resume read the record"))
    args = {"action": "resume", "dispatch_id": original["id"], "answer": "continue", "dry_run": True}
    async with Client(mcp_server.build_server(seeded, "manager")) as client:
        result = await client.call_tool("dispatch", args)
    monkeypatch.undo()

    message = _text(result)
    assert result.is_error and "resume" in message and "dry_run" in message
    assert dispatch_mod.load(seeded, original["id"]) == before_record
    assert {
        path: path.read_bytes() for path in (seeded / ".agent/dispatches").glob("*.prompt*")
    } == before_prompts
    assert (seeded / original["handoff"]).read_bytes() == before_handoff
    assert len(fake_claude.calls()) == 0


async def test_dispatch_code_review_dry_run_refuses_before_lookup_and_launch(
    seeded: Path, fake_claude, monkeypatch
):
    from agent_cli import ops

    def snapshot() -> dict[str, bytes]:
        return {str(p): p.read_bytes() for p in (seeded / ".agent").rglob("*") if p.is_file()}

    seen: list[dict] = []

    def fake_review(root, **kw):
        seen.append(kw)
        return Op({}, "reviewed")

    monkeypatch.setattr(
        ops, "dispatch_code_review", lambda *a, **k: pytest.fail("code-review operation invoked")
    )
    before = snapshot()
    args = {"action": "code-review", "dispatch_id": "999-mac-99", "session_id": "s1"}
    async with Client(mcp_server.build_server(seeded, "manager")) as client:
        refused = await client.call_tool("dispatch", {**args, "dry_run": True})
        monkeypatch.setattr(ops, "dispatch_code_review", fake_review)
        normal = await client.call_tool("dispatch", args)

    message = _text(refused)
    assert refused.is_error and "code-review" in message and "dry_run" in message
    assert "999-mac-99" not in message
    assert snapshot() == before
    assert len(fake_claude.calls()) == 0
    assert normal.is_error is False, _text(normal)
    assert [k["dispatch_id"] for k in seen] == ["999-mac-99"]
