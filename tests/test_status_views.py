"""Every generated view is checked against the generation it
consumes, plan edits re-render from both clients and nested features, render failures are
kept, and canonical task counts feed the feature views."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import frontmatter, status
from agent_cli.hooks import session as session_hook

pytestmark = pytest.mark.usefixtures("notes_remote")


def _feature(run, slug: str = "alpha-work") -> Path:
    run(["feature", "new", slug, "--title", "Alpha work"])
    return next((Path.cwd() / "features").glob(f"*-{slug}/plan.md"))


def test_check_names_each_view_and_passes_after_a_render(tmp_root: Path, run):
    _feature(run)
    run(["status", "render"])
    result = status.check(tmp_root)
    assert result["fresh"], result
    assert ".agent/status.json" in result["views"]
    assert "STATUS.md" not in result["views"]  # Audit 10 plan.md:113, K1
    assert all(row["fresh"] for row in result["views"].values())


def test_a_wrong_status_json_generation_is_stale(tmp_root: Path, run):
    """A controlled wrong `.agent/status.json` passed as fresh."""
    _feature(run)
    run(["status", "render"])
    path = tmp_root / status.STATUS_JSON
    data = json.loads(path.read_text(encoding="utf-8"))
    data["source_hash"] = "000000000000"
    path.write_text(json.dumps(data))
    result = status.check(tmp_root)
    assert not result["fresh"]
    assert not result["views"][".agent/status.json"]["fresh"]
    r = run(["status", "check"])
    assert r.code == 1 and ".agent/status.json" in r.out


def test_an_adr_index_behind_its_adrs_is_stale(tmp_root: Path, run):
    """The committed index was 13 ADRs behind and `status check` said fresh."""
    from agent_cli import adrs as adrs_mod

    adrs = tmp_root / "adrs"
    adrs.mkdir(exist_ok=True)

    def adr(slug: str) -> None:
        n = adrs_mod.next_id(tmp_root)
        text = f"---\nid: {n}\ntitle: {slug}\nstatus: accepted\ndate: 2026-09-03\n---\n\nbody\n"
        (adrs / f"{n:04d}-{slug}.md").write_text(text, encoding="utf-8")

    adr("first")
    run(["status", "render"])
    assert status.check(tmp_root)["fresh"]
    adr("second")
    index = (tmp_root / status.ADR_INDEX).read_text(encoding="utf-8")
    # status.json brought up to date by hand, the index left behind
    data = status.collect(tmp_root)
    payload = json.loads((tmp_root / status.STATUS_JSON).read_text(encoding="utf-8"))
    payload["source_hash"] = status.source_hash(data)
    (tmp_root / status.STATUS_JSON).write_text(json.dumps(payload))
    assert (tmp_root / status.ADR_INDEX).read_text(encoding="utf-8") == index
    result = status.check(tmp_root)
    assert not result["fresh"]
    assert not result["views"]["adrs/README.md"]["fresh"]
    assert result["views"][".agent/status.json"]["fresh"]


def test_an_active_feature_with_every_task_ticked_has_canonical_counts(tmp_root: Path, run):
    plan = _feature(run)
    meta, body = frontmatter.load(plan)
    frontmatter.dump(plan, meta, body.replace("- [ ]\n", "- [x] did the thing\n"))
    result = run(["--json", "status", "render"])
    assert result.json["features"][0]["open_tasks"] == 0
    assert result.json["features"][0]["done_tasks"] == 1


def test_feature_cards_use_only_the_cli_count():
    js = (Path(__file__).resolve().parents[1] / "harness/obsidian/views/features.js").read_text(
        encoding="utf-8"
    )
    assert ".agent/status.json" in js
    assert "active, 0 open: ready to close" in js
    assert "dv.pages" not in js
    assert "file.tasks" not in js
    assert "reload" not in js


# --- plan-edit refresh -------------------------------------------------------------------


@pytest.fixture
def renders(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(status, "render_quietly", lambda root, source="": calls.append(source) or True)
    return calls


def test_a_claude_edit_of_a_plan_renders(tmp_root: Path, renders: list[str]):
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_root / "features/001-x/plan.md")}}
    session_hook.render_after_plan_edit(tmp_root, payload)
    assert renders == ["plan edit"]


def test_a_codex_patch_of_a_plan_renders(tmp_root: Path, renders: list[str]):
    """Codex patches carry no `file_path`, so its plan edits never re-rendered."""
    patch = "*** Begin Patch\n*** Update File: features/001-x/plan.md\n@@\n-a\n+b\n*** End Patch\n"
    session_hook.render_after_plan_edit(
        tmp_root, {"tool_name": "apply_patch", "tool_input": {"command": patch}}
    )
    assert renders == ["plan edit"]


def test_a_nested_feature_plan_renders_and_other_files_do_not(tmp_root: Path, renders: list[str]):
    nested = "initiatives/01-launch/features/061-nested/plan.md"
    session_hook.render_after_plan_edit(tmp_root, {"tool_name": "Write", "tool_input": {"file_path": nested}})
    assert renders == ["plan edit"]
    for other in ("features/001-x/log.md", "features/plan.md", "notes/plan.md", "features/x/plan.md"):
        session_hook.render_after_plan_edit(
            tmp_root, {"tool_name": "Write", "tool_input": {"file_path": other}}
        )
    assert renders == ["plan edit"]


def test_a_failed_render_is_kept_for_doctor_until_a_render_succeeds(tmp_root: Path, run, monkeypatch):
    """The plan-edit handler swallowed render exceptions without a trace."""
    from agent_cli.doctor import checks_state

    _feature(run)

    def broken(root: Path) -> dict[str, object]:
        raise ValueError("plan.md frontmatter is not YAML")

    with monkeypatch.context() as m:
        m.setattr(status, "render", broken)
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "features/001-alpha-work/plan.md"}}
        assert session_hook.render_after_plan_edit(tmp_root, payload) == ""
    failure = status.render_failure(tmp_root)
    assert failure and failure["source"] == "plan edit" and "not YAML" in failure["error"]
    [row] = checks_state.render_failures(tmp_root)
    assert row["level"] == "warn" and "not YAML" in row["detail"]
    status.render(tmp_root)
    assert status.render_failure(tmp_root) is None
    assert checks_state.render_failures(tmp_root) == []


def test_a_live_snapshot_that_cannot_be_written_fails_the_render(tmp_root: Path):
    """With a directory at `.agent/live.json` the render
    returned True, deleted the earlier failure record, and `status check` said fresh."""
    from agent_cli.vault import live

    live.path(tmp_root).unlink(missing_ok=True)
    live.path(tmp_root).mkdir(parents=True)
    status.record_render_failure(tmp_root, "earlier", OSError("previous render failed"))
    assert status.render_quietly(tmp_root, source="probe") is False
    failure = status.render_failure(tmp_root)
    assert failure and failure["source"] == "probe" and "live snapshot" in failure["error"]
    result = status.check(tmp_root)
    assert not result["fresh"] and not result["views"][".agent/live.json"]["fresh"]
    assert result["views"][".agent/status.json"]["fresh"]  # every other view was still written
    live.path(tmp_root).rmdir()
    assert status.render_quietly(tmp_root, source="probe") is True
    assert status.render_failure(tmp_root) is None
    assert status.check(tmp_root)["fresh"]


def test_a_missing_live_snapshot_is_stale(tmp_root: Path):
    from agent_cli.vault import live

    status.render(tmp_root)
    assert status.check(tmp_root)["fresh"]
    live.path(tmp_root).unlink()
    assert not status.check(tmp_root)["views"][".agent/live.json"]["fresh"]


def test_an_expired_live_snapshot_is_stale(tmp_root: Path, run):
    """A snapshot past its `valid_until` was reported fresh
    and `status check` exited 0."""
    from datetime import timedelta

    from agent_cli import core
    from agent_cli.vault import live

    status.render(tmp_root)
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    before = core.now() - timedelta(hours=13)
    snap["observed_at"] = core.iso(before)
    snap["valid_until"] = core.iso(before + timedelta(hours=12))
    live.path(tmp_root).write_text(json.dumps(snap))
    row = status.check(tmp_root)["views"][".agent/live.json"]
    assert row["fresh"] is False and row["found"].startswith("expired at ")
    result = run(["status", "check"])
    assert result.code != 0 and ".agent/live.json" in result.out
    del snap["valid_until"]
    live.path(tmp_root).write_text(json.dumps(snap))
    assert status.check(tmp_root)["views"][".agent/live.json"]["found"] == "no valid_until"
    status.render(tmp_root)  # a fresh write is current again
    assert status.check(tmp_root)["fresh"]
