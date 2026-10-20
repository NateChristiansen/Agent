"""Pass-record flights after Codex's phase-2 review: identical requests share a failed run too, a
selection's extra commands are part of what a certificate certifies, and a run whose cleanup is
unconfirmed keeps its lane and its checkout until the verifier confirms it."""

from __future__ import annotations

import dataclasses
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_cli import leases, verify_record, verify_run
from agent_cli.core import AgentError
from agent_cli.dispatch import holdings, verification, verification_status
from test_record_capacity import REGISTRY
from test_record_capacity import harness as harness
from test_record_capacity import overlap as overlap

FAIL = "echo 1 failed in 0.01s; exit 1"


def _config(root: Path, command: str) -> None:
    (root / "agent.yaml").write_text(REGISTRY.format(lanes=2, command=command), encoding="utf-8")


def _lanes_held(root: Path) -> list[str]:
    return [
        str(lease["area"]) for lease in leases.list_all(root) if str(lease.get("area")).startswith("verify:")
    ]


def test_identical_failing_requests_share_one_failed_run(harness: Path, overlap: dict[str, int]):
    _config(harness, FAIL)
    errors: list[BaseException] = []

    def go() -> None:
        try:
            verify_record.record(harness, "HEAD")
        except verify_record.RecordFailed as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert overlap["runs"] == 1 and len(errors) == 2
    dirs = {exc.run_dir for exc in errors if isinstance(exc, verify_record.RecordFailed)}
    assert len(dirs) == 1, "both callers get the one run's evidence"
    with pytest.raises(verify_record.RecordFailed):
        verify_record.record(harness, "HEAD")
    assert overlap["runs"] == 2, "a request made after the failure is a new flight"


def test_extra_commands_are_certified_and_required_for_reuse(harness: Path, overlap: dict[str, int]):
    _config(harness, "echo 3 passed in 0.01s")
    with pytest.raises(verify_record.RecordFailed, match="command 1"):
        verify_record.record(harness, "HEAD", extra=("false",))
    found = verify_record.record(harness, "HEAD", extra=("true",))
    assert found["extra"] == ["true"] and found["command_count"] == 2 and not found["reused"]
    assert verify_record.record(harness, "HEAD")["reused"], "it certifies a plain request too"
    assert verify_record.record(harness, "HEAD", extra=("true",))["reused"]
    # The phase-5 dogfood: a path-filtered pytest rerun as the extra, which the full-suite check
    # refused the whole certificate for.
    narrow = "echo pytest tests/test_x.py; echo 1 passed in 0.01s"
    other = verify_record.record(harness, "HEAD", extra=(narrow,))
    assert not other["reused"] and other["command_count"] == 2
    assert overlap["runs"] == 3
    # Codex, round 2: the supervisor's reuse path accepted a record for a selection whose extras
    # it never ran.
    assert verify_record.reusable(harness, harness, None, _Selector(("never run",))) is None
    assert verify_record.reusable(harness, harness, None, _Selector((narrow,))) is not None


def test_unconfirmed_cleanup_keeps_the_lane_and_checkout_until_confirmed(
    harness: Path, monkeypatch: pytest.MonkeyPatch
):
    _config(harness, "echo 3 passed in 0.01s")
    real_wait, real_status = verification.wait, verification_status.load_status

    def wait(handle: Any) -> Any:
        return dataclasses.replace(real_wait(handle), cleanup_confirmed=False)

    def still_running(root: Path, binding: Any) -> Any:
        return dataclasses.replace(real_status(root, binding), active=True)

    monkeypatch.setattr(verification, "wait", wait)
    monkeypatch.setattr(verification_status, "load_status", still_running)
    found = verify_record.record(harness, "HEAD")
    run_dir = verify_record.store(harness) / "runs" / found["run_id"]
    pending = (run_dir / verify_run.PENDING).read_text(encoding="utf-8")
    assert len(_lanes_held(harness)) == 1, "the lane stays held"
    checkout = Path(json.loads(pending)["checkout"])
    assert checkout.is_dir(), "the checkout is kept for recovery"

    monkeypatch.setattr(verification_status, "load_status", real_status)
    assert holdings._record_settled(harness, f"record:{found['run_id']}")  # pyright: ignore[reportPrivateUsage]
    assert not checkout.exists() and not (run_dir / verify_run.PENDING).exists()


def test_a_failure_names_the_command_and_a_timeout_says_so(harness: Path):
    _config(harness, "sleep 5")
    (harness / "agent.yaml").write_text(
        (harness / "agent.yaml")
        .read_text(encoding="utf-8")
        .replace("timeout_seconds: 20", "timeout_seconds: 1"),
        encoding="utf-8",
    )
    with pytest.raises(verify_record.RecordFailed, match="command 0 `sleep 5`: timeout") as caught:
        verify_record.record(harness, "HEAD")
    assert caught.value.category == "timeout"


class _Selector:
    """Stands in for `test_select`: every selection requires `extra`."""

    def __init__(self, extra: tuple[str, ...]) -> None:
        self.extra = extra

    def select(self, _worktree: Path, _base: str | None, _head: str) -> Any:
        return self

    @property
    def scope(self) -> str:
        return "full"


def test_a_dead_record_holder_s_unsettled_lane_cannot_be_taken_over(harness: Path):
    run_id = "d" * 32
    run_dir = verify_record.store(harness) / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / verify_run.PENDING).write_text(
        json.dumps({"checkout": "/nonexistent", "holder": "/nonexistent", "binding": {}}), encoding="utf-8"
    )
    leases.take(harness, "verify:harness/1", session_id=f"record:{run_id}", pid=2**22 + 3)
    with pytest.raises(AgentError):
        leases.take(harness, "verify:harness/1", session_id="record:" + "e" * 32, pid=os.getpid())
    (run_dir / verify_run.PENDING).unlink()
    assert leases.take(harness, "verify:harness/1", session_id="record:" + "e" * 32, pid=os.getpid())["taken"]


def test_waiters_on_a_timed_out_flight_get_its_category(harness: Path):
    _config(harness, "echo 3 passed in 0.01s")
    key = "k" * 16
    marker = verify_record.store(harness) / "locks" / f"{key}.failed"
    marker.parent.mkdir(parents=True, exist_ok=True)
    failed: dict[str, Any] = {
        "at": 2e9,
        "message": "timed out",
        "run_dir": str(harness),
        "extra": [],
        "category": "timeout",
    }
    marker.write_text(json.dumps(failed), encoding="utf-8")
    with pytest.raises(verify_record.RecordFailed) as caught:
        verify_record._shared_failure(harness, key, 1e9, ())  # pyright: ignore[reportPrivateUsage]
    assert caught.value.category == "timeout"
