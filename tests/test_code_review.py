"""Blind code review rounds on a branch head: the `code-review` and `code-ledger` actions and the
clean check landing will read.

The round engine is plan review's; these tests cover what code review adds: the subject (a worker
branch from its build dispatch, or a manager branch, base and acceptance file), the pin to the head
sha, a moved head, the report of a complete round, and the cap. The worker launch is mocked:
`dispatch.new` writes a record and starts nothing. Branches are real commits in the fixture's
alpha-api clone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli import code_review, dispatch, ops, sessions
from agent_cli.dispatch.state import dispatches_dir
from harness_root import git
from test_plan_review import Launcher

SID = "4b1c2d3e-0000-4000-8000-00000000c1a2"
REPO = "alpha-api"
WORKER = "001-mac-50"
ORDER = "Build the widget.\n\n## Acceptance\n\n1. `widget()` returns 3.\n"

CONTRACT = """\
Review the change from `{BASE}` to `{HEAD}`. First run `git rev-parse HEAD` and confirm it starts with \
`{HEAD}`; if not, return a question. Read the diff with `git diff {BASE} HEAD`. Change no file; restore \
any probe edit. Do not read other branches, later commits, or any handoff under `features/`: this \
review is blind.

The change's acceptance is below.

Do not stop with a certain number of findings. Deeply review the entire change and bring back the \
ENTIRE list of findings, to avoid multiple revision rounds as best possible.

Blocking rules:
1. A threshold or bar that is not in the acceptance is optional, never blocking.
2. Blocking means a failure against the acceptance on a path real use takes or plausibly would. A \
failure needing tampering, stacked faults, or input no one produces is optional unless you show it is \
likely.

Run probes where they settle a finding.

Return: the first line of your summary is exactly `VERDICT: agree` or `VERDICT: revise`. Revise only \
for blocking findings. List each blocking finding under `Blocking:`, and label every other point \
`Optional:`. Each finding states its file and line and the read or probe behind it. Put the whole \
review in your returned summary.

"""
REVISE = "VERDICT: revise\n\nBlocking:\n- widget.py:1 returns 2, not 3; read the file.\n"


@pytest.fixture
def launch(tmp_root: Path, monkeypatch: pytest.MonkeyPatch) -> Launcher:
    fake = Launcher(tmp_root)
    monkeypatch.setattr(dispatch, "new", fake)
    sessions.set_record(tmp_root, SID, name="mgr-001-x")
    return fake


@pytest.fixture
def clone(tmp_root: Path) -> Path:
    path = tmp_root / "repos" / REPO
    if not git(["rev-parse", "--verify", "--quiet", "HEAD"], path).strip():
        git(["commit", "-q", "--allow-empty", "-m", "start"], path)
    return path


def _sha(clone: Path, ref: str) -> str:
    return git(["rev-parse", ref], clone).strip()


def _commit(clone: Path, branch: str, n: int) -> str:
    """Put commit `n` on `branch` (created from main) and return its sha; main stays checked out."""
    exists = git(["branch", "--list", branch], clone).strip()
    git(["checkout", "-q", branch] if exists else ["checkout", "-q", "-b", branch, "main"], clone)
    (clone / "widget.py").write_text(f"def widget():\n    return {n}\n", encoding="utf-8")
    git(["add", "widget.py"], clone)
    git(["commit", "-q", "-m", f"widget {n}"], clone)
    head = _sha(clone, "HEAD")
    git(["checkout", "-q", "main"], clone)
    return head


def _worker(root: Path, clone: Path) -> str:
    """A finished build dispatch on branch `agent/widget`; returns the base it started from."""
    base = _sha(clone, "main")
    _commit(clone, "agent/widget", 1)
    path = dispatches_dir(root) / f"{WORKER}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": WORKER,
        "repo": REPO,
        "branch": "agent/widget",
        "base": base,
        "start_sha": base,
        "feature": "001-x",
        "state": "done",
        "thread": [{"from": "manager", "text": ORDER}],
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return base


def _acceptance(root: Path) -> Path:
    path = root / "features" / "001-x" / "acceptance.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ORDER, encoding="utf-8")
    return path


def _review(root: Path) -> dict[str, Any]:
    data = ops.dispatch_code_review(root, dispatch_id=WORKER, session_id=SID).data
    assert isinstance(data, dict)
    return data


def _manager(root: Path, base: str) -> ops.Op:
    return ops.dispatch_code_review(
        root, branch="mgr/widget", base=base, paths=[str(_acceptance(root))], repo=REPO, session_id=SID
    )


def _table(*rows: tuple[str, str, str]) -> str:
    return "| id | result | label | reason |\n" + "\n".join(
        f"| {i} | {r} | {lab} | read it |" for i, r, lab in rows
    )


def _round(
    root: Path, launch: Launcher, codex: str, claude: str = "VERDICT: agree", table: str = ""
) -> dict[str, Any]:
    """One whole worker-branch round through `code-review` calls."""
    rec = _review(root)
    assert rec["phase"] == "reviewing"
    first, second = (r["dispatch_id"] for r in rec["reviewers"])
    launch.write(first, state="done", summary=codex)
    launch.write(second, state="done", summary=claude)
    rec = _review(root)
    if rec["phase"] == "verifying":
        launch.write(rec["verifier"]["dispatch_id"], state="done", summary=table)
        rec = _review(root)
    return rec


def _clean(root: Path, head: str, branch: str = "agent/widget") -> bool:
    return code_review.clean(root, branch=branch, head=head, repo=REPO)


# --- the worker branch ------------------------------------------------------------------------------


def test_a_worker_round_pins_both_reviewers_to_the_head(tmp_root: Path, launch: Launcher, clone: Path):
    base = _worker(tmp_root, clone)
    head = _sha(clone, "agent/widget")
    rec = _review(tmp_root)
    assert (rec["repo"], rec["branch"], rec["head"], rec["base"]) == (REPO, "agent/widget", head, base)
    assert rec["round"] == 1 and rec["source"] == {"dispatch_id": WORKER}
    chosen = [(c["provider"], c["model"], c["effort"]) for c in launch.calls]
    assert chosen == [("codex", "gpt-6.1-sol", "high"), ("claude", "sonnet", "high")]
    block = f"----- BEGIN ACCEPTANCE -----\n{ORDER}----- END ACCEPTANCE -----\n"
    for call in launch.calls:
        assert (call["row"], call["writes"], call["no_checks"]) == ("review", [], True)
        assert (call["repo"], call["feature"], call["target"], call["compare"]) == (REPO, "001-x", head, base)
        assert call["base"] is None
        assert call["prompt"] == CONTRACT.format(BASE=base, HEAD=head) + block
    # The acceptance is frozen read-only; the round record sits where the clean check reads it.
    assert Path(rec["acceptance"]).read_text(encoding="utf-8") == ORDER
    assert code_review.round_path(tmp_root, head, 1).is_file()
    assert not _clean(tmp_root, head)


def test_a_call_while_reviewers_run_launches_nothing(tmp_root: Path, launch: Launcher, clone: Path):
    _worker(tmp_root, clone)
    _review(tmp_root)
    op = ops.dispatch_code_review(tmp_root, dispatch_id=WORKER, session_id=SID)
    assert "still running; nothing launched" in op.text and len(launch.calls) == 2
    launch.write("001-mac-01", state="done", summary=REVISE)
    op = ops.dispatch_code_review(tmp_root, dispatch_id=WORKER, session_id=SID)
    assert "001-mac-02 is still running" in op.text and len(launch.calls) == 2


def test_verified_blocking_findings_leave_the_head_not_clean(tmp_root: Path, launch: Launcher, clone: Path):
    base = _worker(tmp_root, clone)
    head = _sha(clone, "agent/widget")
    rec = _review(tmp_root)
    launch.write("001-mac-01", state="done", summary=REVISE)
    launch.write("001-mac-02", state="done", summary="VERDICT: agree")
    rec = _review(tmp_root)
    assert rec["phase"] == "verifying" and rec["findings"] == ["F1"]
    verifier = launch.calls[2]
    assert (verifier["provider"], verifier["target"], verifier["compare"]) == ("codex", head, base)
    assert verifier["writes"] == [] and verifier["repo"] == REPO
    assert "- F1: widget.py:1 returns 2, not 3; read the file." in verifier["prompt"]
    assert f"git diff {base} HEAD" in verifier["prompt"] and ORDER.strip() in verifier["prompt"]
    for identity in ("001-mac-01", "codex", "client", "reviewer"):
        assert identity not in verifier["prompt"]
    assert not _clean(tmp_root, head)  # verifying is not clean
    launch.write(
        verifier_id := rec["verifier"]["dispatch_id"],
        state="done",
        summary=_table(("F1", "kept", "blocking")),
    )
    op = ops.dispatch_code_review(tmp_root, dispatch_id=WORKER, session_id=SID)
    assert op.data["phase"] == "complete" and op.data["blocking"] == ["F1"]
    assert "verified blocking: F1. This head is not clean." in op.text
    assert not _clean(tmp_root, head)
    ledger = ops.dispatch_code_ledger(tmp_root, dispatch_id=WORKER).data
    assert ledger["findings"][0]["head"] == head
    assert ledger["findings"][0]["verifier"]["dispatch_id"] == verifier_id
    # The same head is reported again, never rerun.
    again = ops.dispatch_code_review(tmp_root, dispatch_id=WORKER, session_id=SID)
    assert "already complete" in again.text and len(launch.calls) == 3


def test_a_refuted_only_round_is_clean(tmp_root: Path, launch: Launcher, clone: Path):
    _worker(tmp_root, clone)
    head = _sha(clone, "agent/widget")
    rec = _round(tmp_root, launch, REVISE, table=_table(("F1", "refuted", "-")))
    assert rec["phase"] == "complete" and rec["blocking"] == []
    assert _clean(tmp_root, head)
    assert not code_review.clean(tmp_root, branch="other", head=head, repo=REPO)


def test_a_round_with_no_finding_is_clean_without_a_verifier(tmp_root: Path, launch: Launcher, clone: Path):
    _worker(tmp_root, clone)
    head = _sha(clone, "agent/widget")
    rec = _round(tmp_root, launch, "VERDICT: agree")
    assert rec["phase"] == "complete" and len(launch.calls) == 2
    assert _clean(tmp_root, head)


def test_a_stalled_round_is_not_clean(tmp_root: Path, launch: Launcher, clone: Path):
    _worker(tmp_root, clone)
    head = _sha(clone, "agent/widget")
    _review(tmp_root)
    launch.write("001-mac-01", state="failed", summary="")
    launch.write("001-mac-02", state="done", summary="VERDICT: agree")
    _review(tmp_root)  # relaunches the failed reviewer once
    launch.write("001-mac-03", state="failed", summary="")
    op = ops.dispatch_code_review(tmp_root, dispatch_id=WORKER, session_id=SID)
    assert "cannot complete" in op.text and op.data["stalled"]
    assert not _clean(tmp_root, head)


# --- a moved head ----------------------------------------------------------------------------------


def test_a_moved_head_starts_a_new_round_and_never_mixes_heads(tmp_root: Path, launch: Launcher, clone: Path):
    _worker(tmp_root, clone)
    first = _sha(clone, "agent/widget")
    _review(tmp_root)
    second = _commit(clone, "agent/widget", 2)
    op = ops.dispatch_code_review(tmp_root, dispatch_id=WORKER, session_id=SID)
    assert op.data["round"] == 2 and op.data["head"] == second and len(launch.calls) == 4
    assert all(c["target"] == second for c in launch.calls[2:])
    assert "older head" in op.text and first[:12] in op.text
    [old] = [r for r in code_review.records(tmp_root, branch="agent/widget") if r["head"] == first]
    assert old["phase"] == "reviewing"
    # The old round's returns do not complete the new head's round.
    launch.write("001-mac-01", state="done", summary="VERDICT: agree")
    launch.write("001-mac-02", state="done", summary="VERDICT: agree")
    assert not _clean(tmp_root, first) and not _clean(tmp_root, second)
    launch.write("001-mac-03", state="done", summary="VERDICT: agree")
    launch.write("001-mac-04", state="done", summary="VERDICT: agree")
    assert _review(tmp_root)["phase"] == "complete"
    assert _clean(tmp_root, second) and not _clean(tmp_root, first)


# --- a manager branch ------------------------------------------------------------------------------


def test_a_manager_round_names_branch_base_and_acceptance(tmp_root: Path, launch: Launcher, clone: Path):
    base = _sha(clone, "main")
    head = _commit(clone, "mgr/widget", 1)
    op = _manager(tmp_root, "main")
    rec = op.data
    assert (rec["branch"], rec["head"], rec["base"], rec["feature"]) == ("mgr/widget", head, base, "current")
    assert rec["source"] == {"acceptance_path": str(_acceptance(tmp_root).resolve())}
    assert all(c["target"] == head and c["compare"] == base for c in launch.calls)
    assert ORDER.strip() in launch.calls[0]["prompt"]
    launch.write("001-mac-01", state="done", summary="VERDICT: agree")
    launch.write("001-mac-02", state="done", summary="VERDICT: agree")
    assert "complete and clean" in _manager(tmp_root, "main").text
    assert code_review.clean(tmp_root, branch="mgr/widget", head=head)


def test_the_subject_must_be_one_or_the_other(tmp_root: Path, launch: Launcher, clone: Path):
    from agent_cli.core import AgentError

    _worker(tmp_root, clone)
    with pytest.raises(AgentError, match="not both"):
        ops.dispatch_code_review(tmp_root, dispatch_id=WORKER, branch="x", session_id=SID)
    with pytest.raises(AgentError, match="acceptance file"):
        ops.dispatch_code_review(tmp_root, branch="agent/widget", base="main", repo=REPO, session_id=SID)
    assert launch.calls == []


# --- the ledger and the cap ------------------------------------------------------------------------


def test_a_reraised_rejected_finding_is_a_dispute(tmp_root: Path, launch: Launcher, clone: Path):
    _worker(tmp_root, clone)
    kept = _table(("F1", "kept", "blocking"))
    _round(tmp_root, launch, REVISE, table=kept)
    op = ops.dispatch_code_ledger(
        tmp_root, dispatch_id=WORKER, finding_id="F1", disposition="rejected", note="spec says 2"
    )
    assert op.data["disposition"]["reason"] == "spec says 2"
    _commit(clone, "agent/widget", 2)
    _round(tmp_root, launch, REVISE, table=_table(("F2", "kept", "blocking")))
    by_branch = ops.dispatch_code_ledger(
        tmp_root, branch="agent/widget", repo=REPO, finding_id="F2", reraise_of="F1"
    )
    assert by_branch.data["reraise_of"] == "F1"
    disputes = ops.dispatch_code_ledger(tmp_root, dispatch_id=WORKER).data["disputes"]
    assert [(d["kind"], d["finding"]["id"]) for d in disputes] == [("reraised-rejected", "F2")]


def test_after_the_cap_the_owner_decides(tmp_root: Path, launch: Launcher, clone: Path):
    _worker(tmp_root, clone)
    for n in range(code_review.ROUND_CAP):
        if n:
            _commit(clone, "agent/widget", n + 1)
        rec = _round(tmp_root, launch, REVISE, table=_table((f"F{n + 1}", "kept", "blocking")))
        assert rec["phase"] == "complete" and rec["round"] == n + 1
    launched = len(launch.calls)
    head = _commit(clone, "agent/widget", 9)
    op = ops.dispatch_code_review(tmp_root, dispatch_id=WORKER, session_id=SID)
    assert len(launch.calls) == launched
    assert "reached the cap of 3" in op.text and "owner decides" in op.text
    assert op.data["head"] == head and op.data["rounds_used"] == 3
    assert [f["id"] for f in op.data["ledger"]["findings"]] == ["F1", "F2", "F3"]
    assert any(line.startswith("F3 (round 3, blocking)") for line in op.data["owner_lines"])
    assert not _clean(tmp_root, head)


def test_plan_reviews_do_not_read_code_records(tmp_root: Path, launch: Launcher, clone: Path):
    from agent_cli import plan_review

    _worker(tmp_root, clone)
    _round(tmp_root, launch, "VERDICT: agree")
    assert plan_review.all_records(tmp_root) == []
