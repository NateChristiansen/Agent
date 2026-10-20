"""`thrash-guard`: the split of `src/agent_cli/hooks/stop.py`, mirroring the source split of the
old hooks.py.
"""

import json
from pathlib import Path

import pytest

from hooks_test_helpers import _ctx, _decision, _reason

pytestmark = pytest.mark.usefixtures("notes_remote")

# ---------------------------------------------------------------- thrash-guard


def _denial(reason: str = "Blocked by classifier") -> str:
    return json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "PermissionDenied",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/x"},
            "reason": reason,
        }
    )


def _failure() -> str:
    return json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
            "error": "Exit code 1",
        }
    )


def _post_tool_use(tool: str, tool_input: dict) -> str:
    return json.dumps(
        {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": tool_input}
    )


def test_thrash_guard_denial_threshold(tmp_root: Path, run):
    from agent_cli.hooks import THRASH_DENIALS

    for _ in range(THRASH_DENIALS - 1):
        r = run(["hook", "thrash-guard"], stdin=_denial())
        assert r.out == ""
    r = run(["hook", "thrash-guard"], stdin=_denial())
    ctx = _ctx(r)
    assert f"{THRASH_DENIALS} consecutive permission denials" in ctx
    assert "restate the goal" in ctx
    # the counter reset when it fired: one more denial does not refire immediately
    r = run(["hook", "thrash-guard"], stdin=_denial())
    assert r.out == ""


def test_hook_denials_count_toward_the_thrash_threshold(tmp_root: Path, run):
    """PermissionDenied never fires for a PreToolUse hook deny, so the harness's own denials
    (no-chaining, protect-generated) are counted in hooks.run and the thrash line rides on
    the deny reason the model reads."""
    from agent_cli.hooks import THRASH_DENIALS

    payload = json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls && pwd"},
        }
    )
    for _ in range(THRASH_DENIALS - 1):
        r = run(["hook", "no-chaining"], stdin=payload)
        assert _decision(r) == "deny"
        assert "thrash signal" not in _reason(r)
    r = run(["hook", "no-chaining"], stdin=payload)
    assert _decision(r) == "deny"
    assert f"{THRASH_DENIALS} consecutive hook denials" in _reason(r)
    assert "chained commands are not allowed" in _reason(r)
    # the streak reset when it fired
    r = run(["hook", "no-chaining"], stdin=payload)
    assert "thrash signal" not in _reason(r)
    # a successful tool call resets a partial streak too
    run(["hook", "no-chaining"], stdin=payload)
    run(["hook", "thrash-guard"], stdin=_post_tool_use("Bash", {"command": "ls"}))
    for _ in range(THRASH_DENIALS - 1):
        r = run(["hook", "no-chaining"], stdin=payload)
        assert "thrash signal" not in _reason(r)


def _rework(path: str, i: int) -> str:
    """Edit i of a same-spot chain: it replaces exactly the text edit i-1 wrote."""
    return _post_tool_use(
        "Edit",
        {
            "file_path": path,
            "old_string": f"retry_limit = compute_limit({i})  # attempt",
            "new_string": f"retry_limit = compute_limit({i + 1})  # attempt",
        },
    )


def test_thrash_guard_exempts_the_generated_rubric(tmp_root: Path, run):
    """The old changelog and playbook are gone; only the generated rubric remains exempt.

    The first live false alarm was six normal changelog appends in ten edits. Rewrites of the
    surviving rubric must not count toward the same signal or pollute its window.
    """
    from agent_cli.hooks import THRASH_EDIT_EXEMPT, THRASH_SAME_SPOT

    assert "harness/dispatch-rubric.md" in THRASH_EDIT_EXEMPT
    for name in THRASH_EDIT_EXEMPT:
        path = str(tmp_root / name)
        for i in range(THRASH_SAME_SPOT + 2):
            r = run(["hook", "thrash-guard"], stdin=_rework(path, i))
            assert r.out == "", name
    # a real file still trips it after the exempt edits, at the same threshold
    real = str(tmp_root / "src/agent_cli/dispatch.py")
    for i in range(THRASH_SAME_SPOT - 1):
        assert run(["hook", "thrash-guard"], stdin=_rework(real, i)).out == ""
    r = run(["hook", "thrash-guard"], stdin=_rework(real, THRASH_SAME_SPOT - 1))
    assert f"the same text edited {THRASH_SAME_SPOT} times" in _ctx(r)


def test_thrash_guard_exempts_plan_mode_drafts(tmp_root: Path, run):
    """A plan-mode draft is BUILT by repeated edits to one file (settings.json's
    `plansDirectory` is `.agent/scratch/plans`), so the edit-repeat signal fires on the tool
    working exactly as designed — an earlier shape, one step sideways."""
    from agent_cli.hooks import THRASH_SAME_SPOT

    path = str(tmp_root / ".agent/scratch/plans/t11-draft.md")
    for _ in range(THRASH_SAME_SPOT + 2):
        payload = _post_tool_use("Write", {"file_path": path, "content": "# plan"})
        assert run(["hook", "thrash-guard"], stdin=payload).out == ""
    # Claude Code's own scratchpad is exempt too: an artifact there is built by rounds of edits
    deck = "/private/tmp/claude-502/-project/abc/scratchpad/talk.html"
    for i in range(THRASH_SAME_SPOT + 2):
        assert run(["hook", "thrash-guard"], stdin=_rework(deck, i)).out == ""
    # a draft somewhere else is not exempt: three Writes of one file is a same-spot chain
    other = str(tmp_root / ".agent/scratch/work-orders/t11.md")
    for _ in range(THRASH_SAME_SPOT - 1):
        payload = _post_tool_use("Write", {"file_path": other, "content": "# order"})
        assert run(["hook", "thrash-guard"], stdin=payload).out == ""
    r = run(
        ["hook", "thrash-guard"], stdin=_post_tool_use("Write", {"file_path": other, "content": "# order"})
    )
    assert f"the same text edited {THRASH_SAME_SPOT} times" in _ctx(r)


def test_thrash_guard_error_threshold(tmp_root: Path, run):
    from agent_cli.hooks import THRASH_ERRORS

    for _ in range(THRASH_ERRORS - 1):
        r = run(["hook", "thrash-guard"], stdin=_failure())
        assert r.out == ""
    r = run(["hook", "thrash-guard"], stdin=_failure())
    assert f"{THRASH_ERRORS} consecutive tool errors" in _ctx(r)


def test_thrash_guard_bash_repeat_threshold(tmp_root: Path, run):
    from agent_cli.hooks import THRASH_REPEATS

    payload = _post_tool_use("Bash", {"command": "git status"})
    for _ in range(THRASH_REPEATS - 1):
        r = run(["hook", "thrash-guard"], stdin=payload)
        assert r.out == ""
    r = run(["hook", "thrash-guard"], stdin=payload)
    ctx = _ctx(r)
    assert f"{THRASH_REPEATS} times in a row" in ctx
    assert "git status" in ctx
    # a different command breaks the streak
    run(["hook", "thrash-guard"], stdin=_post_tool_use("Bash", {"command": "git status"}))
    r = run(["hook", "thrash-guard"], stdin=_post_tool_use("Bash", {"command": "ls"}))
    assert r.out == ""


def test_thrash_guard_edit_repeat_threshold(tmp_root: Path, run):
    from agent_cli.hooks import THRASH_SAME_SPOT

    for i in range(THRASH_SAME_SPOT - 1):
        assert run(["hook", "thrash-guard"], stdin=_rework("/x/foo.py", i)).out == ""
    r = run(["hook", "thrash-guard"], stdin=_rework("/x/foo.py", THRASH_SAME_SPOT - 1))
    ctx = _ctx(r)
    assert f"the same text edited {THRASH_SAME_SPOT} times" in ctx
    assert "/x/foo.py" in ctx


def test_thrash_guard_ignores_many_different_edits_to_one_file(tmp_root: Path, run):
    """The per-path count fired twice  on progress, six different export
    edits to one package `__init__.py`. Ten distinct edits to one file never fire, and neither
    does a short fragment like a closing bracket that recurs across unrelated edits."""
    path = "/x/pkg/__init__.py"
    for i in range(10):
        payload = _post_tool_use(
            "Edit",
            {
                "file_path": path,
                "old_string": f'    "name_{i}",\n)',
                "new_string": f'    "name_{i}",\n    "extra_{i}",\n)',
            },
        )
        assert run(["hook", "thrash-guard"], stdin=payload).out == "", i
    for i in range(6):
        payload = _post_tool_use("Edit", {"file_path": path, "old_string": ")", "new_string": f"x{i})"})
        assert run(["hook", "thrash-guard"], stdin=payload).out == "", i


def test_thrash_guard_fires_on_an_edit_undone_twice(tmp_root: Path, run):
    from agent_cli.hooks import THRASH_REVERTS

    path = "/x/flip.py"
    a = "timeout = 30  # seconds, per attempt"
    b = "timeout = 300  # seconds, per attempt"
    out = ""
    edits = [(a, b), (b, a), (a, b)]
    assert len(edits) == THRASH_REVERTS + 1
    for old, new in edits:
        payload = _post_tool_use("Edit", {"file_path": path, "old_string": old, "new_string": new})
        out = run(["hook", "thrash-guard"], stdin=payload).out
    # the third edit is both a revert and the third link of a same-spot chain; either line fires
    assert "thrash signal" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_thrash_guard_success_resets_denial_and_error_streaks(tmp_root: Path, run):
    from agent_cli.hooks import THRASH_DENIALS

    for _ in range(THRASH_DENIALS - 1):
        run(["hook", "thrash-guard"], stdin=_denial())
    run(["hook", "thrash-guard"], stdin=_post_tool_use("Read", {"file_path": "/x.py"}))
    r = run(["hook", "thrash-guard"], stdin=_denial())
    assert r.out == ""  # streak restarted by the successful PostToolUse, not yet at threshold


def test_thrash_guard_ignores_subagents_garbage_and_no_session(tmp_root: Path, run):
    r = run(["hook", "thrash-guard"], stdin=json.dumps({"hook_event_name": "PermissionDenied"}))
    assert r.out == ""
    r = run(["hook", "thrash-guard"], stdin="garbage")
    assert r.code == 0 and r.out == ""
    r = run(
        ["hook", "thrash-guard"],
        stdin=json.dumps({"session_id": "s1", "hook_event_name": "PermissionDenied", "agent_type": "scribe"}),
    )
    assert r.out == ""


def test_thrash_state_survives_a_later_feature_use(tmp_root: Path, run):
    """Regression for sessions.set_record: it used to rebuild the session record from a fixed
    field list, so mapping a feature after thrash-guard had written counters wiped them."""
    run(["hook", "thrash-guard"], stdin=_denial())
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    record = json.loads((tmp_root / ".agent/sessions/s1.json").read_text())
    assert record["thrash"]["denials"] == 1
    assert record["feature"]["id"] == 1
