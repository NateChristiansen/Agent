"""Request execution through the shared dispatch engine: a review without task numbers in pinned
rounds whose provenance survives in the synced handoffs, general research in the registered
harness with no repo named, captured bugs and ideas refused before any side effect, and delivery
through request completion. Every scenario runs on disposable fixture roots with the fake worker
transport; none is a native client observation."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_cli import dispatch, features, frontmatter, ops, requests
from agent_cli import worktree as worktree_mod
from agent_cli.core import AgentError
from agent_cli.ops import requests as request_ops
from conftest import git
from dispatch_helpers import droot, envelope, one  # noqa: F401 -- droot is a pytest fixture

ASK = "Review the parser change and say whether it is safe to land."
VIEW = Path(__file__).resolve().parents[1] / "harness/obsidian/views/handoffs.js"


def _request(root: Path, category: str = "review", repos: list[str] | None = None) -> features.Feature:
    op = request_ops.request_new(
        root,
        slug=f"{category}-ask",
        title=f"A {category}",
        ask=ASK,
        category=category,
        repos=repos,
        use=False,
    )
    return features.resolve(root, str(op.data["id"]))


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n")
    git(["add", name], repo)
    git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name], repo)
    return git(["rev-parse", "HEAD"], repo).strip()


def _review(root: Path, feature: int, target: str, **kw: Any) -> dict[str, Any]:
    return dispatch.new(
        root,
        repo="alpha",
        feature=str(feature),
        prompt=f"# Review {target[:7]}\n\nreport blocking findings only",
        row="review",
        target=target,
        budget=1.0,
        watch=True,
        no_checks=True,
        **kw,
    )


def _no_side_effects(root: Path) -> None:
    assert not list((root / ".agent" / "dispatches").glob("*.json"))
    assert not list(root.glob("features/*/handoffs/*.md"))


def _rounds(root: Path, folder: str, handoffs: list[dict[str, Any]]) -> list[list[Any]]:
    """The request round view as Dataview runs it, over handoff frontmatter alone."""
    harness = """
const fs = require("fs");
const [view, folder, pages] = process.argv.slice(1);
const data = JSON.parse(pages);
function array(values) {
  return {
    where: (fn) => array(values.filter(fn)),
    sort: (fn) => array([...values].sort((a, b) => String(fn(a)).localeCompare(String(fn(b))))),
    map: (fn) => values.map(fn),
  };
}
const tables = [];
const dv = {
  current: () => ({file: {folder}, record_kind: "request"}),
  pages: () => array(data),
  table: (headers, rows) => tables.push({headers, rows}),
  paragraph: () => {},
};
new Function("dv", fs.readFileSync(view, "utf8"))(dv);
process.stdout.write(JSON.stringify(tables));
"""
    out = subprocess.run(
        ["node", "-e", harness, str(VIEW), folder, json.dumps(handoffs, default=str)],
        capture_output=True,
        text=True,
        check=True,
    )
    tables = json.loads(out.stdout)
    assert tables[1]["headers"] == ["Round", "Dispatch", "Target", "Checkout", "Compare", "PR", "Status"]
    return tables[1]["rows"]


# ---------------------------------------------------------------- review rounds


def test_review_rounds_pin_each_commit_keep_provenance_and_deliver_by_completion(droot: Path, fake_claude):
    alpha = droot / "repos" / "alpha-api"
    compare = git(["rev-parse", "HEAD"], alpha).strip()
    first, second = _commit(alpha, "one.txt"), _commit(alpha, "two.txt")
    req = _request(droot, repos=["alpha"])
    fake_claude.scenario(
        [
            one(envelope(summary="round 1: one blocking finding", next_steps=[])),
            one(envelope(summary="round 1 discussed: finding stands", next_steps=[])),
            one(envelope(summary="round 2: approve", next_steps=[])),
        ]
    )
    r1 = _review(droot, req.id, first, compare="main~2", pr="#12")
    assert r1["tasks"] == [] and r1["review"] == {"target": first, "compare_base": compare, "pr": "#12"}
    prompt = dispatch.state.prompt_text(droot, dispatch.load(droot, r1["id"]))
    assert "## Request context" in prompt and f"Request {req.number} review-ask" in prompt
    assert "None assigned" in prompt and f"- compare against: {compare}" in prompt
    assert "- pull request: #12" in prompt

    # Discussing the same artifact resumes its dispatch; the pinned provenance does not move.
    again = dispatch.resume(droot, dispatch_id=r1["id"], answer="is the finding still blocking?", watch=True)
    assert again["attempt"] == 2 and again["review"] == r1["review"]

    # A changed commit is a new, separately pinned dispatch with its own verdict.
    r2 = _review(droot, req.id, second, compare=compare)
    assert r1["id"] != r2["id"] and r2["tasks"] == []
    assert r2["review"] == {"target": second, "compare_base": compare, "pr": None}
    assert (r1["base"], r2["base"]) == (first, second)
    if r2["worktree"] == r1["worktree"]:
        # The engine reuses a clean finished worktree, so round 1 is no longer resumable in place.
        with pytest.raises(AgentError, match="a later dispatch reused it"):
            dispatch.resume(droot, dispatch_id=r1["id"], answer="one more question")

    metas = {}
    for d in (again, dispatch.load(droot, r2["id"])):
        meta, _ = frontmatter.load(droot / d["handoff"])
        metas[d["id"]] = meta
    assert metas[r1["id"]]["review_checkout"] == first != metas[r2["id"]]["review_checkout"] == second
    assert metas[r1["id"]]["review_compare"] == metas[r2["id"]]["review_compare"] == compare
    assert (metas[r1["id"]]["review_pr"], metas[r2["id"]]["review_pr"]) == ("#12", None)
    assert metas[r1["id"]]["review_target"] == first
    results = {d: dispatch.load(droot, d)["result"]["summary"] for d in metas}
    assert results == {r1["id"]: "round 1 discussed: finding stands", r2["id"]: "round 2: approve"}
    log = req.log_path.read_text(encoding="utf-8")
    assert f"review target {first}; compare {compare}; PR #12" in log

    with pytest.raises(AgentError, match=r1["id"]):
        request_ops.request_complete(droot, request=str(req.id), result="Round 2 approves.")
    for d in metas:
        dispatch.ack(droot, dispatch_id=d, note="read")
    request_ops.request_complete(droot, request=str(req.id), result="Round 2 approves.")
    done = features.resolve(droot, str(req.id))
    assert done.status == "done" and requests.result_of(done) == "Round 2 approves."
    assert features.resolve(droot, "1").status == "active"  # the ordinary feature is untouched


def test_round_view_reads_synced_handoffs_after_runtime_state_is_gone(droot: Path, fake_claude):
    if shutil.which("node") is None:
        pytest.skip("Node is needed to execute the Dataview view")
    alpha = droot / "repos" / "alpha-api"
    compare = git(["rev-parse", "HEAD"], alpha).strip()
    first, second = _commit(alpha, "one.txt"), _commit(alpha, "two.txt")
    req = _request(droot, repos=["alpha"])
    fake_claude.scenario([one(envelope()), one(envelope())])
    ids = [
        _review(droot, req.id, sha, compare=compare, pr=pr)["id"]
        for sha, pr in ((first, "PR-7"), (second, None))
    ]
    shutil.rmtree(droot / ".agent" / "dispatches")  # another machine: synced vault, no runtime records
    pages = []
    for path in sorted((req.dir / "handoffs").glob("*.md")):
        meta, _ = frontmatter.load(path)
        pages.append({**meta, "file": {"name": path.stem, "link": meta["id"], "tags": ["#handoff"]}})
    # A review handoff written before provenance was synced still renders a row.
    pages.append(
        {
            "id": "002-mac-00",
            "row": "review",
            "status": "done",
            "file": {"name": "old", "link": "old", "tags": ["#handoff"]},
        }
    )
    rows = _rounds(droot, req.rel_dir(droot), pages)
    assert rows == [
        [1, "old", "—", "—", "—", "—", "done"],
        [2, ids[0], first, first[:12], compare[:12], "PR-7", "done"],
        [3, ids[1], second, second[:12], compare[:12], "—", "done"],
    ]


def test_review_provenance_arguments_are_checked_before_any_side_effect(droot: Path):
    req = _request(droot, repos=["alpha"])
    head = git(["rev-parse", "HEAD"], droot / "repos" / "alpha-api").strip()
    with pytest.raises(AgentError, match="pass them with row=review"):
        dispatch.new(droot, repo="alpha", feature=str(req.id), prompt="# Build it", pr="#3", dry_run=True)
    with pytest.raises(AgentError, match="comparison base 'no-such-ref' is not a commit"):
        _review(droot, req.id, head, compare="no-such-ref")
    with pytest.raises(AgentError, match="must be one line"):
        _review(droot, req.id, head, pr="#3\n#4")
    _no_side_effects(droot)


# ---------------------------------------------------------------- captured requests


def test_a_captured_idea_cannot_preview_launch_or_resume_until_activated(droot: Path, fake_claude):
    op = request_ops.request_capture(
        droot,
        slug="faster-parse",
        title="Faster parse",
        ask="An idea for later.",
        category="idea",
        repos=["alpha"],
    )
    rid = str(op.data["id"])
    for dry_run in (True, False):
        with pytest.raises(AgentError, match="captured idea, still proposed"):
            ops.dispatch_new(droot, repo="alpha", prompt="# Do it", feature=rid, dry_run=dry_run)
    _no_side_effects(droot)
    assert not list(droot.glob("features/*/orders/*.md"))

    ops.feature_set(droot, feature=rid, status="active")
    fake_claude.scenario([one(envelope(status="question", open_questions=["which parser?"]))])
    sent = dispatch.new(droot, repo="alpha", feature=rid, prompt="# Do it", watch=True, no_checks=True)
    assert sent["state"] == "question" and sent["tasks"] == []
    ops.feature_set(droot, feature=rid, status="proposed")
    before = dispatch.load(droot, sent["id"])
    with pytest.raises(AgentError, match="captured idea, still proposed"):
        dispatch.resume(droot, dispatch_id=sent["id"], answer="the fast one")
    assert dispatch.load(droot, sent["id"])["attempt"] == before["attempt"]


def test_an_ordinary_proposed_feature_still_dispatches(droot: Path, fake_claude):
    ops.feature_set(droot, feature="1", status="proposed")
    preview = dispatch.new(droot, repo="alpha", feature="1", prompt="# Do it", dry_run=True)
    assert preview["dry_run"] and preview["label"] == "Feature 001" and "repo_source" not in preview


# ---------------------------------------------------------------- general research


def _harness_is(monkeypatch: pytest.MonkeyPatch, root: Path, repo: Path) -> None:
    """Make `repo` read as this harness's own checkout, as the real harness repo is. The fixture
    root is no Git checkout, so only the comparison is replaced, not where the root's state lives."""
    real = worktree_mod.is_main_checkout_of
    monkeypatch.setattr(
        worktree_mod,
        "is_main_checkout_of",
        lambda r, main: (
            main.resolve() == repo.resolve() if Path(r).resolve() == root.resolve() else real(r, main)
        ),
    )


def test_research_without_a_repo_runs_read_only_in_the_registered_harness(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch
):
    req = _request(droot, category="research")
    assert not req.meta.get("repos")
    _harness_is(monkeypatch, droot, droot / "repos" / "alpha-api")
    preview = ops.dispatch_new(
        droot,
        repo=None,
        prompt="# What do X and Y trade off?",
        feature=str(req.id),
        dry_run=True,
        no_checks=True,
    )
    assert preview.data["repo"] == "alpha-api" and preview.data["repo_source"]
    assert preview.text.startswith(f"dispatch preview — alpha-api for request {req.number:0>3}")
    assert "  repo: registered harness" in preview.text
    fake_claude.scenario([one(envelope(summary="X trades latency for memory", next_steps=[]))])
    sent = dispatch.new(
        droot,
        repo=None,
        feature=str(req.id),
        prompt="# What do X and Y trade off?",
        preview_id=preview.data["preview_id"],
        watch=True,
        no_checks=True,
    )
    assert sent["state"] == "done" and sent["repo"] == "alpha-api" and sent["writes"] == []
    assert sent["result"]["summary"] == "X trades latency for memory"
    assert not features.resolve(droot, str(req.id)).meta.get("repos")  # the harness was not added


def test_research_refuses_a_missing_or_ambiguous_harness_with_what_to_fix(
    droot: Path, monkeypatch: pytest.MonkeyPatch
):
    req = _request(droot, category="research")
    with pytest.raises(AgentError, match="no repo in agent.yaml is this harness checkout"):
        dispatch.new(droot, repo=None, feature=str(req.id), prompt="# Research", dry_run=True)
    reg = droot / "agent.yaml"
    data = yaml.safe_load(reg.read_text(encoding="utf-8"))
    alpha = next(r for r in data["repos"] if r["short"] == "alpha")
    data["repos"].append({**alpha, "name": "alpha-twin", "short": "twin"})
    reg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    _harness_is(monkeypatch, droot, droot / "repos" / "alpha-api")
    with pytest.raises(
        AgentError, match=r"several registered repos are this harness checkout \(alpha-api, alpha-twin\)"
    ):
        dispatch.new(droot, repo=None, feature=str(req.id), prompt="# Research", dry_run=True)
    _no_side_effects(droot)


@pytest.mark.parametrize("record", ["feature", "review request"])
def test_only_a_research_request_may_omit_the_repo(droot: Path, record: str):
    feature = "1" if record == "feature" else str(_request(droot, repos=["alpha"]).id)
    with pytest.raises(AgentError, match="`repo` is required"):
        dispatch.new(droot, repo=None, feature=feature, prompt="# Work", dry_run=True)


def test_cli_and_typed_tool_take_an_omitted_repo_and_review_provenance(droot: Path, run, monkeypatch):
    from agent_cli.mcp import server as mcp_server
    from agent_cli.mcp.tools import Ctx

    req = _request(droot, category="research")
    _harness_is(monkeypatch, droot, droot / "repos" / "alpha-api")
    cli = run(["dispatch", "new", "--feature", str(req.id), "--prompt", "# Research it", "--dry-run"])
    assert cli.code == 0, cli.err
    assert "for request" in cli.out and "registered harness" in cli.out
    spec = mcp_server.MANAGER_TOOLS["dispatch"]
    typed = mcp_server.call_op(
        spec.fn,
        Ctx(root=droot, profile="manager"),
        action="new",
        prompt="# Research it",
        feature=str(req.id),
        dry_run=True,
        session_id="s1",
    )
    assert typed["repo"] == "alpha-api"
    head = git(["rev-parse", "HEAD"], droot / "repos" / "alpha-api").strip()
    review = _request(droot, repos=["alpha"])
    cli = run(
        [
            "dispatch",
            "new",
            "alpha",
            "--feature",
            str(review.id),
            "--prompt",
            "# Review it",
            "--row",
            "review",
            "--target",
            head,
            "--compare",
            "HEAD",
            "--pr",
            "#9",
            "--budget",
            "1",
            "--dry-run",
        ]
    )
    assert cli.code == 0, cli.err
    assert f"review target: {head}" in cli.out and "pull request: #9" in cli.out
