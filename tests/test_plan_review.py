"""Blind plan review rounds replace manager pairing (phase 2.1).

Two reviewers, Codex and Claude, judge a frozen snapshot of the plan; one verifier keeps or refutes
their findings; `plan-review` advances the round one step per call. `plan-gate` lets ExitPlanMode
through only when the latest complete round on the exact sha kept no finding as blocking, for a
plan marked small, or after three complete rounds. The worker launch is mocked throughout:
`dispatch.new` writes a record and starts nothing.
"""

from __future__ import annotations

import importlib
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, hooks, ops, plan_review, sessions
from agent_cli.dispatch.state import dispatches_dir

SID = "4b1c2d3e-0000-4000-8000-00000000c1a0"
ROOT = Path(__file__).resolve().parents[1]
PLAN = "# Plan\n\n## Context\n\nThe owner wants X.\n\n## Steps\n\n1. Build X.\n"


class Launcher:
    """Stands in for `dispatch.new`: records each launch's arguments and writes the dispatch
    record the gate reads, without starting a worker."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict[str, Any]] = []

    def __call__(self, root: Path, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        dispatch_id = f"001-mac-{len(self.calls):02d}"
        self.write(dispatch_id, state="queued", prompt=str(kw["prompt"]))
        return {"id": dispatch_id, "state": "queued"}

    def write(
        self, dispatch_id: str, *, state: str, prompt: str | None = None, summary: str | None = None
    ) -> None:
        path = dispatches_dir(self.root) / f"{dispatch_id}.json"
        d: dict[str, Any] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"id": dispatch_id}
        )
        if prompt is not None:
            d["thread"] = [{"from": "manager", "text": prompt}]
        d["state"] = state
        d["result"] = {"status": state, "summary": summary} if summary is not None else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d))


@pytest.fixture
def launch(tmp_root: Path, monkeypatch: pytest.MonkeyPatch) -> Launcher:
    fake = Launcher(tmp_root)
    monkeypatch.setattr(dispatch, "new", fake)
    monkeypatch.setattr(
        "agent_cli.verify_record.harness_repo",
        lambda root: type("Repo", (), {"name": "agent", "default_branch": "main"}),
    )
    sessions.set_record(tmp_root, SID, name="mgr-001-x")
    return fake


def _plan(root: Path, text: str = PLAN) -> Path:
    path = root / "features" / "001-x" / "plan.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _review(root: Path, plan: Path) -> dict[str, Any]:
    data = ops.dispatch_plan_review(root, paths=[str(plan)], session_id=SID).data
    assert isinstance(data, dict)
    return data


def _finish(launch: Launcher, first: str, second: str = "VERDICT: agree", n: int = 1) -> None:
    """End round `n`'s two reviewers (Codex, then Claude) with these summaries, for a round
    launched when no verifier ran before it."""
    launch.write(f"001-mac-{2 * n - 1:02d}", state="done", summary=first)
    launch.write(f"001-mac-{2 * n:02d}", state="done", summary=second)


def _table(*rows: tuple[str, str, str]) -> str:
    return "| id | result | label | reason |\n" + "\n".join(
        f"| {i} | {r} | {lab} | read it |" for i, r, lab in rows
    )


def _round(
    root: Path,
    launch: Launcher,
    plan: Path,
    codex: str = "VERDICT: agree",
    claude: str = "VERDICT: agree",
    table: str | None = None,
) -> dict[str, Any]:
    """Run one whole round through `plan-review` calls: launch, both reviewers return, then the
    verifier (when one launches) returns `table`."""
    rec = _review(root, plan)
    assert rec["phase"] == "reviewing"
    first, second = (r["dispatch_id"] for r in rec["reviewers"])
    launch.write(first, state="done", summary=codex)
    launch.write(second, state="done", summary=claude)
    rec = _review(root, plan)
    if rec["phase"] == "verifying":
        launch.write(rec["verifier"]["dispatch_id"], state="done", summary=table or "")
        rec = _review(root, plan)
    return rec


def _gate(root: Path, plan: Path | None = None, text: str | None = None) -> dict[str, Any]:
    tool_input: dict[str, str] = {}
    if plan is not None:
        tool_input["planFilePath"] = str(plan)
    if text is not None:
        tool_input["plan"] = text
    out, code = hooks.run(
        root, "plan-gate", json.dumps({"tool_name": "ExitPlanMode", "tool_input": tool_input})
    )
    assert code == 0
    return json.loads(out).get("hookSpecificOutput", {}) if out else {}


def _denied(result: dict[str, Any]) -> str:
    assert result.get("permissionDecision") == "deny", result
    return str(result["permissionDecisionReason"])


# --- the launch ---------------------------------------------------------------------------------


CONTRACT = """\
Plan review of sha256 {sha}: a fresh, read-only review of a plan before the owner sees it.

Review only the plan snapshot below. It is frozen: do not open the plan's live file, which may have \
changed since. Change no file; this dispatch is read-only.

Do not stop with a certain number of findings. Deeply review the entire plan and bring back the \
ENTIRE list of findings, to avoid multiple revision rounds as best possible.

Blocking rules:
1. A threshold or bar that is not in the plan's own acceptance criteria is optional, never blocking.
2. Blocking means a failure against the plan's acceptance on a path real use takes or plausibly \
would. A failure needing tampering, stacked faults, or input no one produces is optional unless you \
show it is likely.

Reply: the first line of your returned summary is exactly `VERDICT: agree` or `VERDICT: revise`. \
Revise only for blocking findings. List each blocking finding under `Blocking:`, and label every \
other point `Optional:`. Each finding states its path and the read or probe behind it. Put the \
whole review in your returned summary.

"""


def test_the_launch_freezes_the_plan_and_embeds_it(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    rec = _review(tmp_root, plan)
    sha = plan_review.digest(PLAN.encode())
    assert len(launch.calls) == 2
    assert rec["sha256"] == sha and rec["plan_path"] == str(plan.resolve())
    for call in launch.calls:
        assert call["row"] == "review" and call["writes"] == [] and call["base"] == "main"
        assert call["no_checks"] is True and call["repo"] == "agent" and call["feature"] == "current"
        assert f"----- BEGIN PLAN SNAPSHOT sha256 {sha} -----\n{PLAN}----- END" in call["prompt"]
        assert str(plan) not in call["prompt"]
    snapshot = Path(rec["snapshot"])
    assert snapshot.read_bytes() == PLAN.encode() and stat.S_IMODE(snapshot.stat().st_mode) == 0o444
    [saved] = plan_review.records(tmp_root, sha)
    assert saved["session_id"] == SID and saved["launched_at"]
    assert (saved["round"], saved["phase"], saved["verifier"]) == (1, "reviewing", None)
    assert plan_review.round_path(tmp_root, sha, 1).is_file()


def test_the_work_order_is_the_deep_contract_then_the_snapshot(tmp_root: Path, launch: Launcher):
    _review(tmp_root, _plan(tmp_root))
    sha = plan_review.digest(PLAN.encode())
    snapshot = (
        f"----- BEGIN PLAN SNAPSHOT sha256 {sha} -----\n{PLAN}----- END PLAN SNAPSHOT sha256 {sha} -----\n"
    )
    assert launch.calls[0]["prompt"] == CONTRACT.format(sha=sha) + snapshot
    assert launch.calls[1]["prompt"] == launch.calls[0]["prompt"]


@pytest.mark.parametrize("manager", ["claude", "codex"])
def test_one_launch_starts_two_blind_reviewers_whatever_the_managers_client(
    tmp_root: Path, launch: Launcher, manager: str
):
    sessions.set_field(tmp_root, SID, "provider", manager)
    rec = _review(tmp_root, _plan(tmp_root))
    chosen = [(c["provider"], c["model"], c["effort"]) for c in launch.calls]
    assert chosen == [("codex", "gpt-6.1-sol", "high"), ("claude", "sonnet", "high")]
    [saved] = plan_review.records(tmp_root, rec["sha256"])
    assert saved["reviewers"] == [
        {"dispatch_id": "001-mac-01", "provider": "codex", "model": "gpt-6.1-sol", "effort": "high"},
        {"dispatch_id": "001-mac-02", "provider": "claude", "model": "sonnet", "effort": "high"},
    ]
    assert plan_review.reviewer_ids(saved) == ["001-mac-01", "001-mac-02"]


def test_a_failed_second_launch_names_the_first_reviewer(tmp_root: Path, launch: Launcher, monkeypatch):
    from agent_cli.core import AgentError

    def second_fails(root: Path, **kw: Any) -> dict[str, Any]:
        if launch.calls:
            raise AgentError("no claude")
        return launch(root, **kw)

    monkeypatch.setattr(dispatch, "new", second_fails)
    plan = _plan(tmp_root)
    with pytest.raises(AgentError, match="launched 001-mac-01 but not its claude reviewer: no claude"):
        _review(tmp_root, plan)
    assert plan_review.records(tmp_root, plan_review.digest(PLAN.encode())) == []


def test_an_edit_after_launch_changes_neither_the_sha_nor_the_embedded_text(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    rec = _review(tmp_root, plan)
    plan.write_text(PLAN + "\n2. Also build Y.\n", encoding="utf-8", newline="")
    assert plan_review.records(tmp_root, rec["sha256"]) == [rec]
    assert Path(rec["snapshot"]).read_text(encoding="utf-8") == PLAN
    assert all("Also build Y" not in c["prompt"] for c in launch.calls)
    # A round covers only its snapshot's sha: the edited plan is not covered...
    _finish(launch, "VERDICT: agree\nOptional: none")
    assert "plan changed since review 001-mac-01 and 001-mac-02" in _denied(_gate(tmp_root, plan))
    # ...and with the reviewed bytes back, the next call completes the round, which opens the gate.
    plan.write_text(PLAN, encoding="utf-8", newline="")
    assert "is reviewing" in _denied(_gate(tmp_root, plan))
    assert _review(tmp_root, plan)["phase"] == "complete"
    assert _gate(tmp_root, plan) == {}


def test_a_running_round_launches_nothing(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    op = ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID)
    assert "001-mac-01 and 001-mac-02 is still running; nothing launched" in op.text
    launch.write("001-mac-01", state="done", summary="VERDICT: agree")
    op = ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID)
    assert "reviewer 001-mac-02 is still running" in op.text
    assert len(launch.calls) == 2
    # A failed reviewer waits for its running peer: the call still launches nothing.
    launch.write("001-mac-01", state="failed", summary="crashed")
    op = ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID)
    assert "reviewer 001-mac-02 is still running; nothing launched" in op.text
    assert len(launch.calls) == 2
    launch.write("001-mac-02", state="done", summary="VERDICT: agree")
    assert (
        "relaunched 001-mac-03 once"
        in ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID).text
    )


def test_a_failed_relaunch_launch_keeps_the_earlier_relaunch(tmp_root: Path, launch: Launcher, monkeypatch):
    from agent_cli.core import AgentError

    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    _finish(launch, "no verdict", "no verdict either")

    def second_fails(root: Path, **kw: Any) -> dict[str, Any]:
        if len(launch.calls) == 3:
            raise AgentError("dispatch cap")
        return launch(root, **kw)

    monkeypatch.setattr(dispatch, "new", second_fails)
    with pytest.raises(AgentError, match="dispatch cap"):
        _review(tmp_root, plan)
    [rec] = plan_review.records(tmp_root, plan_review.digest(PLAN.encode()))
    assert [r["dispatch_id"] for r in rec["reviewers"]] == ["001-mac-03", "001-mac-02"]


def test_concurrent_reviews_of_the_same_bytes_at_two_paths_get_separate_rounds(
    tmp_root: Path, launch: Launcher, monkeypatch
):
    import threading

    from agent_cli.ops import plan_review as op_mod

    first, second = _plan(tmp_root), tmp_root / "features" / "001-x" / "copy.md"
    second.write_text(PLAN, encoding="utf-8", newline="")
    arrived = threading.Barrier(2, timeout=1)
    real = op_mod._next_round

    def meet(root: Path, plan: str, sha: str) -> int:
        try:
            arrived.wait()  # without a shared sha guard, both calls pick a round here together
        except threading.BrokenBarrierError:
            pass
        return real(root, plan, sha)

    monkeypatch.setattr(op_mod, "_next_round", meet)
    errors: list[BaseException] = []

    def run(path: Path) -> None:
        try:
            ops.dispatch_plan_review(tmp_root, paths=[str(path)], session_id=SID)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(p,)) for p in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    recs = plan_review.records(tmp_root, plan_review.digest(PLAN.encode()))
    assert sorted(r["round"] for r in recs) == [1, 2]
    assert {r["plan_path"] for r in recs} == {str(first.resolve()), str(second.resolve())}


def test_a_second_call_after_a_complete_round_launches_a_fresh_round_on_the_same_bytes(
    tmp_root: Path, launch: Launcher
):
    plan = _plan(tmp_root)
    first = _round(tmp_root, launch, plan)
    assert (first["round"], first["phase"], first["blocking"]) == (1, "complete", [])
    assert len(launch.calls) == 2
    second = _review(tmp_root, plan)
    assert (second["round"], second["phase"], second["sha256"]) == (2, "reviewing", first["sha256"])
    assert len(launch.calls) == 4
    assert [r["round"] for r in plan_review.records(tmp_root, first["sha256"])] == [1, 2]
    # The earlier clean round still opens the gate while the fresh one runs.
    assert _gate(tmp_root, plan) == {}


# --- reading a reviewer's findings --------------------------------------------------------------


def test_every_blocking_and_optional_item_is_one_finding():
    from agent_cli.ops.plan_review import findings_of

    summary = (
        "VERDICT: revise\n"
        "Blocking:\n"
        "1. Step 2 has no test.\n"
        "   Path: plan.md step 2; read the steps.\n"
        "   - sub-point stays with it\n"
        "2. **Gate** reads the wrong file.\n"
        "\n"
        "**Optional:**\n"
        "- Rename step 1.\n"
        "- none\n"
        "Optional: one inline point\n"
        "Checked:\n"
        "- every step\n"
        "\n"
        "Blocking: none\n"
    )
    assert findings_of(summary) == [
        "Step 2 has no test. Path: plan.md step 2; read the steps. - sub-point stays with it",
        "**Gate** reads the wrong file.",
        "Rename step 1.",
        "one inline point",
    ]
    assert findings_of("VERDICT: agree\nOptional: none\nAll good.") == []


# --- the combined verdict -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("codex", "claude", "state", "named"),
    [
        ("VERDICT: agree", "VERDICT: agree", "agree", "001-mac-01 and 001-mac-02"),
        ("VERDICT: agree", "VERDICT: revise\nBlocking: x", "revise", "001-mac-02"),
        (
            "VERDICT: revise\nBlocking: x",
            "VERDICT: revise\nBlocking: y",
            "revise",
            "001-mac-01 and 001-mac-02",
        ),
        ("VERDICT: agree", None, "running", "001-mac-02"),
        ("VERDICT: revise\nBlocking: x", None, "running", "001-mac-02"),
        ("VERDICT: agree", "Looks fine.", "none", "001-mac-02"),
    ],
)
def test_the_two_verdicts_combine(
    tmp_root: Path, launch: Launcher, codex: str, claude: str | None, state: str, named: str
):
    rec = _review(tmp_root, _plan(tmp_root))
    launch.write("001-mac-01", state="done", summary=codex)
    if claude is not None:
        launch.write("001-mac-02", state="done", summary=claude)
    seen = plan_review.verdict(tmp_root, rec)
    assert (seen.state, seen.dispatch_id) == (state, named)
    if state == "none":
        assert seen.detail == "reviewer 001-mac-02 (claude sonnet): it ended done without a verdict line"


def test_a_one_reviewer_record_from_before_still_reads(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    sha = plan_review.digest(PLAN.encode())
    launch.write("001-mac-07", state="done", prompt=f"review {sha}", summary="VERDICT: agree")
    old = {"sha256": sha, "plan_path": str(plan.resolve()), "dispatch_id": "001-mac-07", "provider": "codex"}
    plan_review.reviews_dir(tmp_root).mkdir(parents=True, exist_ok=True)
    (plan_review.reviews_dir(tmp_root) / f"{sha}.json").write_text(json.dumps(old), encoding="utf-8")
    assert plan_review.verdict(tmp_root, old) == plan_review.Verdict("agree", "001-mac-07", "VERDICT: agree")
    assert (
        plan_review.records(tmp_root, sha) == [old]
        and plan_review.rounds_used(tmp_root, str(plan.resolve())) == 1
    )
    assert _gate(tmp_root, plan) == {}
    launch.write("001-mac-07", state="done", summary="VERDICT: revise\nBlocking: x")
    assert "001-mac-07 asked for revision" in _denied(_gate(tmp_root, plan))
    assert launch.calls == []
    # A call on the same bytes starts round 2 beside it; the legacy record was round 1.
    rec = _review(tmp_root, plan)
    assert (rec["round"], len(launch.calls)) == (2, 2)
    assert "round 2 (001-mac-01 and 001-mac-02) of this exact plan is reviewing" in _denied(
        _gate(tmp_root, plan)
    )
    launch.write("001-mac-01", state="done", summary="VERDICT: agree")
    launch.write("001-mac-02", state="done", summary="VERDICT: agree")
    assert _review(tmp_root, plan)["phase"] == "complete"
    assert _gate(tmp_root, plan) == {}  # the newest complete round decides
    assert plan_review.rounds_used(tmp_root, str(plan.resolve())) == 2


def test_a_legacy_agreement_opens_until_a_newer_round_completes_with_a_blocking_finding(
    tmp_root: Path, launch: Launcher
):
    plan = _plan(tmp_root)
    sha = plan_review.digest(PLAN.encode())
    launch.write("001-mac-07", state="done", prompt=f"review {sha}", summary="VERDICT: agree")
    old = {"sha256": sha, "plan_path": str(plan.resolve()), "dispatch_id": "001-mac-07"}
    plan_review.reviews_dir(tmp_root).mkdir(parents=True, exist_ok=True)
    (plan_review.reviews_dir(tmp_root) / f"{sha}.json").write_text(json.dumps(old), encoding="utf-8")
    _review(tmp_root, plan)
    assert _gate(tmp_root, plan) == {}  # round 2 is still reviewing: the legacy agreement stands
    launch.write("001-mac-01", state="done", summary="VERDICT: revise\nBlocking:\n- Step 1 has no test.")
    launch.write("001-mac-02", state="done", summary="VERDICT: agree")
    assert _review(tmp_root, plan)["phase"] == "verifying"
    launch.write("001-mac-03", state="done", summary=_table(("F1", "kept", "blocking")))
    _review(tmp_root, plan)
    assert "round 2 (001-mac-01 and 001-mac-02) kept F1 as blocking" in _denied(_gate(tmp_root, plan))


# --- the gate -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker", ["review: small — one-line doc fix", "review: none: owner said skip", "  Review: small - typo"]
)
def test_a_small_or_none_marker_with_a_reason_passes(tmp_root: Path, marker: str):
    plan = _plan(tmp_root, f"# Plan\n\n{marker}\n\n## Context\n\nx\n")
    assert _gate(tmp_root, plan) == {}


@pytest.mark.parametrize("marker", ["review: small", "review: smallish change", "reviews: none — x"])
def test_a_marker_without_a_reason_or_misspelled_does_not_pass(tmp_root: Path, marker: str):
    assert "no blind review round" in _denied(_gate(tmp_root, _plan(tmp_root, f"# Plan\n{marker}\n")))


def test_a_marker_below_the_first_fifteen_lines_does_not_count(tmp_root: Path):
    text = "# Plan\n" + "line\n" * 15 + "review: small — late\n"
    assert "no blind review round" in _denied(_gate(tmp_root, _plan(tmp_root, text)))


def test_no_review_denies_with_the_launch_to_run(tmp_root: Path):
    plan = _plan(tmp_root)
    reason = _denied(_gate(tmp_root, plan))
    assert f"action `plan-review` and paths=[{plan.resolve()}]" in reason
    assert "call `plan-review` again on the same plan after the supervisor reports a return" in reason


def test_a_round_in_progress_denies_and_says_how_it_advances(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    reason = _denied(_gate(tmp_root, plan))
    assert "round 1 (001-mac-01 and 001-mac-02) of this exact plan is reviewing" in reason
    assert "call `plan-review` again on the same plan after the supervisor reports a return" in reason
    # Both verdicts back but not yet advanced: a reviewer's agreement alone does not open it.
    _finish(launch, "VERDICT: agree")
    assert "is reviewing" in _denied(_gate(tmp_root, plan))
    _finish(launch, "VERDICT: revise\nBlocking:\n- Step 1 has no test.")
    assert _review(tmp_root, plan)["phase"] == "verifying"
    assert "is verifying (verifier 001-mac-03)" in _denied(_gate(tmp_root, plan))


@pytest.mark.parametrize(
    ("codex", "claude", "table"),
    [
        ("VERDICT: agree", "VERDICT: agree\nOptional: none", None),  # clean: no findings at all
        (
            "VERDICT: revise\nBlocking:\n- Step 1 has no test.",
            "VERDICT: revise\nBlocking:\n- The gate reads the wrong file.",
            _table(("F1", "refuted", "-"), ("F2", "refuted", "")),
        ),
        (
            "VERDICT: revise\nBlocking:\n- Step 1 has no test.",
            "VERDICT: agree\nOptional:\n- Rename step 1.",
            _table(("F1", "kept", "optional"), ("F2", "kept", "optional")),
        ),
    ],
    ids=["clean", "refuted-only", "optional-only"],
)
def test_a_round_with_no_verified_blocking_finding_opens_the_gate(
    tmp_root: Path, launch: Launcher, codex: str, claude: str, table: str | None
):
    plan = _plan(tmp_root)
    rec = _round(tmp_root, launch, plan, codex, claude, table)
    assert (rec["phase"], rec["blocking"]) == ("complete", [])
    assert len(launch.calls) == (2 if table is None else 3)
    assert _gate(tmp_root, plan) == {}
    # The same bytes presented as text only (no readable file), short of the final newline.
    assert _gate(tmp_root, text=PLAN.rstrip("\n")) == {}


def test_a_verified_blocking_finding_keeps_the_gate_shut(tmp_root: Path, launch: Launcher):
    from agent_cli import plan_review_ledger as ledger

    plan = _plan(tmp_root)
    rec = _round(
        tmp_root,
        launch,
        plan,
        "VERDICT: revise\nBlocking:\n- Step 1 has no test.\nOptional:\n- Rename step 1.",
        "VERDICT: agree\nOptional: the gate reads the wrong file",
        _table(("F1", "kept", "blocking"), ("F2", "kept", "optional"), ("F3", "refuted", "-")),
    )
    assert (rec["phase"], rec["blocking"], rec["findings"]) == ("complete", ["F1"], ["F1", "F2", "F3"])
    entries = ledger.findings(ledger.load(tmp_root, str(plan)))
    assert [(e["id"], e["reviewer"]["client"], e["reviewer"]["dispatch_id"]) for e in entries] == [
        ("F1", "codex", "001-mac-01"),
        ("F2", "codex", "001-mac-01"),
        ("F3", "claude", "001-mac-02"),
    ]
    assert entries[0]["text"] == "Step 1 has no test."
    reason = _denied(_gate(tmp_root, plan))
    assert "round 1 (001-mac-01 and 001-mac-02) kept F1 as blocking after verification" in reason
    assert "for a fresh round" in reason


def test_the_completing_call_names_blocking_findings_and_disputes(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    _finish(launch, "VERDICT: revise\nBlocking:\n- Step 1 has no test.", "VERDICT: revise\nBlocking:\n- y")
    _review(tmp_root, plan)
    launch.write(
        "001-mac-03", state="done", summary=_table(("F1", "kept", "blocking"), ("F2", "refuted", "-"))
    )
    op = ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID)
    assert "round 1 is complete: verified blocking: F1." in op.text
    assert "Disputes for the owner: F2 (claude-refuted)." in op.text
    assert isinstance(op.data, dict) and op.data["disputes"][0]["kind"] == "claude-refuted"


def test_a_reviewer_without_a_verdict_is_relaunched_once_then_the_round_stays_incomplete(
    tmp_root: Path, launch: Launcher
):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    _finish(launch, "Looks fine.\nVERDICT: agree")
    op = ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID)
    assert "relaunched 001-mac-03 once (001-mac-01: it ended done without a verdict line)" in op.text
    relaunch = launch.calls[2]
    assert (relaunch["provider"], relaunch["model"]) == ("codex", "gpt-6.1-sol")
    assert relaunch["prompt"] == launch.calls[0]["prompt"]
    assert isinstance(op.data, dict)
    assert op.data["reviewers"][0] == {
        "dispatch_id": "001-mac-03",
        "relaunch_of": "001-mac-01",
        "provider": "codex",
        "model": "gpt-6.1-sol",
        "effort": "high",
    }
    launch.write("001-mac-03", state="failed", summary="VERDICT: agree")
    op = ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID)
    assert "round 1 cannot complete: reviewer 001-mac-01 and its relaunch 001-mac-03" in op.text
    assert "bring it to the owner" in op.text
    assert len(launch.calls) == 3
    assert "cannot complete" in _denied(_gate(tmp_root, plan))
    ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID)
    assert len(launch.calls) == 3  # a stalled round launches nothing more
    assert plan_review.rounds_used(tmp_root, str(plan.resolve())) == 0


@pytest.mark.parametrize(
    ("prompt", "summary"),
    [
        ("some other work order", "VERDICT: agree"),  # its dispatch reviewed another sha
        (None, "VERDICT: revise\nAll wrong."),  # revise with no finding listed
    ],
)
def test_an_unusable_reviewer_return_is_relaunched(
    tmp_root: Path, launch: Launcher, prompt: str | None, summary: str
):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    _finish(launch, "VERDICT: agree")
    launch.write("001-mac-02", state="done", prompt=prompt, summary=summary)
    assert (
        "relaunched 001-mac-03 once"
        in ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID).text
    )
    assert launch.calls[2]["provider"] == "claude"


def test_a_verifier_that_leaves_ids_without_a_result_is_relaunched_once(tmp_root: Path, launch: Launcher):
    from agent_cli import plan_review_ledger as ledger

    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    _finish(launch, "VERDICT: revise\nBlocking:\n- a\n- b")
    assert _review(tmp_root, plan)["phase"] == "verifying"
    launch.write("001-mac-03", state="done", summary=_table(("F1", "kept", "blocking")))
    op = ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID)
    assert "leaving F2 without a result; relaunched the verifier once as 001-mac-04" in op.text
    assert ledger.load(tmp_root, str(plan))["verifiers"]["1"]["replaces"] == "001-mac-03"
    launch.write("001-mac-04", state="done", summary="no table")
    op = ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID)
    assert "cannot complete: the relaunched verifier 001-mac-04 ended done leaving F1, F2" in op.text
    assert len(launch.calls) == 4
    rec = plan_review.records(tmp_root, plan_review.digest(PLAN.encode()))[-1]
    assert rec["phase"] == "verifying"
    assert "cannot complete" in _denied(_gate(tmp_root, plan))


def test_agree_on_an_older_sha_then_an_edit_denies(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    _round(tmp_root, launch, plan)
    assert _gate(tmp_root, plan) == {}
    plan.write_text(PLAN + "3. Ship it.\n", encoding="utf-8", newline="")
    assert "the plan changed since review 001-mac-01 and 001-mac-02" in _denied(_gate(tmp_root, plan))


def test_the_third_complete_round_hands_the_decision_to_the_owner_with_the_ledger(
    tmp_root: Path, launch: Launcher
):
    from agent_cli import plan_review_ledger as ledger

    plan = _plan(tmp_root)
    # An incomplete round does not count toward the cap.
    _review(tmp_root, plan)
    _finish(launch, "VERDICT: revise\nBlocking:\n- never verified")
    _review(tmp_root, plan)
    for n in (1, 2, 3):
        plan.write_text(PLAN + f"\nround {n}\n", encoding="utf-8", newline="")
        rec = _round(
            tmp_root,
            launch,
            plan,
            f"VERDICT: revise\nBlocking:\n- Step {n} has no test.",
            f"VERDICT: revise\nBlocking:\n- Claude point {n}.",
            # Ids run on across rounds: round 1 holds F1, each later round two more.
            _table((f"F{2 * n}", "kept", "blocking"), (f"F{2 * n + 1}", "refuted", "-")),
        )
        assert rec["phase"] == "complete"
        if n < 3:
            assert "as blocking after verification" in _denied(_gate(tmp_root, plan))
    assert plan_review.rounds_used(tmp_root, str(plan.resolve())) == 3
    ledger.dispose(tmp_root, str(plan), finding_id="F2", disposition="fixed", note="step 1 now has a test")
    result = _gate(tmp_root, plan)
    assert "permissionDecision" not in result  # never `allow`: the owner's own prompt still shows
    note = result["additionalContext"]
    assert "3 complete review rounds" in note and "round cap is reached, so the owner decides" in note
    assert "- F2 (round 2, blocking): Step 1 has no test. [fixed: step 1 now has a test]" in note
    assert "- F6 (round 4, blocking): Step 3 has no test. [no disposition]" in note
    assert "- dispute claude-refuted: F3 (round 2): Claude point 1." in note
    assert "never verified" not in note
    plan.write_text(
        PLAN + "\nround 4\n", encoding="utf-8", newline=""
    )  # an unreviewed 4th version: still the owner's call
    assert "round cap" in _gate(tmp_root, plan)["additionalContext"]


def test_legacy_records_count_one_round_each_toward_the_cap(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    plan_review.reviews_dir(tmp_root).mkdir(parents=True, exist_ok=True)
    for n in (1, 2):
        sha = f"{n:064x}"
        old = {"sha256": sha, "plan_path": str(plan.resolve()), "dispatch_id": f"001-mac-9{n}"}
        (plan_review.reviews_dir(tmp_root) / f"{sha}.json").write_text(json.dumps(old), encoding="utf-8")
    rec = _round(
        tmp_root,
        launch,
        plan,
        "VERDICT: revise\nBlocking:\n- x",
        table=_table(("F1", "kept", "blocking")),
    )
    assert rec["round"] == 3
    assert plan_review.rounds_used(tmp_root, str(plan.resolve())) == 3
    assert "round cap is reached" in _gate(tmp_root, plan)["additionalContext"]


def test_an_unmapped_session_and_a_broken_record_never_deny_for_that(tmp_root: Path, monkeypatch):
    plan = _plan(tmp_root, "review: none — owner skipped\n")
    payload = {"session_id": "nobody", "tool_name": "ExitPlanMode", "tool_input": {"planFilePath": str(plan)}}
    assert hooks.run(tmp_root, "plan-gate", json.dumps(payload)) == ("", 0)
    monkeypatch.setattr(plan_review, "records", lambda root, sha: 1 / 0)
    assert hooks.run(tmp_root, "plan-gate", json.dumps({**payload, "tool_input": {"plan": "x"}})) == ("", 0)


def test_plan_gate_is_wired_on_claude_exit_plan_mode():
    from agent_cli.hooks.groups import wired

    config = json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    assert wired(config, "PreToolUse", "ExitPlanMode")[0] == "plan-gate"
    assert "plan-gate" not in wired(config, "PreToolUse", "AskUserQuestion")


def test_pairing_config_is_gone_and_peers_validation_stays(tmp_path: Path):
    from agent_cli import repos
    from agent_cli.repos_validate import validate_registry_data

    data = {"repos": [], "pairing": {"auto_same_feature": "no"}}
    assert validate_registry_data(data) == ["agent.yaml: unknown key 'pairing'"]
    (tmp_path / "agent.yaml").write_text(json.dumps(data), encoding="utf-8")
    assert repos.load_registry(tmp_path).repos == []
    assert any(
        "peers" in p for p in validate_registry_data({"repos": [], "peers": {"defer_nonurgent_s": -1}})
    )


# --- A Codex manager's trust question is never blocked -----------------------------------

TRUST_QUESTION = "May I trust the disposable fixture under a $1/120-second cap? The plan is already approved."


def test_a5_01_a_codex_trust_question_passes_every_stop_hook(tmp_root: Path, monkeypatch):
    """`plan-gate-stop` read this question as an approval ask and blocked the
    Codex manager's stop, so the owner never saw it. The hook is gone. Since then the
    ask guard blocks a plain-text ask once, until `owner_question` registers it, which is what
    raises the hand; once registered, no Stop hook blocks it."""
    from agent_cli.hooks import attention

    monkeypatch.setattr(
        importlib.import_module("agent_cli.hooks.activity"), "refresh_tab", lambda *a, **k: None
    )
    config = json.loads((ROOT / ".codex/hooks.json").read_text(encoding="utf-8"))
    commands = [h["command"] for g in config["hooks"]["Stop"] for h in g["hooks"]]
    assert not any("plan-gate-stop" in c for c in commands)
    assert "plan-gate-stop" not in hooks.NAMES
    sessions.set_record(tmp_root, SID, name="mgr-045-x", feature={"id": 45, "slug": "x"})
    sessions.set_field(tmp_root, SID, "provider", "codex")
    attention.raise_question(tmp_root, SID, "manual", summary=TRUST_QUESTION.split("?")[0] + "?")
    payload = {"session_id": SID, "hook_event_name": "Stop", "last_assistant_message": TRUST_QUESTION}
    for name in [c.split("agent hook ", 1)[1].split()[0] for c in commands] + ["plan-gate-stop"]:
        out, code = hooks.run(tmp_root, name, json.dumps(payload))
        assert code == 0 and '"block"' not in out, name
    assert hooks.run(tmp_root, "plan-gate-stop", json.dumps(payload)) == ("", 0)


# --- 060 gap 6: `dispatch new` warns on an unreviewed build -------------------------------------


def _set_feature(root: Path, dispatch_id: str, feature: int) -> None:
    path = dispatches_dir(root) / f"{dispatch_id}.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    d["feature"] = feature
    path.write_text(json.dumps(d))


def test_a_build_with_no_agreed_review_on_its_feature_warns(tmp_root: Path, launch: Launcher):
    assert "feature 001 has no agreed plan review" in plan_review.launch_warning(
        tmp_root, feature=1, row="build"
    )
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    _set_feature(tmp_root, "001-mac-01", 1)
    _set_feature(tmp_root, "001-mac-02", 1)
    _finish(launch, "VERDICT: agree")
    # Both reviewers agreed, but the round is not complete until `plan-review` advances it.
    assert plan_review.launch_warning(tmp_root, feature=1, row="design")
    _review(tmp_root, plan)
    assert plan_review.launch_warning(tmp_root, feature=1, row="build") == ""
    # Another feature's agreement does not cover this one.
    assert "feature 002" in plan_review.launch_warning(tmp_root, feature=2, row="build")


@pytest.mark.parametrize("row", ["probe", "review", "mechanical", None])
def test_a_non_implementation_row_never_warns(tmp_root: Path, row: str | None):
    assert plan_review.launch_warning(tmp_root, feature=1, row=row) == ""


def test_dispatch_new_adds_the_warning_and_still_launches(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_cli.ops import dispatch as ops_dispatch

    launched: list[dict[str, Any]] = []

    def fake_new(root: Path, **kw: Any) -> dict[str, Any]:
        launched.append(kw)
        return {"id": "001-mac-09", "state": "queued", "feature": 1, "row": kw["row"]}

    monkeypatch.setattr(dispatch, "new", fake_new)
    monkeypatch.setattr(ops_dispatch, "dispatch_line", lambda d: "line")
    monkeypatch.setattr(ops_dispatch, "shown_record", lambda d: dict(d))
    op = ops.dispatch_new(tmp_root, repo="agent", prompt="Build X.", row="build")
    assert launched and op.text.startswith("line\n  plan review: feature 001")
    assert isinstance(op.data, dict) and op.data["plan_review_warning"]
    op = ops.dispatch_new(tmp_root, repo="agent", prompt="Look.", row="probe")
    assert op.text == "line" and isinstance(op.data, dict) and op.data["plan_review_warning"] == ""
