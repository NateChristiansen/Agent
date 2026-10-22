"""Request records: the shared numbered store, the compact template, the typed and CLI adapters,
completion refusals on every terminal route, promotion in place and the request-filtered views."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli import documents, features, frontmatter, lessons, ops, requests, sessions, status
from agent_cli.core import AgentError
from agent_cli.dispatch.prompt import plan_section
from agent_cli.hooks import session_blocks, session_budget
from agent_cli.mcp import coverage
from agent_cli.mcp import server as mcp_server
from agent_cli.mcp.tools import Ctx
from agent_cli.ops import requests as request_ops
from agent_cli.vault import live

pytestmark = pytest.mark.usefixtures("notes_remote")

ASK = "Review the parser change on branch fix/parser and say whether it is safe to land."


def _new(root: Path, slug: str = "parser-review", category: str = "review", **kw: Any) -> features.Feature:
    op = request_ops.request_new(
        root, slug=slug, title="Parser review", ask=ASK, category=category, session_id="s1", **kw
    )
    return features.resolve(root, str(op.data["id"]))


def _write_dispatch(root: Path, dispatch_id: str, feature: int, **fields: Any) -> None:
    record = {
        "id": dispatch_id,
        "feature": feature,
        "short": "alpha",
        "repo": "alpha-api",
        "state": "done",
        "attempt": 1,
        "branch": "b",
        "worktree": "w",
        "launched_by_session": "s1",
        "finished": "2026-09-03T14:00:00-06:00",
        "thread": [{"from": "manager", "text": "# Review it"}],
    }
    record.update(fields)
    base = root / ".agent" / "dispatches"
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _call(root: Path, **kw: Any) -> dict[str, Any]:
    """The typed tool, as the MCP server runs it."""
    spec = mcp_server.MANAGER_TOOLS["request"]
    return mcp_server.call_op(spec.fn, Ctx(root=root, profile="manager"), **kw)


# ---------------------------------------------------------------- identity and template


def test_requests_share_feature_numbering_and_map_the_manager(tmp_root: Path, run):
    assert run(["feature", "new", "first", "--title", "First"]).code == 0
    req = _new(tmp_root)
    assert run(["feature", "new", "third", "--title", "Third"]).code == 0
    assert (req.id, req.dir.name) == (2, "002-parser-review")
    assert features.resolve(tmp_root, "3").slug == "third"
    meta, _ = frontmatter.load(req.plan_path)
    assert meta["record_kind"] == "request" and meta["category"] == "review"
    assert meta["tags"] == ["request"] and meta["status"] == "active"
    assert req.is_request and req.label == "Request 002"
    assert not features.resolve(tmp_root, "1").is_request
    assert (sessions.get(tmp_root, "s1") or {})["feature"]["id"] == 2
    assert "> [!decision]" in req.log_path.read_text(encoding="utf-8")


def test_the_compact_template_passes_parsers_validation_and_worker_context(tmp_root: Path):
    req = _new(tmp_root)
    text = req.plan_path.read_bytes()
    _, body = documents.plan_frontmatter(text)
    for rendered in (False, True):
        assert set(documents.plan_sections(body, rendered=rendered)) == {"Tasks", "Handoffs"}
    assert features.list_tasks(req.body) == []
    assert ASK in plan_section(req.body, "Context")
    assert requests.ask_of(req) == ASK and requests.result_of(req) == ""
    assert req.body.startswith("# Request 001 — Parser review\n")
    body, index = features.add_task_at(req.body, "check the edge case")
    assert index == 1 and [t.text for t in features.list_tasks(body)] == ["check the edge case"]
    documents._protect_plan(text, text)  # pyright: ignore[reportPrivateUsage]


def test_feature_template_is_unchanged(tmp_root: Path, run):
    run(["feature", "new", "plain", "--title", "Plain"])
    f = features.resolve(tmp_root, "1")
    assert "record_kind" not in f.meta and f.meta["tags"] == ["feature"]
    assert "## Decisions" in f.body and "## Result" not in f.body and not f.is_request


def test_a_heading_in_the_ask_is_refused(tmp_root: Path):
    with pytest.raises(AgentError, match="cannot hold a heading"):
        request_ops.request_new(
            tmp_root, slug="x-y", title="X", ask="look\n## Tasks", category="ask", session_id="s1"
        )
    assert not features.feature_dirs(tmp_root)


def _edit_unchanged(root: Path, f: features.Feature) -> dict[str, Any]:
    """The document-editing preview of plan.md with its own bytes, as a manager's edit starts."""
    source = root / ".agent" / "scratch" / "plan.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(f.plan_path.read_bytes())
    return documents.edit(root, target=f"features/{f.dir.name}/plan.md", source=source)


def test_an_ask_or_result_that_would_break_document_editing_is_refused(tmp_root: Path):
    """A literal `<!--` reads as an unclosed comment to the protected-section scan, so the plan it
    landed in could no longer be edited. It is refused before anything is written."""
    with pytest.raises(AgentError, match="hide plan.md's Tasks or Handoffs"):
        request_ops.request_new(
            tmp_root,
            slug="html-review",
            title="X",
            ask="How does it handle `<!--`?",
            category="review",
            use=False,
        )
    assert not features.feature_dirs(tmp_root)
    op = request_ops.request_new(
        tmp_root, slug="html-review", title="X", ask="Is `<!-- x -->` stripped?", category="review", use=False
    )
    req = features.resolve(tmp_root, str(op.data["id"]))
    assert _edit_unchanged(tmp_root, req)["changed"] is False
    before = req.plan_path.read_bytes()
    with pytest.raises(AgentError, match="hide plan.md's Tasks or Handoffs"):
        request_ops.request_complete(tmp_root, request="1", result="It mishandles `<!--` openers.")
    assert req.plan_path.read_bytes() == before and features.resolve(tmp_root, "1").status == "active"
    request_ops.request_complete(tmp_root, request="1", result="It strips `<!-- x -->` correctly.")
    assert _edit_unchanged(tmp_root, features.resolve(tmp_root, "1"))["changed"] is False


def test_capture_is_proposed_and_keeps_the_mapping(tmp_root: Path, run):
    run(["feature", "new", "host", "--title", "Host", "--use", "--session-id", "s1"])
    op = request_ops.request_capture(
        tmp_root, slug="flaky-test", title="Flaky", ask="test_x flakes", category="bug", session_id="s1"
    )
    assert op.data["status"] == "proposed" and "not authorized to execute" in op.text
    assert (sessions.get(tmp_root, "s1") or {})["feature"]["id"] == 1
    with pytest.raises(AgentError, match="capture records a bug or an idea"):
        request_ops.request_capture(tmp_root, slug="nope-x", title="N", ask="a", category="review")
    shown = request_ops.request_show(tmp_root, request="2")
    assert shown.data["next_action"].startswith("captured: execute only when the owner asks")
    ops.feature_set(tmp_root, feature="2", status="active", reason="owner asked to fix it now")
    assert features.resolve(tmp_root, "2").status == "active"


# ---------------------------------------------------------------- completion


OWED = {
    "running": {"state": "running", "finished": None},
    "queued": {"state": "queued", "finished": None},
    "question": {"state": "question"},
    "blocked": {"state": "blocked"},
    "unread verdict": {"chat_pending": True},
    "unresolved branch": {
        "verdict_read": {"key": "1:0"},
        "git": {"commits": [{"sha": "a", "subject": "s"}], "files": ["x.py"]},
    },
    "verifier cleanup": {"verification_cleanup_pending": True, "actioned_at": "2026-09-03T15:00:00-06:00"},
    "worker cleanup": {"worker_cleanup_pending": True, "actioned_at": "2026-09-03T15:00:00-06:00"},
    "cleanup failed": {"stop_reason": "cleanup_failed", "actioned_at": "2026-09-03T15:00:00-06:00"},
}


def _routes(root: Path, run) -> dict[str, Any]:
    return {
        "request complete": lambda: request_ops.request_complete(root, request="1", result="safe to land"),
        "typed tool": lambda: _call(root, action="complete", request="1", result="safe to land"),
        "cli": lambda: run(["request", "complete", "1", "--result", "safe to land"]),
        "feature close": lambda: ops.feature_close(root, feature="1"),
        "feature close abandoned": lambda: ops.feature_close(root, feature="1", status="abandoned"),
        "feature set": lambda: ops.feature_set(root, feature="1", status="done"),
    }


@pytest.mark.parametrize("owed", sorted(OWED))
def test_every_terminal_route_refuses_while_a_dispatch_owes_work(tmp_root: Path, run, owed: str):
    from mcp.server.mcpserver.exceptions import ToolError

    req = _new(tmp_root)
    _write_dispatch(tmp_root, "001-01", 1, **OWED[owed])
    assert requests.unfinished_work(tmp_root, req), owed
    plan, log = req.plan_path.read_bytes(), req.log_path.read_bytes()
    for name, route in _routes(tmp_root, run).items():
        try:
            outcome = route()
        except (AgentError, ToolError) as exc:
            assert "001-01" in str(exc), (name, str(exc))
        else:
            assert getattr(outcome, "code", 0) != 0 and "001-01" in outcome.err, name
        assert req.plan_path.read_bytes() == plan, name
        assert req.log_path.read_bytes() == log, name
    assert features.resolve(tmp_root, "1").status == "active"


def test_an_open_task_and_a_missing_result_refuse_completion(tmp_root: Path):
    req = _new(tmp_root)
    ops.task_add(tmp_root, text="check the edge case", feature="1")
    with pytest.raises(AgentError, match="request 001 has 1 open task"):
        request_ops.request_complete(tmp_root, request="1", result="done")
    ops.task_done(tmp_root, task="1", feature="1")
    with pytest.raises(AgentError, match="no delivered result"):
        ops.feature_close(tmp_root, feature="1")
    with pytest.raises(AgentError, match="no delivered result"):
        ops.feature_set(tmp_root, feature="1", status="done")
    with pytest.raises(AgentError, match="cannot be empty"):
        request_ops.request_complete(tmp_root, request="1", result="  ")
    assert features.resolve(tmp_root, "1").status == "active" and requests.result_of(req) == ""


def test_a_delivered_request_completes_with_a_full_queue_and_no_retro(tmp_root: Path):
    _new(tmp_root)
    for n in range(10):
        lessons.add(tmp_root, title=f"finding {n}", source="none", evidence="symptom", home="x")
    _write_dispatch(
        tmp_root, "001-01", 1, actioned_at="2026-09-03T15:00:00-06:00", verdict_read={"key": "1:0"}
    )
    op = request_ops.request_complete(tmp_root, request="1", result="Safe to land; one nit.", note="reviewed")
    done = features.resolve(tmp_root, "1")
    assert done.status == "done" and requests.result_of(done) == "Safe to land; one nit."
    assert op.data["retro"] == "not required" and "Missing retro" not in op.text
    assert "next" not in op.data and "journal" not in op.text
    assert op.text.startswith("completed Request 001 parser-review as done")
    assert "transcripts_preserved" in op.data and "worktrees_removed" in op.data
    with pytest.raises(AgentError, match="already done"):
        request_ops.request_complete(tmp_root, request="1", result="again")


def test_an_ordinary_feature_keeps_the_queue_gate_and_the_retro_reminder(tmp_root: Path, run):
    run(["feature", "new", "plain", "--title", "Plain"])
    for n in range(10):
        lessons.add(tmp_root, title=f"finding {n}", source="none", evidence="symptom", home="x")
    with pytest.raises(AgentError, match="lesson queue"):
        ops.feature_close(tmp_root, feature="1")
    with pytest.raises(AgentError, match="is a feature, not a request"):
        request_ops.request_complete(tmp_root, request="1", result="x")


def test_a_closed_request_still_reports_what_it_owes(tmp_root: Path):
    """The completion check reads raw records, so a status set by hand does not hide work."""
    req = _new(tmp_root)
    _write_dispatch(tmp_root, "001-01", 1, state="question")
    req.meta["status"] = "abandoned"
    req.save()
    assert requests.unfinished_work(tmp_root, features.resolve(tmp_root, "1")) == [
        "001-01: question, unanswered"
    ]


# ---------------------------------------------------------------- promotion


def test_promotion_keeps_identity_and_adds_feature_sections(tmp_root: Path):
    op = request_ops.request_capture(tmp_root, slug="new-idea", title="Idea", ask="an idea", category="idea")
    req = features.resolve(tmp_root, str(op.data["id"]))
    req.body = requests.with_result(req.body, "sketched")
    req.save()
    before = req.plan_path.read_bytes()
    raw_dir, plan_dir = req.raw_dir, req.dir
    _, old_body = documents.plan_frontmatter(before)
    with pytest.raises(AgentError, match="owner's approval"):
        request_ops.request_promote(tmp_root, request="1", reason=" ")
    request_ops.request_promote(tmp_root, request="1", reason="owner approved the broader build")
    f = features.resolve(tmp_root, "1")
    assert (f.dir, f.raw_dir, f.id) == (plan_dir, raw_dir, 1)
    assert not f.is_request and f.meta["tags"] == ["feature"] and f.status == "proposed"
    assert f.body.startswith("# 001 — Idea\n")
    for name in ("## Decisions", "## Open questions", "## Verification", "## Result"):
        assert name in f.body
    _, new_body = documents.plan_frontmatter(f.plan_path.read_bytes())
    for rendered in (False, True):
        assert documents.plan_sections(old_body, rendered=rendered) == documents.plan_sections(
            new_body, rendered=rendered
        )
    assert "promoted request 001 to a feature" in f.log_path.read_text(encoding="utf-8")
    assert [x["id"] for x in ops.feature_list(tmp_root).data] == [1]
    with pytest.raises(AgentError, match="is a feature, not a request"):
        request_ops.request_promote(tmp_root, request="1", reason="again")


# ---------------------------------------------------------------- views


def test_feature_views_exclude_requests_and_worker_obligations_stay(tmp_root: Path, run, monkeypatch):
    from agent_cli import core

    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    run(["feature", "new", "plain", "--title", "Plain"])
    _new(tmp_root)
    request_ops.request_capture(tmp_root, slug="later-idea", title="Later", ask="later", category="idea")
    _write_dispatch(tmp_root, "002-01", 2, chat_pending=True, launched_by_session="gone")
    data = status.collect(tmp_root)
    assert [f["id"] for f in data["features"]] == [1]
    assert [(r["id"], r["status"]) for r in data["requests"]] == [(2, "active"), (3, "proposed")]
    payload = status.render(tmp_root)
    assert [f["id"] for f in payload["features"]] == [1] and len(payload["requests"]) == 2
    assert [f["id"] for f in ops.feature_list(tmp_root).data] == [1]
    assert [f["id"] for f in ops.feature_list(tmp_root, include_requests=True).data] == [1, 2, 3]
    assert [r["id"] for r in request_ops.request_list(tmp_root).data] == [2, 3]
    digest = session_budget.status_digest(data)
    assert "## Open features (1)" in digest and "## Requests (2)" in digest
    assert "Request 003 [proposed] idea: later next: captured" in digest
    owed = live.obligations(tmp_root)
    shown = [i["id"] for items in owed["manager_action"].values() for i in items]
    shown += [i for n in owed["needs_you"] for i in n.get("ids", [n.get("id")])]
    assert "002-01" in shown
    assert "Requests" in (tmp_root / "Index.md").read_text(encoding="utf-8")


def test_resolved_requests_leave_the_default_views_but_stay_listable(tmp_root: Path):
    _new(tmp_root)
    request_ops.request_complete(tmp_root, request="1", result="answered")
    assert request_ops.request_list(tmp_root).data == []
    assert [r["id"] for r in request_ops.request_list(tmp_root, include_closed=True).data] == [1]
    assert "## Requests" not in session_budget.status_digest(status.collect(tmp_root))
    shown = request_ops.request_show(tmp_root, request="1")
    assert shown.data["result"] == "answered" and shown.data["next_action"] == "resolved"


def test_requests_raise_no_ready_to_close_or_retro_nags(tmp_root: Path):
    _new(tmp_root)
    ops.task_add(tmp_root, text="one step", feature="1")
    ops.task_done(tmp_root, task="1", feature="1")
    for n in range(6):
        _write_dispatch(tmp_root, f"001-0{n + 1}", 1, actioned_at="2026-09-03T15:00:00-06:00")
    nag = session_blocks.lesson_nag(tmp_root)
    assert "finished, not closed" not in nag and "since the last retro" not in nag
    block = session_blocks.open_tasks_block(tmp_root, {"id": 1})
    assert "Request 001 [active] review:" in block and "task_add" not in block


# ---------------------------------------------------------------- adapters


def test_typed_tool_and_cli_round_trip(tmp_root: Path, run):
    made = _call(
        tmp_root,
        action="new",
        slug="tool-made",
        title="Tool",
        ask="answer x",
        category="ask",
        session_id="s9",
    )
    assert made["label"] == "Request 001" and made["session_id"] == "s9"
    captured = _call(tmp_root, action="capture", slug="tool-bug", title="Bug", ask="x breaks", category="bug")
    assert captured["status"] == "proposed" and "session_id" not in captured
    listed = _call(tmp_root, action="list")
    assert [i["id"] for i in listed["items"]] == [1, 2]
    used = _call(tmp_root, action="use", request="2", session_id="s9", switch=True)
    assert used["request"]["id"] == 2
    shown = _call(tmp_root, action="show", request="current", session_id="s9")
    assert shown["id"] == 2
    cli = run(
        [
            "--json",
            "request",
            "new",
            "cli-made",
            "--title",
            "CLI",
            "--ask",
            "research y",
            "--category",
            "research",
            "--no-use",
        ]
    )
    assert cli.code == 0 and cli.json["category"] == "research"
    assert run(["--json", "request", "list"]).json[-1]["id"] == 3
    assert run(["request", "complete", "3", "--result", "found y"]).code == 0
    assert run(["--json", "request", "list", "--all"]).json[-1]["status"] == "done"
    assert run(["request", "promote", "2", "--reason", "owner approved"]).code == 0
    assert not features.resolve(tmp_root, "2").is_request
    for sub in ("new", "capture", "use", "show", "list", "complete", "promote"):
        assert coverage.tool_for("request", sub) == ("request", sub)
    assert coverage.mcp_wording("`agent request complete 1 --result x`").startswith(
        "the `request` tool with action `complete`"
    )


def _startup(run, session_id: str) -> str:
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": session_id, "source": "startup"}))
    assert r.code == 0
    return json.loads(r.out)["hookSpecificOutput"]["additionalContext"]


def test_session_start_names_the_mapped_request_and_how_it_dispatches(tmp_root: Path, run):
    _new(tmp_root)
    ctx = _startup(run, "s1")
    assert "active request: Request 001 parser-review (review, active)  (features/001-parser-review)" in ctx
    assert f"ask: {requests.short(ASK)}" in ctx
    assert "dispatch without task numbers; a research request may omit repo" in ctx
    request_ops.request_capture(
        tmp_root, slug="later", title="Later", ask="An idea.", category="idea", use=True, session_id="s2"
    )
    ctx = _startup(run, "s2")
    assert "active request: Request 002 later (idea, proposed)" in ctx
    assert "no dispatch until the owner asks to execute it" in ctx
    run(["feature", "new", "plain", "--title", "Plain"])
    run(["feature", "use", "3", "--session-id", "s3"])
    assert "active feature: 003 plain  (features/003-plain)" in _startup(run, "s3")
