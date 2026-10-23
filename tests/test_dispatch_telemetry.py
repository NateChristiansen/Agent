"""Dispatch lifecycle telemetry: the `dispatch.*` events the supervisor and the
manager's dispatch actions record explicitly, the terminal cost source, and the routing that lets
only the verifier, never the worker, record into the main sink.

Every test runs against a fixture root made a git checkout of its own, so `sink_root` resolves to
it and every event lands in the test's temporary directory. The loss file's temp directory is
moved under the test's directory too, and an autouse guard fails any write outside it.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, features, repos
from agent_cli.dispatch import telemetry_events, verification
from agent_cli.dispatch.options import verifier_env, worker_env
from agent_cli.telemetry import recorder as recorder_mod
from dispatch_helpers import (  # noqa: F401 -- droot is a pytest fixture
    _new,
    _new_fast,
    _set_alpha_dispatch,
    droot,
    envelope,
    one,
    says,
    settle,
    turn,
)

ROUTING = ("AGENT_TELEMETRY_SINK", "AGENT_TELEMETRY_PARENT")
# the package re-exports a function named `evaluate`, which hides the submodule attribute
evaluate_mod = importlib.import_module("agent_cli.dispatch.evaluate")
terminal_mod = importlib.import_module("agent_cli.dispatch.terminal")


@pytest.fixture(autouse=True)
def isolated_sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh recorder, the loss file under this test's directory, and a record of every path
    the sink appends to. Teardown fails the test if any write left `tmp_path`."""
    loss_tmp = tmp_path / "losstmp"
    loss_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(loss_tmp))
    monkeypatch.setattr(recorder_mod, "_process", None)
    written: list[Path] = []
    original = recorder_mod.append

    def guarded(path: Path, line: bytes) -> None:
        written.append(Path(path))
        original(path, line)

    monkeypatch.setattr(recorder_mod, "append", guarded)
    yield written
    base = tmp_path.resolve()
    outside = [p for p in written if not p.resolve().is_relative_to(base)]
    assert not outside, f"telemetry wrote outside the test directory: {outside}"


@pytest.fixture
def troot(droot: Path) -> Path:
    """`droot` as a git checkout of its own, so it is its own main checkout and sink root."""
    subprocess.run(["git", "init", "-q"], cwd=droot, check=True)
    assert telemetry_events.main_for(droot) == droot.resolve()
    return droot


def _events(root: Path, kind: str | None = None, dispatch_id: str | None = None) -> list[dict[str, Any]]:
    events_dir = telemetry_events.sink_dir(root.resolve()) / "events"
    out: list[dict[str, Any]] = []
    for path in sorted(events_dir.glob("*.jsonl")) if events_dir.is_dir() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if kind and event["kind"] != kind:
                continue
            if dispatch_id and event["dispatch"] != dispatch_id:
                continue
            out.append(event)
    return out


def _queued(root: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> dict:
    """A dispatch created and left queued: the detached supervisor is never spawned."""
    monkeypatch.setattr(
        dispatch.supervisor, "popen", type("P", (), {"pid": 1, "__init__": lambda self, *a, **k: None})
    )
    return _new(root, watch=False, **kw)


@pytest.fixture
def verifier_envs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """The environment of every verifier the supervisor launches, as it built it."""
    seen: list[dict[str, str]] = []
    original = verification.launch_prepared

    def capture(prepared: verification.Prepared) -> verification.Handle:
        seen.append(dict(prepared.environment))
        return original(prepared)

    monkeypatch.setattr(verification, "launch_prepared", capture)
    return seen


def _assert_lifecycle(root: Path, dispatch_id: str, provider: str) -> dict[str, dict[str, Any]]:
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in (
        "dispatch.preflight",
        "dispatch.launch",
        "dispatch.setup",
        "dispatch.verification",
        "dispatch.terminal",
    ):
        events = _events(root, kind, dispatch_id)
        assert len(events) == 1, kind
        event = events[0]
        assert event["provider"] == provider and event["feature"] == "001", kind
        assert event["source"] == "live" and event["schema"] == 1
        if kind != "dispatch.terminal":
            assert event["cost_usd"] is None and event["cost_source"] is None, kind
        by_kind[kind] = event
    setup = by_kind["dispatch.setup"]["data"]
    assert isinstance(setup["worktree_s"], float) and isinstance(setup["env_s"], float)
    assert setup["reused"] is False
    preflight = by_kind["dispatch.preflight"]
    assert preflight["outcome"] == "passed" and isinstance(preflight["duration_s"], float)
    assert {"allowed", "denied", "unmatched"} <= set(preflight["data"])
    launch = by_kind["dispatch.launch"]
    assert launch["outcome"] == "fresh" and launch["data"]["attempt"] == 1
    assert launch["data"]["budget_usd"] > 0 and launch["data"]["base"] == "origin/main"
    verification_event = by_kind["dispatch.verification"]
    assert verification_event["outcome"] == "verified"
    assert isinstance(verification_event["duration_s"], float)
    assert verification_event["data"]["run_id"]
    return by_kind


# ------------------------------------------------------------------ lifecycle per provider


def test_claude_run_records_every_lifecycle_event_with_dispatch_id_set(
    troot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch, verifier_envs: list[dict[str, str]]
) -> None:
    fake_claude.scenario([one(envelope(), commit="add the worker line")])  # a change, so the verifier runs
    d = _queued(troot, monkeypatch)
    # the supervisor runs inside a dispatch; explicit recording must not be silenced by it
    monkeypatch.setenv("AGENT_DISPATCH_ID", d["id"])
    final = dispatch.supervisor.run(troot, dispatch_id=d["id"])

    assert final["state"] == "done"
    events = _assert_lifecycle(troot, d["id"], "claude")
    terminal = events["dispatch.terminal"]
    assert terminal["outcome"] == "done" and terminal["data"] == {"turns": 5, "attempt": 1}
    assert terminal["cost_source"] == "provider" and terminal["cost_usd"] == pytest.approx(0.42)
    assert final["cost_evidence"] == {"1": "provider"}

    # the verifier the supervisor launched routes into the main sink, under this event
    assert len(verifier_envs) == 1
    env = verifier_envs[0]
    assert env["AGENT_TELEMETRY_SINK"] == str(troot.resolve() / ".agent" / "telemetry")
    assert env["AGENT_TELEMETRY_PARENT"] == events["dispatch.verification"]["event_id"]
    # the worker the same supervisor launched carries neither key
    assert not set(ROUTING) & set(fake_claude.calls()[0]["env"])


def test_codex_run_records_every_lifecycle_event(
    troot: Path, monkeypatch: pytest.MonkeyPatch, verifier_envs: list[dict[str, str]]
) -> None:
    from agent_cli.dispatch import codex_transport

    def fake_run(root: Path, d: dict, repo: object, **kwargs: object) -> dict:
        wt = Path(d["worktree"])  # a change, so the verifier runs
        (wt / "worker.txt").write_text("codex line\n", encoding="utf-8")
        for args in (
            ["add", "worker.txt"],
            ["-c", "user.name=w", "-c", "user.email=w@w", "commit", "-qm", "codex"],
        ):
            subprocess.run(["git", *args], cwd=wt, check=True)
        final = envelope(nonce=d["nonce"], cost=0.05)
        final["session_id"] = d["session_id"]
        final["cost_source"] = "local-estimate"
        return {
            "last_result": final,
            "session_established": True,
            "exit_code": 0,
            "timed_out": False,
            "stop_reason": "result",
            "estimated_cost_usd": 0.05,
            "resolved_model": "gpt-6.1-sol",
        }

    monkeypatch.setattr(codex_transport, "run_codex", fake_run)
    d = _new(troot, provider="codex", row="build", budget_reason="row band")

    assert d["state"] == "done"
    events = _assert_lifecycle(troot, d["id"], "codex")
    assert events["dispatch.launch"]["data"]["row"] == "build"
    terminal = events["dispatch.terminal"]
    assert terminal["cost_source"] == "estimate" and terminal["cost_usd"] == pytest.approx(0.05)
    assert verifier_envs[0]["AGENT_TELEMETRY_PARENT"] == events["dispatch.verification"]["event_id"]


# ------------------------------------------------------------------ terminal cost source


def test_resumed_dispatch_with_provider_evidence_on_every_attempt(troot: Path, fake_claude) -> None:
    fake_claude.scenario(
        [
            one(envelope("question", open_questions=["which port?"])),
            one(envelope(summary="done now", cost=0.84)),
        ]
    )
    d = _new_fast(troot)
    assert d["state"] == "question"
    d = dispatch.resume(troot, dispatch_id=d["id"], answer="use 8080", watch=True)
    assert d["state"] == "done" and d["attempt"] == 2

    terminals = _events(troot, "dispatch.terminal", d["id"])
    assert [t["outcome"] for t in terminals] == ["question", "done"]
    assert terminals[-1]["cost_source"] == "provider"
    assert terminals[-1]["cost_usd"] == pytest.approx(0.84) == d["cost_usd"]
    launches = _events(troot, "dispatch.launch", d["id"])
    assert [e["outcome"] for e in launches] == ["fresh", "resume"]
    preflights = _events(troot, "dispatch.preflight", d["id"])
    assert [e["data"]["source"] for e in preflights] == ["work-order", "answer"]
    assert preflights[-1]["data"]["attempt"] == 2

    dispatch.ack(troot, dispatch_id=d["id"], note="pushed it, secret detail here")
    (ack,) = _events(troot, "dispatch.ack", d["id"])
    assert ack["data"] == {"state": "done", "noted": True}
    assert "secret" not in json.dumps(ack)


def test_measured_provider_zero_is_provider(troot: Path, fake_claude) -> None:
    fake_claude.scenario([one(envelope(cost=0.0))])
    d = _new_fast(troot)
    (event,) = _events(troot, "dispatch.terminal", d["id"])
    assert event["cost_source"] == "provider" and event["cost_usd"] == 0.0


def test_initialized_zero_with_no_result_is_unknown(
    troot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _queued(troot, monkeypatch, no_checks=True)
    final = dispatch.cancel(troot, dispatch_id=d["id"])
    assert final["state"] == "cancelled" and final["cost_usd"] == 0.0
    (event,) = _events(troot, "dispatch.terminal", d["id"])
    assert event["cost_source"] == "unknown" and event["cost_usd"] is None


def _attempts(root: Path, d: dict, outcomes: list[dict[str, Any]]) -> dict:
    """Account each outcome as its own attempt, the first ones through the timeout auto-resume
    and the last through the terminal commit, the two places attempt cost is added."""
    feat = features.resolve(root, str(d["feature"]))
    for outcome in outcomes[:-1]:
        d = evaluate_mod.queue_auto_resume(root, d, feat, {"state": "blocked", **outcome}, "timeout")
    terminal_mod._account(d, outcomes[-1])
    return d


def _fresh_record(root: Path, fake_claude) -> dict:
    fake_claude.scenario([one(envelope())])
    d = dispatch.load(root, _new_fast(root)["id"])
    for key in ("cost_evidence", "cost_estimated_attempts", "cost_accounted_attempt", "cost_attempts"):
        d.pop(key, None)
    d.update(attempt=1, cost_usd=0.0)
    return d


def _copy(root: Path, fresh: dict) -> dict:
    """A copy of the fresh record at the stored record's current revision, so each case's saves
    apply on top of the previous case's."""
    d = copy.deepcopy(fresh)
    d["record_revision"] = dispatch.load(root, fresh["id"]).get("record_revision", 0)
    return d


PROVIDER = {"cost_usd": 0.3, "cost_evidence": "provider"}
# The provider total is the session's running total, so a second attempt reports 0.6 for 0.3.
PROVIDER_LATER = {"cost_usd": 0.6, "cost_evidence": "provider"}
ESTIMATE = {"cost_usd": 0.2, "cost_source": "local-estimate", "cost_evidence": "estimate"}
MISSING = {"cost_usd": 0.0}


COST_CASES = [
    ([PROVIDER, PROVIDER_LATER], "provider", 0.6),
    ([PROVIDER, ESTIMATE], "estimate", 0.5),
    ([ESTIMATE, PROVIDER], "estimate", 0.3),
    ([PROVIDER, MISSING], "unknown", None),
    ([MISSING, PROVIDER], "unknown", None),
]


def test_cost_source_covers_every_attempt(troot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every case accounts its attempts through the real auto-resume and terminal paths, on a copy
    of one fresh dispatch record (verify speed: it used to run two dispatches per case)."""
    fresh = _fresh_record(troot, fake_claude)
    for outcomes, source, cost in COST_CASES:
        with_field = _attempts(troot, _copy(troot, fresh), outcomes)
        figure = telemetry_events.cost_figure(with_field)
        assert figure == ((pytest.approx(cost), source) if cost is not None else (None, "unknown")), outcomes

        # the new field changes neither the cost nor the estimated-attempt list
        with monkeypatch.context() as mp:
            mp.setattr(evaluate_mod, "note_cost_evidence", lambda d, outcome: None)
            mp.setattr(terminal_mod, "note_cost_evidence", lambda d, outcome: None)
            without = _attempts(troot, _copy(troot, fresh), outcomes)
        assert "cost_evidence" not in without
        assert without["cost_usd"] == pytest.approx(with_field["cost_usd"])
        assert without.get("cost_estimated_attempts") == with_field.get("cost_estimated_attempts")


def test_auto_resume_estimate_is_an_estimated_attempt(troot: Path, fake_claude) -> None:
    """The timeout auto-resume lists an estimated attempt in `cost_estimated_attempts`, as the
    terminal commit does."""
    d = _attempts(troot, _fresh_record(troot, fake_claude), [ESTIMATE, PROVIDER])
    assert d["cost_evidence"] == {"1": "estimate", "2": "provider"}
    assert d["cost_estimated_attempts"] == [1]


def test_record_without_per_attempt_evidence_is_unknown() -> None:
    legacy = {"attempt": 1, "cost_usd": 1.25, "last_result": {"type": "result", "total_cost_usd": 1.25}}
    assert telemetry_events.cost_figure(legacy) == (None, "unknown")
    crash = {"attempt": 2, "cost_usd": 0.4, "cost_evidence": {"1": "provider"}}
    assert telemetry_events.cost_figure(crash) == (None, "unknown")


# ------------------------------------------------------------------ launch kinds, reverify


def test_timeout_auto_resume_and_manager_resume_are_labelled_apart(
    troot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch.state, "WATCHDOG_SECONDS", 0.5)
    _set_alpha_dispatch(troot, timeout_minutes=0.01)
    fake_claude.scenario([{"turns": [turn(says("still thinking"))]}, one(envelope(summary="second"))])
    d = _new_fast(troot)
    assert d["auto_resumes"][0]["reason"] == "timeout" and d["state"] == "done"
    settle(troot, d)
    d = dispatch.resume(troot, dispatch_id=d["id"], answer="once more", watch=True)

    launches = _events(troot, "dispatch.launch", d["id"])
    assert [(e["outcome"], e["data"]["attempt"]) for e in launches] == [
        ("fresh", 1),
        ("auto_resume", 2),
        ("resume", 3),
    ]
    # the timed-out attempt left no result, so the cumulative figure is unknown
    assert _events(troot, "dispatch.terminal", d["id"])[-1]["cost_source"] == "unknown"


def test_reverify_records_its_own_setup_split(
    troot: Path, fake_claude, verifier_envs: list[dict[str, str]]
) -> None:
    from agent_cli.dispatch import reverify as reverify_mod

    fake_claude.scenario([one(envelope())])
    d = _new(troot)
    assert d["state"] == "done"
    row = reverify_mod.reverify(troot, d["id"])

    (event,) = _events(troot, "dispatch.reverify", d["id"])
    assert event["outcome"] == row["result"]["status"] == "verified"
    assert event["data"]["run_id"] == row["binding"]["run_id"]
    assert isinstance(event["data"]["worktree_s"], float) and event["data"]["env_s"] is None
    assert isinstance(event["duration_s"], float) and event["duration_s"] == event["data"]["verify_s"]
    assert verifier_envs[-1]["AGENT_TELEMETRY_PARENT"] == event["event_id"]
    assert verifier_envs[-1]["AGENT_TELEMETRY_SINK"] == str(troot.resolve() / ".agent" / "telemetry")


# ------------------------------------------------------------------ routing


def test_worker_environments_never_carry_the_routing_keys(
    troot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent_cli.dispatch import codex_transport

    monkeypatch.setenv("AGENT_TELEMETRY_SINK", str(tmp_path / "manager-sink"))
    monkeypatch.setenv("AGENT_TELEMETRY_PARENT", "11111111-1111-4111-8111-111111111111")
    _set_alpha_dispatch(
        troot,
        env_allow=["AGENT_TELEMETRY_*"],
        env={"AGENT_TELEMETRY_SINK": str(tmp_path / "repo-sink"), "AGENT_TELEMETRY_PARENT": "x"},
    )
    fake_claude.scenario([one(envelope())])
    d = _new_fast(troot)
    rp = repos.load_registry(troot).require("alpha")

    assert not set(ROUTING) & set(worker_env(troot, d, rp))
    assert not set(ROUTING) & set(fake_claude.calls()[0]["env"])
    assert not set(ROUTING) & set(verifier_env(rp))

    seen: list[dict[str, str]] = []
    networks: list[bool] = []

    @contextlib.asynccontextmanager
    async def capture(root, repo, worktree, *, env, handle_request, policy):
        seen.append(dict(env))
        networks.append(policy.network_access)
        raise RuntimeError("stop before any server starts")
        yield  # pragma: no cover

    monkeypatch.setattr(codex_transport, "isolated_server", capture)
    codex_transport.run_codex(
        troot,
        {**dispatch.load(troot, d["id"]), "model": "gpt-6-luna"},
        rp,
        text="x",
        resume=False,
        mcp_servers={},
        inbox=tmp_path / "inbox",
        timeout=5.0,
    )
    assert len(seen) == 1 and seen[0]["AGENT_DISPATCH_ID"] == d["id"]
    assert not set(ROUTING) & set(seen[0])
    assert networks == [False]


def test_verifier_routing_replaces_whatever_the_manager_supplied(
    troot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch, verifier_envs, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_TELEMETRY_SINK", str(tmp_path / "manager-sink"))
    _set_alpha_dispatch(
        troot,
        verify=[{"command": "git diff --exit-code HEAD --", "timeout_seconds": 3}],
        verify_timeout_seconds=5,
        env_allow=["AGENT_TELEMETRY_*"],
        env={"AGENT_TELEMETRY_PARENT": "x"},
    )
    fake_claude.scenario([one(envelope(), commit="add the worker line")])  # a change, so the verifier runs
    d = _new(troot)
    (event,) = _events(troot, "dispatch.verification", d["id"])
    assert verifier_envs[0]["AGENT_TELEMETRY_SINK"] == str(troot.resolve() / ".agent" / "telemetry")
    assert verifier_envs[0]["AGENT_TELEMETRY_PARENT"] == event["event_id"]
    assert not (tmp_path / "manager-sink").exists()


# ------------------------------------------------------------------ failure


OUTCOME_FIELDS = (
    "state",
    "attempt",
    "cost_usd",
    "cost_evidence",
    "num_turns",
    "error",
    "verification_result",
)


def test_failing_sink_changes_no_outcome_or_record(
    troot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sink = telemetry_events.sink_dir(troot.resolve())
    sink.parent.mkdir(parents=True, exist_ok=True)
    # The fixture's own setup now writes events (task.add); start from no sink at all.
    shutil.rmtree(sink, ignore_errors=True)
    sink.write_text("not a directory\n", encoding="utf-8")  # the events directory cannot be made
    fake_claude.scenario([one(envelope(), commit="add the worker line")])  # a change, so the verifier runs
    broken = _new(troot)
    assert broken["state"] == "done" and broken["verification_result"]["status"] == "verified"
    assert _events(troot) == []
    loss_files = list((tmp_path / "losstmp" / "agent-telemetry").glob("*/loss.jsonl"))
    assert loss_files, "a failed write leaves an independent loss line"

    sink.unlink()
    monkeypatch.setattr(recorder_mod, "_process", None)
    settle(troot, broken)
    working = _new(troot)
    assert _events(troot, "dispatch.terminal", working["id"])
    for field in OUTCOME_FIELDS:
        assert broken.get(field) == working.get(field), field
