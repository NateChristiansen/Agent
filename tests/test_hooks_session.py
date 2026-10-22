"""`session-start`, `capture`, and the two session-start nags (retro, journal): the split of
`src/agent_cli/hooks/session.py`, mirroring the source split of the old hooks.py.
"""

import json
import sys
from pathlib import Path

import pytest

from hooks_test_helpers import _ctx

pytestmark = pytest.mark.usefixtures("notes_remote")


@pytest.fixture(autouse=True)
def _local_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    """`feature new` claims its id on the notes remote with a fetch and a push each time; these
    tests read the features, so ids come from the vault alone (the remote claim has its own tests)."""
    from agent_cli import notes_claims

    def claim(root: Path, kind: str, slug: str, *, op: str | None = None) -> notes_claims.Claim:
        return notes_claims.Claim(kind, notes_claims._next_id(root, [], [], kind), slug, op or "local")

    monkeypatch.setattr(notes_claims, "claim", claim)


def _venv_agent(root: Path) -> Path:
    """The file `fresh_clone_warnings` looks for, which a Windows fixture root does not carry."""
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "agent.exe"
    return root / ".venv" / "bin" / "agent"


def test_full_pass_warning_is_actionable_on_source_only(tmp_root: Path, monkeypatch):
    import sys

    from agent_cli import git_discovery, harness_role, verify_record
    from agent_cli.hooks import session_blocks

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(git_discovery, "common_dir", lambda root: tmp_root / ".git")
    monkeypatch.setattr(verify_record, "lookup", lambda *args: (None, "missing"))
    monkeypatch.setattr(harness_role, "consumer", lambda root: False)
    assert session_blocks.full_pass_block(tmp_root).startswith("WARNING:")
    monkeypatch.setattr(harness_role, "consumer", lambda root: True)
    assert session_blocks.full_pass_block(tmp_root) == ""


def test_windows_full_pass_preview_info_prints_once(tmp_root: Path, monkeypatch):
    import sys

    from agent_cli.hooks.session_blocks import full_pass_block

    monkeypatch.setattr(sys, "platform", "win32")
    assert full_pass_block(tmp_root).startswith("INFO:")
    assert full_pass_block(tmp_root) == ""


# ---------------------------------------------------------------- session-start


def test_session_start_mapped(tmp_root: Path, run):
    _venv_agent(tmp_root).parent.mkdir(parents=True, exist_ok=True)
    _venv_agent(tmp_root).touch()
    run(["feature", "new", "gizmo-dial", "--title", "Gizmo dial"])
    run(["feature", "use", "1", "--session-id", "abc"])
    r = run(
        ["hook", "session-start"], stdin=json.dumps({"session_id": "abc", "source": "startup", "cwd": "/x"})
    )
    assert r.code == 0
    ctx = _ctx(r)
    assert "source: startup" in ctx
    assert "session_id: abc" in ctx
    assert "active feature: 001 gizmo-dial  (features/001-gizmo-dial)" in ctx
    assert "## Open features (1)" in ctx
    assert "Never assume" in ctx
    assert "owner: not set." in ctx
    assert (
        "Record with the agent tools: log, task_done, task_add, feature_use (mcp__agent__*). "
        "Never run bin/agent in a shell; if the agent MCP server is down, ask the owner to reconnect it."
        in ctx
    )
    assert "fallback" not in ctx
    assert ".venv is missing" not in ctx
    record = json.loads((tmp_root / ".agent/sessions/abc.json").read_text())
    assert record["cwd"] == "/x"
    # the MCP server finds its session through the pid Claude Code spawned both of us from
    import os

    assert (tmp_root / f".agent/sessions/by-pid/{os.getppid()}").read_text().strip() == "abc"
    _venv_agent(tmp_root).unlink()
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "abc", "source": "startup"}))
    assert ".venv is missing" in _ctx(r)  # loud, since hooks fail open without it


def test_session_start_names_the_owner_from_the_registry(tmp_root: Path, run):
    """One place holds the owner's name: agent.yaml `owner.name`. The block
    tells the model who "the owner" is; nothing else in the harness carries the name."""
    from agent_cli import repos

    reg = tmp_root / "agent.yaml"
    reg.write_text(reg.read_text(encoding="utf-8") + "\nowner:\n  name: Sam Rivera\n", encoding="utf-8")
    assert repos.owner_name(tmp_root) == "Sam Rivera"
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "own", "source": "startup"}))
    assert (
        'owner: Sam Rivera. Every harness file, skill and tool says "the owner"; that is Sam Rivera.'
        in _ctx(r)
    )
    assert repos.validate_registry_data({"repos": [], "owner": {"name": ""}}) == [
        "'owner' must be a mapping with a non-empty name"
    ]


def test_session_start_unmapped_and_reorientation(tmp_root: Path, run):
    run(["status", "render"])
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "def", "source": "compact"}))
    ctx = _ctx(r)
    assert "reorientation — source: compact" in ctx
    assert "no feature mapped" in ctx
    # 052: plan mode kept a manager from mapping itself; the block says it may
    assert "before presenting a plan" in ctx and "allowed in plan mode" in ctx
    assert "When the owner assigns feature work" in ctx
    assert "keeps the mapping" in ctx
    assert "A peer message is not the owner's assignment" in ctx and "`switch=true`" in ctx
    assert "session def" in ctx
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "def", "session_reason": "resume"}))
    assert "source: resume" in _ctx(r)


def test_dispatch_alerts_follow_the_managers_feature_or_launch(tmp_root: Path, run, monkeypatch):
    import importlib

    from agent_cli import sessions
    from agent_cli.hooks import session as session_hooks

    notify = importlib.import_module("agent_cli.dispatch.notify")

    run(["feature", "new", "alpha", "--title", "Alpha"])
    run(["feature", "new", "beta", "--title", "Beta"])
    sessions.set_record(
        tmp_root, "manager-a", feature={"id": 1, "slug": "alpha", "dir": "features/001-alpha"}
    )
    sessions.set_record(tmp_root, "manager-b", feature={"id": 2, "slug": "beta", "dir": "features/002-beta"})
    records = [
        {
            "id": "001-01",
            "short": "alpha",
            "feature": 1,
            "state": "done",
            "result": {"summary": "alpha finished"},
            "worktree": "/tmp/alpha",
            "chat_pending": True,
        },
        {
            "id": "002-01",
            "short": "beta",
            "feature": 2,
            "state": "question",
            "result": {"summary": "beta asks"},
            "worktree": "/tmp/beta",
            "chat_pending": True,
        },
        {
            "id": "002-02",
            "short": "launched",
            "feature": 2,
            "state": "running",
            "started": "2026-09-24T21:00:00",
            "worktree": "/tmp/launched",
            "launched_by_session": "manager-a",
            "chat_pending": False,
        },
    ]
    monkeypatch.setattr(notify, "load_all", lambda root: records)

    a_start = session_hooks.session_start(tmp_root, {"session_id": "manager-a"})
    b_start = session_hooks.session_start(tmp_root, {"session_id": "manager-b"})
    all_start = session_hooks.session_start(tmp_root, {"session_id": "unmapped"})
    assert "001-01 alpha" in a_start and "002-02 launched" in a_start
    assert "002-01 beta" not in a_start
    assert "002-01 beta" in b_start and "002-02 launched" in b_start
    assert "001-01 alpha" not in b_start
    # An unmapped session sees owner obligations and running work, not another live
    # manager's own results (001-01 is manager-a's manager_action).
    assert all(name in all_start for name in ("002-01 beta", "002-02 launched"))
    assert "001-01 alpha" not in all_start

    a_inbox = session_hooks.dispatch_inbox(tmp_root, {"session_id": "manager-a"})
    b_inbox = session_hooks.dispatch_inbox(tmp_root, {"session_id": "manager-b"})
    all_inbox = session_hooks.dispatch_inbox(tmp_root, {"session_id": "unmapped"})
    # Per turn, only the returns waiting on a session; what is running is the session start's.
    assert "001-01 alpha" in a_inbox and "002-02 launched" not in a_inbox
    assert "002-01 beta" not in a_inbox
    assert "002-01 beta" in b_inbox and "001-01 alpha" not in b_inbox
    assert all(name in all_inbox for name in ("001-01 alpha", "002-01 beta"))


def test_session_start_by_pid_prefers_claude_pid(tmp_root: Path, run, monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", "4242")
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "ghi", "source": "startup"}))
    assert r.code == 0
    assert (tmp_root / ".agent/sessions/by-pid/4242").read_text().strip() == "ghi"
    # a /resume in the same process rewrites the same file
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "jkl", "source": "resume"}))
    assert (tmp_root / ".agent/sessions/by-pid/4242").read_text().strip() == "jkl"
    monkeypatch.setenv("CLAUDE_PID", "not-a-pid")
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "mno", "source": "startup"}))
    assert r.code == 0 and "session_id: mno" in _ctx(r)


def test_session_start_no_other_live_sessions(tmp_root: Path, run):
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "abc", "source": "startup"}))
    assert "no other live sessions" in _ctx(r)


def test_session_start_lists_another_live_session_and_its_lease(tmp_root: Path, run):
    """Item 5: another live session (its own pid — always alive here — mapped to a feature,
    holding a lease) shows up by name, with its feature and what it holds."""
    import os

    from agent_cli import leases, sessions

    run(["feature", "new", "other-feat", "--title", "Other"])
    sessions.set_record(
        tmp_root,
        "peer",
        name="peer-session",
        pid=os.getpid(),
        feature={"id": 1, "slug": "other-feat", "dir": "features/001-other-feat"},
    )
    leases.take(tmp_root, "vault:harness", session_id="peer", pid=os.getpid())
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "abc", "source": "startup"}))
    ctx = _ctx(r)
    assert "## Other live sessions" in ctx
    assert "peer-session — feature 001 other-feat, holds vault:harness (touched 0m ago)" in ctx
    assert "no other live sessions" not in ctx


def test_session_start_a_dead_peer_never_shows_up(tmp_root: Path, run):
    from agent_cli import sessions

    sessions.set_record(tmp_root, "gone", name="gone-session", pid=2**22)
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "abc", "source": "startup"}))
    assert "no other live sessions" in _ctx(r)


def _codex_manager(root: Path, sid: str, *, quiet_minutes: int, state: str = "idle", **extra):
    from datetime import timedelta

    from agent_cli import core, session_liveness, sessions

    sessions.set_record(root, sid, name=sid)
    session_liveness.set_identity(root, sid, None, False)
    at = (core.now() - timedelta(minutes=quiet_minutes)).isoformat()
    sessions.update_record(
        root, sid, lambda r: {**r, "provider": "codex", "activity": {"state": state, "at": at}, **extra}
    )


def _start(run) -> str:
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "abc", "source": "startup"}))
    return _ctx(r)


def test_session_start_lists_quiet_codex_manager_as_unverified(tmp_root: Path, run):
    _codex_manager(tmp_root, "mgr-045-x", quiet_minutes=120)
    ctx = _start(run)
    assert "unverified (quiet 2h): mgr-045-x" in ctx
    assert "no other live sessions" not in ctx


def test_session_start_unverified_line_follows_live_lines(tmp_root: Path, run):
    import os

    from agent_cli import sessions

    sessions.set_record(tmp_root, "peer", name="peer-session", pid=os.getpid())
    _codex_manager(tmp_root, "mgr-045-x", quiet_minutes=45)
    ctx = _start(run)
    assert ctx.index("peer-session") < ctx.index("unverified (quiet 45m): mgr-045-x")


def test_session_start_recent_codex_manager_is_live_not_unverified(tmp_root: Path, run):
    _codex_manager(tmp_root, "mgr-045-x", quiet_minutes=5, state="working")
    ctx = _start(run)
    assert "mgr-045-x — no feature" in ctx
    assert "unverified" not in ctx


def test_session_start_hides_codex_manager_quiet_over_a_day(tmp_root: Path, run):
    _codex_manager(tmp_root, "mgr-045-x", quiet_minutes=25 * 60)
    ctx = _start(run)
    assert "mgr-045-x" not in ctx
    assert "no other live sessions" in ctx


def test_session_start_hides_ended_and_worker_codex_records(tmp_root: Path, run):
    _codex_manager(tmp_root, "mgr-ended", quiet_minutes=120, state="ended")
    _codex_manager(tmp_root, "worker-1", quiet_minutes=120, dispatch_id="d1")
    ctx = _start(run)
    assert "mgr-ended" not in ctx and "worker-1" not in ctx
    assert "no other live sessions" in ctx


def test_session_start_recent_worker_record_never_listed_live(tmp_root: Path, run):
    _codex_manager(tmp_root, "worker-2", quiet_minutes=5, state="working", dispatch_id="d2")
    ctx = _start(run)
    assert "worker-2" not in ctx
    assert "no other live sessions" in ctx


def test_session_start_ended_record_with_running_pid_never_listed_live(tmp_root: Path, run):
    import os

    from agent_cli import sessions

    sessions.set_record(tmp_root, "done", name="done-session", pid=os.getpid())
    sessions.update_record(tmp_root, "done", lambda r: {**r, "activity": {"state": "ended"}})
    ctx = _start(run)
    assert "done-session" not in ctx
    assert "no other live sessions" in ctx


def test_session_start_warns_on_dirty_src(tmp_root: Path, run):
    from conftest import git

    git(["init", "-q", "--initial-branch=main"], tmp_root)
    (tmp_root / "src").mkdir(exist_ok=True)
    (tmp_root / "src" / "scratch.py").write_text("x = 1\n", encoding="utf-8")
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "abc", "source": "startup"}))
    assert "uncommitted changes under src/ or tests/: 1 file(s)" in _ctx(r)


def test_session_start_silent_on_dirty_src_when_root_is_not_a_git_repo(tmp_root: Path, run):
    """`tmp_root` itself is not a git repo by default (only the nested alpha-api clone
    is) — the warning fails open rather than erroring."""
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "abc", "source": "startup"}))
    assert "uncommitted changes" not in _ctx(r)


def test_session_start_subagent_silent(tmp_root: Path, run):
    r = run(
        ["hook", "session-start"],
        stdin=json.dumps({"session_id": "abc", "source": "startup", "agent_type": "scribe"}),
    )
    assert r.code == 0 and r.out == ""


@pytest.mark.parametrize("source", ["startup", "compact", "resume"])
def test_session_digest_reads_current_features_without_status_file(tmp_root: Path, run, source):
    from agent_cli import frontmatter

    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "new", "y", "--title", "Y", "--status", "blocked"])
    run(["feature", "new", "z", "--title", "Z", "--status", "proposed"])
    plan = tmp_root / "features/001-x/plan.md"
    meta, body = frontmatter.load(plan)
    # No render after this edit: the digest must read current state, not an old snapshot.
    frontmatter.dump(plan, meta, body.replace("- [ ]\n", "- [ ] repair the symptom\n"))
    result = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": source}))
    ctx = _ctx(result)
    assert "## Open features (3)" in ctx
    assert "001 X [active] next: repair the symptom" in ctx
    assert "002 Y [blocked] next: —" in ctx
    assert "003 Z [proposed] next: —" in ctx
    frontmatter.dump(plan, meta, body.replace("- [ ]\n", "- [x] repair the symptom\n"))
    result = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": source}))
    assert "001 X [active] next: active, 0 open: ready to close" in _ctx(result)
    assert "finished, not closed: 001 — run /retro then /close" in _ctx(result)
    assert not (tmp_root / "STATUS.md").exists()


@pytest.mark.parametrize("source", ["startup", "compact", "resume"])
def test_session_digest_keeps_blocked_reason(tmp_root: Path, run, source):
    from agent_cli import status
    from agent_cli.hooks import session_budget as budget

    run(["feature", "new", "x", "--title", "X"])
    run(
        [
            "feature",
            "set",
            "1",
            "--status",
            "blocked",
            "--reason",
            "await approval",
            "--waits-on",
            "owner",
            "--resume-when",
            "Approved",
        ]
    )
    data = status.collect(tmp_root)
    since = data["features"][0]["blocked_since"]["ts"]
    expected = f"[blocked (blocked since {since}: await approval)"
    assert expected in budget.status_digest(data)
    assert "· next: owner · resume: Approved]" in budget.status_digest(data, limit=None)
    result = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": source}))
    assert expected in _ctx(result)


def test_session_digest_flags_work_on_blocked_features(tmp_root: Path, run, monkeypatch):
    from agent_cli import status
    from agent_cli.hooks import session_budget as budget

    run(["feature", "new", "x", "--title", "X"])
    run(
        [
            "feature",
            "set",
            "1",
            "--status",
            "blocked",
            "--reason",
            "await approval",
            "--waits-on",
            "owner",
            "--resume-when",
            "Approved",
        ]
    )
    monkeypatch.setattr(
        status, "_dispatches", lambda root: [{"id": "001-01", "feature": 1, "state": "running"}]
    )
    data = status.collect(tmp_root)
    assert "[blocked? ⚠ active work: dispatch 001-01 running" in budget.status_digest(data)
    full = budget.status_digest(data, limit=None)
    assert "blocked since " in full and ": await approval)" in full


@pytest.mark.parametrize("source", ["startup", "compact", "resume"])
def test_session_digest_marks_landed_unticked_task(tmp_root: Path, run, monkeypatch, source):
    from agent_cli import status

    run(["feature", "new", "x", "--title", "X"])
    run(["task", "add", "1", "check the symptom"])
    monkeypatch.setattr(
        status,
        "_dispatches",
        lambda root: [{"id": "001-01", "feature": 1, "tasks": [1], "integration": {"state": "landed"}}],
    )
    result = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": source}))
    expected = "001 X [active] next: #1 landed, unticked; check the symptom"
    assert expected in _ctx(result)
    full = (tmp_root / ".agent/scratch/session-start/s1.md").read_text(encoding="utf-8")
    assert expected in full


def test_session_digest_caps_detailed_rows_and_keeps_full_details(tmp_root: Path, run):
    from agent_cli import status
    from agent_cli.hooks import session_budget as budget

    run(["feature", "new", "x", "--title", "X"])
    run(["task", "add", "1", "待" * 150])
    run(
        [
            "feature",
            "set",
            "1",
            "--status",
            "blocked",
            "--reason",
            "await approval",
            "--waits-on",
            "owner",
            "--resume-when",
            "Approved",
        ]
    )
    data = status.collect(tmp_root)
    [feature] = data["features"]
    data["features"] = [{**feature, "id": n} for n in range(1, 40)]
    digest = budget.status_digest(data)
    assert len(digest.encode("utf-8")) <= 2500
    rows = [line.removeprefix("  ") for line in digest.splitlines() if line.startswith("  ")]
    assert rows and all(len(row) <= 110 and row.endswith("…") for row in rows)
    assert "more line(s) in the full-list file" in digest
    full = budget.status_digest(data, limit=None)
    assert full.count("待" * 150) == 39
    assert "039 X [blocked (blocked since " in full


def test_session_start_missing_status_and_garbage(tmp_root: Path, run):
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "abc"}))
    assert "Dashboard.md" in _ctx(r)
    r = run(["hook", "session-start"], stdin="not json")
    assert r.code == 0
    assert "no session_id" in _ctx(r)


# ---------------------------------------------------------------- capture


def test_capture_hook_routes_each_event(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    for event, _kind in (("UserPromptSubmit", "prompt"), ("Stop", "stop"), ("SubagentStop", "subagent-stop")):
        r = run(
            ["hook", "capture"],
            stdin=json.dumps({"session_id": "s1", "hook_event_name": event, "prompt": "hi"}),
        )
        assert r.code == 0 and r.out == "", event
    records = [
        json.loads(line) for line in (tmp_root / ".agent/raw/001-x/2026-09-03.jsonl").read_text().splitlines()
    ]
    assert [x["kind"] for x in records] == ["prompt", "stop", "subagent-stop"]


def _subagent_stop(run, sid: str, agent: str, **extra: str) -> None:
    payload = {"session_id": sid, "hook_event_name": "SubagentStop", "agent_id": agent, **extra}
    r = run(["hook", "capture"], stdin=json.dumps(payload))
    assert r.code == 0 and r.out == ""


def _subagent_sources(root: Path) -> list[dict]:
    from agent_cli import features, log, reconcile

    parsed = [reconcile.parse_entry(e) for e in log.parse(features.resolve(root, "1").log_path)]
    return [r.data for r in parsed if isinstance(r, reconcile.Rec) and r.verb == "source"]


def test_a_mapped_managers_subagent_stop_registers_once(tmp_root: Path, run, monkeypatch):
    from agent_cli import reconcile

    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    _subagent_stop(run, "s1", "ag1", last_assistant_message="found three callers")
    _subagent_stop(run, "s1", "ag1", last_assistant_message="found three callers")  # a re-delivered hook
    _subagent_stop(run, "s1", "ag2")  # no message: the generation is the agent id
    rows = _subagent_sources(tmp_root)
    assert [(r["source"], r["G"]) for r in rows] == [
        ("subagent:ag1", reconcile.subagent_source(1, "ag1", "found three callers")["G"]),
        ("subagent:ag2", "ag2"),
    ]
    assert [r["session"] for r in rows] == ["s1", "s1"]
    led = reconcile.ledger(tmp_root, "1")
    assert sorted(i.source for i in led.owed) == ["subagent:ag1", "subagent:ag2"]
    assert led.sessions == {"subagent:ag1": "s1", "subagent:ag2": "s1"}
    # one batched call settles every subagent result
    from agent_cli.ops.reconcile import reconcile as reconcile_op

    reconcile_op(
        tmp_root,
        feature="1",
        items=[
            {"source": s, "item": "result", "disposition": "no-task", "reason": "search result, read"}
            for s in ("subagent:ag1", "subagent:ag2")
        ],
    )
    assert reconcile.ledger(tmp_root, "1").owed == []


def test_worker_and_unmapped_subagent_stops_register_nothing(tmp_root: Path, run, monkeypatch):
    from agent_cli import sessions

    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "mapped-worker"])
    sessions.set_field(tmp_root, "mapped-worker", "dispatch_id", "001-mac-01")
    _subagent_stop(run, "mapped-worker", "ag1", last_assistant_message="m")
    _subagent_stop(run, "unmapped", "ag2", last_assistant_message="m")
    run(["feature", "use", "1", "--session-id", "s1"])
    monkeypatch.setenv("AGENT_DISPATCH_ID", "001-mac-02")
    _subagent_stop(run, "s1", "ag3", last_assistant_message="m")
    assert _subagent_sources(tmp_root) == []


def test_session_start_says_how_many_items_the_feature_owes(tmp_root: Path, run, monkeypatch):
    from agent_cli import reconcile

    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    run(["feature", "new", "gizmo-dial", "--title", "Gizmo dial"])
    run(["feature", "use", "1", "--session-id", "abc"])
    start = json.dumps({"session_id": "abc", "source": "startup"})
    assert "reconcile:" not in _ctx(run(["hook", "session-start"], stdin=start))
    reconcile.add_manual(tmp_root, "1", ["check the retry bound"])
    ctx = _ctx(run(["hook", "session-start"], stdin=start))
    assert "reconcile: 1 item(s) owed; list and settle them with the `reconcile` tool" in ctx
    assert "active feature: 001 gizmo-dial  (features/001-gizmo-dial)" in ctx


def test_capture_hook_session_end_renders(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    (tmp_root / "Dashboard.md").unlink()
    r = run(["hook", "capture"], stdin=json.dumps({"session_id": "zz", "hook_event_name": "SessionEnd"}))
    assert r.out == ""
    assert (tmp_root / "Dashboard.md").exists()
    assert (tmp_root / ".agent/raw/unassigned/2026-09-03.jsonl").exists()


def test_capture_hook_session_end_releases_leases(tmp_root: Path, run):
    import os

    from agent_cli import leases

    leases.take(tmp_root, "vault:src", session_id="zz", pid=os.getpid())
    leases.take(tmp_root, "vault:harness", session_id="zz", pid=os.getpid())
    leases.take(tmp_root, "repo:alpha", session_id="someone-else", pid=os.getpid())
    r = run(["hook", "capture"], stdin=json.dumps({"session_id": "zz", "hook_event_name": "SessionEnd"}))
    assert r.out == ""
    assert leases.get(tmp_root, "vault:src") is None
    assert leases.get(tmp_root, "vault:harness") is None
    assert leases.get(tmp_root, "repo:alpha") is not None  # a different session's lease is untouched


def test_capture_hook_ignores_unknown_and_garbage(tmp_root: Path, run):
    r = run(["hook", "capture"], stdin=json.dumps({"session_id": "s1", "hook_event_name": "PreToolUse"}))
    assert r.code == 0 and r.out == ""
    r = run(["hook", "capture"], stdin="garbage")
    assert r.code == 0 and r.out == ""
    assert not (tmp_root / ".agent" / "raw").exists()


# ---------------------------------------------------------------- session-start: retro nag


def test_session_start_nags_on_a_heavy_unassigned_day(tmp_root: Path, run):
    """`.agent/raw/unassigned/2026-09-10` is 140 records — a whole working day with no
    feature on it — and nothing ever said so. Silent below the threshold, because every
    session's first prompt lands there before /intake can map it."""
    from agent_cli.hooks import UNASSIGNED_HEAVY_RECORDS

    payload = json.dumps({"session_id": "zzz", "hook_event_name": "Stop"})
    for _ in range(UNASSIGNED_HEAVY_RECORDS):
        run(["capture", "--kind", "stop", "--stdin"], stdin=payload)
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert "records with no feature" not in _ctx(r)
    run(["capture", "--kind", "stop", "--stdin"], stdin=payload)
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    ctx = _ctx(r)
    assert f".agent/raw/unassigned/2026-09-03: {UNASSIGNED_HEAVY_RECORDS + 1} records with no feature" in ctx
    assert "the `feature_use` tool" in ctx


def _large_vault(tmp_root: Path, run, monkeypatch) -> str:
    import importlib

    notify = importlib.import_module("agent_cli.dispatch.notify")
    for n in range(25):
        run(["feature", "new", f"feat-{n}", "--title", f"Feature number {n} with a fairly long title " * 2])
    run(["status", "render"])
    base = tmp_root / ".agent/raw/unassigned"
    base.mkdir(parents=True, exist_ok=True)
    for day in range(1, 11):
        (base / f"2026-10-{day:02d}.jsonl").write_text('{"k": 1}\n' * 30, encoding="utf-8")
    records = [
        {
            "id": f"099-{i:02d}",
            "short": f"worker{i}",
            "feature": 99,
            "state": "question" if i % 2 else "running",
            "started": "2026-10-15T10:00:00",
            "result": {"summary": "a long summary of what the worker found " * 6},
            "worktree": f"/tmp/worktree-{i}",
            "session_id": f"sess-{i}",
            "chat_pending": True,
        }
        for i in range(30)
    ]
    monkeypatch.setattr(notify, "load_all", lambda root: records)
    return "big"


def test_session_start_block_stays_under_the_byte_budget(tmp_root: Path, run, monkeypatch):
    from agent_cli.hooks import session_budget

    sid = _large_vault(tmp_root, run, monkeypatch)
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": sid, "source": "startup"}))
    ctx = _ctx(r)
    assert len(ctx.encode("utf-8")) <= session_budget.BUDGET_BYTES
    assert ".agent/scratch/session-start/big.md" in ctx
    assert "## Open features (25)" in ctx
    assert "more in the full-list file" in ctx
    # The full-list file holds what the block cut.
    full = (tmp_root / ".agent/scratch/session-start/big.md").read_text(encoding="utf-8")
    assert all(f"099-{i:02d} worker{i}" in full for i in range(30))
    assert full.count("records with no feature") == 10
    assert all(f"{i + 1:03d} Feature number {i}" in full for i in range(25))
    assert "## Other live sessions" in full or "no other live sessions" in full


def test_compact_dispatch_block_caps_orphans_and_counts_the_rest():
    from agent_cli.dispatch.notify import _render_compact

    ds = [{"id": f"1-{i}", "short": "w", "state": "done"} for i in range(6)]
    orphaned = [({"feature": n}, ds) for n in range(5)]
    text = _render_compact([], [], orphaned, [])
    assert "orphaned dispatches (30 on 5 feature(s))" in text
    assert "… 2 more in the full-list file" in text
    assert text.count("  feature ") == 3


def test_spill_moves_low_priority_sections_and_says_so():
    from agent_cli.hooks import session_budget as b

    parts = {name: "x" * 4000 for name in ("unassigned", "journal", "pending")}

    def assemble(p: dict[str, str], note: str) -> str:
        return "\n".join([*p.values(), note])

    text, moved = b.fit(parts, assemble, "the-file")
    assert moved[0] == "unassigned"
    assert "moved to the-file" in text and "unassigned" in text.splitlines()[-1]


def test_retro_nag_names_an_active_feature_whose_tasks_are_all_ticked(tmp_root: Path, run):
    """The other half of the same gap. A feature with every task ticked is finished in
    every way except the close, and nothing said so."""
    run(["feature", "new", "x", "--title", "X"])
    run(["task", "add", "1", "wire the thing"])
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert "finished, not closed" not in _ctx(r)  # still open
    run(["task", "done", "1", "1"])
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert "finished, not closed: 001" in _ctx(r)
    (tmp_root / "features/001-x/retro.md").write_text("# retro\n", encoding="utf-8")
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert "finished, not closed" not in _ctx(r)


def test_retro_nag_is_silent_for_a_feature_with_no_tasks_at_all(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert "finished, not closed" not in _ctx(r)


def test_retro_nag_dispatch_count_below_threshold_is_silent(tmp_root: Path, run):
    from agent_cli.hooks.session_blocks import DISPATCHES_SINCE_RETRO_THRESHOLD

    dd = tmp_root / ".agent" / "dispatches"
    dd.mkdir(parents=True, exist_ok=True)
    for i in range(DISPATCHES_SINCE_RETRO_THRESHOLD - 1):
        (dd / f"001-{i:02d}.json").write_text("{}", encoding="utf-8")
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert "dispatches since the last retro" not in _ctx(r)


def test_retro_nag_dispatch_count_since_last_retro(tmp_root: Path, run):
    import os

    from agent_cli.hooks.session_blocks import DISPATCHES_SINCE_RETRO_THRESHOLD

    run(["feature", "new", "x", "--title", "X"])
    dd = tmp_root / ".agent" / "dispatches"
    dd.mkdir(parents=True, exist_ok=True)
    old = 1_000_000_000.0
    for i in range(DISPATCHES_SINCE_RETRO_THRESHOLD):
        p = dd / f"001-{i:02d}.json"
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (old, old))
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert f"{DISPATCHES_SINCE_RETRO_THRESHOLD} dispatches since the last retro" in _ctx(r)
    # a retro newer than every dispatch resets the count to zero
    (tmp_root / "features/001-x/retro.md").write_text("# retro\n", encoding="utf-8")
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert "dispatches since the last retro" not in _ctx(r)


# ---------------------------------------------------------------- session-start: journal nag


def test_journal_nag_lists_a_day_with_activity_and_no_note(tmp_root: Path, run, monkeypatch):
    run(["feature", "new", "x", "--title", "X"])  # activity on 2026-09-03 (opened)
    monkeypatch.setenv("AGENT_NOW", "2026-09-05T09:00:00-07:00")
    run(["log", "1", "did something"])  # activity on 2026-09-05 (a log entry)
    monkeypatch.setenv("AGENT_NOW", "2026-09-06T09:00:00-07:00")
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert "journal missing for: 2026-09-03, 2026-09-05" in _ctx(r)


def test_journal_nag_silent_once_the_block_is_written(tmp_root: Path, run, monkeypatch):
    from datetime import date

    from agent_cli import journal

    run(["feature", "new", "x", "--title", "X"])
    monkeypatch.setenv("AGENT_NOW", "2026-09-05T09:00:00-07:00")
    run(["log", "1", "did something"])
    journal.write_generated(tmp_root, date(2026, 9, 3), "opened the feature")
    journal.write_generated(tmp_root, date(2026, 9, 5), "did something")
    monkeypatch.setenv("AGENT_NOW", "2026-09-06T09:00:00-07:00")
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert "journal missing" not in _ctx(r)


# ---------------------------------------------------------------- both clients


def test_session_start_serves_a_codex_session_the_same_block(tmp_root: Path, run, monkeypatch):
    """The reorientation block is provider-neutral on purpose.

    Codex honours `hookSpecificOutput.additionalContext` on SessionStart exactly as Claude does
    (codex-cli 0.155.1, `session-start.command.output`), so nothing about the block itself needs
    a branch. Before this, `.codex/hooks.json` wired only the activity handler and a Codex
    manager started with no STATUS.md, no feature, no tasks and no peer inbox.

    Codex's payload is the richer of the two — it requires `model`, `permission_mode` and
    `transcript_path` — so the hook must not choke on fields Claude never sends.
    """
    run(["feature", "new", "gizmo-dial", "--title", "Gizmo dial"])
    run(["feature", "use", "1", "--session-id", "cdx"])
    monkeypatch.setenv("CODEX_SESSION_ID", "cdx")
    r = run(
        ["hook", "session-start"],
        stdin=json.dumps(
            {
                "session_id": "cdx",
                "source": "startup",
                "cwd": str(tmp_root),
                "model": "gpt-6-sol",
                "permission_mode": "default",
                "transcript_path": "/tmp/rollout.jsonl",
            }
        ),
    )
    assert r.code == 0
    ctx = _ctx(r)
    assert "session_id: cdx" in ctx
    assert "active feature: 001 gizmo-dial  (features/001-gizmo-dial)" in ctx
    assert "## Open features (1)" in ctx
    assert "Never assume" in ctx


def test_a_codex_session_is_never_sent_a_session_title(tmp_root: Path, run, monkeypatch):
    """Codex defines no `sessionTitle` in any hook output schema, and its schemas are
    `additionalProperties: false` — an unknown key risks the whole output being rejected, which
    would cost the context block as well as the rename. Claude still gets it."""
    run(["feature", "new", "gizmo-dial", "--title", "Gizmo dial"])

    run(["feature", "use", "1", "--session-id", "cdx"])
    monkeypatch.setenv("CODEX_SESSION_ID", "cdx")
    codex = json.loads(
        run(["hook", "session-start"], stdin=json.dumps({"session_id": "cdx", "source": "startup"})).out
    )["hookSpecificOutput"]
    assert "sessionTitle" not in codex
    assert codex["additionalContext"]

    monkeypatch.delenv("CODEX_SESSION_ID")
    run(["feature", "use", "1", "--session-id", "cld"])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cld")
    claude = json.loads(
        run(["hook", "session-start"], stdin=json.dumps({"session_id": "cld", "source": "startup"})).out
    )["hookSpecificOutput"]
    assert claude["sessionTitle"] == "mgr-001-gizmo-dial"


def test_the_provider_is_recorded_for_both_clients(tmp_root: Path, run, monkeypatch):
    """Only Codex used to write `provider`, so every reader inferred Claude from a missing key.
    A label present for one client and absent for the other is not a label."""
    from agent_cli import sessions

    monkeypatch.setenv("CODEX_SESSION_ID", "cdx")
    run(["hook", "session-start"], stdin=json.dumps({"session_id": "cdx", "source": "startup"}))
    assert (sessions.get(tmp_root, "cdx") or {}).get("provider") == "codex"

    monkeypatch.delenv("CODEX_SESSION_ID")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cld")
    run(["hook", "session-start"], stdin=json.dumps({"session_id": "cld", "source": "startup"}))
    assert (sessions.get(tmp_root, "cld") or {}).get("provider") == "claude"


def test_session_start_warns_about_a_fresh_clone_that_is_not_set_up(tmp_root: Path, run):
    """A missing .venv is the fresh-clone shape that fails in a way that looks like the harness
    misbehaving rather than a setup step being missed."""
    # the fixture builds a healthy install, so a fresh clone is made by taking it apart: the
    # template comments say tests wanting these warnings remove the pieces themselves
    _venv_agent(tmp_root).unlink(missing_ok=True)
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert ".venv is missing" in _ctx(r)

    # present: silence, because a warning that is always on is not a warning
    _venv_agent(tmp_root).parent.mkdir(parents=True, exist_ok=True)
    _venv_agent(tmp_root).write_text("")
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    assert "WARNING" not in _ctx(r)


@pytest.mark.parametrize("source", ["startup", "compact", "resume"])
def test_session_start_drops_missing_retro_and_stale_number_nags(tmp_root: Path, run, monkeypatch, source):
    """Audit 10 plan.md:118 (K6) retires missing-retro/stale-number nags, keeps cadence."""
    from datetime import date

    from agent_cli import features, journal, lessons
    from agent_cli.hooks import session_blocks

    run(["feature", "new", "x", "--title", "X"])
    features.close(tmp_root, features.resolve(tmp_root, "1"), "done")
    monkeypatch.setattr(journal, "stale_days", lambda root: [date(2026, 9, 3)])
    monkeypatch.setattr(lessons, "nudge", lambda root: "lesson queue: 10 open entries")
    monkeypatch.setattr(session_blocks, "dispatches_since_last_retro", lambda root: 5)
    result = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": source}))
    ctx = _ctx(result)
    assert "closed without a retro" not in ctx
    assert "5 dispatches since the last retro" in ctx
    assert "journal numbers stale" not in ctx
    assert "lesson queue: 10 open entries" in ctx
    full = (tmp_root / ".agent/scratch/session-start/s1.md").read_text()
    assert "closed without a retro" not in full and "journal numbers stale" not in full
    assert "5 dispatches since the last retro" in full
