"""A harness attempt that needs the full suite runs it once, as a pass record (C6).

On 2026-10-06 the supervisor ran the full suite in the worktree and `agent land` ran it again for
the same tree: seven pairs, 92 minutes. A full selection on a clean harness head now goes through
`verify_record.record`, which holds a verification lane itself, and the dispatch relies on that
record the way it relies on a reused one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli import verify_record
from agent_cli.dispatch import lane, test_select, verification
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.state import load, save
from dispatch_helpers import droot  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture
from test_dispatch_verification import (
    finish,
    ready,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture
)

RECORD: dict[str, Any] = {"run_id": "a" * 32, "tree": "b" * 40}
AWAIT_LANE = lane.await_lane


@pytest.fixture
def full_harness_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean harness worktree whose selection is full and which has no record yet."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a certified attempt takes no lane of its own")

    def covers(_root: Path, _worktree: Path) -> bool:
        return True

    def no_record(_root: Path, _worktree: Path, _base: str | None, _selector: object = None) -> None:
        return None

    def full(_worktree: Path, _base: str | None, _head: str, _policy: int = 1) -> test_select.Selection:
        return test_select.Selection("full", "core", ("src/agent_cli/core.py",))

    monkeypatch.setattr(verify_record, "covers", covers)
    monkeypatch.setattr(verify_record, "reusable", no_record)
    monkeypatch.setattr(test_select, "select", full)
    monkeypatch.setattr(lane, "await_lane", refuse)
    monkeypatch.setattr(verification, "launch_prepared", refuse)


def test_a_full_attempt_is_certified_once_and_relied_on(
    ready: tuple[Path, dict[str, Any]], full_harness_head: None, monkeypatch: pytest.MonkeyPatch
):
    root, d = ready
    made: list[str] = []

    def record(_root: Path, rev: str, **_kwargs: object) -> dict[str, Any]:
        made.append(rev)
        return {**RECORD, "reused": False}

    def lookup(_root: Path, _rev: str, **_kwargs: object) -> tuple[dict[str, Any], str]:
        return RECORD, "ok"

    monkeypatch.setattr(verify_record, "record", record)
    monkeypatch.setattr(verify_record, "lookup", lookup)
    gate.execute(root, d["id"], d["attempt"])
    after = load(root, d["id"])
    assert len(made) == 1 and after["verification"]["mode"] == "record"
    assert finish(root, after)["verification_result"]["status"] == "verified"


def test_certificate_selection_context_names_observed_head_and_time(
    ready: tuple[Path, dict[str, Any]], full_harness_head: None, monkeypatch: pytest.MonkeyPatch
):
    root, d = ready
    monkeypatch.setenv("AGENT_NOW", "2026-10-03T10:00:00+00:00")
    monkeypatch.setattr(verify_record, "record", lambda *_args, **_kw: {**RECORD, "reused": False})
    gate.execute(root, d["id"], d["attempt"])
    after = load(root, d["id"])
    row = after["verification_scopes"][0]
    assert row["head_sha"] == after["verification"]["binding"]["final_sha"]
    assert row["observed_at"] == "2026-10-03T10:00:00+00:00"


def test_a_certificate_that_fails_is_a_failed_verification_with_its_tests(
    ready: tuple[Path, dict[str, Any]], full_harness_head: None, monkeypatch: pytest.MonkeyPatch
):
    root, d = ready
    run_dir = root / ".agent" / "verify" / "runs" / ("c" * 32)
    run_dir.mkdir(parents=True)
    (run_dir / "output.log").write_text("FAILED tests/test_procs.py::test_imports - boom\n", encoding="utf-8")

    def record(_root: Path, rev: str, **_kwargs: object) -> dict[str, Any]:
        raise verify_record.RecordFailed(f"{rev[:12]} did not pass: a command did not pass", run_dir)

    monkeypatch.setattr(verify_record, "record", record)
    gate.execute(root, d["id"], d["attempt"])
    result = finish(root, load(root, d["id"]))["verification_result"]
    assert result["status"] == "failed"
    assert "did not pass" in result["reasons"][0]
    assert "FAILED tests/test_procs.py::test_imports - boom" in result["reasons"]


def test_a_selection_s_extra_commands_reach_the_certificate(
    ready: tuple[Path, dict[str, Any]], full_harness_head: None, monkeypatch: pytest.MonkeyPatch
):
    """Codex's phase-2 review: a full selection with a mandatory extra command `false` was
    certified with only the configured commands and accepted as verified."""
    root, d = ready
    asked: list[tuple[str, ...]] = []

    def full(_worktree: Path, _base: str | None, _head: str, _policy: int = 1) -> test_select.Selection:
        return test_select.Selection(
            "full", "verification-runtime", ("src/agent_cli/procs.py",), (), ("false",)
        )

    def record(_root: Path, rev: str, *, extra: tuple[str, ...] = (), **_kwargs: object) -> dict[str, Any]:
        asked.append(extra)
        raise verify_record.RecordFailed(f"{rev[:12]} did not pass: command 1 `false`", root)

    monkeypatch.setattr(test_select, "select", full)
    monkeypatch.setattr(verify_record, "record", record)
    gate.execute(root, d["id"], d["attempt"])
    assert asked == [("false",)]
    assert finish(root, load(root, d["id"]))["verification_result"]["status"] == "failed"


def _failed_run(root: Path, head: str, digest: str) -> Path:
    """A failed certificate run's evidence, whose binding names what actually ran."""
    from agent_cli.dispatch.verification_evidence import Binding, Evidence

    run_dir = root / ".agent" / "verify" / "runs" / ("e" * 32)
    run_dir.mkdir(parents=True)
    evidence = Evidence(
        1, Binding("000-verify-01", 1, head, digest, "e" * 32), "complete", None, None, (), 0, None
    )
    (run_dir / "terminal.json").write_text(json.dumps(evidence.to_dict()), encoding="utf-8")
    return run_dir


def test_a_failed_certificate_is_reverifiable_with_what_actually_ran(
    ready: tuple[Path, dict[str, Any]], full_harness_head: None, monkeypatch: pytest.MonkeyPatch
):
    """The phase-5 dogfood, then Codex rounds 8-9: a failed certificate is rerunnable, bound to
    the head and effective config its own evidence names (with the selection's extras), not to
    inputs read elsewhere that may describe different commands."""
    root, d = ready
    head, digest = "a" * 40, "f" * 64

    def full(_worktree: Path, _base: str | None, _head: str, _policy: int = 1) -> test_select.Selection:
        return test_select.Selection(
            "full", "verification-runtime", ("src/agent_cli/procs.py",), (), ("false",)
        )

    def record(_root: Path, rev: str, **_kwargs: object) -> dict[str, Any]:
        raise verify_record.RecordFailed(
            f"{rev[:12]} did not pass: timeout", _failed_run(root, head, digest), "timeout"
        )

    monkeypatch.setattr(test_select, "select", full)
    monkeypatch.setattr(verify_record, "record", record)
    gate.execute(root, d["id"], d["attempt"])
    wait = load(root, d["id"])["verification_wait"]
    # Audit 10 plan.md:45-47 (§A1): a certificate time bound is infrastructure; its rerun binding stays.
    assert wait["outcome"] == "failed" and (wait["head"], wait["config_digest"]) == (head, digest)
    assert wait["extra"] == ["false"]
    result = finish(root, load(root, d["id"]))
    assert result["state"] == "done" and "WARNING" in result["result"]["notes"]


def test_a_failed_certificate_without_evidence_offers_no_rerun(
    ready: tuple[Path, dict[str, Any]], full_harness_head: None, monkeypatch: pytest.MonkeyPatch
):
    root, d = ready

    def record(_root: Path, rev: str, **_kwargs: object) -> dict[str, Any]:
        raise verify_record.RecordFailed(f"{rev[:12]} did not pass", root / "no-such-run")

    monkeypatch.setattr(verify_record, "record", record)
    gate.execute(root, d["id"], d["attempt"])
    assert "head" not in load(root, d["id"])["verification_wait"]


def _policy_2(root: Path, dispatch_id: str) -> None:
    d = load(root, dispatch_id)
    d["selection_policy"] = 2
    save(root, d)


def test_a_policy_2_attempt_is_never_certified(
    ready: tuple[Path, dict[str, Any]], full_harness_head: None, monkeypatch: pytest.MonkeyPatch
):
    """ADR-0045: the full suite runs once, for the landing candidate, never at dispatch."""
    root, d = ready
    _policy_2(root, d["id"])
    lanes: list[object] = []

    def record(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("a policy-2 dispatch never runs the full suite")

    def no_lane(*args: object) -> None:
        lanes.append(args)

    monkeypatch.setattr(verify_record, "record", record)
    monkeypatch.setattr(lane, "await_lane", no_lane)
    gate.execute(root, d["id"], d["attempt"])
    assert len(lanes) == 1


def test_a_refused_selection_is_incomplete_and_runs_nothing(
    ready: tuple[Path, dict[str, Any]], full_harness_head: None, monkeypatch: pytest.MonkeyPatch
):
    root, d = ready
    monkeypatch.setenv("AGENT_NOW", "2026-10-03T10:00:00+00:00")
    _policy_2(root, d["id"])
    asked: list[int] = []

    def refused(_worktree: Path, _base: str | None, _head: str, policy: int = 1) -> test_select.Selection:
        asked.append(policy)
        return test_select.Selection("refused", "base predates selection v2", (), policy=2)

    monkeypatch.setattr(test_select, "select", refused)
    monkeypatch.setattr(lane, "await_lane", AWAIT_LANE)
    gate.execute(root, d["id"], d["attempt"])
    after = load(root, d["id"])
    assert asked and set(asked) == {2}
    assert "no scoped selection: base predates selection v2" in json.dumps(after)
    assert [r["scope"] for r in after["verification_scopes"]] == ["refused"]
    assert after["verification_scopes"][0]["head_sha"] == gate.worktree_head(Path(d["worktree"]))
    assert after["verification_scopes"][0]["observed_at"] == "2026-10-03T10:00:00+00:00"
    result = finish(root, after)["verification_result"]
    assert result["status"] == "incomplete", result
