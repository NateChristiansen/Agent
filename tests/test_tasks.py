import json
from pathlib import Path

import pytest

from agent_cli import frontmatter

pytestmark = pytest.mark.usefixtures("notes_remote")


def _seed(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    plan = tmp_root / "features/001-x/plan.md"
    meta, body = frontmatter.load(plan)
    body = body.replace(
        "## Tasks\n- [ ]\n", "## Tasks\n- [x] write the parser\n- [ ] write the tests\n- [ ] write the docs\n"
    )
    frontmatter.dump(plan, meta, body)
    return plan


def test_list_numbers_undo_and_add(tmp_root: Path, run):
    plan = _seed(tmp_root, run)
    r = run(["--json", "task", "list", "1"])
    assert [(t["index"], t["done"], t["text"]) for t in r.json["tasks"]] == [
        (1, True, "write the parser"),
        (2, False, "write the tests"),
        (3, False, "write the docs"),
    ]
    r = run(["--json", "task", "list", "1", "--open"])
    assert [t["index"] for t in r.json["tasks"]] == [2, 3]
    r = run(["task", "list", "1"])
    assert "[ ] #2 write the tests" in r.out

    run(["task", "undo", "1", "1"])
    assert "- [ ] write the parser" in plan.read_text()
    r = run(["--json", "task", "add", "1", "ship it"])
    assert r.json["task"] == 4
    body = plan.read_text()
    assert "- [ ] write the docs\n- [ ] ship it\n\n## Open questions" in body
    assert run(["task", "add", "1", "   "]).code == 2


def test_done_by_number_and_substring(tmp_root: Path, run):
    plan = _seed(tmp_root, run)
    r = run(["--json", "task", "done", "1", "2"])
    assert r.json["changed"] is True
    assert "- [x] write the tests" in plan.read_text()
    r = run(["--json", "task", "done", "1", "docs", "--note", "abc123"])
    assert r.json["changed"] is True
    assert "- [x] write the docs (abc123)" in plan.read_text()
    r = run(["--json", "task", "done", "1", "2"])
    assert r.json["changed"] is False
    assert run(["task", "done", "1", "write"]).code == 2  # ambiguous
    assert run(["task", "done", "1", "9"]).code == 1
    assert run(["task", "done", "1", "nope"]).code == 1
    status = json.loads((tmp_root / ".agent/status.json").read_text())
    assert status["features"][0]["open_tasks"] == 0


def test_add_replaces_template_placeholder(tmp_root: Path, run):
    run(["feature", "new", "y", "--title", "Y"])
    run(["task", "add", "1", "first real task"])
    body = (tmp_root / "features/001-y/plan.md").read_text()
    assert "- [ ]\n" not in body
    assert "## Tasks\n- [ ] first real task\n\n## Open questions" in body


def test_pickup_runs_the_task_check_and_session_start_lists_open_tasks(tmp_root: Path, run):
    _seed(tmp_root, run)
    r = run(["--json", "feature", "use", "1", "--session-id", "abc"])
    assert r.code == 0, r.err
    assert r.json["task_check"]["open"] == 2
    r = run(["feature", "use", "1", "--session-id", "abc"])
    assert "task check 001 x: 2 open" in r.out
    r = run(["task", "check", "1"])
    assert r.code == 0, r.err
    assert r.out.startswith("task check 001 x: 2 open")

    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "abc", "source": "startup"}))
    ctx = json.loads(r.out)["hookSpecificOutput"]["additionalContext"]
    assert "open tasks (2)" in ctx
    assert "#2 write the tests" in ctx
    assert "#3 write the docs" in ctx
    assert "#1 write the parser" not in ctx
    r = run(["--json", "task", "done", "current", "2", "--session-id", "abc"])
    assert r.json["changed"] is True
    run(["task", "done", "1", "3"])
    r = run(["hook", "session-start"], stdin=json.dumps({"session_id": "abc", "source": "startup"}))
    assert "open tasks: none" in json.loads(r.out)["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------- superseded


def test_supersede_keeps_the_number_and_counts_as_neither(tmp_root: Path, run):
    plan = _seed(tmp_root, run)
    r = run(["--json", "task", "supersede", "1", "2", "--reason", "gone since then", "--link", "693353f"])
    assert r.code == 0, r.err
    assert r.json["changed"] is True
    assert "- [-] write the tests (superseded: gone since then; 693353f)" in plan.read_text()
    r = run(["--json", "task", "list", "1"])
    assert [(t["index"], t["state"], t["done"]) for t in r.json["tasks"]] == [
        (1, "done", True),
        (2, "superseded", False),
        (3, "open", False),
    ]
    assert "[-] #2 write the tests" in run(["task", "list", "1"]).out
    assert [t["index"] for t in run(["--json", "task", "list", "1", "--open"]).json["tasks"]] == [3]
    shown = run(["--json", "feature", "show", "1"]).json
    assert (shown["open_tasks"], shown["done_tasks"], shown["superseded_tasks"]) == (1, 1, 1)
    assert "1 open, 1 done, 1 superseded" in run(["feature", "show", "1"]).out
    status = json.loads((tmp_root / ".agent/status.json").read_text())
    assert (status["features"][0]["open_tasks"], status["features"][0]["done_tasks"]) == (1, 1)
    assert (
        run(["--json", "task", "supersede", "1", "2", "--reason", "x", "--link", "y"]).json["changed"]
        is False
    )


def test_supersede_needs_a_reason_and_a_link_and_a_superseded_task_is_not_ticked(tmp_root: Path, run):
    plan = _seed(tmp_root, run)
    assert run(["task", "supersede", "1", "2", "--reason", " ", "--link", "abc"]).code != 0
    assert run(["task", "supersede", "1", "2", "--reason", "moved"]).code != 0  # argparse: --link required
    assert "- [ ] write the tests" in plan.read_text()
    run(["task", "supersede", "1", "2", "--reason", "moved", "--link", "002 #1"])
    assert run(["task", "done", "1", "2"]).code != 0
    r = run(["--json", "task", "undo", "1", "2"])
    assert r.json["changed"] is True
    assert "- [ ] write the tests (superseded: moved; 002 #1)" in plan.read_text()


def test_a_note_goes_before_a_structured_suffix():
    """Slice 2 stores `{writes=… after=…} ^tN` last on the line; a tick or supersede note must
    not land after it. Today's lines have no suffix and keep the old `text (note)` form."""
    from agent_cli import features

    assert features.with_note("write the docs", "abc123") == "write the docs (abc123)"
    assert features.with_note("write the docs ^t4", "abc123") == "write the docs (abc123) ^t4"
    suffixed = "write the docs {writes=[docs/a.md] after=[^t2]} ^t4"
    assert features.with_note(suffixed, "abc") == "write the docs (abc) {writes=[docs/a.md] after=[^t2]} ^t4"
    assert features.with_note("costs {a} b", "n") == "costs {a} b (n)"
    body = f"## Tasks\n- [ ] {suffixed}\n"
    task = features.list_tasks(body)[0]
    ticked = features.set_task(body, task, True, "abc")
    assert ticked == "## Tasks\n- [x] write the docs (abc) {writes=[docs/a.md] after=[^t2]} ^t4\n"
    gone = features.supersede_task(body, task, "moved", "002 #1")
    assert gone.endswith("(superseded: moved; 002 #1) {writes=[docs/a.md] after=[^t2]} ^t4\n")


def test_markers_count_as_dataview_does():
    """Only `x`/`X` is done (Dataview's `completed`); `-` is superseded; every other marker,
    `/` and custom ones such as `>` included, is open. Before then `>` counted as done."""
    from agent_cli import features

    body = "## Tasks\n- [ ] a\n- [/] b\n- [x] c\n- [X] d\n- [-] e\n- [>] f\n- [ ]\n  - [x] nested\n"
    states = [(t.index, t.state) for t in features.list_tasks(body)]
    assert states == [(1, "open"), (2, "open"), (3, "done"), (4, "done"), (5, "superseded"), (6, "open")]
    counts = features.parse_tasks(body)
    assert (counts["open"], counts["done"], counts["superseded"]) == (3, 2, 1)


def test_close_is_refused_while_a_task_is_open(tmp_root: Path, run):
    plan = _seed(tmp_root, run)
    r = run(["feature", "close", "1"])
    assert r.code != 0
    assert "2 open task(s): #2, #3" in r.err
    assert "status: active" in plan.read_text()
    run(["task", "done", "1", "2"])
    run(["task", "supersede", "1", "3", "--reason", "moved", "--link", "002 #1"])
    r = run(["feature", "close", "1"])
    assert r.code == 0, r.err
    assert "status: done" in plan.read_text()


# ----------------------------------------------------------------
def test_feature_set_cannot_close_around_open_tasks(tmp_root: Path, run):
    """Finding 1: `feature set --status done|abandoned` (CLI and MCP) persisted a terminal
    status with two open tasks while `feature close` refused. `features.set_fields` refuses now."""
    from types import SimpleNamespace

    from agent_cli import features
    from agent_cli.core import AgentError
    from agent_cli.mcp import specs_features

    plan = _seed(tmp_root, run)
    ctx = SimpleNamespace(root=tmp_root, session_for=lambda *args: None)
    for status in ("done", "abandoned"):
        r = run(["feature", "set", "1", "--status", status])
        assert r.code != 0
        assert "2 open task(s): #2, #3" in r.err
        with pytest.raises(AgentError, match="open task"):
            specs_features.feature_set(ctx, "1", status=status)  # pyright: ignore[reportArgumentType]
    meta, body = frontmatter.load(plan)
    assert meta["status"] == "active" and features.parse_tasks(body)["open"] == 2
    assert run(["feature", "set", "1", "--status", "blocked", "--reason", "waiting"]).code == 0
    run(["task", "done", "1", "2"])
    run(["task", "supersede", "1", "3", "--reason", "moved", "--link", "002 #1"])
    assert run(["feature", "set", "1", "--status", "abandoned"]).code == 0
    assert frontmatter.load(plan)[0]["status"] == "abandoned"


SEPARATORS = ["\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


@pytest.mark.parametrize("sep", SEPARATORS, ids=[f"U+{ord(s[-1]):04X}" for s in SEPARATORS])
def test_every_line_separator_is_refused(sep: str):
    """Finding 6: a `\\r` in a supersede reason split task 1 in two and renumbered task 2."""
    from agent_cli import features
    from agent_cli.core import AgentError

    body = "## Tasks\n- [ ] first ^t1\n- [ ] second ^t2\n"
    first = features.resolve_task(body, "1")
    with pytest.raises(AgentError, match="one line"):
        features.supersede_task(body, first, f"moved{sep}- [ ] extra", "002")
    with pytest.raises(AgentError, match="one line"):
        features.supersede_task(body, first, "moved", f"002{sep}- [ ] extra")
    with pytest.raises(AgentError, match="one line"):
        features.set_task(body, first, True, f"abc{sep}- [ ] extra")
    with pytest.raises(AgentError, match="one line"):
        features.add_task_at(body, f"third{sep}- [ ] extra")
    assert [t.text for t in features.list_tasks(body)] == ["first ^t1", "second ^t2"]


# ----------------------------------------------------------------
def test_task_add_reports_the_new_task_and_shifts_none(tmp_root: Path, run):
    """Finding 1: with two Tasks sections, add went into the first, took #2 and reported #3,
    which was the existing `ship`."""
    from agent_cli import features

    run(["feature", "new", "x", "--title", "X"])
    plan = tmp_root / "features/001-x/plan.md"
    meta, _ = frontmatter.load(plan)
    body = "## Tasks\n- [-] moved\n### Detail\ntext\n## Tasks (phase 2)\n- [ ] ship\n  - [ ] sub-step\n\n## Notes\n"
    frontmatter.dump(plan, meta, body)
    added = run(["--json", "task", "add", "1", "new work"])
    assert added.code == 0, added.err
    f = features.resolve(tmp_root, "1")
    assert features.resolve_task(f.body, str(added.json["task"])).text == "new work"
    assert [(t.index, t.text) for t in features.list_tasks(f.body)] == [
        (1, "moved"),
        (2, "ship"),
        (3, "new work"),
    ]
    assert "- [ ] ship\n  - [ ] sub-step\n- [ ] new work\n\n## Notes" in f.body


def test_writes_keep_list_markers_line_endings_and_numbers():
    from agent_cli import features

    body = "## Tasks\r\n* [/] work {writes=[a.py] after=[^t2]} ^t1\r\n+ [ ] last ^t2\r\n"
    for transform in (
        lambda b: features.set_task(b, features.list_tasks(b)[0], True, "checked"),
        lambda b: features.set_task(b, features.list_tasks(b)[0], False),
        lambda b: features.supersede_task(b, features.list_tasks(b)[0], "moved", "002 #1"),
    ):
        body = transform(body)
        tasks = features.list_tasks(body)
        assert len(tasks) == 2 and tasks[0].text.endswith("^t1") and tasks[1].text == "last ^t2"
    assert body.splitlines()[1].startswith("* [-] work (checked) (superseded: moved; 002 #1)")


# ----------------------------------------------------------------
ADD_CASES = {
    # Right after `first`'s item the new line sat under the nested `### Detail`
    # and did not count; the op reported #1, which is `first`.
    "nested_heading": "## Tasks\n- [ ] first\n\n  ### Detail\n  Sub-step notes.\n",
    # The new line went inside the HTML block, was saved, and counted as nothing.
    "html_block": "## Tasks\n<div>\nExample.\n</div>\n",
    "empty_fenced": "## Tasks\n```markdown\nexample\n```\n",
    "setext": "Tasks\n-----\n",
    "ordered": "## Tasks\n1. [ ] first\n",
    "two_sections": "## Tasks\n- [-] moved\n### Detail\ntext\n## Tasks (phase 2)\n- [ ] ship\n",
    "lazy_paragraph": "## Tasks\n- [ ] first\ncontinued lazily\n",
}


@pytest.mark.parametrize("case", sorted(ADD_CASES))
def test_an_added_task_counts_is_last_and_shifts_nothing(case: str):
    from agent_cli import features

    body = ADD_CASES[case]
    before = [(t.index, t.text) for t in features.list_tasks(body)]
    new_body, index = features.add_task_at(body, "new work")
    after = [(t.index, t.text) for t in features.list_tasks(new_body)]
    assert after == [*before, (len(before) + 1, "new work")]
    assert features.resolve_task(new_body, str(index)).text == "new work"


def test_a_plan_where_no_placement_counts_is_refused_not_written():
    """An unclosed fence swallows the rest of the plan, a new `## Tasks` heading included."""
    from agent_cli import features
    from agent_cli.core import AgentError

    with pytest.raises(AgentError, match="could not place the task"):
        features.add_task_at("## Tasks\n```markdown\nexample\n", "new work")


def test_a_terminal_to_terminal_move_refuses_open_tasks(tmp_root: Path, run):
    """Close, reopen a task, then `feature set --status abandoned` went through."""
    from agent_cli import features

    run(["feature", "new", "x", "--title", "X"])
    assert run(["task", "add", "1", "work"]).code == 0
    assert run(["task", "done", "1", "1"]).code == 0
    assert run(["feature", "close", "1"]).code == 0
    assert run(["task", "undo", "1", "1"]).code == 0
    for status in ("abandoned", "done"):
        result = run(["feature", "set", "1", "--status", status])
        assert result.code != 0 and "1 open task(s): #1" in result.err
    f = features.resolve(tmp_root, "1")
    assert f.status == "done" and f.tasks()["open"] == 1
    assert run(["feature", "set", "1", "--title", "Renamed"]).code == 0  # other fields still edit
