"""Handoff views and records stay intact after retiring the one-time migration.

Audit 10 plan.md:115 (K3) retires migration preview/apply, not handoff display.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli import features
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401


def test_dispatch_new_terminal_resume_and_abandon_leave_plan_bytes_unchanged(droot: Path, fake_claude):
    from agent_cli import dispatch

    fake_claude.scenario([one(envelope(status="question", open_questions=["continue?"])), one(envelope())])
    path = features.resolve(droot, "1").dir / "plan.md"
    before = path.read_bytes()
    sent = _new_fast(droot)
    assert path.read_bytes() == before
    returned = dispatch.resume(droot, dispatch_id=sent["id"], answer="continue", watch=True)
    assert returned["state"] == "done"
    assert path.read_bytes() == before
    dispatch.abandon(droot, dispatch_id=sent["id"], note="finished probe")
    assert path.read_bytes() == before


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed to execute the Dataview view")
def test_dataview_renders_both_machines_and_refreshes_indexed_status():
    view = Path(__file__).resolve().parents[1] / "harness/obsidian/views/handoffs.js"
    harness = """
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const data = [
  {id: "001-win-01", repo: "alpha-api", status: "running", cost_usd: 0.5,
   file: {name: "win-01-alpha", link: "win handoff", tags: ["#handoff"]}},
  {id: "001-mac-01", repo: "alpha-api", status: "done", cost_usd: 0.25,
   file: {name: "mac-01-alpha", link: "mac handoff", tags: ["#handoff"]}},
];
function array(values) {
  return {
    where: (fn) => array(values.filter(fn)),
    sort: (fn) => array([...values].sort((a, b) => String(fn(a)).localeCompare(String(fn(b))))),
    map: (fn) => values.map(fn),
  };
}
const tables = [];
const dv = {
  current: () => ({file: {folder: "features/001-work"}}),
  pages: (query) => {
    if (query !== '\"features/001-work/handoffs\"') throw Error("wrong feature folder");
    return array(data);
  },
  table: (headers, rows) => tables.push({headers, rows}),
};
new Function("dv", source)(dv);
data[0].status = "done";
new Function("dv", source)(dv);
process.stdout.write(JSON.stringify(tables));
"""
    result = subprocess.run(["node", "-e", harness, str(view)], capture_output=True, text=True, check=True)
    tables = json.loads(result.stdout)
    assert tables[0]["rows"] == [
        ["mac handoff", "alpha-api", "done", "$0.25"],
        ["win handoff", "alpha-api", "running", "$0.50"],
    ]
    assert tables[1]["rows"][1][2] == "done"
