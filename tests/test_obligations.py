"""The obligation model. Three facts kept apart (delivered, verdict-read, resolved)
and one derivation of what is owed, `vault.live.obligations`.

Each acceptance case runs the real settlement and consumption functions in a temporary root:
the notice intent and the hook's `hook-consumed` tombstone (`hooks.run`), `ops.dispatch_consume`,
the MCP `dispatch` tool's legacy `show` alias, and the landing's `mark_landed`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_cli import obsidian_tabs, ops, peers, sessions
from agent_cli.core import AgentError, iso, now
from agent_cli.dispatch import closing, notice_delivery, notices, verdicts
from agent_cli.dispatch.state import load, save
from agent_cli.dispatch.verification_gate import record_guard
from agent_cli.mcp import server as mcp_server
from agent_cli.mcp.specs_dispatch import dispatch, dispatch_read
from agent_cli.mcp.tools import Ctx
from agent_cli.vault import live
from test_dispatch_notice_hooks import DID, MGR, consumed, notify, stop

ADOPTER = "mgr-3"
FEATURE = 23
BUILT = {"commits": [{"sha": "a" * 40, "subject": "work"}], "files": ["src/x.py"]}
RECORD: dict[str, Any] = {
    "short": "agent",
    "repo": "Agent",
    "feature": FEATURE,
    "state": "done",
    "attempt": 1,
    "notify": ["chat"],
    "branch": "agent/023-x-mac-01",
    "budget_usd": 2.0,
    "finished": "2026-09-03T12:00:00-06:00",
    "launched_by_session": MGR,
    "verdict_read": None,
    "thread": [],
}


def manager(root: Path, sid: str, *, feature: int | None = FEATURE, provider: str = "claude") -> None:
    """A live manager session: a live pid, mapped to `feature`."""
    sessions.set_record(root, sid, name=f"{sid}-name", pid=os.getpid())
    sessions.set_field(root, sid, "provider", provider)
    if feature is not None:
        sessions.set_field(root, sid, "feature", {"id": feature, "slug": "x"})


def end(root: Path, sid: str) -> None:
    sessions.set_field(root, sid, "activity", {"state": "ended", "at": iso(now())})


def worker(root: Path, did: str = DID, **fields: Any) -> dict[str, Any]:
    """Write a dispatch record, or update the one already there."""
    try:
        d = {**load(root, did), **fields}
    except AgentError:
        d = {**RECORD, "id": did, "worktree": f"/wt/{did}", "handoff": None, **fields}
    save(root, d)
    return load(root, did)


def owed(root: Path) -> dict[str, Any]:
    return live.obligations(root)


def action_ids(root: Path, sid: str) -> list[str]:
    return [i["id"] for i in owed(root)["manager_action"].get(sid, [])]


def needs(root: Path) -> list[tuple[str, Any]]:
    return [(n["kind"], n.get("id") or n.get("ids") or n.get("session_id")) for n in owed(root)["needs_you"]]


def ctx(root: Path) -> Ctx:
    return Ctx(root=root, profile="manager")


@pytest.fixture
def mgr(tmp_root: Path) -> Path:
    """A live Claude manager that launched one finished dispatch, with a terminal notice queued."""
    manager(tmp_root, MGR)
    sessions.set_field(tmp_root, MGR, "inbox", str(tmp_root / "no-such.sock"))
    worker(tmp_root)
    return tmp_root


# ------------------------------------------------------------------ delivered is not read


def test_a_hook_consumed_notice_whose_verdict_is_unread_stays_a_manager_action(mgr: Path):
    nid = notify(mgr)
    assert json.loads(stop(mgr))["decision"] == "block"
    assert consumed(mgr, nid) == "hook-consumed"
    assert not verdicts.is_read(load(mgr, DID))
    assert action_ids(mgr, MGR) == [DID]
    assert owed(mgr)["manager_action"][MGR][0]["reasons"] == ["unread"]


def test_a_hook_that_dies_after_hook_consumed_before_display_leaves_the_verdict_unread(
    mgr: Path, monkeypatch: pytest.MonkeyPatch
):
    nid = notify(mgr)

    def crash(rows: object) -> str:
        raise RuntimeError("the hook died after the consumption save")

    with monkeypatch.context() as patch:
        patch.setattr(peers, "block", crash)
        assert stop(mgr) == ""
    assert consumed(mgr, nid) == "hook-consumed"
    assert action_ids(mgr, MGR) == [DID]


def test_consume_writes_a_generation_bound_read_and_retires_the_notice(mgr: Path):
    nid = notify(mgr)
    ops.dispatch_consume(mgr, dispatch_id=DID, session_id=MGR)
    d = load(mgr, DID)
    assert d["verdict_read"]["key"] == f"1:{notices.generation(d)}"
    assert d["verdict_read"]["by"] == MGR and d["verdict_read"]["via"] == "consume"
    assert d["notice_outbox"][nid]["consumption"] == "show-consumed" and d["chat_pending"] is False
    assert action_ids(mgr, MGR) == []


# ------------------------------------------------------------------ read is not resolved


@pytest.mark.parametrize("state", ["question", "blocked"])
def test_a_consumed_worker_in_question_or_blocked_stays_needs_you(mgr: Path, state: str):
    worker(mgr, state=state)
    notify(mgr, notices.Disposition(1, "terminal", state, "unverified", None, False, None))
    ops.dispatch_consume(mgr, dispatch_id=DID, session_id=MGR)
    assert verdicts.is_read(load(mgr, DID))
    [item] = [n for n in owed(mgr)["needs_you"] if n["kind"] == "worker"]
    assert (item["id"], item["state"], item["read"]) == (DID, state, True)
    assert action_ids(mgr, MGR) == []


def test_a_consumed_unlanded_build_returns_to_needs_you_when_its_manager_ends(mgr: Path):
    worker(mgr, git=BUILT)
    notify(mgr)
    ops.dispatch_consume(mgr, dispatch_id=DID, session_id=MGR)
    [item] = owed(mgr)["manager_action"][MGR]
    assert item["reasons"] == ["unresolved-branch"] and live.age_s(item["since"]) is not None
    end(mgr, MGR)
    rows = [n for n in owed(mgr)["needs_you"] if n["kind"] == "orphaned"]
    assert [(r["feature"], r["ids"], r["count"]) for r in rows] == [(FEATURE, [DID], 1)]
    assert rows[0]["action"] == "resume feature manager"
    assert rows[0]["resume"]["command"] == f"claude --resume {MGR}"


def test_a_read_result_whose_ledger_sources_are_unpublished_stays_a_manager_action(mgr: Path):
    from agent_cli import reconcile

    pending = reconcile.dispatch_source({**load(mgr, DID), "result": {"status": "done"}})
    assert pending is not None and reconcile.INTENT == "reconcile_pending"
    worker(mgr, reconcile_pending=[pending])
    notify(mgr)
    ops.dispatch_consume(mgr, dispatch_id=DID, session_id=MGR)
    [item] = owed(mgr)["manager_action"][MGR]
    assert item["reasons"] == ["reconcile"]
    worker(mgr, reconcile_pending=[])
    assert action_ids(mgr, MGR) == []  # once published, the ledger owns the obligation


# ------------------------------------------------------------------ who may read


def test_a_cross_manager_show_does_not_consume(mgr: Path):
    manager(mgr, "mgr-2")  # live, mapped to the same feature, while the launcher lives too
    nid = notify(mgr)
    dispatch(ctx(mgr), action="show", dispatch_id=DID, session_id="mgr-2")
    d = load(mgr, DID)
    assert d["verdict_read"] is None and d["chat_pending"] is True
    assert d["notice_outbox"][nid]["consumption"] == "none"
    with pytest.raises(AgentError, match="launched by another manager that is still live"):
        ops.dispatch_consume(mgr, dispatch_id=DID, session_id="mgr-2")
    assert action_ids(mgr, MGR) == [DID]


def test_a_resume_starts_unread(mgr: Path):
    notify(mgr)
    ops.dispatch_consume(mgr, dispatch_id=DID, session_id=MGR)
    assert verdicts.is_read(load(mgr, DID))
    with record_guard(mgr, DID) as d:  # the next attempt's terminal notice: a new generation
        d["attempt"] = 2
        harness = notices.harness_identity(mgr)
        notices.record_intent(
            d,
            notices.Disposition(2, "terminal", "done", "verified", None, False, None),
            harness=harness,
            launch_route=notices.route(harness, MGR),
        )
        save(mgr, d)
    assert not verdicts.is_read(load(mgr, DID))
    assert action_ids(mgr, MGR) == [DID]


def test_an_adopting_manager_clears_the_orphaned_results(tmp_root: Path):
    """Orphaned results in live.json: their
    launcher ended, so they were the owner's; a manager mapped to their feature adopts them."""
    ids = [f"045-mac-{n}" for n in ("09", "10", "12", "13", "15", "16")]
    manager(tmp_root, MGR, feature=45)
    for did in ids:
        worker(tmp_root, did, feature=45, chat_pending=True)
    end(tmp_root, MGR)
    rows = [n for n in owed(tmp_root)["needs_you"] if n["kind"] == "orphaned"]
    assert [(r["feature"], r["ids"]) for r in rows] == [(45, ids)]
    manager(tmp_root, ADOPTER, feature=45)
    assert [n for n in owed(tmp_root)["needs_you"] if n["kind"] == "orphaned"] == []
    assert action_ids(tmp_root, ADOPTER) == ids
    for did in ids:
        ops.dispatch_consume(tmp_root, dispatch_id=did, session_id=ADOPTER)
    assert action_ids(tmp_root, ADOPTER) == []
    assert all(load(tmp_root, did)["verdict_read"]["by"] == ADOPTER for did in ids)


def test_a_legacy_show_by_an_authorised_manager_writes_verdict_read(
    mgr: Path, monkeypatch: pytest.MonkeyPatch
):
    """A manager on the cached schema has no `consume`: its `show` alias still consumes as
    before and also writes the read, so the obligation counts drop. The server
    knows its caller the way it always did, from the client's session variable here."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", MGR)
    nid = notify(mgr)
    assert action_ids(mgr, MGR) == [DID]
    dispatch(ctx(mgr), action="show", dispatch_id=DID)
    d = load(mgr, DID)
    assert d["verdict_read"]["via"] == "show" and d["notice_outbox"][nid]["consumption"] == "show-consumed"
    assert action_ids(mgr, MGR) == [] and owed(mgr)["needs_you"] == []


def test_a_legacy_show_by_the_adopting_manager_writes_verdict_read(mgr: Path):
    notify(mgr)
    end(mgr, MGR)
    manager(mgr, ADOPTER)
    assert action_ids(mgr, ADOPTER) == [DID]
    dispatch(ctx(mgr), action="show", dispatch_id=DID, session_id=ADOPTER)
    assert load(mgr, DID)["verdict_read"]["by"] == ADOPTER
    assert action_ids(mgr, ADOPTER) == []


def test_dispatch_read_writes_nothing(mgr: Path):
    notify(mgr)
    path = mgr / ".agent" / "dispatches" / f"{DID}.json"
    before = path.read_bytes()
    summary = dispatch_read(ctx(mgr), action="show", dispatch_id=DID)
    assert summary.data["verdict_read"] is False and summary.data["state"] == "done"
    assert set(summary.data["evidence"]) == {"handoff", "branch", "worktree"}
    full = dispatch_read(ctx(mgr), action="show", dispatch_id=DID, detail=True)
    assert full.data["id"] == DID and "notice_outbox" in full.data
    dispatch_read(ctx(mgr), action="list")
    dispatch_read(ctx(mgr), action="watch", dispatch_id=DID)
    assert path.read_bytes() == before


# ------------------------------------------------------------------ resolved: landing, cancel


def test_a_shell_landing_resolves_the_branch_and_records_the_landing_user(mgr: Path):
    d = worker(mgr, git=BUILT, chat_pending=True)
    closing.mark_landed(d, {"id": "20260903T190000-abcd", "session": None})
    assert d["chat_pending"] is False and d["actioned"]["action"] == "landed"
    assert all(entry.get("consumption") != "none" for entry in notices.outbox(d).values())
    assert d["verdict_read"]["by"].startswith("user:") and d["verdict_read"]["via"] == "land"
    save(mgr, d)
    assert action_ids(mgr, MGR) == []


def test_landing_reads_the_features_reviewer_linked_to_the_landed_branch(mgr: Path):
    worker(mgr, git=BUILT)
    review = {"writes": [], "row": "review", "chat_pending": True}
    worker(mgr, "002-mac-02", branch="agent/023-r-mac-02", base=RECORD["branch"], **review)
    worker(mgr, "002-mac-03", branch="agent/023-r-mac-03", base="main", **review)
    journal = {"id": "20260903T190000-abcd", "branch": RECORD["branch"], "tip": "b" * 40, "session": MGR}
    assert closing.read_landed_reviews(mgr, journal) == ["002-mac-02"]
    assert load(mgr, "002-mac-02")["verdict_read"]["via"] == "land"
    # The reviewer of main stays unread; the build is resolved by `mark_landed`, not by this pass.
    assert action_ids(mgr, MGR) == [DID, "002-mac-03"]


def test_cancel_and_close_never_leave_chat_pending(mgr: Path):
    from agent_cli.dispatch.notify import notify as notify_return

    d = worker(mgr, state="cancelled")
    assert notify_return(mgr, d, "cancelled") == {"chat": "skipped: closed"}
    assert load(mgr, DID).get("chat_pending") in (None, False)


# ------------------------------------------------------------------ presentation contract


def test_every_derived_item_carries_its_validity(mgr: Path):
    sessions.set_field(mgr, MGR, "owner_question_pending", True)
    sessions.set_field(mgr, MGR, "owner_question", "A or B?")
    result = owed(mgr)
    items = [*result["needs_you"], *result["manager_action"][MGR]]
    assert [i["kind"] for i in result["needs_you"]] == ["manager"]
    assert result["needs_you"][0]["reason"] == "question" and result["needs_you"][0]["text"] == "A or B?"
    for item in items:
        assert item["observed_at"] == result["observed_at"] and item["valid_until"] == result["valid_until"]
        assert "show_after" in item and item["show_after"] is None  # the debounce is slice 2
    assert result["valid_until"] > result["observed_at"]


def test_live_json_carries_obligations_and_manager_rows_their_ages(mgr: Path):
    live.write(mgr)
    snap = json.loads(live.path(mgr).read_text(encoding="utf-8"))
    assert [i["id"] for i in snap["manager_action"][MGR]] == [DID]
    [row] = [m for m in snap["managers"] if m["session_id"] == MGR]
    assert row["manager_action"] == 1 and row["manager_action_since"] == [RECORD["finished"]]
    assert live.write(mgr) is False  # only the clock moved: nothing to rewrite


def test_result_ages_count_from_when_the_result_arrived_not_from_a_stored_age(
    tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The age was stored, and a rewrite only the clock would change is
    skipped, so five hours later the snapshot and the Dashboard still said 0m. Now only `since`
    is stored and each surface counts from it at display time."""
    from datetime import timedelta

    from agent_cli import status
    from agent_cli.dispatch.notify import pending_block
    from live_pane_helpers import texts, view

    manager(tmp_root, MGR)
    start = now()
    worker(tmp_root, finished=iso(start), git=BUILT, writes=["src/x.py"])
    assert live.write(tmp_root)
    first = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    [item] = first["manager_action"][MGR]
    assert item["since"] == iso(start) and "age_s" not in item
    monkeypatch.setenv("AGENT_NOW", iso(start + timedelta(hours=5)))
    assert live.age_s(item["since"]) == 18000
    status.render(tmp_root)
    after = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    [row] = [m for m in after["managers"] if m["session_id"] == MGR]
    assert row["manager_action_since"] == [iso(start)]
    assert "oldest 5h" in texts(view(tmp_path, "managers", 0, after, at=now()))
    assert "5h00m old" in pending_block(tmp_root, session_id=MGR)


def test_the_session_block_lists_this_managers_actions_with_their_age(mgr: Path):
    from agent_cli.dispatch.notify import pending_block

    block = pending_block(mgr, session_id=MGR)
    assert "manager actions for this session (1)" in block
    assert f"{DID} agent — done ($0.00; unread; " in block and "old)" in block


# ------------------------------------------------------------------ glyphs


def test_the_glyph_mapping():
    label = obsidian_tabs.label_for
    assert label("n", "waiting_on_you", []) == "✋ n"
    assert label("n", "idle", [], 0, ["question"]) == "✋ n"
    assert label("n", "idle", ["002-mac-01"]) == "⏳ n"
    assert label("n", "idle", [], 2) == "⏳ n"
    assert label("n", "working", ["002-mac-01"]) == "⚙️ n"
    assert label("n", "idle", []) == "✅ n"
    # The owed count left the tab (the dashboard keeps "owes N"); old labels still read.
    assert label("030 slug · cl 834e", "idle", [], 0, (), 2) == "✅ 030 slug · cl 834e"
    assert obsidian_tabs.label_name("✅ +2 030 slug · cl 834e") == "030 slug · cl 834e"
    assert obsidian_tabs.label_name("⚙️ 030 slug · cl 834e") == "030 slug · cl 834e"


def test_a_finished_unconsumed_result_is_a_count_not_an_hourglass(mgr: Path):
    from agent_cli.hooks.activity import desired_label

    sessions.set_field(mgr, MGR, "activity", {"state": "idle", "at": iso(now())})
    record = sessions.get(mgr, MGR) or {}
    tab = (desired_label(mgr, MGR, record) or ("", "", ""))[2]
    assert tab.startswith("✅ ") and "+1" not in tab
    worker(mgr, "002-mac-02", state="running", supervisor_pid=os.getpid())
    tab = (desired_label(mgr, MGR, sessions.get(mgr, MGR) or {}) or ("", "", ""))[2]
    assert tab.startswith("⏳ ") and "+1" not in tab


# ------------------------------------------------------------------ MCP contract


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_dispatch_keeps_every_old_action_and_parameter(tmp_root: Path):
    """Live managers call with the schema they cached at connect: every action and
    parameter `dispatch` had before then is still there, and the read tool is read-only."""
    from mcp import Client

    server = mcp_server.build_server(tmp_root, "manager")
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    schema = tools["dispatch"].input_schema
    old_actions = {
        "new", "list", "show", "spans", "resume", "cancel", "abandon", "ack", "say",
        "collect", "collect-files", "reverify", "cleanup", "watch", "ledger", "plan-review",
    }  # fmt: skip
    assert old_actions | {"consume"} <= set(schema["properties"]["action"]["enum"])
    old_params = {
        "action", "dispatch_id", "repo", "prompt", "feature", "tasks", "budget_usd", "budget_reason",
        "timeout_minutes", "model", "effort", "choice_reason", "provider", "row", "just_go", "dry_run",
        "allow_unmatched", "mcp", "branch", "base", "answer", "message", "force", "wait", "paths",
        "lines", "include_all", "session_id", "by", "refresh", "no_checks", "escalate_from", "again",
        "writes", "verbose",
    }  # fmt: skip
    assert old_params <= set(schema["properties"])
    assert schema["required"] == ["action"]
    read = tools["dispatch_read"]
    assert read.input_schema["properties"]["action"]["enum"] == ["show", "watch", "list"]
    assert read.annotations is not None and read.annotations.read_only_hint is True
    assert not read.annotations.destructive_hint
    assert tools["dispatch"].annotations is not None and tools["dispatch"].annotations.destructive_hint


def test_store_tombstone_is_the_only_trace_of_delivery(mgr: Path):
    """Delivered stays where it was: the recipient's store, never the read field."""
    nid = notify(mgr)
    stop(mgr)
    assert notice_delivery.consumed_as(mgr, MGR, nid) == "hook-consumed"
    assert load(mgr, DID)["verdict_read"] is None


# ----------------------------------------------------------------
def _legacy(root: Path, **fields: Any) -> None:
    """A record from before `verdict_read` and typed notices: `chat_pending` is its read fact."""
    d = worker(root, chat_pending=True, writes=[], **fields)
    d.pop("verdict_read", None)
    save(root, d)


def test_another_managers_show_leaves_a_legacy_verdict_unread(tmp_root: Path):
    """F1: the legacy clear ran before reader authority, so the launcher's obligation vanished."""
    manager(tmp_root, MGR)
    manager(tmp_root, "mgr-2")
    _legacy(tmp_root)
    assert not verdicts.is_read(load(tmp_root, DID))
    dispatch(ctx(tmp_root), action="show", dispatch_id=DID, session_id="mgr-2")
    current = load(tmp_root, DID)
    assert current["chat_pending"] is True and "verdict_read" not in current
    assert action_ids(tmp_root, MGR) == [DID]


def test_a_shell_show_leaves_a_legacy_verdict_unread(tmp_root: Path):
    manager(tmp_root, MGR)
    _legacy(tmp_root)
    ops.dispatch_show(tmp_root, dispatch_id=DID)  # no session: the CLI, or an unknown caller
    assert load(tmp_root, DID)["chat_pending"] is True
    assert action_ids(tmp_root, MGR) == [DID]


def test_the_launchers_show_still_reads_a_legacy_verdict(tmp_root: Path):
    manager(tmp_root, MGR)
    _legacy(tmp_root)
    dispatch(ctx(tmp_root), action="show", dispatch_id=DID, session_id=MGR)
    current = load(tmp_root, DID)
    assert current["chat_pending"] is False and current["verdict_read"]["by"] == MGR
    assert action_ids(tmp_root, MGR) == []


@pytest.mark.parametrize(("hook", "provider"), [("activity", "claude"), ("codex-activity", "codex")])
@pytest.mark.parametrize(
    ("source", "prior", "after"),
    [
        ("compact", "working", "working"),
        ("compact", "idle", "idle"),
        ("resume", "working", "working"),
        ("resume", "idle", "idle"),
        ("startup", "working", "idle"),
        ("clear", "working", "idle"),
        ("fork", "working", "idle"),
    ],
)
def test_session_start_keeps_working_through_compact_and_resume(
    tmp_root: Path, hook: str, provider: str, source: str, prior: str, after: str
):
    """F2 as written: a compaction leaves the state alone, a resume keeps working, and
    startup, clear and fork are a rest. Through the real hook entry point, for both clients."""
    from agent_cli import hooks
    from agent_cli.hooks.activity import desired_label

    manager(tmp_root, MGR, provider=provider)
    sessions.set_field(tmp_root, MGR, "activity", {"state": prior, "at": iso(now())})
    payload = {"session_id": MGR, "hook_event_name": "SessionStart", "source": source}
    _, code = hooks.run(tmp_root, hook, json.dumps(payload))
    assert code == 0
    record = sessions.get(tmp_root, MGR) or {}
    assert record["activity"]["state"] == after
    if after == "working":
        assert (desired_label(tmp_root, MGR, record) or ("", "", ""))[2].startswith("⚙️")


def test_the_status_line_shows_nothing_for_a_consumed_read_only_review(tmp_root: Path):
    """F3: the status line used `is_live` and still said "1 waiting on you"."""
    manager(tmp_root, MGR)
    worker(tmp_root, writes=[], chat_pending=True)
    assert ops.status_line(tmp_root).data["waiting"][0]["id"] == DID  # unread: still owed
    ops.dispatch_consume(tmp_root, dispatch_id=DID, session_id=MGR)
    assert live.obligations(tmp_root)["manager_action"] == {}
    for session in (None, MGR, "other"):
        line = ops.status_line(tmp_root, session_id=session)
        assert line.data["waiting"] == [] and "waiting on you" not in line.text


def test_the_status_line_shows_a_pending_owner_question_with_no_worker(tmp_root: Path, run):
    """F3: a question with no worker had no waiting indication. Also through Claude's own
    statusLine command, which reads the session id from stdin."""
    manager(tmp_root, MGR)
    sessions.set_field(tmp_root, MGR, "owner_question_pending", True)
    sessions.set_field(tmp_root, MGR, "owner_question", "Choose A or B.")
    line = ops.status_line(tmp_root, session_id=MGR)
    assert line.data["asks"] == [{"session_id": MGR, "name": f"{MGR}-name", "reason": "question"}]
    assert f"✋ {MGR}-name question waiting on you" in line.text
    out = run(["status", "line"], stdin=json.dumps({"session_id": MGR, "cwd": str(tmp_root)}))
    assert out.code == 0 and "question waiting on you" in out.out


# ----------------------------------------------------------------
def _next_attempt(root: Path) -> None:
    """A second attempt finishes and its terminal notice lands: a new attempt and generation."""
    current = load(root, DID)
    current.update(attempt=2, verdict_read=None, chat_pending=True)
    harness = notices.harness_identity(root)
    notices.record_intent(
        current,
        notices.Disposition(2, "terminal", "done", "verified", None, False, None),
        harness=harness,
        launch_route=notices.route(harness, MGR),
    )
    save(root, current)


def test_a_legacy_show_never_reads_an_attempt_it_did_not_return(mgr: Path, monkeypatch: pytest.MonkeyPatch):
    """R2 F1: the read was written from a reload after `show` returned its snapshot, so a newer
    attempt that finished in between was marked read unseen."""
    worker(mgr, writes=[], chat_pending=True)
    notify(mgr)
    original = ops.dispatch_show

    def show_then_finish(*args: Any, **kwargs: Any) -> Any:
        observed = original(*args, **kwargs)
        _next_attempt(mgr)
        return observed

    monkeypatch.setattr(ops, "dispatch_show", show_then_finish)
    returned = dispatch(ctx(mgr), action="show", dispatch_id=DID, session_id=MGR)
    current = load(mgr, DID)
    assert returned.data["attempt"] == 1 and current["attempt"] == 2
    assert current["verdict_read"] is None and not verdicts.is_read(current)
    assert action_ids(mgr, MGR) == [DID]


def test_a_read_bound_to_a_shown_snapshot_rechecks_it_under_the_guard(
    mgr: Path, monkeypatch: pytest.MonkeyPatch
):
    """The binding holds inside the record guard too: a change between the pre-check and the
    guard writes nothing."""
    from agent_cli.dispatch import verification_gate

    worker(mgr, writes=[], chat_pending=True)
    notify(mgr)
    shown = verdicts.key(load(mgr, DID))
    real = verification_gate.record_guard

    def changed_first(root: Path, dispatch_id: str, **kwargs: Any) -> Any:
        _next_attempt(root)
        return real(root, dispatch_id, **kwargs)

    monkeypatch.setattr(verification_gate, "record_guard", changed_first)
    assert verdicts.record_read(mgr, DID, MGR, "show", expected=shown) is None
    assert load(mgr, DID)["verdict_read"] is None
    monkeypatch.setattr(verification_gate, "record_guard", real)
    assert verdicts.record_read(mgr, DID, MGR, "show", expected=verdicts.key(load(mgr, DID))) is not None


def test_a_manager_row_shows_a_pending_question_from_the_shared_projection(tmp_root: Path, tmp_path: Path):
    """R2 F4: the row read raw activity, so an idle manager with an owner question read "idle"
    on the Dashboard while Needs you and its tab showed the question."""
    from live_pane_helpers import texts, view

    manager(tmp_root, MGR)
    sessions.set_field(tmp_root, MGR, "activity", {"state": "idle", "at": iso(now())})
    ops.owner_question(tmp_root, session_id=MGR, summary="Choose A or B.")
    snap = live.build(tmp_root)
    row = next(m for m in snap["managers"] if m["session_id"] == MGR)
    assert snap["needs_you"][0]["reason"] == "question"
    assert (row["attention"], row["state"]) == ("question", "question for you")
    shown = view(tmp_path, "managers", 0, json.loads(json.dumps(snap, default=str)))
    assert ["span", "idle"] not in shown
    assert "question for you" in texts(shown)


def test_a_manager_row_spells_out_what_it_owes(mgr: Path):
    notify(mgr)
    sessions.set_field(mgr, MGR, "activity", {"state": "idle", "at": iso(now())})
    row = next(m for m in live.build(mgr)["managers"] if m["session_id"] == MGR)
    assert (row["attention"], row["state"]) == ("manager_action", "idle, owes 1 result")
