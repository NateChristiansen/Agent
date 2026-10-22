"""Two blind plan reviewers, Codex and Claude, replace manager pairing (phase 2.1).

They judge a frozen snapshot of the plan, and `plan-gate` lets ExitPlanMode through only for
the exact sha both finished reviews agreed to, a plan marked small, or after three rounds.
The worker launch is mocked throughout: `dispatch.new` writes a record and starts nothing.
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
    """End round `n`'s two reviewers (Codex, then Claude) with these summaries."""
    launch.write(f"001-mac-{2 * n - 1:02d}", state="done", summary=first)
    launch.write(f"001-mac-{2 * n:02d}", state="done", summary=second)


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
    saved = plan_review.record(tmp_root, sha)
    assert saved is not None and saved["session_id"] == SID and saved["launched_at"]


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
    saved = plan_review.record(tmp_root, rec["sha256"])
    assert saved is not None and saved["reviewers"] == [
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
    assert plan_review.record(tmp_root, plan_review.digest(PLAN.encode())) is None


def test_an_edit_after_launch_changes_neither_the_sha_nor_the_embedded_text(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    rec = _review(tmp_root, plan)
    plan.write_text(PLAN + "\n2. Also build Y.\n", encoding="utf-8", newline="")
    assert plan_review.record(tmp_root, rec["sha256"]) == {k: v for k, v in rec.items() if k != "states"}
    assert Path(rec["snapshot"]).read_text(encoding="utf-8") == PLAN
    assert all("Also build Y" not in c["prompt"] for c in launch.calls)
    # The verdict approves only its snapshot's sha: the edited plan is not covered...
    _finish(launch, "VERDICT: agree\nOptional: none")
    assert "plan changed since review 001-mac-01 and 001-mac-02" in _denied(_gate(tmp_root, plan))
    # ...and restoring the reviewed bytes restores the approval.
    plan.write_text(PLAN, encoding="utf-8", newline="")
    assert _gate(tmp_root, plan) == {}


def test_a_running_or_revised_review_of_the_same_sha_is_not_relaunched(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    with pytest.raises(Exception, match="still running"):
        _review(tmp_root, plan)
    _finish(launch, "VERDICT: revise\nBlocking: step 1 duplicates Codex")
    with pytest.raises(Exception, match="001-mac-01 of this exact plan asked for revision"):
        _review(tmp_root, plan)
    _finish(launch, "VERDICT: agree", "VERDICT: agree")
    assert (
        "001-mac-01 and 001-mac-02 already agreed"
        in ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID).text
    )
    assert len(launch.calls) == 2


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
    assert _gate(tmp_root, plan) == {}
    assert (
        "001-mac-07 already agreed"
        in ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID).text
    )
    launch.write("001-mac-07", state="done", summary="VERDICT: revise\nBlocking: x")
    assert "001-mac-07 asked for revision" in _denied(_gate(tmp_root, plan))
    assert launch.calls == []


# --- the gate -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker", ["review: small — one-line doc fix", "review: none: owner said skip", "  Review: small - typo"]
)
def test_a_small_or_none_marker_with_a_reason_passes(tmp_root: Path, marker: str):
    plan = _plan(tmp_root, f"# Plan\n\n{marker}\n\n## Context\n\nx\n")
    assert _gate(tmp_root, plan) == {}


@pytest.mark.parametrize("marker", ["review: small", "review: smallish change", "reviews: none — x"])
def test_a_marker_without_a_reason_or_misspelled_does_not_pass(tmp_root: Path, marker: str):
    assert "no cross-client review" in _denied(_gate(tmp_root, _plan(tmp_root, f"# Plan\n{marker}\n")))


def test_a_marker_below_the_first_fifteen_lines_does_not_count(tmp_root: Path):
    text = "# Plan\n" + "line\n" * 15 + "review: small — late\n"
    assert "no cross-client review" in _denied(_gate(tmp_root, _plan(tmp_root, text)))


def test_no_review_denies_with_the_launch_to_run(tmp_root: Path):
    plan = _plan(tmp_root)
    reason = _denied(_gate(tmp_root, plan))
    assert f"action `plan-review` and paths=[{plan.resolve()}]" in reason


def test_a_running_review_denies_and_names_it(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    assert "001-mac-01 and 001-mac-02 of this exact plan is still running" in _denied(_gate(tmp_root, plan))
    launch.write("001-mac-01", state="done", summary="VERDICT: agree")
    assert "001-mac-02 of this exact plan is still running" in _denied(_gate(tmp_root, plan))


def test_a_revise_verdict_denies(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    _finish(launch, "VERDICT: revise\nBlocking: no acceptance")
    assert "001-mac-01 asked for revision" in _denied(_gate(tmp_root, plan))


def test_an_agree_verdict_on_the_presented_sha_passes(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    _finish(launch, "VERDICT: agree\nOptional: rename step 1")
    assert _gate(tmp_root, plan) == {}
    # The same bytes presented as text only (no readable file), short of the final newline.
    assert _gate(tmp_root, text=PLAN.rstrip("\n")) == {}


def test_an_agree_that_is_not_the_first_line_or_not_done_does_not_pass(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    _finish(launch, "Looks fine.\nVERDICT: agree")
    assert "001-mac-01 gave no verdict: reviewer 001-mac-01 (codex gpt-6.1-sol)" in _denied(
        _gate(tmp_root, plan)
    )
    launch.write("001-mac-01", state="failed", summary="VERDICT: agree")
    assert "gave no verdict" in _denied(_gate(tmp_root, plan))


def test_a_record_whose_dispatch_reviewed_another_sha_is_not_a_verdict(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    _finish(launch, "VERDICT: agree")
    launch.write("001-mac-01", state="done", prompt="some other work order", summary="VERDICT: agree")
    assert "not a review of this sha" in _denied(_gate(tmp_root, plan))


def test_agree_on_an_older_sha_then_an_edit_denies(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    _review(tmp_root, plan)
    _finish(launch, "VERDICT: agree")
    assert _gate(tmp_root, plan) == {}
    plan.write_text(PLAN + "3. Ship it.\n", encoding="utf-8", newline="")
    assert "the plan changed since review 001-mac-01 and 001-mac-02" in _denied(_gate(tmp_root, plan))


def test_the_third_round_hands_the_decision_to_the_owner(tmp_root: Path, launch: Launcher):
    plan = _plan(tmp_root)
    for n in (1, 2, 3):
        plan.write_text(PLAN + f"\nround {n}\n", encoding="utf-8", newline="")
        _review(tmp_root, plan)
        _finish(launch, "VERDICT: revise\nBlocking: x", n=n)
        if n < 3:
            assert "asked for revision" in _denied(_gate(tmp_root, plan))
    result = _gate(tmp_root, plan)
    assert "permissionDecision" not in result  # never `allow`: the owner's own prompt still shows
    assert "round cap is reached, so the owner decides" in result["additionalContext"]
    plan.write_text(
        PLAN + "\nround 4\n", encoding="utf-8", newline=""
    )  # an unreviewed 4th version: still the owner's call
    assert "round cap" in _gate(tmp_root, plan)["additionalContext"]


def test_an_unmapped_session_and_a_broken_record_never_deny_for_that(tmp_root: Path, monkeypatch):
    plan = _plan(tmp_root, "review: none — owner skipped\n")
    payload = {"session_id": "nobody", "tool_name": "ExitPlanMode", "tool_input": {"planFilePath": str(plan)}}
    assert hooks.run(tmp_root, "plan-gate", json.dumps(payload)) == ("", 0)
    monkeypatch.setattr(plan_review, "record", lambda root, sha: 1 / 0)
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
    _review(tmp_root, _plan(tmp_root))
    _set_feature(tmp_root, "001-mac-01", 1)
    _set_feature(tmp_root, "001-mac-02", 1)
    _finish(launch, "VERDICT: agree", "VERDICT: revise\nBlocking: x")
    assert plan_review.launch_warning(tmp_root, feature=1, row="design")
    _finish(launch, "VERDICT: agree")
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
