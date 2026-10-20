"""A finished attempt that never got a verification lane can be reverified.

A dispatch waited for the single lane and ended blocked; `dispatch reverify` then refused
with "has no recorded verification binding to rerun", leaving a resume (a model turn) or a new
dispatch as the only ways forward."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_cli.core import AgentError
from agent_cli.dispatch import lane, reverify, state
from dispatch_helpers import (
    _new_fast as new_fast,  # pyright: ignore[reportPrivateUsage] -- shared test helper
)
from dispatch_helpers import (
    droot,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture
    envelope,
    one,
)

DIGEST = "e" * 64


def _timed_out(**wait: Any) -> dict[str, Any]:
    return {
        "id": "001-mac-17",
        "attempt": 1,
        "state": "blocked",
        "terminal_committed": True,
        "verification": None,
        "verification_wait": {"attempt": 1, "outcome": "timeout", **wait},
    }


def test_a_timed_out_wait_with_its_head_is_rerunnable(tmp_root: Path):
    binding = reverify.recorded_binding(tmp_root, _timed_out(head="a" * 40, config_digest=DIGEST))
    assert binding.final_sha == "a" * 40 and binding.config_digest == DIGEST
    assert binding.attempt == 1 and binding.run_id == "lane-timeout"


def test_a_wait_without_its_head_still_refuses(tmp_root: Path):
    with pytest.raises(AgentError, match="no recorded verification binding"):
        reverify.recorded_binding(tmp_root, _timed_out())


def test_a_wait_from_an_earlier_attempt_does_not_count(tmp_root: Path):
    record = _timed_out(head="a" * 40, config_digest=DIGEST)
    record["attempt"] = 2
    with pytest.raises(AgentError, match="no recorded verification binding"):
        reverify.recorded_binding(tmp_root, record)


def test_giving_up_on_the_lane_records_the_head_and_config(droot: Path, fake_claude: Any):
    fake_claude.scenario([one(envelope())])
    d: dict[str, Any] = new_fast(droot)
    current = state.load(droot, d["id"])
    current.update(terminal_committed=False, verification=None)
    state.save(droot, current)
    lane.give_up(
        droot, d["id"], int(current["attempt"]), "timeout", "no verification lane within 1800 s", 1800.0
    )
    wait = state.load(droot, d["id"])["verification_wait"]
    assert len(wait["head"]) == 40 and len(wait["config_digest"]) == 64
