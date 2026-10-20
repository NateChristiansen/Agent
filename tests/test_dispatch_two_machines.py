"""Two machines, one notes repo: dispatch ids and handoff files collide.

Each machine is a full harness root with its own gitignored `.agent/` and its private paths
linked into its own clone of one shared notes remote, the way setup and ADR-0034 leave a real
pair. Both machines start synced, then each dispatches into the same feature before the other's
push reaches it.

With distinct `machine:` labels (ADR-0036) the two machines allocate distinct ids, handoffs,
branches and worktree paths, and B's notes rebase hits no handoff conflict (the plan's
plans remain byte-identical). A copied `agent.local.yaml` gives
both machines one label, and the remote label reservation refuses the second installation. The ownership guard
(`dispatch/ownership.py`) contains that collision: the remaining tests pin that machine B's
record never repaints machine A's handoff or plan line through any shared write path.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli import dispatch
from agent_cli import features as features_mod
from agent_cli import notes as notes_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import handoffs as handoffs_mod
from agent_cli.dispatch import machine
from conftest import git
from dispatch_helpers import (  # noqa: F401 -- droot is a pytest fixture, used only as a parameter name
    _new_fast,
    droot,
    envelope,
    one,
)
from harness_root import NOTES_REMOTE, link_notes

GIT_ID = ["-c", "user.email=owner@example.com", "-c", "user.name=Owner"]


def _commit(notes: Path, message: str) -> None:
    git(["add", "-A"], notes)
    git([*GIT_ID, "commit", "-q", "-m", message], notes)


def _two_machines(a: Path, label_b: str | None) -> tuple[Path, Path, Path, Path]:
    """Machine A is `a`; machine B is a copy of its public tree with an empty `.agent/` and its
    own clone of the shared notes remote. B keeps A's copied `agent.local.yaml`, and with it A's
    `machine:` label, unless `label_b` names its own. Returns (a, notes_a, b, notes_b)."""
    base = a.parent
    notes_a = link_notes(a)
    (notes_a / ".gitignore").write_text(notes_mod.GITIGNORE, encoding="utf-8")
    (notes_a / ".gitattributes").write_text(notes_mod.GITATTRIBUTES, encoding="utf-8")
    _commit(notes_a, "seed")
    git([*GIT_ID, "pull", "-q", "--rebase"], notes_a)
    git(["push", "-q", "origin", "main"], notes_a)
    remote = base / NOTES_REMOTE

    machine_b = base / "machine-b"
    machine_b.mkdir()
    b = machine_b / a.name
    private = {e.path for e in notes_mod.PRIVATE_SET} | {".agent"}

    def _ignore(directory: str, names: list[str]) -> set[str]:
        rel = Path(directory).relative_to(a)
        return {n for n in names if (rel / n).as_posix().removeprefix("./") in private}

    shutil.copytree(a, b, symlinks=True, ignore=_ignore)
    notes_b = machine_b / notes_mod.DEFAULT_NOTES_DIRNAME
    git(["clone", "-q", str(remote), str(notes_b)], machine_b)
    git(["config", "user.email", "owner@example.com"], notes_b)
    git(["config", "user.name", "Owner"], notes_b)
    notes_mod.link(b, notes_b)
    notes_mod.write_notes_root(b, notes_b)
    if label_b is not None:
        (b / "agent.local.yaml").write_text(f"machine: {label_b}\n", encoding="utf-8")
    return a, notes_a, b, notes_b


def _pair(droot: Path, fake_claude, label_b: str | None) -> dict:
    """Both machines dispatch before either sees the other's push."""
    fake_claude.scenario([one(envelope())])
    a, notes_a, b, notes_b = _two_machines(droot, label_b)
    da = _new_fast(a, prompt="machine A work order\n\nfirst machine's job")
    db = _new_fast(b, prompt="machine B work order\n\nsecond machine's job")
    # A's turn ends: the Stop hook commits and pushes (ADR-0034).
    _commit(notes_a, "machine A turn")
    git([*GIT_ID, "pull", "-q", "--rebase"], notes_a)
    git(["push", "-q", "origin", "main"], notes_a)
    # B's turn ends: commit, then fetch + rebase onto A's push.
    _commit(notes_b, "machine B turn")
    rebase = subprocess.run(
        ["git", *GIT_ID, "pull", "-q", "--rebase"], cwd=notes_b, capture_output=True, text=True
    )
    return {"a": a, "b": b, "notes_a": notes_a, "notes_b": notes_b, "da": da, "db": db, "rebase": rebase}


@pytest.fixture
def pair(droot: Path, fake_claude) -> dict:
    """Machine A is labelled `mac` (the suite's agent.local.yaml), machine B `win`."""
    return _pair(droot, fake_claude, "win")


@pytest.fixture
def settled(droot: Path, fake_claude) -> dict:
    """A historical colliding local record survives upgrade; writers still refuse foreign data."""
    fake_claude.scenario([one(envelope())])
    a, notes_a, b, notes_b = _two_machines(droot, None)
    da = _new_fast(a, prompt="machine A work order\n\nfirst machine's job")
    _commit(notes_a, "machine A turn")
    git([*GIT_ID, "pull", "-q", "--rebase"], notes_a)
    git(["push", "-q", "origin", "main"], notes_a)
    git([*GIT_ID, "pull", "-q", "--rebase"], notes_b)
    db = {
        **dispatch.load(a, da["id"]),
        "session_id": "historical-machine-b-session",
        "install": machine.install(b)["token"],
        "record_revision": 0,
    }
    db.pop("verification", None)  # Historical record has no local verifier evidence on B.
    dispatch.state.save(b, db)
    rel = da["handoff"]
    synced = (b / rel).read_text(encoding="utf-8")
    assert synced == (a / rel).read_text(encoding="utf-8")
    assert "goal: machine A work order" in synced
    return {
        "a": a,
        "b": b,
        "notes_a": notes_a,
        "notes_b": notes_b,
        "da": da,
        "db": db,
        "rel": rel,
        "synced": synced,
    }


def _handoffs_section(root: Path) -> str:
    plan = features_mod.resolve(root, "1").body
    return plan.split("## Handoffs", 1)[1].split("\n## ", 1)[0]


def test_two_machines_allocate_distinct_dispatch_ids(pair: dict):
    da, db = pair["da"], pair["db"]
    # Before ADR-0036 both were 001-01 and features/001-alpha-work/handoffs/01-alpha.md, both
    # worked on the bare feature/alpha-work branch, and B's rebase stopped on an add/add
    # conflict in that file (see the design note for the captured output).
    assert (da["id"], db["id"]) == ("001-mac-01", "001-win-01")
    assert da["handoff"] != db["handoff"], f"both machines write {da['handoff']}"
    assert da["branch"] != db["branch"], f"both machines work on {da['branch']}"
    assert Path(da["worktree"]).name != Path(db["worktree"]).name
    # Neither independent handoff files nor the fixed plan view conflict.
    unmerged = git(["diff", "--name-only", "--diff-filter=U"], pair["notes_b"]).split()
    assert not [p for p in unmerged if "/handoffs/" in p], unmerged
    assert not unmerged, pair["rebase"].stdout + pair["rebase"].stderr
    assert pair["rebase"].returncode == 0
    assert _handoffs_section(pair["a"]) == _handoffs_section(pair["b"])
    for d in (da, db):
        assert (pair["b"] / d["handoff"]).is_file()


def test_render_leaves_another_machines_handoff_alone(settled: dict):
    b, rel, synced = settled["b"], settled["rel"], settled["synced"]
    # B's next `status render` refreshes live fields from B's own record of "001-mac-01".
    skipped: list[dict] = []
    assert handoffs_mod.sync_live_fields(b, skipped=skipped) == 0
    after = (b / rel).read_text(encoding="utf-8")
    assert "goal: machine B work order" not in after, "B's render wrote its goal into A's handoff"
    assert after == synced
    assert [s["handoff"] for s in skipped] == [rel]
    assert "session_id" in skipped[0]["reason"]


def test_write_handoff_leaves_another_machines_handoff_alone(settled: dict):
    b, rel, synced = settled["b"], settled["rel"], settled["synced"]
    record = dispatch.load(b, settled["db"]["id"])
    assert handoffs_mod.write_handoff(b, record, features_mod.resolve(b, "1")) is None
    assert (b / rel).read_text(encoding="utf-8") == synced


def test_bs_abandon_repaints_neither_handoff_nor_plan(settled: dict):
    """The whole shared write path: `abandon` calls write_handoff, then
    renders. B's record of the colliding id reaches none of A's synced files."""
    b, rel, synced = settled["b"], settled["rel"], settled["synced"]
    before = _handoffs_section(b)
    dispatch.abandon(b, dispatch_id=settled["db"]["id"], note="superseded on machine A")
    assert (b / rel).read_text(encoding="utf-8") == synced
    assert _handoffs_section(b) == before
    assert "abandoned" not in before


def test_copied_label_refuses_before_dispatch_records_or_worktrees(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    a, _, b, _ = _two_machines(droot, None)
    _new_fast(a)
    with pytest.raises(AgentError, match="belongs to another installation"):
        _new_fast(b)
    assert not dispatch.load_all(b)
    assert not list((b / "repos/_worktrees/alpha").glob("*"))
