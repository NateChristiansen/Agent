import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("notes_remote")

STOP = {
    "session_id": "s-123",
    "hook_event_name": "Stop",
    "cwd": "/tmp/x",
    "transcript_path": "/tmp/x.jsonl",
    "last_assistant_message": "all done",
    "stop_hook_active": False,
    "permission_mode": "default",
}


def test_session_set_get(tmp_root: Path, run):
    assert run(["session", "get", "--session-id", "s1"]).code == 1
    r = run(["--json", "session", "set", "--session-id", "s1", "--cwd", "/tmp"])
    assert r.code == 0
    assert r.json["feature"] is None
    assert r.json["cwd"] == "/tmp"
    r = run(["--json", "session", "get", "--session-id", "s1"])
    assert r.json["session_id"] == "s1"


def test_feature_use_resolution_order(tmp_root: Path, run, monkeypatch):
    run(["feature", "new", "x", "--title", "X"])
    r = run(["--json", "feature", "use", "1", "--session-id", "explicit"])
    assert r.json["feature"] == {"id": 1, "slug": "x", "dir": "features/001-x"}
    monkeypatch.setenv("CLAUDE_SESSION_ID", "from-old-env")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    r = run(["--json", "feature", "use", "1"])
    assert r.json["session_id"] == "from-env"
    assert "CLAUDE_CODE_SESSION_ID" in r.err
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    r = run(["--json", "feature", "use", "1"])
    assert r.json["session_id"] == "from-old-env"
    monkeypatch.delenv("CLAUDE_SESSION_ID")
    r = run(["--json", "feature", "current"])
    assert r.code == 2  # no env: never guess from `latest` or a stale default file
    assert "name a feature explicitly" in r.err
    (tmp_root / ".agent/default-feature").write_text("1\n")
    assert run(["--json", "feature", "current"]).code == 2
    assert run(["feature", "default", "1"]).code == 2
    r = run(["feature", "use", "1"])
    assert r.code == 2
    assert "pass --session-id" in r.err


def test_current_is_session_mapped_only_when_stale_default_file_exists(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "new", "y", "--title", "Y"])
    run(["feature", "use", "1", "--session-id", "s1"])
    run(["feature", "use", "2", "--session-id", "s2"])
    (tmp_root / ".agent/default-feature").write_text("2\n")

    assert run(["--json", "feature", "current", "--session-id", "s1"]).json["id"] == 1
    assert run(["--json", "feature", "current", "--session-id", "s2"]).json["id"] == 2
    run(["session", "set", "--session-id", "unmapped"])
    unmapped = run(["feature", "current", "--session-id", "unmapped"])
    assert unmapped.code == 1
    assert "name a feature explicitly" in unmapped.err


def test_feature_new_with_use(tmp_root: Path, run):
    r = run(["--json", "feature", "new", "x", "--title", "X", "--use", "--session-id", "s9"])
    assert r.json["session_id"] == "s9"
    assert run(["--json", "session", "get", "--session-id", "s9"]).json["feature"]["id"] == 1


def test_feature_use_adopts_the_calling_sessions_unassigned_captures(tmp_root: Path, run):
    """Mapping used to move nothing, so every session's first prompt (captured
    before /intake can map it) stayed in .agent/raw/unassigned/ forever. Another session's
    records in the same file are left exactly where they are."""
    run(["capture", "--kind", "stop", "--stdin"], stdin=json.dumps(STOP))
    run(["capture", "--kind", "prompt", "--stdin"], stdin=json.dumps({**STOP, "hook_event_name": "x"}))
    run(["capture", "--kind", "stop", "--stdin"], stdin=json.dumps({**STOP, "session_id": "other"}))
    run(["feature", "new", "x", "--title", "X"])
    r = run(["--json", "feature", "use", "1", "--session-id", "s-123"])
    assert r.json["adopted"] == 2
    adopted = (tmp_root / ".agent/raw/001-x/2026-09-03.jsonl").read_text().splitlines()
    assert len(adopted) == 2
    assert all(json.loads(line)["feature"] == 1 for line in adopted)
    left = (tmp_root / ".agent/raw/unassigned/2026-09-03.jsonl").read_text().splitlines()
    assert [json.loads(line)["session_id"] for line in left] == ["other"]
    assert "1 capture(s) adopted" in run(["feature", "use", "1", "--session-id", "other"]).out
    # nothing left to adopt on the second call, and it does not report a phantom move
    assert run(["--json", "feature", "use", "1", "--session-id", "s-123"]).json["adopted"] == 0


def test_feature_new_warns_when_the_session_it_runs_in_is_unmapped(tmp_root: Path, run, monkeypatch):
    """An earlier candidate: one feature was built inside another's session with an empty Context. One
    line on stderr, never a refusal. Silent when the session IS mapped, or when --use
    maps it here."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-new")
    r = run(["feature", "new", "a", "--title", "A"])
    assert "mapped to no feature" in r.err
    assert "feature_use" in r.err
    r = run(["feature", "new", "b", "--title", "B", "--use"])
    assert "mapped to no feature" not in r.err
    r = run(["feature", "new", "c", "--title", "C"])
    assert "mapped to no feature" not in r.err  # the session is mapped now, by --use above


def test_feature_new_outside_a_session_says_nothing(tmp_root: Path, run):
    r = run(["feature", "new", "a", "--title", "A"])
    assert "mapped to no feature" not in r.err


def test_capture_routes_to_unassigned_then_feature(tmp_root: Path, run):
    r = run(["--json", "capture", "--kind", "stop", "--stdin"], stdin=json.dumps(STOP))
    assert r.code == 0
    assert r.json == {"written": ".agent/raw/unassigned/2026-09-03.jsonl", "feature": None, "kind": "stop"}
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s-123"])
    r = run(["--json", "capture", "--kind", "stop", "--stdin"], stdin=json.dumps(STOP))
    assert r.json["written"] == ".agent/raw/001-x/2026-09-03.jsonl"
    record = json.loads((tmp_root / ".agent/raw/001-x/2026-09-03.jsonl").read_text().splitlines()[0])
    assert record["kind"] == "stop"
    assert record["feature"] == 1
    assert record["last_assistant_message"] == "all done"
    assert record["transcript_path"] == "/tmp/x.jsonl"
    assert record["extra"] == {"stop_hook_active": False, "permission_mode": "default"}
    assert record["ts"] == "2026-09-03T14:05:00-07:00"


def test_capture_replaces_surrogates_and_reads_back_both_records(tmp_root: Path, run):
    from agent_cli.capture import read_jsonl

    ordinary = run(["--json", "capture", "--kind", "stop", "--stdin"], stdin=json.dumps(STOP))
    payload = {
        **STOP,
        "last_assistant_message": "emoji \ud83d and 😀",
        "nested": [
            {"key\udc00": "value\ud83d"},
            {"good": "café"},
            42,
            None,
        ],
    }
    affected = run(["--json", "capture", "--kind", "stop", "--stdin"], stdin=json.dumps(payload))
    assert ordinary.code == affected.code == 0
    rows = read_jsonl(tmp_root / ordinary.json["written"])
    assert len(rows) == 2
    assert rows[0]["last_assistant_message"] == STOP["last_assistant_message"]
    assert rows[1]["last_assistant_message"] == "emoji \ufffd and 😀"
    assert rows[1]["extra"]["nested"] == [{"key\ufffd": "value\ufffd"}, {"good": "café"}, 42, None]
    assert payload["last_assistant_message"] == "emoji \ud83d and 😀"


def test_capture_bad_stdin_writes_nothing(tmp_root: Path, run):
    r = run(["capture", "--kind", "prompt", "--stdin"], stdin="{not json")
    assert r.code == 2
    assert not (tmp_root / ".agent" / "raw").exists()


def test_capture_kind_validation(tmp_root: Path, run):
    assert run(["capture", "--kind", "bogus", "--stdin"], stdin="{}").code == 2
    assert not (tmp_root / ".agent" / "raw").exists()


# ---------------------------------------------------------------- preserve_transcripts


def test_preserve_transcripts_copies_referenced_files(tmp_root: Path, run):
    """The evidence a retro reads (plan 3.5): Claude Code sweeps ~/.claude/projects on its
    own cleanupPeriodDays, so a transcript a raw capture points at has to be copied into the
    feature's own `.agent/raw/<slug>/` before that happens."""
    from agent_cli import capture as capture_mod
    from agent_cli import features as features_mod

    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    transcript = tmp_root.parent / "transcript.jsonl"
    transcript.write_text('{"type": "assistant"}\n', encoding="utf-8")
    run(
        ["capture", "--kind", "stop", "--stdin"],
        stdin=json.dumps({**STOP, "session_id": "s1", "transcript_path": str(transcript)}),
    )
    f = features_mod.resolve(tmp_root, "1")
    copied = capture_mod.preserve_transcripts(tmp_root, f)
    dest = f.raw_dir / "transcripts" / "transcript.jsonl"
    assert copied == [dest]
    assert dest.read_text() == transcript.read_text()
    # already present and no older than its source: not re-copied
    assert capture_mod.preserve_transcripts(tmp_root, f) == []


def test_preserve_transcripts_keeps_the_subagents_own_transcript(tmp_root: Path, run):
    """SubagentStop's `transcript_path` is the PARENT session's, and that file has no
    sidechain records — the subagent's own work is in `extra.agent_transcript_path`
    (`.../<session>/subagents/agent-<id>.jsonl`). Retro-readers, journal drafters and review
    agents are most of the model work on a harness day, so preserving only the parent kept
    none of it past Claude Code's 30-day sweep."""
    from agent_cli import capture as capture_mod
    from agent_cli import features as features_mod

    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    parent = tmp_root.parent / "parent.jsonl"
    parent.write_text('{"type": "assistant"}\n', encoding="utf-8")
    agent = tmp_root.parent / "agent-abc123.jsonl"
    agent.write_text('{"type": "assistant", "message": {"model": "claude-sonnet-5"}}\n', encoding="utf-8")
    run(
        ["capture", "--kind", "subagent-stop", "--stdin"],
        stdin=json.dumps(
            {
                **STOP,
                "hook_event_name": "SubagentStop",
                "session_id": "s1",
                "transcript_path": str(parent),
                "agent_transcript_path": str(agent),
            }
        ),
    )
    f = features_mod.resolve(tmp_root, "1")
    copied = capture_mod.preserve_transcripts(tmp_root, f)
    assert sorted(p.name for p in copied) == ["agent-abc123.jsonl", "parent.jsonl"]
    assert (f.raw_dir / "transcripts" / "agent-abc123.jsonl").read_text() == agent.read_text()


def test_preserve_transcripts_skips_missing_files_silently(tmp_root: Path, run):
    from agent_cli import capture as capture_mod
    from agent_cli import features as features_mod

    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    run(
        ["capture", "--kind", "stop", "--stdin"],
        stdin=json.dumps({**STOP, "session_id": "s1", "transcript_path": "/nonexistent/path/gone.jsonl"}),
    )
    f = features_mod.resolve(tmp_root, "1")
    assert capture_mod.preserve_transcripts(tmp_root, f) == []
    assert not (f.raw_dir / "transcripts").exists()


def test_preserve_transcripts_dedupes_the_same_path(tmp_root: Path, run):
    from agent_cli import capture as capture_mod
    from agent_cli import features as features_mod

    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    transcript = tmp_root.parent / "shared.jsonl"
    transcript.write_text("one line\n", encoding="utf-8")
    for _ in range(2):
        run(
            ["capture", "--kind", "stop", "--stdin"],
            stdin=json.dumps({**STOP, "session_id": "s1", "transcript_path": str(transcript)}),
        )
    f = features_mod.resolve(tmp_root, "1")
    assert len(capture_mod.preserve_transcripts(tmp_root, f)) == 1


def test_preserve_transcripts_on_a_feature_with_no_raw_dir(tmp_root: Path, run):
    from agent_cli import capture as capture_mod
    from agent_cli import features as features_mod

    run(["feature", "new", "x", "--title", "X"])
    f = features_mod.resolve(tmp_root, "1")
    assert capture_mod.preserve_transcripts(tmp_root, f) == []


# ------------------------------------------------- transcript auth redaction

AUTH_LINES = (
    '{"type":"atis-latch","atis":"v1.pretend-bearer-token","sessionId":"s1"}\n'
    '{"type":"bridge-session","sessionId":"s1","ownerAccountUuid":"u","ownerOrganizationUuid":"o"}\n'
)


def test_redact_jsonl_drops_only_the_auth_records(tmp_root: Path):
    """Claude Code writes its own session-auth records into a transcript and a preserved copy
    keeps them forever. A credential does not belong in an artifact handed to an AI
    tool. Whole records only — everything else, including a line that is not JSON, survives
    byte for byte, because a transcript is evidence."""
    from agent_cli import capture as capture_mod

    path = tmp_root / "t.jsonl"
    path.write_text(
        '{"type":"assistant","message":{"model":"m"}}\n'
        + AUTH_LINES
        + "not json at all\n"
        + '{"type":"user","atis":"kept: not an atis-latch record"}\n'
        + "[1, 2, 3]\n",
        encoding="utf-8",
    )
    assert capture_mod.redact_jsonl(path) == 2
    kept = path.read_text(encoding="utf-8").splitlines()
    assert kept == [
        '{"type":"assistant","message":{"model":"m"}}',
        "not json at all",
        '{"type":"user","atis":"kept: not an atis-latch record"}',
        "[1, 2, 3]",
    ]
    # idempotent
    assert capture_mod.redact_jsonl(path) == 0


def test_redact_jsonl_keeps_mode_and_mtime(tmp_root: Path):
    """The mtime has to survive: `preserve_transcripts` skips a copy no older than its source,
    and a redaction that bumped the mtime would make every preserved transcript look stale and
    re-copy the lot on every close."""
    from agent_cli import capture as capture_mod

    path = tmp_root / "t.jsonl"
    path.write_text(AUTH_LINES + '{"type":"assistant"}\n', encoding="utf-8")
    path.chmod(0o600)
    before = path.stat()
    assert capture_mod.redact_jsonl(path) == 2
    after = path.stat()
    assert after.st_mtime == before.st_mtime
    if sys.platform != "win32":  # Windows keeps no POSIX mode bits to preserve
        assert after.st_mode & 0o777 == 0o600
    assert not list(tmp_root.glob("*.redact"))


def test_preserve_transcripts_redacts_what_it_copies(tmp_root: Path, run):
    from agent_cli import capture as capture_mod
    from agent_cli import features as features_mod

    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    transcript = tmp_root.parent / "withauth.jsonl"
    transcript.write_text('{"type":"assistant"}\n' + AUTH_LINES, encoding="utf-8")
    run(
        ["capture", "--kind", "stop", "--stdin"],
        stdin=json.dumps({**STOP, "session_id": "s1", "transcript_path": str(transcript)}),
    )
    f = features_mod.resolve(tmp_root, "1")
    (dest,) = capture_mod.preserve_transcripts(tmp_root, f)
    assert dest.read_text(encoding="utf-8") == '{"type":"assistant"}\n'
    # the source is left alone: it is Claude Code's file, not ours
    assert "atis-latch" in transcript.read_text(encoding="utf-8")
    # and the smaller copy does not look stale
    assert capture_mod.preserve_transcripts(tmp_root, f) == []


def test_raw_redact_walks_every_preserved_transcript(tmp_root: Path, run):
    from agent_cli import features as features_mod

    run(["feature", "new", "x", "--title", "X"])
    f = features_mod.resolve(tmp_root, "1")
    dest_dir = f.raw_dir / "transcripts"
    dest_dir.mkdir(parents=True)
    (dest_dir / "dirty.jsonl").write_text(AUTH_LINES + '{"type":"assistant"}\n', encoding="utf-8")
    (dest_dir / "clean.jsonl").write_text('{"type":"assistant"}\n', encoding="utf-8")
    r = run(["--json", "raw", "redact"])
    assert r.json["scanned"] == 2
    assert r.json["changed"] == 1
    assert r.json["dropped"] == 2
    assert "atis-latch" not in (dest_dir / "dirty.jsonl").read_text(encoding="utf-8")
    again = run(["--json", "raw", "redact"])
    assert again.json["dropped"] == 0


def test_agent_subtree_dirs_include_raw(tmp_root: Path, run):
    """Raw captures live under `.agent/raw/`, protected the same way as every
    other CLI-owned `.agent/` subtree (hooks.py's Bash-write regex, the native `Edit(...)` deny
    rules, and doctor's settings.json check all derive from this one registry)."""
    from agent_cli import core

    assert "raw" in core.AGENT_SUBTREE_DIRS


# ------------------------------------------------------------------ one pid resolver (T4 item 3)


def test_resolve_pid_prefers_claude_pid_then_the_parent(monkeypatch):
    """`sessions.resolve_pid` is the one implementation behind the session-start hook's by-pid
    map and record, `ops._lease_pid`, and (through `candidate_pids`) the MCP server's session
    lookup. They each had their own copy, and each docstring claimed the others agreed."""
    import os

    # `session_identity`, not `sessions`: the latter re-exports these names, and a re-export is
    # a second binding. Patching it would leave the module that actually calls `client_pid`
    # untouched, so this test would pass against unpatched code.
    from agent_cli import session_identity as sessions

    # Three steps since because a client may export no pid of its own: the env var,
    # then the nearest ancestor that looks like a client, then the parent. `client_pid` is
    # stubbed here so this pins the order rather than whatever process happens to be running the
    # suite — the walk has its own test below.
    monkeypatch.setattr(sessions, "client_process", lambda *a, **k: None)

    monkeypatch.setenv("CLAUDE_PID", "4242")
    assert sessions.resolve_pid() == 4242
    assert sessions.candidate_pids() == [4242]
    monkeypatch.setenv("CLAUDE_PID", "not-a-pid")
    assert sessions.resolve_pid() == os.getppid()
    monkeypatch.delenv("CLAUDE_PID")
    assert sessions.resolve_pid() == os.getppid()
    # and when a client IS found up the tree, it wins over the parent: the parent of a hook is
    # the shell that ran it, which is dead moments later
    monkeypatch.setattr(sessions, "client_process", lambda *a, **k: (777, "claude"))
    assert sessions.resolve_pid() == 777
    monkeypatch.setattr(sessions, "client_process", lambda *a, **k: None)
    # no CLAUDE_PID: the parent first, then the grandparent a shell in between would add
    pids = sessions.candidate_pids()
    assert pids[0] == os.getppid()
    assert len(pids) in (1, 2)


def test_the_three_callers_share_the_resolver(monkeypatch, tmp_root: Path):
    """Patching the shared resolver moves all three: the hook's by-pid file, a lease taken
    through ops, and the MCP server's candidate list."""
    from agent_cli import hooks, ops
    from agent_cli.mcp.tools import Ctx

    monkeypatch.setenv("CLAUDE_PID", "4242")
    hooks.run(tmp_root, "session-start", json.dumps({"session_id": "abc", "source": "startup"}))
    assert (tmp_root / ".agent/sessions/by-pid/4242").read_text().strip() == "abc"
    assert ops.lease_take(tmp_root, area="vault:src", session_id="abc").data["lease"]["pid"] == 4242
    assert Ctx(root=tmp_root, profile="manager").session_id() == "abc"


def test_parallel_writers_keep_every_field(tmp_root: Path):
    """Found live: hooks on one event rewrote the session record in parallel and
    dropped each other's fields (`name`, `started_at`, `feature`). Every writer now holds the
    record's lock for its read-modify-write, so none is lost."""
    import threading

    from agent_cli import sessions

    def writer(n: int) -> None:
        for i in range(15):
            sessions.set_field(tmp_root, "race", f"f{n}_{i}", i)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    threads.append(
        threading.Thread(target=lambda: sessions.set_record(tmp_root, "race", name="mgr-001-a", pid=7))
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    record = sessions.get(tmp_root, "race") or {}
    assert all(f"f{n}_{i}" in record for n in range(8) for i in range(15))
    assert (record["name"], record["pid"]) == ("mgr-001-a", 7) and record["started_at"]


def test_lock_deadline_bounds_waits_in_its_scope_only(tmp_root: Path):
    """One deadline spans every session-lock wait in the scope; a tighter caller timeout keeps its
    own error, a spent scope writes nothing, and the scope ends with the block."""
    import threading
    import time

    from agent_cli import sessions
    from agent_cli.core import AgentError

    taken, release = threading.Event(), threading.Event()

    def hold() -> None:
        with sessions._locked(tmp_root, "s"):
            taken.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert taken.wait(5)
    try:
        with sessions.lock_deadline(0.3):
            began = time.monotonic()
            with pytest.raises(AgentError) as tight:
                sessions.set_field(tmp_root, "s", "a", 1, timeout=0.05)
            assert type(tight.value) is AgentError
            with pytest.raises(sessions.LockDeadline):
                sessions.set_field(tmp_root, "s", "b", 1)
            with sessions.lock_deadline(5), pytest.raises(sessions.LockDeadline):
                sessions.set_field(tmp_root, "s", "c", 1)  # a nested scope keeps the earlier deadline
            assert time.monotonic() - began < 1
        assert sessions._LOCK_DEADLINE.get() is None
    finally:
        release.set()
        holder.join()
    with sessions.lock_deadline(0), pytest.raises(sessions.LockDeadline):
        sessions.set_field(tmp_root, "s", "d", 1)  # free, but spent
    sessions.set_field(tmp_root, "s", "e", 1)
    assert set(sessions.get(tmp_root, "s") or {}) == {"session_id", "e"}
