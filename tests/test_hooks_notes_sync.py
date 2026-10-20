"""The `notes-sync` hook and the pull inside `session-start` (ADR-0034).

The repos are `test_notes_sync`'s pair: a bare remote, this machine's clone `a` and the other
machine's clone `b`. The harness root is a real one less its clones, venv and views, so
`session-start` runs for real.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from agent_cli import notes_sync, sessions
from agent_cli.hooks import notes_sync as hook
from agent_cli.hooks.session import session_start
from harness_root import FIXED_NOW, repoint_files
from test_notes_sync import Pair, git, pair  # noqa: F401 -- reuse the repo pair fixture

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _root_template: Path) -> Path:
    """The shared harness root without its clones, venv and Obsidian views, which neither the
    hook nor `session-start` reads here."""
    shutil.copytree(
        _root_template / "harness",
        tmp_path / "harness",
        symlinks=True,
        ignore=lambda d, _names: ["repos", ".venv", "obsidian"] if Path(d).name == "harness" else [],
        dirs_exist_ok=True,
    )
    repoint_files(tmp_path, _root_template, ("harness/agent.yaml",))
    root = tmp_path / "harness"
    monkeypatch.setenv("AGENT_ROOT", str(root))
    monkeypatch.setenv("AGENT_NOW", FIXED_NOW)
    monkeypatch.setenv("AGENT_NOTIFY", "off")
    for name in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def hp(pair: Pair, tmp_root: Path, monkeypatch: pytest.MonkeyPatch) -> Pair:  # noqa: F811
    (tmp_root / ".agent").mkdir(exist_ok=True)
    (tmp_root / ".agent" / "notes-root").write_text(str(pair.a), encoding="utf-8")
    pair.harness = tmp_root
    pair.mode("on")
    monkeypatch.setattr(sessions, "resolve_pid", lambda: 4242)
    return pair


@pytest.fixture
def pulls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    real = notes_sync.pull

    def counting(root):
        seen.append(1)
        return real(root)

    monkeypatch.setattr(notes_sync, "pull", counting)
    return seen


def stop(p: Pair) -> str:
    return hook.notes_sync_hook(p.harness, {"hook_event_name": "Stop", "session_id": SID})


def prompt(p: Pair) -> str:
    return hook.notes_sync_hook(p.harness, {"hook_event_name": "UserPromptSubmit", "session_id": SID})


def context(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def test_stop_commits_and_publishes_and_says_nothing(hp: Pair):
    sessions.set_field(hp.harness, SID, "name", "mgr-011-notes-auto-sync")
    (hp.a / "log.md").write_text("entry\n", encoding="utf-8")
    assert stop(hp) == ""
    assert git(hp.remote, "show", "main:log.md") == "entry"
    assert "mgr-011-notes-auto-sync" in git(hp.a, "log", "-1", "--format=%b")


def test_a_subagent_stop_does_nothing(hp: Pair):
    (hp.a / "log.md").write_text("entry\n", encoding="utf-8")
    out = hook.notes_sync_hook(hp.harness, {"hook_event_name": "Stop", "session_id": SID, "agent_id": "x"})
    assert out == ""
    assert git(hp.a, "status", "--porcelain") == "?? log.md"


def test_a_stop_result_the_core_did_not_record_is_reported_once(hp: Pair, monkeypatch):
    monkeypatch.setattr(notes_sync, "sync", lambda root, s: notes_sync.Result("error", "git status failed"))
    stop(hp)
    first = context(prompt(hp))
    assert "notes sync error: git status failed" in first
    assert context(prompt(hp)) == ""
    stop(hp)  # the error again; the prompts' clean pulls had verified recovery and cleared it
    for unresolved in ("off", "none", "ahead", "pulled", "locked"):  # none of these is recovery
        monkeypatch.setattr(notes_sync, "sync", lambda root, s, u=unresolved: notes_sync.Result(u))
        stop(hp)
        assert (sessions.get(hp.harness, SID) or {})[hook.PENDING_KEY], unresolved
    converged = notes_sync.Result("pushed", count=1, converged=True)
    monkeypatch.setattr(notes_sync, "sync", lambda root, s: converged)
    stop(hp)
    assert (sessions.get(hp.harness, SID) or {})[hook.PENDING_KEY] is None


def test_a_real_off_stop_keeps_an_unresolved_error(hp: Pair, monkeypatch):
    real = notes_sync.sync
    monkeypatch.setattr(notes_sync, "sync", lambda root, s: notes_sync.Result("error", "git status failed"))
    stop(hp)
    monkeypatch.setattr(notes_sync, "sync", real)
    hp.mode("off")
    stop(hp)
    assert (sessions.get(hp.harness, SID) or {}).get(hook.PENDING_KEY)
    hp.mode("on")
    stop(hp)  # nothing to publish, HEAD equals upstream: verified convergence
    assert (sessions.get(hp.harness, SID) or {}).get(hook.PENDING_KEY) is None


def test_a_prompt_pull_error_is_kept_and_reported_once(hp: Pair, monkeypatch):
    failed = notes_sync.Result("error", "could not read the notes repo state")
    monkeypatch.setattr(notes_sync, "pull", lambda root: failed)
    assert "could not read the notes repo state" in context(prompt(hp))
    assert "notes repo state" not in context(prompt(hp))


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "Stop"])
def test_with_the_switch_off_no_hook_runs_git(hp: Pair, monkeypatch, event: str):
    hp.mode("off")
    sessions.set_field(hp.harness, SID, hook.PENDING_KEY, {"id": "kept", "status": "error", "detail": "x"})
    seen: list[object] = []
    monkeypatch.setattr(notes_sync.procs, "run_bounded", lambda cmd, **kw: seen.append(cmd))
    if event == "SessionStart":
        block = json.loads(session_start(hp.harness, {"session_id": SID, "source": "startup"}))
        assert "notes sync error: x" in block["hookSpecificOutput"]["additionalContext"]
    elif event == "UserPromptSubmit":
        assert "notes sync error: x" in context(prompt(hp))
    else:
        stop(hp)
    assert seen == []


def test_a_standing_failure_is_reported_once_per_session(hp: Pair):
    git(hp.a, "remote", "set-url", "origin", str(hp.remote.parent / "gone.git"))
    (hp.a / "x.md").write_text("x\n", encoding="utf-8")
    stop(hp)
    assert "notes sync offline" in context(prompt(hp))
    assert context(prompt(hp)) == ""
    other = "99999999-2222-3333-4444-555555555555"
    out = hook.notes_sync_hook(hp.harness, {"hook_event_name": "UserPromptSubmit", "session_id": other})
    assert "notes sync offline" in context(out)


def test_one_pull_per_client_process_and_a_resumed_process_pulls_again(hp: Pair, pulls, monkeypatch):
    prompt(hp)
    prompt(hp)
    assert len(pulls) == 1
    hp.commit(hp.b, "from-b.md", "b\n", push=True)
    monkeypatch.setattr(sessions, "resolve_pid", lambda: 5353)  # `codex resume` in a new process
    assert "pulled 1 commit" in context(prompt(hp))
    assert len(pulls) == 2
    assert (hp.a / "from-b.md").exists()


def test_a_failed_pull_retries_after_the_interval(hp: Pair, pulls, monkeypatch):
    url = git(hp.a, "remote", "get-url", "origin")
    git(hp.a, "remote", "set-url", "origin", str(hp.remote.parent / "gone.git"))
    clock = [1000.0]
    monkeypatch.setattr(hook.time, "time", lambda: clock[0])
    prompt(hp)
    clock[0] += hook.RETRY_S - 1
    prompt(hp)
    assert len(pulls) == 1
    git(hp.a, "remote", "set-url", "origin", url)
    clock[0] += 2
    prompt(hp)
    assert len(pulls) == 2


def test_an_unproven_process_pulls_unless_it_just_pulled(hp: Pair, pulls, monkeypatch):
    """A session whose process cannot be proven may be a resume, so its pull was never
    suppressed, which meant a fetch on every machine wake. A bound fixes it: a successful pull within `UNPROVEN_FRESH_S` covers the next
    prompt; after that, or after a failed pull, it pulls again. Here the other machine pushes
    between prompts of a daemon session, as after a `codex resume` the harness cannot see."""
    monkeypatch.setattr(sessions, "resolve_pid", lambda: None)
    clock = [1000.0]
    monkeypatch.setattr(hook.time, "time", lambda: clock[0])
    prompt(hp)
    hp.commit(hp.b, "from-b.md", "b\n", push=True)
    clock[0] += hook.UNPROVEN_FRESH_S - 1
    prompt(hp)
    assert len(pulls) == 1
    clock[0] += 2
    assert "pulled 1 commit" in context(prompt(hp))
    assert len(pulls) == 2


def test_a_prompt_naming_a_missing_notes_commit_pulls_whatever_the_freshness(hp: Pair, pulls, monkeypatch):
    """A peer hands over evidence as a path plus the notes commit that
    published it. Within the freshness window, or after this process already pulled, a prompt
    naming a commit this clone lacks still pulls; one naming a commit it has does not."""
    prompt(hp)
    assert len(pulls) == 1
    hp.commit(hp.b, "windows-probe.md", "result\n", push=True)
    sha = git(hp.b, "rev-parse", "HEAD").strip()
    named = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": SID,
        "prompt": f"see it. notes-commit: `{sha}`",
    }
    assert "pulled 1 commit" in context(hook.notes_sync_hook(hp.harness, named))
    assert (hp.a / "windows-probe.md").exists()
    hook.notes_sync_hook(hp.harness, named)  # the commit is here now: no further pull
    assert len(pulls) == 2


def test_an_inbox_reference_on_a_machine_wake_pulls_too(hp: Pair, pulls):
    """Codex review, phases 2-3: the reference arrives as an unread inbox row on a wake prompt."""
    from agent_cli import peers

    prompt(hp)
    hp.commit(hp.b, "probe.md", "new evidence\n", push=True)
    sha = git(hp.b, "rev-parse", "HEAD")
    peers.send(hp.harness, to=SID, text=f"features/001/evidence/probe.md notes-commit: {sha}", frm="other")
    hook.notes_sync_hook(
        hp.harness, {"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": "wake"}
    )
    assert len(pulls) == 2 and (hp.a / "probe.md").exists()


def test_the_inbox_hook_pulls_named_evidence_before_notes_sync_runs(hp: Pair, pulls):
    """Codex review, phases 2-3, round 2: both clients run the inbox hook first, which drains the
    row before notes-sync could see it. The consumer pulls itself."""
    from agent_cli import peers
    from agent_cli.hooks.session import dispatch_inbox
    from agent_cli.ops.peer_wake import WAKE_TEXT

    prompt(hp)
    hp.commit(hp.b, "probe.md", "new evidence\n", push=True)
    sha = git(hp.b, "rev-parse", "HEAD")
    peers.send(hp.harness, to=SID, text=f"see probe.md notes-commit: {sha}", frm="other")
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": WAKE_TEXT}
    out = dispatch_inbox(hp.harness, payload)
    assert sha in out and (hp.a / "probe.md").exists()
    hook.notes_sync_hook(hp.harness, payload)  # runs second, finds nothing left to do
    assert (hp.a / "probe.md").exists()


def test_a_missing_commit_from_a_consumed_message_is_pulled_for_later(hp: Pair, pulls):
    """Codex review, round 3: the inbox row is gone after the first warning, so the next prompt
    forgot the commit. It is kept on the session until present."""
    from agent_cli import peers
    from agent_cli.hooks.session import dispatch_inbox

    prompt(hp)
    hp.commit(hp.b, "probe.md", "evidence\n", push=False)  # not published yet
    sha = git(hp.b, "rev-parse", "HEAD")
    peers.send(hp.harness, to=SID, text=f"notes-commit: {sha}", frm="other")
    out = dispatch_inbox(
        hp.harness, {"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": "x"}
    )
    assert "Do not use that evidence" in out and not (hp.a / "probe.md").exists()
    git(hp.b, "push", "-q", "origin", "HEAD:main")  # published now
    hook.notes_sync_hook(
        hp.harness, {"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": "y"}
    )
    assert (hp.a / "probe.md").exists()
    assert not (sessions.get(hp.harness, SID) or {}).get(hook.EVIDENCE_KEY)


def test_a_failing_check_warns_instead_of_staying_silent(hp: Pair, monkeypatch):
    import subprocess as sp

    def timeout(*_a: object, **_k: object) -> None:
        raise sp.TimeoutExpired("git", 5)

    monkeypatch.setattr(hook.subprocess, "run", timeout)
    assert hook.missing_commits(hp.harness, "notes-commit: " + "a" * 40)


def test_a_fetched_but_unmerged_commit_is_still_missing(hp: Pair, pulls):
    prompt(hp)
    hp.commit(hp.b, "probe.md", "new evidence\n", push=True)
    sha = git(hp.b, "rev-parse", "HEAD")
    git(hp.a, "fetch", "origin")
    assert hook.missing_commits(hp.harness, f"notes-commit: {sha}")
    named = {"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": f"notes-commit: {sha}"}
    hook.notes_sync_hook(hp.harness, named)
    assert (hp.a / "probe.md").exists()


@pytest.mark.parametrize("why", ["unpublished", "dirty"])
def test_a_commit_still_missing_after_the_pull_is_reported(hp: Pair, pulls, why: str):
    """Receive-before-publish and a dirty recipient both left the file absent, silently."""
    prompt(hp)
    hp.commit(hp.b, "probe.md", "evidence\n", push=why != "unpublished")
    sha = git(hp.b, "rev-parse", "HEAD")
    if why == "dirty":
        (hp.a / "local.md").write_text("local edit\n", encoding="utf-8")
    named = {"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": f"notes-commit: {sha}"}
    out = context(hook.notes_sync_hook(hp.harness, named))
    if (hp.a / "probe.md").exists():  # a pull that could apply despite the edit is also fine
        return
    assert f"notes commit {sha[:12]}" in out and "Do not use that evidence" in out
    assert hook.missing_commits(hp.harness, named["prompt"])  # the next prompt tries again


def test_notes_publish_names_the_pushed_commit_or_refuses_a_ready_reference(hp: Pair):
    """Evidence is published before another machine is told where it is."""
    from agent_cli import ops

    (hp.a / "probe.md").write_text("windows result\n", encoding="utf-8")
    op = ops.notes_publish(hp.harness)
    assert op.data["published"] and op.data["commit"] == git(hp.a, "rev-parse", "HEAD")
    assert git(hp.remote, "rev-parse", "main") == op.data["commit"]
    assert f"notes-commit: {op.data['commit']}" in op.text
    git(hp.a, "remote", "set-url", "origin", str(hp.remote.parent / "gone.git"))
    (hp.a / "probe.md").write_text("second result\n", encoding="utf-8")
    failed = ops.notes_publish(hp.harness)
    assert not failed.data["published"] and failed.data["commit"] is None
    assert "do not send a ready reference" in failed.text


def test_two_prompt_hooks_at_once_pull_and_report_without_noise(hp: Pair, pulls):
    from concurrent.futures import ThreadPoolExecutor

    hp.commit(hp.b, "from-b.md", "b\n", push=True)
    with ThreadPoolExecutor(2) as pool:
        outs = list(pool.map(lambda _: prompt(hp), range(2)))
    assert (hp.a / "from-b.md").exists()
    assert len(pulls) >= 1
    assert not any("notes sync" in context(o) for o in outs)  # a lock wait is not a failure
    assert not (sessions.get(hp.harness, SID) or {}).get(hook.PENDING_KEY)
    mark = (sessions.get(hp.harness, SID) or {})[hook.PULL_KEY]
    assert mark["pid"] == 4242


def test_session_start_pulls_and_renders_status_before_the_block_and_names_it(hp: Pair, monkeypatch):
    from agent_cli import repos, status

    def render(root, source=""):
        (root / "Dashboard.md").write_text("rendered after the pull\n", encoding="utf-8")

    def owner_name(root):
        # Audit 10 plan.md:113 (K1): context no longer embeds a STATUS digest.
        assert (root / "Dashboard.md").read_text() == "rendered after the pull\n"
        return "Rendered owner"

    monkeypatch.setattr(status, "render_quietly", render)
    monkeypatch.setattr(repos, "owner_name", owner_name)
    (hp.harness / "Dashboard.md").write_text("stale\n", encoding="utf-8")
    hp.commit(hp.b, "from-b.md", "b\n", push=True)
    out = session_start(hp.harness, {"session_id": SID, "source": "startup"})
    block = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "owner: Rendered owner" in block
    assert "notes repo: pulled 1 commit." in block
    assert (hp.a / "from-b.md").exists()
    mark = (sessions.get(hp.harness, SID) or {})[hook.PULL_KEY]
    assert mark["ok"] is True


def test_session_start_does_not_render_when_nothing_arrived(hp: Pair, monkeypatch):
    from agent_cli import status

    calls: list[Path] = []
    monkeypatch.setattr(status, "render_quietly", lambda root, source="": calls.append(root))
    session_start(hp.harness, {"session_id": SID, "source": "startup"})
    assert calls == []


def test_stop_renders_after_its_rebase_brought_commits_in(hp: Pair, monkeypatch):
    """Stop rebased onto incoming notes during publication and left STATUS and
    status.json a generation behind until some other trigger rendered."""
    from agent_cli import status

    calls: list[str] = []
    monkeypatch.setattr(status, "render_quietly", lambda root, source="": calls.append(source))
    hp.commit(hp.b, "from-b.md", "b\n", push=True)
    (hp.a / "local.md").write_text("mine\n", encoding="utf-8")
    hook.notes_sync_hook(hp.harness, {"session_id": SID, "hook_event_name": "Stop"})
    assert calls == ["notes-sync stop"]
    assert (hp.a / "from-b.md").exists()


def test_stop_does_not_render_when_nothing_arrived(hp: Pair, monkeypatch):
    from agent_cli import status

    calls: list[str] = []
    monkeypatch.setattr(status, "render_quietly", lambda root, source="": calls.append(source))
    (hp.a / "local.md").write_text("mine\n", encoding="utf-8")
    hook.notes_sync_hook(hp.harness, {"session_id": SID, "hook_event_name": "Stop"})
    assert calls == []


def test_a_hook_exception_is_kept_for_doctor_and_logged(hp: Pair, monkeypatch):
    """The hook swallowed every exception, so not even `hook.error` fired."""
    from agent_cli.doctor import checks_notes
    from agent_cli.hooks import decision_log

    errors: list[str] = []
    monkeypatch.setattr(decision_log, "error", lambda root, name, payload, exc, s: errors.append(name))

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(hook, "report_lines", boom)
    out = hook.notes_sync_hook(hp.harness, {"session_id": SID, "hook_event_name": "UserPromptSubmit"})
    assert out == "" and errors == ["notes-sync"]
    kept = notes_sync.last_outcomes(hp.harness)["last_error"]
    assert kept["op"] == "hook UserPromptSubmit" and "disk on fire" in kept["detail"]
    [row] = checks_notes.check_notes_sync(hp.harness)
    assert row["level"] == "warn" and "disk on fire" in row["detail"]


def test_off_leaves_no_pull_mark(hp: Pair):
    hp.mode("off")
    prompt(hp)
    assert hook.PULL_KEY not in (sessions.get(hp.harness, SID) or {})


def test_an_undecodable_notes_root_marker_never_fails_reporting(hp: Pair):
    (hp.harness / ".agent" / "notes-root").write_bytes(b"\xff\xfe")
    assert notes_sync.standing_failure(hp.harness) is None
    assert hook.report_lines(hp.harness, SID) == []
    prompt(hp)  # does not raise
    assert stop(hp) == ""


def test_session_start_reports_a_standing_failure_and_the_prompt_does_not_repeat_it(hp: Pair):
    git(hp.a, "remote", "set-url", "origin", str(hp.remote.parent / "gone.git"))
    out = session_start(hp.harness, {"session_id": SID, "source": "startup"})
    block = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "notes repo: offline" in block and "notes sync offline" in block
    assert "notes sync offline" not in context(prompt(hp))


def test_session_start_reports_a_kept_error_before_its_pull_clears_it(hp: Pair):
    sessions.set_field(
        hp.harness, SID, hook.PENDING_KEY, {"id": "e1", "status": "error", "detail": "git status failed"}
    )
    line = hook.startup_line(hp.harness, SID, notes_sync.Result("current", converged=True))
    assert "notes sync error: git status failed" in line
    assert (sessions.get(hp.harness, SID) or {}).get(hook.PENDING_KEY) is None


def test_session_start_with_the_switch_off_adds_no_line(hp: Pair):
    hp.mode("off")
    out = session_start(hp.harness, {"session_id": SID, "source": "startup"})
    assert "notes repo" not in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _hard_link_voice(p: Pair) -> tuple[Path, Path]:
    """`harness/voice.md` as plain Windows links it: a hard link into the notes repo, recorded
    in `.agent/notes-linked.json` as linked by this clone."""
    import os

    from agent_cli import notes

    target = p.a / "harness" / "voice.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old voice\n", encoding="utf-8")
    git(p.a, "add", "harness/voice.md")
    git(p.a, "commit", "-q", "-m", "voice")
    git(p.a, "push", "-q")
    link = p.harness / "harness" / "voice.md"
    link.unlink(missing_ok=True)
    os.link(target, link)
    notes.write_linked_record(p.harness, {"harness/voice.md": str(target)})
    return link, target


def _hard_link(p: Pair, *rels: str) -> None:
    """Each of `rels` committed in clone `a` and hard-linked into the harness, recorded as
    linked by this clone, the way plain Windows setup leaves them."""
    import os

    from agent_cli import notes

    for rel in rels:
        (p.a / rel).parent.mkdir(parents=True, exist_ok=True)
        (p.a / rel).write_text(f"old {rel}\n", encoding="utf-8")
        git(p.a, "add", rel)
    git(p.a, "commit", "-q", "-m", "linked files")
    git(p.a, "push", "-q")
    for rel in rels:
        (p.harness / rel).parent.mkdir(parents=True, exist_ok=True)
        (p.harness / rel).unlink(missing_ok=True)
        os.link(p.a / rel, p.harness / rel)
    notes.write_linked_record(p.harness, {rel: str(p.a / rel) for rel in rels})


def _push_from_b(p: Pair, *rels: str) -> None:
    git(p.b, "pull", "-q")
    for rel in rels:
        (p.b / rel).write_text(f"new {rel}\n", encoding="utf-8")
    git(p.b, "commit", "-q", "-am", "from the other machine")
    git(p.b, "push", "-q")


def _parked(p: Pair) -> list[str]:
    from agent_cli import links

    folder = p.harness / links.DIVERGED_DIR
    return sorted(f.read_text(encoding="utf-8") for f in folder.iterdir()) if folder.is_dir() else []


def _linked(p: Pair, rel: str) -> bool:
    from agent_cli import links

    return links.points_at(p.harness / rel, p.a / rel)


def _relink(p: Pair) -> dict:
    return (sessions.get(p.harness, SID) or {}).get(hook.RELINK_KEY) or {}


TWO = ("harness/voice.md", ".mcp.json")


def test_a_prompt_pull_reports_each_relinked_file_and_its_parked_copy(hp: Pair):
    _hard_link(hp, *TWO)
    _push_from_b(hp, *TWO)
    out = context(prompt(hp))
    for rel in TWO:
        assert _linked(hp, rel)
        assert (hp.harness / rel).read_text(encoding="utf-8") == f"new {rel}\n"
        assert f"notes sync relinked: {rel} relinked to" in out
    assert _parked(hp) == sorted(f"old {rel}\n" for rel in TWO)
    for entry in _relink(hp).values():
        assert Path(entry["detail"].rsplit(" is at ", 1)[1]).is_file()


def test_session_start_reports_a_relink(hp: Pair):
    """Its pull rewrites the notes copy, which severs the hard link; the link is remade to it."""
    _hard_link(hp, "harness/voice.md")
    _push_from_b(hp, "harness/voice.md")
    block = json.loads(session_start(hp.harness, {"session_id": SID, "source": "startup"}))
    assert (
        "notes sync relinked: harness/voice.md relinked to"
        in block["hookSpecificOutput"]["additionalContext"]
    )
    assert _linked(hp, "harness/voice.md")
    assert (hp.harness / "harness/voice.md").read_text(encoding="utf-8") == "new harness/voice.md\n"
    assert _parked(hp) == ["old harness/voice.md\n"]


def test_a_stop_rebase_relink_is_kept_until_the_next_prompt_reports_it(hp: Pair):
    _hard_link(hp, "harness/voice.md")
    _push_from_b(hp, "harness/voice.md")
    (hp.a / "local.md").write_text("x\n", encoding="utf-8")  # a commit to publish, so Stop rebases
    assert stop(hp) == ""
    assert _linked(hp, "harness/voice.md")
    assert _relink(hp)["harness/voice.md"]["status"] == "relinked"
    stop(hp)  # the link is healthy now; the outcome is still unreported
    assert "notes sync relinked: harness/voice.md" in context(prompt(hp))
    assert _parked(hp) == ["old harness/voice.md\n"]
    stop(hp)
    assert _relink(hp) == {}


def test_a_refusal_is_reported_per_file_and_outlives_git_convergence(hp: Pair, monkeypatch):
    from agent_cli import links

    real, refuse = links.recover, [True]

    def recover(root, path, target, **kw):
        if refuse[0] and path.name == "voice.md":
            raise PermissionError("the file is in use")
        return real(root, path, target, **kw)

    monkeypatch.setattr(links, "recover", recover)
    _hard_link(hp, *TWO)
    _push_from_b(hp, *TWO)
    out = context(prompt(hp))
    assert "notes sync relinked: .mcp.json" in out and _linked(hp, ".mcp.json")
    assert "notes sync relink refused: harness/voice.md" in out and "the file is in use" in out
    assert (hp.harness / "harness/voice.md").read_text(encoding="utf-8") == "old harness/voice.md\n"
    assert (hp.a / "harness/voice.md").read_text(encoding="utf-8") == "new harness/voice.md\n"
    stop(hp)  # HEAD equals upstream: git converged, the link did not
    assert (sessions.get(hp.harness, SID) or {}).get(hook.PENDING_KEY) is None
    assert _relink(hp)["harness/voice.md"]["status"] == "relink refused"
    assert "relink refused" not in context(prompt(hp))  # reported once per session
    refuse[0] = False
    stop(hp)
    assert _linked(hp, "harness/voice.md")
    assert "notes sync relinked: harness/voice.md" in context(prompt(hp))
    assert _parked(hp) == sorted(["old .mcp.json\n", "old harness/voice.md\n"])


def test_a_failed_check_is_reported_not_swallowed(hp: Pair, monkeypatch):
    def broken(root):
        raise OSError("notes repo unreadable")

    monkeypatch.setattr(hook, "relink_diverged", broken)
    out = context(prompt(hp))
    assert "notes sync relink refused: could not check the notes links" in out


def test_an_unchanged_link_reports_nothing(hp: Pair):
    _hard_link(hp, "harness/voice.md")
    assert context(prompt(hp)) == ""
    stop(hp)
    assert _relink(hp) == {}
    assert _linked(hp, "harness/voice.md") and _parked(hp) == []


@pytest.mark.parametrize("step", ["make", "replace"])
def test_recover_refused_at_any_step_keeps_both_copies_and_parks_nothing(hp: Pair, monkeypatch, step: str):
    from agent_cli import links
    from agent_cli.core import AgentError

    _hard_link(hp, "harness/voice.md")
    _push_from_b(hp, "harness/voice.md")
    git(hp.a, "pull", "-q")  # real git replacement severs the hard link
    link, target = hp.harness / "harness/voice.md", hp.a / "harness/voice.md"

    def refuse(*a, **kw):
        raise AgentError("refused") if step == "make" else PermissionError("refused")

    if step == "make":
        monkeypatch.setattr(links, "make", refuse)
    else:
        monkeypatch.setattr(links.os, "replace", refuse)
    with pytest.raises((AgentError, OSError)):
        links.recover(hp.harness, link, target, expected=link.read_bytes())
    assert link.read_text(encoding="utf-8") == "old harness/voice.md\n"
    assert target.read_text(encoding="utf-8") == "new harness/voice.md\n"
    assert _parked(hp) == []
    assert [f.name for f in link.parent.iterdir() if "voice" in f.name] == ["voice.md"]


def test_a_native_sharing_violation_is_refused_visibly_with_both_copies_kept(hp: Pair):
    if sys.platform != "win32":
        pytest.skip("a native Windows sharing violation")
    import _winapi

    _hard_link(hp, "harness/voice.md")
    _push_from_b(hp, "harness/voice.md")
    git(hp.a, "pull", "-q")
    link = hp.harness / "harness/voice.md"
    # open without FILE_SHARE_DELETE, as an editor or scanner holds a file: no rename over it
    handle = _winapi.CreateFile(str(link), _winapi.GENERIC_READ, 1 | 2, 0, _winapi.OPEN_EXISTING, 0, 0)
    try:
        out = context(prompt(hp))
    finally:
        _winapi.CloseHandle(handle)
    assert "notes sync relink refused: harness/voice.md" in out and str(link) in out
    assert link.read_text(encoding="utf-8") == "old harness/voice.md\n"
    assert (hp.a / "harness/voice.md").read_text(encoding="utf-8") == "new harness/voice.md\n"
    assert not _linked(hp, "harness/voice.md") and _parked(hp) == []


def _sever_with_an_edit(p: Pair, rel: str, text: str) -> Path:
    """An editor's save in the harness: a temp file renamed over the hard link."""
    import os

    link = p.harness / rel
    edited = link.with_name(f"{link.name}.tmp")
    edited.write_text(text, encoding="utf-8")
    os.replace(edited, link)
    return link


def test_an_edited_copy_is_refused_visibly_and_both_copies_are_kept(hp: Pair):
    _hard_link(hp, "harness/voice.md")
    _push_from_b(hp, "harness/voice.md")
    link = _sever_with_an_edit(hp, "harness/voice.md", "my edit\n")
    out = context(prompt(hp))
    assert (
        "notes sync relink refused: harness/voice.md: " in out and "holds an edit" in out and str(link) in out
    )
    assert link.read_text(encoding="utf-8") == "my edit\n"
    assert (hp.a / "harness/voice.md").read_text(encoding="utf-8") == "new harness/voice.md\n"
    assert not _linked(hp, "harness/voice.md") and _parked(hp) == []
    stop(hp)
    assert _relink(hp)["harness/voice.md"]["status"] == "relink refused"
    assert link.read_text(encoding="utf-8") == "my edit\n" and _parked(hp) == []


def test_an_edit_written_while_a_stale_copy_is_relinked_is_never_lost(hp: Pair, monkeypatch):
    import os

    from agent_cli import links

    _hard_link(hp, "harness/voice.md")
    _push_from_b(hp, "harness/voice.md")
    link, real, fired = hp.harness / "harness/voice.md", os.replace, []

    def edit_first(src, dst):
        if not fired and os.path.normcase(str(link)) in (
            os.path.normcase(str(src)),
            os.path.normcase(str(dst)),
        ):
            fired.append(1)
            with open(link, "a", encoding="utf-8") as f:  # the owner saves in place, mid-recovery
                f.write("late edit\n")
        return real(src, dst)

    monkeypatch.setattr(links.os, "replace", edit_first)
    out = context(prompt(hp))
    assert fired
    copies = [link.read_text(encoding="utf-8"), *_parked(hp)]
    assert any("late edit" in c for c in copies), copies
    assert (hp.a / "harness/voice.md").read_text(encoding="utf-8") == "new harness/voice.md\n"
    assert "notes sync relink refused: harness/voice.md" in out
    assert "late edit" in link.read_text(encoding="utf-8") and _parked(hp) == []


def test_a_notes_copy_replaced_during_recovery_is_not_reported_relinked(hp: Pair, monkeypatch):
    from agent_cli import links

    def refuse(*a, **kw):
        raise OSError("A required privilege is not held by the client")

    monkeypatch.setattr(links.os, "symlink", refuse)  # plain Windows: the link is a hard link
    monkeypatch.setattr(links, "IS_WINDOWS", True)
    _hard_link(hp, "harness/voice.md")
    _push_from_b(hp, "harness/voice.md")
    real, fired = links.make, []

    def make_then_pull(path, target, *, is_dir):
        kind = real(path, target, is_dir=is_dir)
        if not fired:
            fired.append(1)
            git(hp.b, "pull", "-q")
            (hp.b / "harness/voice.md").write_text("newer\n", encoding="utf-8")
            git(hp.b, "commit", "-q", "-am", "newer voice")
            git(hp.b, "push", "-q")
            git(hp.a, "pull", "-q")  # real git replaces the notes copy after the link was made
        return kind

    monkeypatch.setattr(links, "make", make_then_pull)
    out = context(prompt(hp))
    assert fired
    entry = _relink(hp)["harness/voice.md"]
    assert entry["status"] == "relink refused" and "was replaced" in entry["detail"], entry
    assert "notes sync relink refused: harness/voice.md" in out
    assert not _linked(hp, "harness/voice.md")
    assert _parked(hp) == ["old harness/voice.md\n"]
    stop(hp)  # the next check sees a stale copy again and relinks it to the current notes copy
    assert _linked(hp, "harness/voice.md")
    assert (hp.harness / "harness/voice.md").read_text(encoding="utf-8") == "newer\n"
    assert "notes sync relinked: harness/voice.md" in context(prompt(hp))


@pytest.mark.parametrize("side", ["harness", "notes"])
def test_a_refusal_stays_until_a_missing_file_is_reported(hp: Pair, monkeypatch, side: str):
    from agent_cli import links

    def recover(root, path, target, **kw):
        raise PermissionError("the file is in use")

    monkeypatch.setattr(links, "recover", recover)
    _hard_link(hp, "harness/voice.md")
    _push_from_b(hp, "harness/voice.md")
    assert "notes sync relink refused: harness/voice.md" in context(prompt(hp))
    gone = (hp.harness if side == "harness" else hp.a) / "harness/voice.md"
    gone.unlink()
    stop(hp)
    entry = _relink(hp).get("harness/voice.md")
    assert entry and entry["status"] == "link missing" and str(gone) in entry["detail"], entry
    assert (
        f"notes sync link missing: harness/voice.md: {'this clone' if side == 'harness' else 'the notes'}"
        in (context(prompt(hp)))
    )
    stop(hp)
    assert _relink(hp)["harness/voice.md"]["status"] == "link missing"


def test_a_file_this_clone_never_linked_is_left_alone(hp: Pair):
    from agent_cli import notes

    link, target = _hard_link_voice(hp)
    notes.write_linked_record(hp.harness, {})
    link.unlink()
    link.write_text("the owner's own copy\n", encoding="utf-8")
    hook.relink_diverged(hp.harness)
    assert link.read_text(encoding="utf-8") == "the owner's own copy\n"


def test_the_native_windows_pull_gate(hp: Pair, monkeypatch):
    """Plan 041 N3's native gate: real git replacement of hard-linked files in an autocrlf
    checkout, through startup, prompt and Stop. Unchanged and off controls stay quiet, a stale
    copy relinks with its bytes parked, an edited copy and a copy held open without
    FILE_SHARE_DELETE are refused with both kept. `-s` prints the evidence."""
    if sys.platform != "win32":
        pytest.skip("the native Windows pull gate")
    import _winapi
    import hashlib
    import os

    from agent_cli import links, notes

    for clone in (hp.a, hp.b):
        git(clone, "config", "core.autocrlf", "true")
    first = {
        "agent.yaml": (hp.harness / "agent.yaml").read_text(encoding="utf-8"),
        "harness/voice.md": "# Voice\n",
        ".mcp.json": '{"mcpServers": {}}\n',
    }
    for rel, text in first.items():
        (hp.a / rel).parent.mkdir(parents=True, exist_ok=True)
        (hp.a / rel).write_text(text, encoding="utf-8")
        git(hp.a, "add", rel)
    git(hp.a, "commit", "-q", "-m", "private files")
    git(hp.a, "push", "-q")
    for rel in first:
        (hp.harness / rel).parent.mkdir(parents=True, exist_ok=True)
        (hp.harness / rel).unlink(missing_ok=True)
        os.link(hp.a / rel, hp.harness / rel)
    notes.write_linked_record(hp.harness, {rel: str(hp.a / rel) for rel in first})
    evidence: dict = {}

    def ident(path: Path) -> dict | None:
        if not path.exists():
            return None
        s, data = os.stat(path), path.read_bytes()
        last = data.decode("utf-8").splitlines()[-1]
        return {
            "dev": s.st_dev,
            "ino": s.st_ino,
            "nlink": s.st_nlink,
            "sha256": hashlib.sha256(data).hexdigest()[:16],
            "last_line": last,
            "crlf": b"\r\n" in data,
        }

    def snap(step: str, **outputs: str) -> dict:
        folder = hp.harness / links.DIVERGED_DIR
        evidence[step] = {
            "outputs": outputs,
            "notes_head": git(hp.a, "rev-parse", "--short", "HEAD"),
            "files": {
                rel: {
                    "harness": ident(hp.harness / rel),
                    "notes": ident(hp.a / rel),
                    "linked": links.points_at(hp.harness / rel, hp.a / rel),
                }
                for rel in first
            },
            "parked": {f.name: ident(f) for f in sorted(folder.iterdir())} if folder.is_dir() else {},
            "kept": _relink(hp),
        }
        return evidence[step]

    def start() -> str:
        out = json.loads(session_start(hp.harness, {"session_id": SID, "source": "startup"}))
        return out["hookSpecificOutput"]["additionalContext"]

    def push_from_b(changes: dict[str, str]) -> None:
        git(hp.b, "pull", "-q")
        for rel, line in changes.items():
            with open(hp.b / rel, "a", encoding="utf-8") as f:
                f.write(line)
        git(hp.b, "commit", "-q", "-am", "from the other machine")
        git(hp.b, "push", "-q")

    def quiet(outs: dict[str, str]) -> bool:
        return not any("notes sync" in o or "notes repo: pulled" in o for o in outs.values())

    s = snap("unchanged", startup=start(), prompt=context(prompt(hp)), stop=stop(hp))
    assert quiet(s["outputs"]) and all(f["linked"] for f in s["files"].values())
    assert s["parked"] == {} and s["kept"] == {}

    head = s["notes_head"]
    push_from_b(
        {"agent.yaml": "# from the other machine\n", "harness/voice.md": "theirs\n", ".mcp.json": "\n"}
    )
    hp.mode("off")
    s = snap("off", startup=start(), prompt=context(prompt(hp)), stop=stop(hp))
    assert quiet(s["outputs"]) and s["notes_head"] == head and all(f["linked"] for f in s["files"].values())

    hp.mode("on")
    _sever_with_an_edit(hp, "harness/voice.md", "# Voice\nmy edit\n")
    s = snap("startup pull: stale relinks, edit refused", startup=start())
    # Windows temp paths make the refusal line long enough that session start's budget moves the
    # relink lines to the full-list file.
    from agent_cli.hooks.session_budget import full_list_path

    out = s["outputs"]["startup"] + full_list_path(hp.harness, SID).read_text(encoding="utf-8")
    files = s["files"]
    assert "notes sync relinked: agent.yaml" in out and "notes sync relinked: .mcp.json" in out
    assert "notes sync relink refused: harness/voice.md: " in out and "holds an edit" in out
    assert (
        files["agent.yaml"]["linked"]
        and files["agent.yaml"]["harness"]["last_line"] == "# from the other machine"
    )
    assert files[".mcp.json"]["linked"]
    voice = files["harness/voice.md"]
    assert (
        not voice["linked"]
        and voice["harness"]["last_line"] == "my edit"
        and voice["notes"]["last_line"] == "theirs"
    )
    assert sorted(n.split(".")[0] for n in s["parked"]) == ["", "agent"]  # .mcp.json.* and agent.yaml.*

    push_from_b({".mcp.json": "\n"})
    git(hp.a, "pull", "-q")  # real git replaces the notes copy outside any hook
    held = hp.harness / ".mcp.json"
    handle = _winapi.CreateFile(str(held), _winapi.GENERIC_READ, 1 | 2, 0, _winapi.OPEN_EXISTING, 0, 0)
    try:
        s = snap("Stop with the harness copy held open", stop=stop(hp))
    finally:
        _winapi.CloseHandle(handle)
    mcp = s["files"][".mcp.json"]
    assert not mcp["linked"] and mcp["harness"]["sha256"] != mcp["notes"]["sha256"]
    assert s["kept"][".mcp.json"]["status"] == "relink refused" and len(s["parked"]) == 2

    monkeypatch.setattr(sessions, "resolve_pid", lambda: 4343)  # a new client process pulls
    s = snap("prompt after the handle closed", prompt=context(prompt(hp)))
    out = s["outputs"]["prompt"]
    assert "notes sync relink refused: .mcp.json" in out and "notes sync relinked: .mcp.json" in out
    assert s["files"][".mcp.json"]["linked"] and len(s["parked"]) == 3
    assert not s["files"]["harness/voice.md"]["linked"]
    print(json.dumps(evidence, indent=1))
