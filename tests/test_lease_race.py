"""A lease released by another dispatch between the directory scan and the read is skipped, not
raised (C1). Before the fix, `leases.list_all` raised FileNotFoundError and crashed the
supervisor of several dispatches while unrelated dispatches finished."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli import leases
from agent_cli.core import AgentError
from agent_cli.dispatch import allocation, holdings, lane

pytestmark = pytest.mark.usefixtures("notes_remote")


@pytest.fixture
def vanishing(tmp_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two leases on disk; reading the first unlinks it just before the read, as a concurrent
    release would."""
    leases.take(tmp_root, "repo-slot:agent/0", session_id="dispatch:001-mac-90", pid=os.getpid())
    leases.take(tmp_root, "repo-slot:agent/1", session_id="dispatch:001-mac-91", pid=os.getpid())
    gone = leases.leases_dir(tmp_root) / f"{leases.area_key('repo-slot:agent/0')}.json"
    assert gone.exists()
    real = Path.read_text

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == gone and self.exists():
            self.unlink()
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read_text)
    return tmp_root


def test_list_all_skips_a_lease_released_mid_scan(vanishing: Path):
    areas = [lease["area"] for lease in leases.list_all(vanishing)]
    assert areas == ["repo-slot:agent/1"]


def test_a_malformed_surviving_lease_still_holds_its_area(tmp_root: Path):
    """Codex's review: skipping it let another session take an overlapping subtree."""
    leases.leases_dir(tmp_root).mkdir(parents=True, exist_ok=True)
    (leases.leases_dir(tmp_root) / f"{leases.area_key('vault:src')}.json").write_text(
        "{not json", encoding="utf-8"
    )
    [held] = leases.list_all(tmp_root)
    assert held["area"] == "vault:src" and held["status"] in leases.BLOCKING_STATUSES
    with pytest.raises(AgentError):
        leases.take(tmp_root, "vault:src/agent_cli/dispatch", session_id="other", pid=os.getpid())


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        '{"area": "vault:harness", "session_id": "s", "pid": null}',
        '{"area": "vault:src", "session_id": "s", "pid": "12"}',
        '{"area": "vault:src", "session_id": "s", "pid": 0}',
        '{"area": "vault:src", "session_id": "s"}',
        '{"area": "vault:src", "session_id": "", "pid": null}',
    ],
    ids=["empty", "mismatched-area", "string-pid", "zero-pid", "no-pid-key", "empty-holder"],
)
def test_a_parsed_but_invalid_lease_still_holds_its_area(tmp_root: Path, content: str):
    """Codex's round-2 review: contents that parse but do not fit the file name let a take through."""
    leases.leases_dir(tmp_root).mkdir(parents=True, exist_ok=True)
    (leases.leases_dir(tmp_root) / f"{leases.area_key('vault:src')}.json").write_text(
        content, encoding="utf-8"
    )
    [held] = leases.list_all(tmp_root)
    assert held["area"] == "vault:src" and held["status"] in leases.BLOCKING_STATUSES
    with pytest.raises(AgentError):
        leases.take(tmp_root, "vault:src/agent_cli/dispatch", session_id="other", pid=os.getpid())


def test_allocation_reconciliation_and_completion_survive_the_race(vanishing: Path):
    """The three supervisor paths that scan leases: lane accounting, held-slot lookup at
    completion, and reconciliation of stale reservations."""
    assert lane.concurrent_workers(vanishing, "agent") == 1
    assert holdings.held_areas(vanishing, "dispatch:001-mac-91", ("repo-slot:",)) == ["repo-slot:agent/1"]
    assert allocation.reconcile_allocations(vanishing) == []
