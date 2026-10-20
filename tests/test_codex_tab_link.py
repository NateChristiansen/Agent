"""A Codex thread finds its ribbon tab by process, never by title."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli import codex_tab_link, obsidian_tabs, sessions
from agent_cli.codex_tab_link import Process
from agent_cli.hooks.activity import codex_activity

# Synthetic UUIDv7 thread; its TUI starts two seconds before thread creation.
SID = "019b76da-a800-7000-8000-000000000001"
CREATED = int("019b76daa800", 16) / 1000
MARKER = "codex:11111111-2222-4333-8444-555555555555"
OTHER = "codex:0f5c1e2a-1111-4222-8333-944455556666"
CODEX_BIN = "/opt/homebrew/lib/node_modules/@openai/codex/vendor/aarch64-apple-darwin/bin/codex"


def _ribbon(shell_pid: int, marker: str, started: float) -> list[Process]:
    """shell -> node shim -> native TUI, as a ribbon tab launches Codex."""
    command = f"/bin/zsh -i --login -c AGENT_RIBBON_LAUNCH={marker} codex; exec /bin/zsh --login"
    return [
        Process(shell_pid, 1, started, tuple(command.split())),
        Process(shell_pid + 7, shell_pid, started, ("node", "/opt/homebrew/bin/codex")),
        Process(shell_pid + 8, shell_pid + 7, started, (CODEX_BIN,)),
    ]


def _plain(shell_pid: int, started: float) -> list[Process]:
    return [
        Process(shell_pid, 1, started, ("/bin/zsh", "-l")),
        Process(shell_pid + 1, shell_pid, started, (CODEX_BIN,)),
    ]


@pytest.fixture
def rollout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a rollout for SID whose first line names `originator`."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    def write(originator: str, sid: str = SID) -> None:
        created = codex_tab_link.thread_created(sid) or CREATED
        day = (
            tmp_path
            / "codex"
            / "sessions"
            / codex_tab_link.time.strftime("%Y/%m/%d", codex_tab_link.time.localtime(created))
        )
        day.mkdir(parents=True, exist_ok=True)
        meta = {"type": "session_meta", "payload": {"id": sid, "originator": originator}}
        (day / f"rollout-2026-01-01T00-00-00-{sid}.jsonl").write_text(
            json.dumps(meta) + "\n{}\n", encoding="utf-8"
        )
        refs = tmp_path / "codex" / "tui-thread-reference-capabilities"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / sid).touch()

    return write


def _uuid7(at: float, tail: str) -> str:
    """A UUIDv7-shaped thread id created at `at`."""
    ms = f"{int(at * 1000):012x}"
    return f"{ms[:8]}-{ms[8:]}-7{tail[:3]}-8{tail[3:6]}-{tail[6:18]}"


def test_uuid7_carries_the_thread_creation_time():
    assert codex_tab_link.thread_created(SID) == pytest.approx(CREATED)
    assert codex_tab_link.thread_created("a9a50000-0000-4000-8000-000000000005") is None  # UUIDv4


def test_the_tui_that_started_just_before_the_thread_gives_its_marker(rollout):
    rollout("codex-tui")
    procs = _ribbon(20990, MARKER, CREATED - 2) + _plain(10990, CREATED - 600)
    assert codex_tab_link.marker_for(SID, procs) == MARKER


def test_a_thread_not_created_by_a_tui_gets_no_marker(rollout):
    for originator in ("codex_exec", "agent-harness", ""):
        rollout(originator)
        assert codex_tab_link.marker_for(SID, _ribbon(20990, MARKER, CREATED - 2)) == ""


def test_a_missing_rollout_gets_no_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
    assert codex_tab_link.marker_for(SID, _ribbon(20990, MARKER, CREATED - 2)) == ""


def test_two_tuis_in_the_window_refuse(rollout):
    rollout("codex-tui")
    procs = _ribbon(20990, MARKER, CREATED - 2) + _ribbon(30990, OTHER, CREATED - 5)
    assert codex_tab_link.marker_for(SID, procs) == ""


def test_a_plain_terminal_thread_cannot_take_a_ribbon_tab(rollout):
    """Codex's blocker: thread B, from a plain terminal, must not claim thread A's ribbon tab.
    A's TUI started a minute earlier; B's own TUI has no marker above it."""
    rollout("codex-tui")
    procs = _ribbon(20990, MARKER, CREATED - 60) + _plain(40990, CREATED - 1)
    assert codex_tab_link.marker_for(SID, procs) == ""


def test_a_plain_terminal_tui_in_the_window_blocks_a_ribbon_match(rollout):
    rollout("codex-tui")
    procs = _ribbon(20990, MARKER, CREATED - 2) + _plain(40990, CREATED - 3)
    assert codex_tab_link.marker_for(SID, procs) == ""


def test_a_tui_outside_the_window_gets_no_marker(rollout):
    """/clear or resume in a ribbon tab: the thread is created long after the TUI started."""
    rollout("codex-tui")
    rival = _plain(10990, CREATED - 900)  # a second TUI, so the sole-TUI rule does not apply
    assert codex_tab_link.marker_for(SID, _ribbon(20990, MARKER, CREATED - 600) + rival) == ""
    assert codex_tab_link.marker_for(SID, _ribbon(20990, MARKER, CREATED + 10) + rival) == ""


def test_a_clear_thread_cannot_take_a_ribbon_tab_that_started_with_it(rollout, tmp_path: Path):
    """Codex's second blocker: TUI A started 10 minutes ago and /clear made thread X now, just
    as ribbon TUI B started. B's window holds X and B's own thread, so X claims nothing."""
    rollout("codex-tui")
    b_own = _uuid7(CREATED - 0.5, "111222333444555666")
    (tmp_path / "codex" / "tui-thread-reference-capabilities" / b_own).touch()
    procs = _plain(10990, CREATED - 600) + _ribbon(20990, MARKER, CREATED - 2)
    assert codex_tab_link.marker_for(SID, procs) == ""


def test_the_tuis_own_thread_is_its_only_thread(rollout):
    """The positive case of the same rule: X alone in B's window is B's thread."""
    rollout("codex-tui")
    procs = _plain(10990, CREATED - 600) + _ribbon(20990, MARKER, CREATED - 2)
    assert codex_tab_link.marker_for(SID, procs) == MARKER


def test_no_claim_until_the_tui_window_settles(rollout):
    rollout("codex-tui")
    procs = _ribbon(20990, MARKER, CREATED - 2) + _plain(10990, CREATED - 600)
    assert codex_tab_link.marker_for(SID, procs, now=CREATED + 1) == ""
    assert codex_tab_link.marker_for(SID, procs, now=CREATED + 4) == MARKER


def test_an_unreadable_or_empty_thread_list_refuses(rollout, monkeypatch: pytest.MonkeyPatch):
    rollout("codex-tui")
    procs = _ribbon(20990, MARKER, CREATED - 2) + _plain(10990, CREATED - 600)
    assert codex_tab_link.marker_for(SID, procs, threads=[]) == ""
    monkeypatch.setattr(codex_tab_link, "tui_threads", lambda: None)
    assert codex_tab_link.marker_for(SID, procs) == ""


def test_the_daemon_is_not_a_rival(rollout):
    rollout("codex-tui")
    daemon = Process(
        58746, 69627, CREATED - 1, (CODEX_BIN, "app-server", "--listen", "unix://", "--managed-daemon")
    )
    assert codex_tab_link.marker_for(SID, [*_ribbon(20990, MARKER, CREATED - 2), daemon]) == MARKER


def test_a_powershell_ribbon_marker_is_found(rollout):
    rollout("codex-tui")
    shell = Process(
        500,
        4,
        CREATED - 3,
        ("powershell.exe", "-NoLogo", "-NoExit", "-Command", f"$env:AGENT_RIBBON_LAUNCH='{MARKER}'; codex"),
    )
    node = Process(501, 500, CREATED - 2, ("C:\\Program Files\\nodejs\\node.exe", "codex.js"))
    tui = Process(502, 501, CREATED - 2, ("C:\\Users\\o\\AppData\\Roaming\\npm\\codex.exe",))
    assert codex_tab_link.marker_for(SID, [shell, node, tui]) == MARKER


def test_posix_ps_lines_parse(monkeypatch: pytest.MonkeyPatch):
    out = (
        "20990     1 Mon Sep 28 14:39:27 2026 /bin/zsh -i --login -c AGENT_RIBBON_LAUNCH=codex:x codex\n"
        "  garbage line\n"
    )
    monkeypatch.setattr(
        codex_tab_link.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=out)
    )
    procs = codex_tab_link._posix_processes()
    assert [(p.pid, p.ppid, p.argv[0]) for p in procs] == [(20990, 1, "/bin/zsh")]
    assert codex_tab_link.time.localtime(procs[0].started)[3:6] == (14, 39, 27)


def test_codex_hook_claims_with_the_recovered_marker(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    sessions.set_record(tmp_root, SID, name="mgr-unmapped-019b76-codex-00000001")
    sessions.set_field(tmp_root, SID, "provider", "codex")
    monkeypatch.setattr(codex_tab_link, "marker_for", lambda sid: MARKER)
    claimed: list[dict] = []
    monkeypatch.setattr(obsidian_tabs, "claim_active", lambda name, label, **kw: claimed.append(kw) or True)
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "UserPromptSubmit"})
    assert claimed and claimed[0]["launch_marker"] == MARKER
    assert claimed[0]["provisional_title"] == obsidian_tabs.CODEX_STARTING_TITLE


def test_codex_hook_without_a_link_offers_no_provisional_title(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    sessions.set_record(tmp_root, SID, name="mgr-unmapped-019b76-codex-00000001")
    sessions.set_field(tmp_root, SID, "provider", "codex")
    monkeypatch.setattr(codex_tab_link, "marker_for", lambda sid: "")
    claimed: list[dict] = []
    monkeypatch.setattr(obsidian_tabs, "claim_active", lambda name, label, **kw: claimed.append(kw) or False)
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "UserPromptSubmit"})
    # No marker, no claim: the active untitled leaf may be another Codex session's tab, since
    # every Codex hook runs in the same daemon (Codex review of #67).
    assert claimed == []


def test_a_sole_tui_owns_a_thread_created_late(rollout):
    """#67: Codex 0.159.2 creates the thread at the first prompt, 46 s after the TUI started on
    the fresh Windows clone. With one TUI on the machine, the thread is still its."""
    rollout("codex-tui")
    procs = _ribbon(20990, MARKER, CREATED - 600)
    assert codex_tab_link.marker_for(SID, procs, now=CREATED + 5) == MARKER
    # Codex review: an old thread whose TUI exited must not take the older TUI left behind
    assert codex_tab_link.marker_for(SID, procs, now=CREATED + 600) == ""


# ---- the claim JS, executed in Node against fake Terminal leaves


def _run(leaves: list[dict], marker: str) -> tuple[str, list[str]]:
    code = obsidian_tabs.eval_code(
        "mgr-x-codex-00000001",
        "✅ new · cx 0000",
        claim_active=True,
        provisional_title=obsidian_tabs.CODEX_STARTING_TITLE,
        launch_marker=marker,
    )
    setup = (
        "const leaves = "
        + json.dumps(leaves, ensure_ascii=False)
        + ".map((l) => ({view: {state: {userTitle: l.title, profile: {args: l.args}}}, getDisplayText() { return l.title; }}));"
        + "globalThis.app = {workspace: {getLeavesOfType: () => leaves, activeLeaf: leaves[0]}};"
    )
    script = (
        setup
        + f"console.log(JSON.stringify([eval({json.dumps(code)}), leaves.map(l => l.view.state.userTitle)]));"
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True
    )
    out, titles = json.loads(result.stdout)
    return out, titles


NODE = pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed to execute Obsidian eval JS")
START = obsidian_tabs.CODEX_STARTING_TITLE
POSIX_ARGS = ["-i", "--login", "-c", f"AGENT_RIBBON_LAUNCH={MARKER} codex; exec /bin/zsh --login"]
PS_ARGS = ["-NoLogo", "-NoExit", "-Command", f"$env:AGENT_RIBBON_LAUNCH='{MARKER}'; codex"]


@NODE
@pytest.mark.parametrize("args", [POSIX_ARGS, PS_ARGS], ids=["posix", "powershell"])
def test_the_marker_selects_its_background_tab(args: list[str]):
    other = ["-c", f"AGENT_RIBBON_LAUNCH={OTHER} codex"]
    out, titles = _run(
        [
            {"title": "⚙️ 030 x · cl a9a5", "args": []},
            {"title": START, "args": other},
            {"title": START, "args": args},
        ],
        MARKER,
    )
    assert out.startswith("relabeled 1 hits=1")
    assert titles == ["⚙️ 030 x · cl a9a5", START, "✅ new · cx 0000"]


@NODE
def test_two_leaves_with_one_marker_change_neither():
    """A duplicated or restored tab carries the same profile marker (Codex review)."""
    out, titles = _run([{"title": START, "args": POSIX_ARGS}, {"title": START, "args": POSIX_ARGS}], MARKER)
    assert out.startswith("relabeled 0 hits=2")
    assert titles == [START, START]


def test_ambiguous_tabs_are_the_recorded_reason(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.setenv("AGENT_ROOT", str(tmp_root))
    monkeypatch.setattr("agent_cli.dispatch.opener.obsidian_cli", lambda: "/usr/local/bin/obsidian")
    monkeypatch.setattr(obsidian_tabs.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, stdout="relabeled 0 hits=2 active_terminal=true"
        ),
    )
    assert not obsidian_tabs.claim_active("mgr-x", "✅ x", provisional_title=START, launch_marker=MARKER)
    failure = json.loads(
        (tmp_root / ".agent/tabs/claim-failures.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert failure["reason"].startswith("ambiguous-tabs:2")


def test_an_old_thread_does_not_take_a_newer_sole_tui(rollout):
    """Thread A outlived its TUI; a later ribbon TUI B is the only one left.
    A's delayed hook must not get B's marker."""
    rollout("codex-tui")
    assert codex_tab_link.marker_for(SID, _ribbon(20990, MARKER, CREATED + 60)) == ""


def test_session_start_claims_nothing_without_a_marker(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    """With lazy threads, SessionStart's one-shot untitled claim could take
    another Codex session's active tab, so no marker means no claim; a later event retries."""
    sessions.set_record(tmp_root, SID, name="mgr-unmapped-019b76-codex-00000001")
    sessions.set_field(tmp_root, SID, "provider", "codex")
    monkeypatch.setattr(codex_tab_link, "marker_for", lambda sid: "")
    claimed: list[dict] = []
    monkeypatch.setattr(obsidian_tabs, "claim_active", lambda name, label, **kw: claimed.append(kw) or True)
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionStart"})
    assert claimed == []
