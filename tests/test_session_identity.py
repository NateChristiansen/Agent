"""Which session, which pid, which client: `src/agent_cli/session_identity.py`.

Split from `test_sessions_capture.py`, which hit the 500-line limit. Identity is
its own concern: `resolve_pid` decides what the session record, the by-pid map and a lease all
name, and `resolve_provider` decides which client the rest of the harness thinks it is serving.

These patch `session_identity` rather than `sessions`. `sessions` re-exports the same names, but
a re-export is a second binding: patching it leaves the module that actually calls the helper
untouched, so the test would pass against unpatched code.
"""

from __future__ import annotations

import sys


def test_client_pid_walks_ancestors_for_a_client_that_exports_no_pid(monkeypatch):
    """Claude exports `CLAUDE_PID`; Codex exports none, so `resolve_pid` fell back to
    `os.getppid()` — the short-lived shell that ran the hook. A live Codex manager registered a
    pid that had already exited, so it was on screen and absent from both the Managers view and
    `peer list`.

    The hint is matched as the exact basename of a token, never as a substring of the whole
    command. A hook's own command is `/bin/sh -c bin/agent hook codex-activity`, which contains
    "codex": a substring match stopped the walk at the very shell whose death caused the
    problem, and this test caught that.

    The walk is bounded because it reaches the login shell and the terminal, and those outlive
    the session: unbounded, an ended session would look live forever, the same failure inverted.
    """
    from agent_cli import session_identity as sessions

    monkeypatch.delenv("AGENT_NOW", raising=False)
    tree = {10: (11, "/bin/sh -c bin/agent hook codex-activity"), 11: (12, "-zsh"), 12: (1, "codex")}
    monkeypatch.setattr(sessions, "_parent_pid", lambda pid: tree.get(pid, (None, ""))[0])
    monkeypatch.setattr(sessions, "_command", lambda pid: tree.get(pid, (None, ""))[1])
    assert sessions.client_pid(10) == 12

    # a client launched through an interpreter is still the client: argv[0] is `node`
    vianode = {20: (21, "/bin/sh -c hook"), 21: (1, "node /opt/homebrew/bin/codex")}
    monkeypatch.setattr(sessions, "_parent_pid", lambda pid: vianode.get(pid, (None, ""))[0])
    monkeypatch.setattr(sessions, "_command", lambda pid: vianode.get(pid, (None, ""))[1])
    assert sessions.client_pid(20) == 21

    # a chain with no client resolves to nothing rather than to the terminal
    bare = {30: (31, "/bin/sh"), 31: (1, "login")}
    monkeypatch.setattr(sessions, "_parent_pid", lambda pid: bare.get(pid, (None, ""))[0])
    monkeypatch.setattr(sessions, "_command", lambda pid: bare.get(pid, (None, ""))[1])
    assert sessions.client_pid(30) is None

    # and the walk stops rather than climbing forever
    deep = {n: (n + 1, "sh") for n in range(40, 70)}
    monkeypatch.setattr(sessions, "_parent_pid", lambda pid: deep.get(pid, (None, ""))[0])
    monkeypatch.setattr(sessions, "_command", lambda pid: deep.get(pid, (None, ""))[1])
    assert sessions.client_pid(40, limit=3) is None


def test_client_pid_is_off_under_the_test_clock(monkeypatch):
    """The suite runs under a real client process, so an unguarded walk climbs out of the
    simulated environment and finds it — an earlier leak one level up. Two tests failed that way
    before this guard: one wrote its by-pid entry under the real client's pid."""
    from agent_cli import session_identity as sessions

    monkeypatch.setenv("AGENT_NOW", "2026-09-22T23:00:00-06:00")
    monkeypatch.setattr(sessions, "_command", lambda pid: "codex")
    assert sessions.client_pid(1234) is None


def test_provider_comes_from_env_when_only_one_client_is_present(monkeypatch):
    """The ordinary case, and it must cost nothing: `activity` fires on every PostToolUse, so a
    provider lookup that walked the process tree every time would add six `ps` calls per tool
    call."""
    from agent_cli import session_identity as sessions

    def _never(pid):  # the walk is the expensive path and must not be reached here
        raise AssertionError("resolve_provider walked the ancestry with an unambiguous env")

    monkeypatch.setattr(sessions, "client_process", _never)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-claude")
    assert sessions.resolve_provider() == "claude"

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setenv("CODEX_SESSION_ID", "sid-codex")
    assert sessions.resolve_provider() == "codex"

    # `CLAUDE_PID` alone is enough: Claude Code exports it and Codex exports nothing like it
    monkeypatch.delenv("CODEX_SESSION_ID")
    monkeypatch.setenv("CLAUDE_PID", "4242")
    assert sessions.resolve_provider() == "claude"


def test_both_clients_in_the_environment_resolve_to_the_nearest_one(monkeypatch):
    """A session started from the other client's terminal inherits its variables, so the
    environment cannot answer. The nearest client ancestor is the one that owns this process."""
    from agent_cli import session_identity as sessions

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-claude")
    monkeypatch.setenv("CODEX_SESSION_ID", "sid-codex")

    monkeypatch.setattr(sessions, "client_process", lambda pid, **_: (99, "codex"))
    assert sessions.resolve_provider() == "codex"

    monkeypatch.setattr(sessions, "client_process", lambda pid, **_: (99, "claude"))
    assert sessions.resolve_provider() == "claude"

    # and when the walk finds nothing, the answer is the documented default rather than a raise:
    # a provider is a label on a record, and a hook must not die over one
    monkeypatch.setattr(sessions, "client_process", lambda pid, **_: None)
    assert sessions.resolve_provider() == sessions.DEFAULT_PROVIDER


def test_the_ancestry_walk_reports_the_client_it_found(monkeypatch):
    """One walk answers both halves of identity. The name used to be computed and discarded,
    which is why recognising a Codex session needed a second mechanism."""
    from agent_cli import session_identity as sessions

    monkeypatch.delenv("AGENT_NOW", raising=False)
    tree = {10: (11, "/bin/sh -c bin/agent hook codex-activity"), 11: (12, "-zsh"), 12: (1, "codex")}
    monkeypatch.setattr(sessions, "_parent_pid", lambda pid: tree.get(pid, (None, ""))[0])
    monkeypatch.setattr(sessions, "_command", lambda pid: tree.get(pid, (None, ""))[1])
    assert sessions.client_process(10) == (12, "codex")
    assert sessions.client_pid(10) == 12


def test_session_pid_and_provider_agree_when_both_clients_are_in_the_environment(monkeypatch):
    """A Codex session started from a Claude terminal inherits `CLAUDE_CODE_SESSION_ID` and
    `CLAUDE_PID`. Which session, which pid and which client are one question, and answering
    them from three independent preferences gave three different answers: provider `codex`
    from the nearest ancestor, session id from the outer Claude session, liveness from the
    outer Claude process. Worse than the uniform wrong answer it replaced — found in review.
    """
    from pathlib import Path

    from agent_cli import session_identity as sessions

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "outer-claude")
    monkeypatch.setenv("CLAUDE_PID", "4242")
    monkeypatch.setenv("CODEX_SESSION_ID", "inner-codex")
    monkeypatch.setattr(sessions, "client_process", lambda *a, **k: (777, "codex"))

    assert sessions.resolve_provider() == "codex"
    # the Codex id, not the Claude one that happens to be first in ENV_VARS
    assert sessions.resolve_session_id(Path(".")) == "inner-codex"
    # the Codex process, not the inherited CLAUDE_PID
    assert sessions.resolve_pid() == 777
    assert sessions.candidate_pids()[0] == 777

    # and the mirror case: a Claude session under a Codex terminal answers Claude throughout
    monkeypatch.setattr(sessions, "client_process", lambda *a, **k: (888, "claude"))
    assert sessions.resolve_provider() == "claude"
    assert sessions.resolve_session_id(Path(".")) == "outer-claude"
    assert sessions.resolve_pid() == 4242  # Claude exports its own pid, so no walk is needed


def test_the_other_client_variable_is_a_last_resort_not_a_refusal(monkeypatch):
    """A session variable inherited from an outer session is a poor answer, but raising is
    worse: the whole point of `ENV_VARS` is that a `bin/agent` call from any client resolves."""
    from pathlib import Path

    from agent_cli import session_identity as sessions

    assert sessions.session_env_vars("codex")[0] == "CODEX_SESSION_ID"
    assert "CLAUDE_CODE_SESSION_ID" in sessions.session_env_vars("codex")
    assert sessions.session_env_vars("claude")[0] == "CLAUDE_CODE_SESSION_ID"

    # provider resolves to codex (its var is set), but only a Claude id exists to be had
    monkeypatch.setenv("CODEX_SESSION_ID", "")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "only-one-going")
    monkeypatch.setattr(sessions, "client_process", lambda *a, **k: None)
    assert sessions.resolve_session_id(Path(".")) == "only-one-going"


def test_a_resumed_session_is_found_by_the_thread_id_on_its_command_line(monkeypatch):
    """`codex resume <thread-id>` does not fire `SessionStart` — measured on codex-cli 0.155.1.

    So the record keeps the previous session's `ended` state and a pid that exited until
    someone prompts it, and until then the session is missing from `peer list`, invisible in the
    Managers view, and any message sent to it queues against a session nothing believes is
    alive. Before the owner prompted it the record read ended/40628; after, working/8556.

    The Codex manager checked its own runtime for a better answer and found none: the
    documented `SessionStart source=resume` did not fire on this version, no config forces it,
    the app-server `thread/loaded/list` API only covers threads in that daemon's process and a
    standalone resume is not one, and `state_5.sqlite` carries no live status or pid. So argv is
    the available signal. Matching a uuid cannot claim the wrong process.
    """
    from agent_cli import session_identity as sessions

    monkeypatch.delenv("AGENT_NOW", raising=False)
    if sys.platform == "win32":
        monkeypatch.setattr(sessions.sys, "platform", "darwin")  # the `ps` branch is what this table feeds
    sid = "019b76da-a800-7000-8000-000000000004"
    table = (
        f"  8551 node /opt/homebrew/bin/codex resume\n"
        f"  8556 /opt/homebrew/.../bin/codex resume {sid}\n"
        f"  9001 /bin/zsh -c something-else\n"
    )
    monkeypatch.setattr(
        sessions.subprocess,
        "run",
        lambda *a, **k: type("P", (), {"stdout": table, "returncode": 0})(),
    )
    assert sessions.pid_running_session(sid) == 8556
    assert sessions.pid_running_session("no-such-thread") is None
    assert sessions.pid_running_session("") is None


def test_the_resume_scan_is_off_under_the_test_clock(monkeypatch):
    """Same guard as the ancestry walk: the suite runs under a real client, so an unguarded
    scan finds the developer's own sessions — an earlier leak, one mechanism over."""
    from agent_cli import session_identity as sessions

    monkeypatch.setenv("AGENT_NOW", "2026-09-23T14:00:00-06:00")
    assert sessions.pid_running_session("anything") is None
