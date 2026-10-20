"""A landing records its outcome on the harness dispatch whose branch it landed, without rewriting
that dispatch's attempt state (C1). A supervisor that crashed left it `failed`
while its branch landed; the record now says both. Only the harness repo's dispatches are marked:
a product dispatch on a same-named branch is not ."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_cli import land
from agent_cli.dispatch import state

REGISTRY = """version: 1
repos:
  - name: Harness
    short: harness
    path: .
    remote: git@github.com:example/harness.git
    default_branch: main
    harness: "none"
    dispatch:
      verify:
        - command: "echo ok"
          timeout_seconds: 20
      verify_timeout_seconds: 30
    notes: ""
"""
JOURNAL = {"id": "20261006T214801-631c36", "record": "a" * 32, "cand": "c" * 40}


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "harness"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "agent").write_text("")
    (root / "agent.yaml").write_text(REGISTRY, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    monkeypatch.setenv("AGENT_ROOT", str(root))
    return root


def _record(root: Path, dispatch_id: str, branch: str, status: str, repo: str = "Harness") -> None:
    state.save(
        root, {"id": dispatch_id, "repo": repo, "short": repo.lower(), "branch": branch, "state": status}
    )


def test_a_landed_branch_records_integration_and_keeps_its_failed_state(harness: Path):
    _record(harness, "001-mac-19", "agent/033-x-mac-19", "failed")
    _record(harness, "001-mac-20", "agent/033-x-mac-20", "done")
    land.note_integration(harness, "refs/heads/agent/033-x-mac-19", JOURNAL, "d" * 40)
    landed = state.load(harness, "001-mac-19")
    assert landed["state"] == "failed"
    assert landed["integration"]["state"] == "landed"
    assert landed["integration"]["journal"] == JOURNAL["id"]
    assert landed["integration"]["record"] == JOURNAL["record"]
    assert landed["integration"]["main"] == JOURNAL["cand"]
    assert landed["integration"]["tip"] == "d" * 40
    assert "integration" not in state.load(harness, "001-mac-20")


def test_landing_actions_a_finished_dispatch_but_not_a_running_one(harness: Path):
    """Only `ack` or `abandon` cleared a finished dispatch, so a landed one kept
    waiting on its manager. Landing is that action; a resumed, running attempt is left alone."""
    journal = {**JOURNAL, "id": "20261011T170000-aaaaaa"}  # landing began 17:00 UTC
    _record(harness, "002-mac-01", "agent/057-a", "done")
    _record(harness, "002-mac-02", "agent/057-b", "running")
    finished = state.load(harness, "002-mac-01")
    finished["finished"] = "2026-10-11T10:30:00-06:00"  # before the landing started
    state.save(harness, finished)
    land.note_integration(harness, "agent/057-a", journal)
    land.note_integration(harness, "agent/057-b", journal)
    landed = state.load(harness, "002-mac-01")
    assert landed["actioned"] == {"action": "landed", "note": "20261011T170000-aaaaaa"}
    assert state.is_actioned(landed)
    assert "actioned_at" not in state.load(harness, "002-mac-02")


@pytest.mark.parametrize("finished", ["2026-10-11T11:00:30-06:00", "2026-10-11T07:00:30-10:00"])
def test_a_resume_finished_after_the_landing_still_waits_even_through_recovery(harness: Path, finished: str):
    """Codex reviews, phase 1: attempt 2 finished 30 s after attempt 1's landing began; recovering
    that journal rewrote its `ts` and stamped past attempt 2, hiding its unreviewed return. The
    stamp comes from the journal id, which recovery never changes, and the comparison is by
    instant, so another offset on the finish does not reorder it."""
    journal = {**JOURNAL, "id": "20261011T170000-bbbbbb", "ts": "2026-10-11T17:01:00+00:00"}
    _record(harness, "002-mac-03", "agent/057-c", "done")
    resumed = state.load(harness, "002-mac-03")
    resumed["finished"] = finished
    state.save(harness, resumed)
    land.note_integration(harness, "agent/057-c", journal)  # the recovery replays the old journal
    assert not state.is_actioned(state.load(harness, "002-mac-03"))


def test_is_actioned_compares_instants_not_text():
    """An ack at 11:00 MDT is earlier than a finish at 07:05 HST (11:05 MDT), though it sorts later."""
    d = {"actioned_at": "2026-10-11T11:00:00-06:00", "finished": "2026-10-11T07:05:00-10:00"}
    assert not state.is_actioned(d)
    assert state.is_actioned({**d, "finished": "2026-10-11T06:55:00-10:00"})


def test_a_product_dispatch_on_a_same_named_branch_is_not_marked(harness: Path):
    _record(harness, "003-mac-01", "agent/shared-name", "done", repo="DifferentProduct")
    land.note_integration(harness, "agent/shared-name", JOURNAL)
    assert "integration" not in state.load(harness, "003-mac-01")


def test_no_matching_dispatch_is_a_no_op(harness: Path):
    land.note_integration(harness, "cleanup/branch", JOURNAL)
    assert state.load_all(harness) == []
