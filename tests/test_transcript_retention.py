import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_cli import capture, features, sessions
from agent_cli.hooks import transcript_retention as retention

pytestmark = pytest.mark.usefixtures("notes_remote")


def report(root, record, source):
    return retention.load(root / retention.BASE / "reports" / f"{retention.key(record, str(source))}.json")


def evidence(root, record, source):
    return root / report(root, record, source)["preserved"]


def event(sid, source, **fields):
    return {"session_id": sid, "transcript_path": str(source), **fields}


@pytest.mark.parametrize("mapping", ["unmapped", "mapped", "closed", "worker", "subagent"])
def test_backfill_keeps_full_evidence_after_source_removal(tmp_root, run, mapping):
    source = tmp_root / "native.jsonl"
    source.write_text('{"type":"user","text":"ask"}\n{"type":"assistant","text":"full tool history"}\n')
    payload = event("native-session", source)
    if mapping != "unmapped":
        run(["feature", "new", "x", "--title", "X"])
        run(["feature", "use", "1", "--session-id", "native-session"])
        if mapping == "closed":
            features.close(tmp_root, features.resolve(tmp_root, "1"))
    if mapping == "worker":
        sessions.set_field(tmp_root, "native-session", "dispatch_id", "worker-1")
    child = tmp_root / "child.jsonl"
    if mapping == "subagent":
        child.write_text('{"type":"assistant","text":"full child work"}\n')
        payload["agent_transcript_path"] = str(child)
    if mapping == "worker":
        from agent_cli.dispatch.supervisor import worker_stop

        worker_stop(tmp_root, payload)
    else:
        capture.capture(tmp_root, "subagent-stop" if mapping == "subagent" else "stop", payload)
    retention.run(tmp_root)
    record = {**payload, "extra": {"agent_transcript_path": str(child)} if mapping == "subagent" else {}}
    saved = evidence(tmp_root, record, source)
    expected = source.read_text()
    source.unlink()
    assert saved.read_text() == expected
    if mapping == "subagent":
        saved_child = evidence(tmp_root, record, child)
        child.unlink()
        assert "full child work" in saved_child.read_text()
    outcome = report(tmp_root, record, source)
    assert outcome["status"] == "preserved"
    if mapping == "worker":
        assert outcome["associations"][0]["dispatch_id"] == "worker-1"
    assert saved.stat().st_mode & 0o077 == 0 if os.name != "nt" else True


def test_refresh_redacts_credentials_and_preserves_published_copy_on_partial_tail(tmp_root):
    source = tmp_root / "live.jsonl"
    record = event("s", source)
    source.write_text('{"type":"user","text":"first"}\n')
    with retention.lock(tmp_root, "writer.lock", 0):
        retention.retain(tmp_root, record, deadline=time.monotonic() + 10)
        source.write_text(
            source.read_text()
            + '{"type":"atis-latch","secret":"hidden"}\n{"type":"assistant","text":"tail"}\n'
        )
        retention.retain(tmp_root, record, deadline=time.monotonic() + 10)
        saved = evidence(tmp_root, record, source)
        assert "tail" in saved.read_text()
        assert "hidden" not in saved.read_text()
        published = saved.read_bytes()
        source.write_text(source.read_text() + '{"type":"bridge-session","secret":')
        retention.retain(tmp_root, record, deadline=time.monotonic() + 10)
        assert saved.read_bytes() == published
        assert report(tmp_root, record, source)["status"] == "partial"
        source.write_text(source.read_text() + '"secret"}\n{"type":"assistant","text":"finished"}\n')
        retention.retain(tmp_root, record, deadline=time.monotonic() + 10)
        assert "finished" in saved.read_text()
        assert "secret" not in saved.read_text()


def test_equal_basenames_and_sessions_have_distinct_source_identity(tmp_root):
    sources = [tmp_root / "a" / "same.jsonl", tmp_root / "b" / "same.jsonl"]
    paths = []
    for index, source in enumerate(sources):
        source.parent.mkdir()
        source.write_text(json.dumps({"message": index}) + "\n")
        for sid in ("one", "two"):
            record = event(sid, source)
            retention.retain(tmp_root, record, deadline=time.monotonic() + 10)
            paths.append(evidence(tmp_root, record, source))
    assert len(set(paths)) == 4
    assert [json.loads(p.read_text())["message"] for p in paths] == [0, 0, 1, 1]


def test_missing_source_reports_loss_without_erasing_retained_evidence(tmp_root):
    source = tmp_root / "gone.jsonl"
    source.write_text('{"message":"available"}\n')
    record = event("s", source)
    retention.retain(tmp_root, record, deadline=time.monotonic() + 10)
    saved = evidence(tmp_root, record, source)
    source.unlink()
    retention.retain(tmp_root, record, deadline=time.monotonic() + 10)
    assert report(tmp_root, record, source)["status"] == "missing"
    assert "available" in saved.read_text()
    never = event("never", source)
    retention.retain(tmp_root, never, deadline=time.monotonic() + 10)
    assert report(tmp_root, never, source)["preserved"] is None
    assert report(tmp_root, never, source)["status"] == "missing"


def test_concurrent_capture_adoption_and_copy_keep_identity_and_associations(tmp_root, run):
    run(["feature", "new", "x", "--title", "X"])
    feature = features.resolve(tmp_root, "1")
    source = tmp_root / "source.jsonl"
    source.write_text('{"message":"complete"}\n')
    payload = event("s", source)
    capture.capture(tmp_root, "stop", payload)
    retention.run(tmp_root)
    saved = evidence(tmp_root, payload, source)

    def append():
        for _ in range(10):
            capture.capture(tmp_root, "stop", payload)

    def adopt():
        sessions.set_record(
            tmp_root, "s", feature={"id": feature.id, "slug": feature.slug, "dir": "features/001-x"}
        )
        capture.adopt(tmp_root, feature, "s")

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(append),
            pool.submit(adopt),
            pool.submit(capture.preserve_transcripts, tmp_root, feature),
        ]
        for future in futures:
            future.result()
    retention.backfill(tmp_root, time.monotonic() + 10)
    outcome = report(tmp_root, payload, source)
    assert {a["feature"] for a in outcome["associations"]} == {None, 1}
    source.unlink()
    assert saved.read_text() == '{"message":"complete"}\n'
    assert not list((tmp_root / ".agent/raw/unassigned").glob("*.jsonl"))
    assert sum(len(capture.read_jsonl(p)) for p in feature.raw_dir.glob("*.jsonl")) == 11


def test_trigger_enqueues_before_copy_and_returns_under_busy_writer(tmp_root, monkeypatch):
    from agent_cli.usage import job

    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_USAGE", raising=False)
    launched = []
    monkeypatch.setattr(job, "spawn", lambda cmd, root: launched.append(cmd))
    monkeypatch.setattr(retention, "backfill", lambda *a: pytest.fail("history scan in trigger"))
    monkeypatch.setattr(retention, "retain", lambda *a, **k: pytest.fail("copy in trigger"))
    with retention.lock(tmp_root, "writer.lock", 0):
        start = time.monotonic()
        retention.enqueue(tmp_root, event("s", tmp_root / "not-yet.jsonl"))
        assert time.monotonic() - start < 1
    assert launched
    assert list((tmp_root / retention.BASE / "pending").glob("*.json"))
    assert not (tmp_root / retention.BASE / "transcripts").exists()


def test_worker_process_exits_and_resumes_interrupted_pending_refresh(tmp_root):
    source = tmp_root / "source.jsonl"
    source.write_text('{"message":"full evidence"}\n')
    record = event("s", source)
    pending = tmp_root / retention.BASE / "pending" / "interrupted.json"
    retention.save(pending, record)
    completed = subprocess.run(
        [sys.executable, "-m", "agent_cli.hooks.transcript_retention", str(tmp_root), "0"],
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert not pending.exists()
    assert evidence(tmp_root, record, source).read_text() == source.read_text()


def test_discovery_retains_native_claude_sidechain_without_stop_event(tmp_root):
    parent = tmp_root / "parent.jsonl"
    parent.write_text('{"type":"user"}\n')
    children = tmp_root / "parent" / "subagents"
    children.mkdir(parents=True)
    child = children / "agent-1.jsonl"
    child.write_text('{"type":"assistant","message":"sidechain tools"}\n')
    capture.capture(tmp_root, "stop", event("parent", parent))
    retention.run(tmp_root)
    saved = evidence(tmp_root, event("parent", child), child)
    child.unlink()
    assert "sidechain tools" in saved.read_text()


def test_native_codex_discovery_follows_children_and_archived_rollouts(tmp_root, monkeypatch):
    native = tmp_root / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(native))
    paths = []
    for sid, parent, folder in [
        ("manager", "", "archived_sessions"),
        ("child", "manager", "sessions"),
        ("grandchild", "child", "sessions"),
        ("other", "", "sessions"),
    ]:
        directory = native / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-{sid}.jsonl"
        source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
        path.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": sid, "source": source}})
            + '\n{"type":"response_item","payload":"full reasoning and tools"}\n'
        )
        paths.append(path)
    capture.capture(tmp_root, "stop", {"session_id": "manager"})
    retention.run(tmp_root)
    pointer = retention.load(tmp_root / retention.reports({"session_id": "manager"})[0])
    assert pointer["status"] == "discovered"
    assert len(pointer["related_reports"]) == 3
    for path in paths[:3]:
        saved = evidence(tmp_root, event("manager", path), path)
        path.unlink()
        assert "full reasoning and tools" in saved.read_text()
    assert not report(tmp_root, event("manager", paths[3]), paths[3])


def test_later_followup_refreshes_evidence_after_backfill_and_close(tmp_root, run):
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s"])
    source = tmp_root / "native.jsonl"
    source.write_text('{"message":"first conversation"}\n')
    payload = event("s", source)
    capture.capture(tmp_root, "stop", payload)
    retention.run(tmp_root)
    features.close(tmp_root, features.resolve(tmp_root, "1"))
    source.write_text(source.read_text() + '{"message":"post-close followup"}\n')
    capture.capture(tmp_root, "session-end", payload)
    retention.run(tmp_root)
    saved = evidence(tmp_root, payload, source)
    source.unlink()
    assert "first conversation" in saved.read_text()
    assert "post-close followup" in saved.read_text()


def test_no_source_is_explicitly_unknown_and_raw_event_links_report(tmp_root):
    result = capture.capture(tmp_root, "stop", {"session_id": "unknown"})
    retention.run(tmp_root)
    raw = capture.read_jsonl(tmp_root / result["written"])[0]
    outcome = retention.load(tmp_root / raw["retention_reports"][0])
    assert outcome["status"] == "unavailable"
    assert outcome["preserved"] is None
    assert "no native transcript path" in outcome["detail"]


def test_interrupted_copy_keeps_previous_evidence_and_pending_request(tmp_root, monkeypatch):
    source = tmp_root / "native.jsonl"
    source.write_text('{"message":"published"}\n')
    record = event("s", source)
    retention.retain(tmp_root, record, deadline=time.monotonic() + 10)
    saved = evidence(tmp_root, record, source)
    source.write_text(source.read_text() + '{"message":"new tail"}\n')
    pending = tmp_root / retention.BASE / "pending" / "retry.json"
    retention.save(pending, record)

    def interrupt(*args):
        raise TimeoutError("interrupted refresh")

    with monkeypatch.context() as patch:
        patch.setattr(retention, "_copy", interrupt)
        retention.run(tmp_root)
    assert pending.exists()
    assert saved.read_text() == '{"message":"published"}\n'
    retention.run(tmp_root)
    assert not pending.exists()
    assert "new tail" in saved.read_text()


def test_trigger_during_copy_is_consumed_before_worker_releases_queue(tmp_root, monkeypatch):
    from agent_cli.usage import job

    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_USAGE", raising=False)
    monkeypatch.setattr(job, "spawn", lambda *args: None)
    first = tmp_root / "first.jsonl"
    second = tmp_root / "second.jsonl"
    first.write_text('{"message":"first"}\n')
    second.write_text('{"message":"second"}\n')
    retention.enqueue(tmp_root, event("first", first))
    original = retention._copy

    def enqueue_during_copy(source, dest, deadline):
        if source == first:
            retention.enqueue(tmp_root, event("second", second))
        return original(source, dest, deadline)

    monkeypatch.setattr(retention, "_copy", enqueue_during_copy)
    retention.run(tmp_root)
    assert "second" in evidence(tmp_root, event("second", second), second).read_text()
    assert not list((tmp_root / retention.BASE / "pending").glob("*.json"))
