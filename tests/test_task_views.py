"""Parity: compatibility views and pane rows keep Python task counts (audits 6 P4 and 8 B).

The feature header runs under node against a fake `dv` whose task objects follow Dataview's rule
(`markdown-file.ts`, fetched 2026-10-13): `status` is the bracket character, `completed` is true
only for `x`/`X`, and a nested item has a `parent`. The Python counts for the same plan must equal
what each view shows. B1 retires Dataview counts in features.js/tasks.js; those compatibility
views consume the real Python snapshot, while the header still consumes indexed tasks."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_cli import features, frontmatter, status

VIEWS = Path(__file__).resolve().parents[1] / "harness/obsidian/views"

BODY = """# 001 work

## Context
- [ ] not a task: outside ## Tasks

## Tasks
- [x] parse the plan
- [ ] write the tests
- [/] write the docs
- [-] the old exporter (superseded: moved; 002 #1)
- [X] ship the alpha
- [>] deferred, a custom status
- [ ]
  - [x] a nested sub-step
- [ ] land it

## Notes
"""

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node is needed to execute the Dataview views"
)


def dataview_tasks(body: str) -> list[dict[str, Any]]:
    """The list items Dataview indexes, with the fields the views read. The structure is
    CommonMark's, read straight from markdown-it rather than through `task_md`: an item's section
    is its nearest heading (an empty `###` included), and only an item of a top-level list has
    no parent, whichever of its 0-3 indent spaces it uses."""
    from markdown_it import MarkdownIt

    lines = body.replace("\r\n", "\n").split("\n")
    tokens = MarkdownIt("commonmark").parse(body)
    tasks: list[dict[str, Any]] = []
    section = None
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            section = tokens[i + 1].content.strip()
        elif token.type == "list_item_open" and token.map:
            m = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+\[(.)\] ?(.*)$", lines[token.map[0]])
            if not m:
                continue
            status = m.group(1)
            tasks.append(
                {
                    "status": status,
                    "completed": status in ("x", "X"),
                    "text": m.group(2),
                    "section": {"subpath": section},
                    "parent": None if token.level == 1 else 1,
                }
            )
    return tasks


HARNESS = r"""
const fs = require("fs");
const [viewPath, planJson] = process.argv.slice(1);
const plan = JSON.parse(planJson);
function array(values) {
  return {
    values,
    get length() { return values.length; },
    where: (fn) => array(values.filter(fn)),
    sort: () => array([...values]),
    forEach: (fn) => values.forEach(fn),
    map: (fn) => values.map(fn),
    array: () => values,
    [Symbol.iterator]: () => values[Symbol.iterator](),
  };
}
function element(out) {
  const el = {
    createDiv: (opts) => element(out).withText(opts),
    createEl: (_tag, opts) => element(out).withText(opts),
    setAttr: () => {},
    addEventListener: () => {},
    withText: (opts) => { if (opts && opts.text !== undefined) out.push(String(opts.text)); return el; },
  };
  return el;
}
const out = { list: [], paragraphs: [], card: [] };
const page = {
  id: 1, slug: "work", title: "Work", status: "active", opened: "2026-10-01",
  file: {
    name: "plan", path: "features/001-work/plan.md", folder: "features/001-work",
    tasks: array(plan.tasks), link: { withDisplay: (s) => s },
  },
  ...(plan.page || {}),
};
function DateTimeOf(iso) {
  const d = new Date(iso);
  return {
    isValid: !isNaN(d), startOf: () => DateTimeOf(iso), toISODate: () => iso.slice(0, 10),
    diff: (other) => ({ days: (d - new Date(other.toISODate())) / 86400000 }),
  };
}
const dv = {
  pages: (q) => (q === '"features"' ? array([page]) : array([])),
  current: () => page,
  el: () => {},
  list: (items) => out.list.push(...items),
  paragraph: (text) => out.paragraphs.push(text),
  container: element(out.card),
  luxon: { DateTime: { now: () => DateTimeOf("2026-10-13"), fromISO: DateTimeOf } },
  io: { load: async (path) => (plan.files || {})[path] ?? null },
};
const source = fs.readFileSync(viewPath, "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const app = {vault:{adapter:{read:async()=>JSON.stringify(plan.snapshot)}}};
new AsyncFunction("dv", "app", "input", source)(dv, app, plan.input).then(() => process.stdout.write(JSON.stringify(out)));
"""


def _snapshot(root: Path, body: str, *, render: bool = False) -> dict[str, Any]:
    plan = root / "features/001-work/plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    frontmatter.dump(
        plan,
        {"id": 1, "slug": "work", "title": "Work", "status": "active", "opened": "2026-10-01"},
        body,
    )
    return status.render(root) if render else status.collect(root)


def _run(view: str, body: str = BODY) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        snapshot = _snapshot(Path(directory), body)
    plan = json.dumps({"tasks": dataview_tasks(body), "snapshot": snapshot}, default=str)
    result = subprocess.run(
        ["node", "-e", HARNESS, str(VIEWS / view), plan], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def test_tasks_view_lists_exactly_the_open_tasks_with_cli_numbers():
    expected = [f"#{t.index} {t.text}" for t in features.list_tasks(BODY) if t.is_open]
    assert expected == [
        "#2 write the tests",
        "#3 write the docs",
        "#6 deferred, a custom status",
        "#7 land it",
    ]
    assert _run("tasks.js")["list"] == expected


def test_features_view_card_counts_the_open_tasks():
    card = _run("features.js")["card"]
    assert card[card.index("Open tasks") + 1] == str(features.parse_tasks(BODY)["open"])


def test_feature_header_counts_done_of_live_tasks_and_superseded_apart():
    counts = features.parse_tasks(BODY)
    assert (counts["open"], counts["done"], counts["superseded"]) == (4, 2, 1)
    header = _run("feature.js")["paragraphs"][0]
    total = counts["done"] + counts["open"]
    assert f"tasks {counts['done']}/{total} done, {counts['superseded']} superseded" in header


# A `### Detail` subsection inside `## Tasks`, then a second
# `## Tasks`. Python counted `detail` and numbered `ship` #3; the views showed `ship` as #2.
SUBSECTIONS = "## Tasks\n- [-] moved\n### Detail\n- [ ] detail\n## Tasks\n- [ ] ship\n- [x] shipped\n"


def test_a_subsection_ends_the_counted_run_in_python_and_every_view():
    tasks = features.list_tasks(SUBSECTIONS)
    assert [(t.index, t.state, t.text) for t in tasks] == [
        (1, "superseded", "moved"),
        (2, "open", "ship"),
        (3, "done", "shipped"),
    ]
    assert _run("tasks.js", SUBSECTIONS)["list"] == ["#2 ship"]
    card = _run("features.js", SUBSECTIONS)["card"]
    assert card[card.index("Open tasks") + 1] == "1"
    assert "tasks 1/2 done, 1 superseded" in _run("feature.js", SUBSECTIONS)["paragraphs"][0]
    assert features.resolve_task(SUBSECTIONS, "2").text == "ship"  # the view's #2 is the CLI's #2


def test_a_suffixed_tasks_heading_counts_in_python_and_every_view():
    body = (
        "## Tasks (phase 2)\n- [ ] one\n- [x] two\n### Tasksfoo\n- [ ] not here\n## Notes\n- [ ] nor here\n"
    )
    assert [f"#{t.index} {t.text}" for t in features.list_tasks(body) if t.is_open] == ["#1 one"]
    assert _run("tasks.js", body)["list"] == ["#1 one"]
    card = _run("features.js", body)["card"]
    assert card[card.index("Open tasks") + 1] == "1"
    assert "tasks 1/2 done" in _run("feature.js", body)["paragraphs"][0]


def test_a_task_added_after_a_subsection_lands_where_it_counts():
    body, index = features.add_task_at(SUBSECTIONS.split("## Tasks\n- [ ] ship")[0], "new one")
    assert index == 2
    assert "- [-] moved\n- [ ] new one\n### Detail" in body
    assert [t.text for t in features.list_tasks(body)] == ["moved", "new one"]


# Valid CommonMark the line rules got wrong.
@pytest.mark.parametrize("indent", [" ", "  ", "   "])
def test_items_indented_up_to_three_spaces_are_top_level_tasks(indent: str):
    body = "## Tasks\n" + indent + "- [ ] first\n- [ ] second\n"
    expected = [f"#{t.index} {t.text}" for t in features.list_tasks(body) if t.is_open]
    assert expected == ["#1 first", "#2 second"]
    assert _run("tasks.js", body)["list"] == expected
    card = _run("features.js", body)["card"]
    assert card[card.index("Open tasks") + 1] == "2"


def test_an_empty_child_heading_ends_the_counted_run():
    body = "## Tasks\n- [x] first\n###\n- [ ] under empty heading\n## Tasks\n- [ ] last\n"
    expected = [f"#{t.index} {t.text}" for t in features.list_tasks(body) if t.is_open]
    assert expected == ["#2 last"]
    assert _run("tasks.js", body)["list"] == expected
    assert "tasks 1/2 done" in _run("feature.js", body)["paragraphs"][0]


def test_a_child_item_and_a_fenced_example_are_not_tasks():
    body = "## Tasks\n- [ ] parent\n  - [ ] child step\n```\n- [ ] in a code block\n```\n1. [ ] ordered\n"
    expected = [f"#{t.index} {t.text}" for t in features.list_tasks(body) if t.is_open]
    assert expected == ["#1 parent", "#2 ordered"]
    assert _run("tasks.js", body)["list"] == expected


def test_the_markdown_parser_confirms_the_structure():
    from markdown_it import MarkdownIt

    parser = MarkdownIt()
    assert [t.tag for t in parser.parse("## Tasks\n###\n") if t.type == "heading_open"] == ["h2", "h3"]
    for indent in (" ", "  ", "   "):
        items = [
            t
            for t in parser.parse(f"## Tasks\n{indent}- [ ] first\n- [ ] second\n")
            if t.type == "list_item_open"
        ]
        assert len(items) == 2 and items[0].level == items[1].level


def _node(view: str, plan: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(plan, default=str)
    result = subprocess.run(
        ["node", "-e", HARNESS, str(VIEWS / view), payload], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


@pytest.mark.usefixtures("notes_remote")
def test_a_request_renders_with_its_identity_ask_and_result_and_no_task_graph(tmp_root: Path):
    from agent_cli import requests
    from agent_cli.ops import requests as request_ops

    request_ops.request_new(
        tmp_root, slug="look-at", title="Look", ask="Is the parser safe?", category="review", use=False
    )
    capture = request_ops.request_capture(
        tmp_root, slug="an-idea", title="Idea", ask="cache the index", category="idea"
    )
    req = features.resolve(tmp_root, "1")
    snapshot = status.collect(tmp_root)
    assert snapshot["features"] == [] and [r["id"] for r in snapshot["requests"]] == [1, 2]
    page = {"record_kind": "request", "category": "review"}
    files = {"features/001-work/plan.md": req.body}  # the harness page's path
    out = _node(
        "feature.js", {"tasks": dataview_tasks(req.body), "page": page, "files": files, "snapshot": {}}
    )
    header, ask, result = out["paragraphs"][:3]
    assert header.startswith("**Request 001** · **active** · review") and "tasks" not in header
    assert ask == "ask: Is the parser safe?" and result == "result: not delivered yet"
    assert features.list_tasks(req.body) == [] and requests.result_of(req) == ""
    feature_header = _node("feature.js", {"tasks": dataview_tasks(BODY), "snapshot": {}})["paragraphs"][0]
    assert feature_header.startswith("**active** · tasks 2/6 done")
    plain = json.loads(json.dumps(snapshot, default=str))
    cards = _node("index.js", {"tasks": [], "snapshot": plain, "input": {"section": "requests"}})["card"]
    assert cards == [
        "Request 001 · Look",
        "active · review · Is the parser safe? · next: deliver the ask, then complete it with the result",
        "Request 002 · Idea",
        f"proposed · idea · cache the index · next: {capture.data['next_action']}",
    ]
    assert _node("features.js", {"tasks": [], "snapshot": plain})["card"] == []


def test_status_json_and_real_pane_keep_all_task_counting_cases(tmp_root: Path):
    cases = [
        (BODY, ["#2 write the tests", "#3 write the docs", "#6 deferred, a custom status", "#7 land it"], 2),
        (SUBSECTIONS, ["#2 ship"], 1),
        ("## Tasks (phase 2)\n- [ ] one\n- [x] two\n### Tasksfoo\n- [ ] not here\n", ["#1 one"], 1),
        ("## Tasks\n- [x] first\n###\n- [ ] hidden\n## Tasks\n- [ ] last\n", ["#2 last"], 1),
        (
            "## Tasks\n- [ ] parent\n  - [ ] child\n```\n- [ ] example\n```\n1. [ ] ordered\n",
            ["#1 parent", "#2 ordered"],
            0,
        ),
        *[
            (f"## Tasks\n{indent}- [ ] first\n- [ ] second\n", ["#1 first", "#2 second"], 0)
            for indent in (" ", "  ", "   ")
        ],
    ]
    probes = []
    for body, expected, done in cases:
        _snapshot(tmp_root, body, render=True)
        snapshot = json.loads((tmp_root / status.STATUS_JSON).read_text())
        [row] = snapshot["features"]
        assert row["open_tasks"] == len(expected)
        assert row["done_tasks"] == done
        assert [f"#{t['index']} {t['text']}" for t in row["open_task_items"]] == expected
        probes.append({"snapshot": snapshot, "expected": expected})
    root = VIEWS.parents[2]
    result = subprocess.run(
        [
            "node",
            str(root / "tests/fixtures/probe_dashboard.js"),
            str(root / "harness/obsidian/plugins/agent-terminal-focus/main.js"),
            json.dumps(probes),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["parityCases"] == len(cases)
