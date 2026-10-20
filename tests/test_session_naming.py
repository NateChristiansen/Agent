"""A mapped manager session is titled `mgr-NNN-slug`, and a name the owner
chose is never overwritten."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import hooks, sessions, tab_drain

pytestmark = pytest.mark.usefixtures("notes_remote")

PID = 424242


@pytest.fixture
def claude_dir(tmp_path: Path, monkeypatch) -> Path:
    base = tmp_path / "claude-config"
    (base / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CLAUDE_PID", str(PID))
    return base


def _live(claude_dir: Path, name: str | None, source: str | None) -> None:
    data = {"pid": PID, "sessionId": "s1"}
    if name is not None:
        data |= {"name": name, "nameSource": source}
    (claude_dir / "sessions" / f"{PID}.json").write_text(json.dumps(data), encoding="utf-8")


def _prompt() -> str:
    return json.dumps({"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "go"})


def _title(r) -> str | None:
    return json.loads(r.out)["hookSpecificOutput"].get("sessionTitle") if r.out else None


def test_a_mapped_session_with_an_auto_name_is_titled_from_its_feature(tmp_root: Path, run, claude_dir):
    run(["feature", "new", "worker-management", "--title", "W"])
    _live(claude_dir, "worker-sessions-ui-enhancements", "auto")
    # not mapped yet: still a manager, named so it can be found
    assert _title(run(["hook", "session-name"], stdin=_prompt())) == "mgr-unmapped-s1"
    _live(claude_dir, "mgr-unmapped-s1", "hook")
    run(["feature", "use", "1", "--session-id", "s1"])
    r = run(["hook", "session-name"], stdin=_prompt())
    assert _title(r) == "mgr-001-worker-management"
    assert (sessions.get(tmp_root, "s1") or {})["name"] == "mgr-001-worker-management"
    # once Claude Code carries the name, the hook is silent
    _live(claude_dir, "mgr-001-worker-management", "hook")
    assert run(["hook", "session-name"], stdin=_prompt()).out == ""


def test_an_unmapped_session_with_a_derived_name_is_named_as_a_manager(tmp_root: Path, run, claude_dir):
    """Found live: an unmapped manager kept Claude's `agent-ba`, derived from the
    working directory, so it could not be told from any other session."""
    sid = "d8400000-0000-4000-8000-000000000004"
    data = {"pid": PID, "sessionId": sid, "name": "agent-ba", "nameSource": "derived"}
    (claude_dir / "sessions" / f"{PID}.json").write_text(json.dumps(data), encoding="utf-8")
    prompt = json.dumps({"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": "go"})
    assert _title(run(["hook", "session-name"], stdin=prompt)) == "mgr-unmapped-d84000"
    assert (sessions.get(tmp_root, sid) or {})["name"] == "mgr-unmapped-d84000"
    assert hooks.desired_title(None, sid) == "mgr-unmapped-d84000"


def test_a_record_that_lost_its_name_heals_on_the_next_prompt(tmp_root: Path, run, claude_dir):
    """Found live: Claude Code carried `mgr-022-...` but the record's `name` was
    null, lost to a parallel hook's write, and the hook stayed silent forever, so the tab relabel
    skipped the session and a permission prompt showed no ✋."""
    run(["feature", "new", "a", "--title", "A"])
    run(["feature", "use", "1", "--session-id", "s1"])
    _live(claude_dir, "mgr-001-a", "hook")
    assert (sessions.get(tmp_root, "s1") or {}).get("name") is None
    assert run(["hook", "session-name"], stdin=_prompt()).out == ""  # Claude needs no new title
    assert (sessions.get(tmp_root, "s1") or {})["name"] == "mgr-001-a"


def test_a_remap_renames_a_session_the_harness_named(tmp_root: Path, run, claude_dir):
    run(["feature", "new", "a", "--title", "A"])
    run(["feature", "new", "b", "--title", "B"])
    run(["feature", "use", "1", "--session-id", "s1"])
    run(["hook", "session-name"], stdin=_prompt())
    _live(claude_dir, "mgr-001-a", "hook")
    run(["feature", "use", "2", "--session-id", "s1"])
    assert _title(run(["hook", "session-name"], stdin=_prompt())) == "mgr-002-b"


def test_feature_use_relabels_a_generated_claude_tab_mid_turn(
    tmp_root: Path, run, claude_dir: Path, monkeypatch
):
    sid = "d1a20000-0000-4000-8000-000000000003"
    old = "mgr-unmapped-d1a200"
    run(["feature", "new", "tab-width", "--title", "Tab width"])
    sessions.set_record(tmp_root, sid, pid=PID, name=old)
    sessions.set_field(tmp_root, sid, "provider", "claude")
    (claude_dir / "sessions" / f"{PID}.json").write_text(
        json.dumps({"pid": PID, "sessionId": sid, "name": old, "nameSource": "hook"}),
        encoding="utf-8",
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, identity, match, label, **kw: calls.append((match, label)) or {"started": True},
    )
    run(["feature", "use", "1", "--session-id", sid])
    record = sessions.get(tmp_root, sid) or {}
    assert record["name"] == "mgr-001-tab-width"
    assert record["pending_client_name"] == old
    assert record["former_names"] == [old]  # a peer that learned the old name can still reach it
    assert calls == [(old, "✅ 001 tab-width · cl d1a2")]
    prompt = json.dumps({"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": "go"})
    assert _title(run(["hook", "session-name"], stdin=prompt)) == "mgr-001-tab-width"


def test_feature_use_renames_a_session_claude_named_from_its_folder(
    tmp_root: Path, run, claude_dir: Path, monkeypatch
):
    """052: a session mapped in plan mode kept `mgr-unmapped-…` because Claude's own title was the
    derived `agent-02`, which matched neither the record nor the generated name."""
    sid = "57bd0000-0000-4000-8000-000000000002"
    run(["feature", "new", "tab-width", "--title", "Tab width"])
    sessions.set_record(tmp_root, sid, pid=PID, name="mgr-unmapped-57bd00")
    sessions.set_field(tmp_root, sid, "provider", "claude")
    (claude_dir / "sessions" / f"{PID}.json").write_text(
        json.dumps({"pid": PID, "sessionId": sid, "name": "agent-02", "nameSource": "derived"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(tab_drain, "request", lambda *a, **kw: {"started": True})
    run(["feature", "use", "1", "--session-id", sid])
    assert (sessions.get(tmp_root, sid) or {})["name"] == "mgr-001-tab-width"
    prompt = json.dumps({"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": "go"})
    assert _title(run(["hook", "session-name"], stdin=prompt)) == "mgr-001-tab-width"


def test_feature_use_preserves_an_owner_named_claude_tab(tmp_root: Path, run, claude_dir: Path, monkeypatch):
    sid = "d1a20000-0000-4000-8000-000000000003"
    run(["feature", "new", "tab-width", "--title", "Tab width"])
    sessions.set_record(tmp_root, sid, pid=PID, name="mgr-unmapped-d1a200")
    (claude_dir / "sessions" / f"{PID}.json").write_text(
        json.dumps({"pid": PID, "sessionId": sid, "name": "my-name", "nameSource": "user"}),
        encoding="utf-8",
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, identity, match, label, **kw: calls.append((match, label)) or {"started": True},
    )
    run(["feature", "use", "1", "--session-id", sid])
    record = sessions.get(tmp_root, sid) or {}
    assert record["name"] == "my-name"
    assert calls == [("my-name", "✅ my-name · cl d1a2")]


def test_feature_use_does_not_relabel_a_worker(tmp_root: Path, run, monkeypatch):
    run(["feature", "new", "tab-width", "--title", "Tab width"])
    sessions.set_record(tmp_root, "worker-one", name="002-01")
    sessions.set_field(tmp_root, "worker-one", "dispatch_id", "002-01")
    calls: list[str] = []
    monkeypatch.setattr(tab_drain, "request", lambda *a, **kw: calls.append("request"))
    run(["feature", "use", "1", "--session-id", "worker-one"])
    assert calls == []
    assert (sessions.get(tmp_root, "worker-one") or {})["name"] == "002-01"


def test_a_name_the_owner_chose_is_never_overwritten(tmp_root: Path, run, claude_dir):
    run(["feature", "new", "a", "--title", "A"])
    run(["feature", "use", "1", "--session-id", "s1"])
    _live(claude_dir, "retro-006", "user")
    assert run(["hook", "session-name"], stdin=_prompt()).out == ""
    assert (sessions.get(tmp_root, "s1") or {}).get("name") is None


def test_session_start_carries_the_title_and_workers_are_left_alone(tmp_root: Path, run, claude_dir):
    run(["feature", "new", "a", "--title", "A"])
    run(["feature", "use", "1", "--session-id", "s1"])
    _live(claude_dir, None, None)
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "s1", "source": "startup"}))
    out = json.loads(r.out)["hookSpecificOutput"]
    assert out["sessionTitle"] == "mgr-001-a" and "additionalContext" in out
    assert hooks.desired_title({"dispatch_id": "001-01", "feature": {"id": 1, "slug": "a"}}) is None


def test_the_hook_is_wired():
    from agent_cli.hooks.groups import wired

    settings = json.loads((Path(__file__).resolve().parents[1] / ".claude" / "settings.json").read_text())
    assert "session-name" in wired(settings, "UserPromptSubmit")


def test_session_id_resolves_for_either_provider(monkeypatch, tmp_root: Path):
    """A Codex manager exports `CODEX_SESSION_ID` and `CODEX_THREAD_ID`, and a
    child shell inherits both — verified in a live Codex session on 2026-09-22, not assumed.

    Until this list carried them, every `bin/agent` call from a Codex session raised, so nothing
    registered, no Codex session appeared in the Managers view, and no cross-provider message
    could be addressed. `resolve_pid` already falls back to the parent process, so the session id
    was the only half of provider identity missing.
    """
    from agent_cli.core import AgentError

    for var in sessions.ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("CODEX_SESSION_ID", "01a0cc50-codex")
    assert sessions.resolve_session_id(tmp_root) == "01a0cc50-codex"

    # an explicit id still wins over anything a client exported
    assert sessions.resolve_session_id(tmp_root, "explicit") == "explicit"

    # Claude's is tried first, so a Claude session in a shell holding a stale Codex var still
    # resolves to itself rather than to whatever ran there before
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-one")
    assert sessions.resolve_session_id(tmp_root) == "claude-one"

    for var in sessions.ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(AgentError) as err:
        sessions.resolve_session_id(tmp_root)
    # the error names every supported variable, not only Claude's
    assert "CODEX_SESSION_ID" in str(err.value) and "CLAUDE_CODE_SESSION_ID" in str(err.value)
