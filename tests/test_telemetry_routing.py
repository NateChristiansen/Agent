"""Telemetry routing against a real repository and linked worktree.

`main_checkout` is never mocked: the fixture's repository is the main checkout, and its linked
worktree and a detached checkout must resolve to the same sink.
"""

from __future__ import annotations

import pytest

from agent_cli import telemetry
from telemetry_helpers import events, isolated, lines  # noqa: F401 — `isolated` is an autouse fixture
from verification_fixtures import fixture, git  # noqa: F401 — `fixture` is a pytest fixture

PARENT = "6f1c0000-0000-4000-8000-000000000006"


@pytest.fixture
def checkouts(fixture):
    _, worktree, _ = fixture
    main = worktree.parent / "repository"
    detached = worktree.parent / "detached"
    git(main, "worktree", "add", "--detach", str(detached))
    return main, worktree, detached


def test_explicit_recording_uses_the_passed_root_and_ignores_dispatch(checkouts, tmp_path, monkeypatch):
    main, worktree, _ = checkouts
    monkeypatch.setenv("AGENT_DISPATCH_ID", "001-03")
    elsewhere = tmp_path / "elsewhere"
    assert telemetry.record(elsewhere, "dispatch.launch") is not None
    assert len(events(elsewhere / ".agent/telemetry")) == 1
    assert telemetry.record(telemetry.sink_root(worktree), "dispatch.setup") is not None
    assert [row["kind"] for row in events(main / ".agent/telemetry")] == ["dispatch.setup"]
    assert not (worktree / ".agent").exists()


@pytest.mark.parametrize("where", ["main", "worktree", "detached"])
def test_implicit_recording_resolves_every_checkout_to_the_main_sink(checkouts, where, monkeypatch):
    main, worktree, detached = checkouts
    cwd = {"main": main, "worktree": worktree, "detached": detached}[where]
    target, why = telemetry.resolve({}, cwd)
    assert why == "main" and target is not None
    assert target.directory.resolve() == (main / ".agent/telemetry").resolve()
    monkeypatch.chdir(cwd)
    assert telemetry.record_implicit("dispatch.launch") is not None
    assert len(events(main / ".agent/telemetry")) == 1
    assert not (worktree / ".agent").exists() and not (detached / ".agent").exists()


def test_implicit_recording_inside_a_dispatch_needs_a_named_sink(checkouts, tmp_path, monkeypatch):
    main, worktree, _ = checkouts
    monkeypatch.chdir(worktree)
    monkeypatch.setenv("AGENT_DISPATCH_ID", "001-03")
    assert telemetry.resolve() == (None, "dispatch")
    assert telemetry.record_implicit("dispatch.launch") is None
    assert events(main / ".agent/telemetry") == []
    assert telemetry.process_recorder().counts["attempted"] == 0, "a routed-off call is not an attempt"
    named = tmp_path / "verifier" / ".agent" / "telemetry"
    monkeypatch.setenv("AGENT_TELEMETRY_SINK", str(named))
    monkeypatch.setenv("AGENT_TELEMETRY_PARENT", PARENT)
    assert telemetry.record_implicit("dispatch.launch") is not None
    [row] = events(named)
    assert row["parent_id"] == PARENT
    assert telemetry.Sink.from_directory(named).key == tmp_path / "verifier"
    assert events(main / ".agent/telemetry") == []


def test_telemetry_off_records_nothing_anywhere(checkouts, tmp_path, monkeypatch):
    main, _, _ = checkouts
    monkeypatch.chdir(main)
    named = tmp_path / "named"
    for env in ({"AGENT_TELEMETRY": "off"}, {"AGENT_TELEMETRY": "off", "AGENT_TELEMETRY_SINK": str(named)}):
        assert telemetry.resolve(env, main) == (None, "off")
        assert telemetry.record_implicit("dispatch.launch", env=env, cwd=main) is None
    monkeypatch.setenv("AGENT_TELEMETRY", "off")
    assert telemetry.record_implicit("dispatch.launch") is None
    assert events(main / ".agent/telemetry") == [] and not named.exists()


def test_no_root_is_a_counted_loss(tmp_path):
    outside = tmp_path / "plain"
    outside.mkdir()
    assert telemetry.resolve({}, outside) == (None, "no_root")
    assert telemetry.record_implicit("dispatch.launch", env={}, cwd=outside) is None
    assert telemetry.process_recorder().counts == {"attempted": 1, "written": 0, "lost": 1, "skipped": 0}
    assert not (outside / ".agent").exists()


def test_loss_lines_for_the_main_sink_share_one_temp_file(checkouts, isolated):
    main, worktree, detached = checkouts
    paths = {telemetry.resolve({}, cwd)[0].loss for cwd in (main, worktree, detached)}  # type: ignore[union-attr]
    assert len(paths) == 1
    assert next(iter(paths)).is_relative_to(isolated)
    assert telemetry.record_implicit("dispatch.terminal", env={}, cwd=worktree, data={"output": "x"}) is None
    [loss] = lines(next(iter(paths)))
    assert loss["reason"] == "refused"
