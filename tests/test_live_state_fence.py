"""The live-state fence: fixture telemetry never reaches an enclosing checkout's sink.

Reproduces the 2026-10-05 escape: a pytest temp root inside a live checkout, a fixture harness
that is not a git repository, and explicit dispatch recording that resolved the enclosing
checkout through `main_checkout`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_cli import telemetry
from agent_cli.dispatch import telemetry_events
from agent_cli.telemetry import exclusions, fence


def _live_checkout_with_temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """An outer git checkout with the suite's temp root inside it, fenced the way conftest fences."""
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    temp_root = outer / "pytest-tmp"
    harness = temp_root / "harness"
    harness.mkdir(parents=True)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(temp_root.resolve()))
    monkeypatch.setenv(fence.FENCE_ENV, str(temp_root.resolve()))
    return outer, harness


def _files_under(path: Path) -> list[Path]:
    if not path.exists():
        return []
    files = [p for p in path.rglob("*") if p.is_file()]
    if sys.platform == "win32":
        # the telemetry sink locks a `<log>.jsonl.lock` sibling there instead of the log itself
        files = [p for p in files if not p.name.endswith(".jsonl.lock")]
    return files


def test_fixture_dispatch_telemetry_does_not_reach_the_enclosing_checkout(tmp_path, monkeypatch):
    outer, harness = _live_checkout_with_temp_root(tmp_path, monkeypatch)
    d = {"id": "001-mac-01", "feature": 1, "provider": "claude"}
    assert telemetry_events.emit(harness, d, "dispatch.launch", outcome="fresh") is None
    assert _files_under(outer / ".agent") == []


def _unfenced(monkeypatch: pytest.MonkeyPatch) -> None:
    """No fence, no git ceiling, no inherited git or harness routing: the production environment.
    Every `GIT_*` and `AGENT_*` variable is dropped, whatever the caller's sandbox set, and the
    code under test must be this checkout's, not another install's."""
    for name in [n for n in os.environ if n.startswith(("GIT_", "AGENT_"))]:
        monkeypatch.delenv(name)
    here = Path(__file__).resolve().parents[1] / "src"
    assert Path(telemetry_events.__file__).resolve().is_relative_to(here), telemetry_events.__file__


def test_unfenced_fixture_harness_does_not_reach_the_enclosing_checkout(tmp_path, monkeypatch):
    """Only the harness itself keeps the sink out: the temp root may sit anywhere, even in a checkout."""
    _unfenced(monkeypatch)
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    harness = outer / "fixture" / "harness"
    harness.mkdir(parents=True)
    d = {"id": "001-mac-01", "feature": 1, "provider": "claude"}
    assert telemetry_events.emit(harness, d, "dispatch.launch", outcome="fresh") is None
    assert _files_under(outer / ".agent") == []


def test_unfenced_checkout_roots_still_record(tmp_path, monkeypatch):
    _unfenced(monkeypatch)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@example.com")
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "-b", "w", str(wt)], check=True)
    d = {"id": "001-mac-01", "feature": 1}
    assert telemetry_events.emit(main, d, "dispatch.launch", outcome="fresh") is not None
    assert telemetry_events.emit(wt, d, "dispatch.launch", outcome="fresh") is not None
    assert len(_files_under(main / ".agent" / "telemetry" / "events")) == 1


def test_a_child_process_is_fenced_too(tmp_path, monkeypatch):
    outer, harness = _live_checkout_with_temp_root(tmp_path, monkeypatch)
    code = (
        "import sys; from pathlib import Path; from agent_cli.dispatch import telemetry_events as t; "
        "t.emit(Path(sys.argv[1]), {'id': '001-mac-01', 'feature': 1}, 'dispatch.launch')"
    )
    subprocess.run([sys.executable, "-c", code, str(harness)], check=True)
    assert _files_under(outer / ".agent") == []


def test_an_explicit_record_outside_the_fence_is_refused_and_noted(tmp_path, monkeypatch):
    outer, _ = _live_checkout_with_temp_root(tmp_path, monkeypatch)
    assert telemetry.record(outer, "dispatch.launch", dispatch="001-mac-01") is None
    assert _files_under(outer / ".agent") == []
    violations = (outer / "pytest-tmp" / fence.VIOLATIONS).read_text().splitlines()
    assert [json.loads(line)["kind"] for line in violations] == ["dispatch.launch"]


def test_the_health_probe_cannot_append_outside_the_fence(tmp_path, monkeypatch):
    outer, _ = _live_checkout_with_temp_root(tmp_path, monkeypatch)
    report = telemetry.health(telemetry.Sink.for_main(outer), recorder=telemetry.Recorder())
    assert report["status"] == "failed"
    assert _files_under(outer / ".agent") == []


def test_a_sink_inside_the_fence_still_records(tmp_path, monkeypatch):
    monkeypatch.setenv(fence.FENCE_ENV, str(tmp_path.resolve()))
    sink = telemetry.Sink.from_directory(tmp_path / "sink")
    assert telemetry.Recorder().record(sink, "health.probe") is not None
    assert len(list((tmp_path / "sink" / "events").glob("*.jsonl"))) == 1


@pytest.mark.skipif(os.name != "nt", reason="only Windows realpath returns the \\\\?\\ form")
def test_a_sink_resolved_while_another_process_creates_it_is_inside_the_fence(tmp_path, monkeypatch):
    """A sink another process creates mid-resolve: `realpath` returns it in the `\\\\?\\` form,
    simulated here for the one path that race hits."""
    monkeypatch.setenv(fence.FENCE_ENV, str(tmp_path.resolve()))
    sink = tmp_path / "main" / ".agent" / "telemetry"
    real = os.path.realpath
    plain = real(sink)

    def racing(path, **kwargs):
        resolved = real(path, **kwargs)
        return "\\\\?\\" + resolved if resolved == plain else resolved

    monkeypatch.setattr(os.path, "realpath", racing)
    assert sink.resolve().drive.startswith("\\\\?\\")
    assert fence.allows(sink)
    assert not fence.allows(tmp_path.parent / "elsewhere")


NOTE = r"""
import sys, time
from pathlib import Path
from agent_cli.telemetry import fence
ready, go = Path(sys.argv[1]), Path(sys.argv[2])
ready.write_text("")
while not go.exists():
    time.sleep(0.001)
fence.note(Path(sys.argv[3]), "dispatch.terminal")
"""


def test_concurrent_violations_each_keep_their_line(tmp_path, monkeypatch):
    monkeypatch.setenv(fence.FENCE_ENV, str(tmp_path))
    gate = tmp_path / "gate"
    gate.mkdir()
    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                NOTE,
                str(gate / f"ready-{n}"),
                str(gate / "go"),
                str(tmp_path / f"outside-{n}"),
            ]
        )
        for n in range(8)
    ]
    deadline = time.monotonic() + 60
    while len(list(gate.glob("ready-*"))) < 8 and time.monotonic() < deadline:
        time.sleep(0.01)
    (gate / "go").write_text("")
    assert [child.wait(timeout=60) for child in children] == [0] * 8
    lines = (tmp_path / fence.VIOLATIONS).read_text(encoding="utf-8").splitlines()
    assert sorted(json.loads(line)["sink"] for line in lines) == sorted(
        str(tmp_path / f"outside-{n}") for n in range(8)
    )


def _hold_violation_lock(path: Path, held: threading.Event, release: threading.Event) -> None:
    with telemetry.sink.locked(path):
        held.set()
        release.wait(30)


@pytest.mark.parametrize("hold", ["past_the_sink_budget", "past_the_note_budget"])
def test_a_violation_survives_a_held_lock(tmp_path, monkeypatch, hold):
    """A held sink lock once cost the refusal its line, so the test that leaked passed."""
    monkeypatch.setenv(fence.FENCE_ENV, str(tmp_path))
    if hold == "past_the_note_budget":
        monkeypatch.setattr(fence, "NOTE_LOCK_TIMEOUT", 0.5)
    path = tmp_path / fence.VIOLATIONS
    held, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_violation_lock, args=(path, held, release))
    holder.start()
    try:
        assert held.wait(10)
        if hold == "past_the_sink_budget":
            threading.Timer(3 * telemetry.sink.LOCK_TIMEOUT, release.set).start()
        fence.note(tmp_path / "outside", "dispatch.terminal")
    finally:
        release.set()
        holder.join(10)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["sink"] for line in lines] == [str(tmp_path / "outside")]


def _event(event_id: str, seq: int) -> str:
    now = datetime.now(UTC).isoformat()
    writer = {"host": "h", "pid": 1, "instance": "11111111-1111-4111-8111-111111111111", "seq": seq}
    return json.dumps(
        {
            "schema": 1,
            "event_id": event_id,
            "parent_id": None,
            "kind": "dispatch.launch",
            "ts": now,
            "duration_s": None,
            "outcome": "fresh",
            "writer": writer,
            "session": None,
            "feature": "001",
            "dispatch": "001-mac-01",
            "provider": "claude",
            "model": None,
            "cost_usd": None,
            "cost_source": None,
            "commit": None,
            "source": "live",
            "data": {},
        }
    )


def _sink_with_two_events(tmp_path: Path) -> tuple[telemetry.Sink, list[str]]:
    sink = telemetry.Sink.from_directory(tmp_path / "sink")
    ids = ["22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"]
    sink.events.mkdir(parents=True)
    day = sink.day(datetime.now(UTC).date())
    day.write_text("".join(_event(i, n + 1) + "\n" for n, i in enumerate(ids)))
    return sink, ids


def test_the_exclusion_manifest_removes_listed_events_from_counts(tmp_path, monkeypatch):
    monkeypatch.setenv(fence.FENCE_ENV, str(tmp_path.resolve()))
    sink, ids = _sink_with_two_events(tmp_path)
    manifest = {"schema": 1, "reason": "fixture launches", "event_ids": [ids[0]]}
    (sink.directory / exclusions.MANIFEST).write_text(json.dumps(manifest))
    report = telemetry.health(sink, recorder=telemetry.Recorder())
    assert report["excluded"] == 1
    assert exclusions.load(sink).skip({"event_id": ids[0]})
    assert not exclusions.load(sink).skip({"event_id": ids[1]})


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"schema": 2, "event_ids": ["x"]})])
def test_a_missing_or_unusable_manifest_excludes_nothing(tmp_path, monkeypatch, content):
    monkeypatch.setenv(fence.FENCE_ENV, str(tmp_path.resolve()))
    sink, _ = _sink_with_two_events(tmp_path)
    if content is not None:
        (sink.directory / exclusions.MANIFEST).write_text(content)
    assert telemetry.health(sink, recorder=telemetry.Recorder())["excluded"] == 0
