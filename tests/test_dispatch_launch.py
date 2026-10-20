"""Dispatch runner: ids/paths/registry allocation, worktree reuse (1.5), the SDK options a
launch builds, steering a running worker, and worktree setup.

Split out of `test_dispatch.py`. Every run uses
watch=True. The supervisor loop runs inline. Shared fixtures and scenario-building helpers are
in `dispatch_helpers.py`.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from agent_cli import dispatch
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from conftest import git
from dispatch_helpers import (  # noqa: F401 -- droot is a pytest fixture, used only as a parameter name
    _new_fast,
    _options,
    _set_alpha_dispatch,
    _steer_when,
    droot,
    envelope,
    one,
    says,
    turn,
)

# ------------------------------------------------------------------ ids, paths, registry


def test_id_allocation_and_seq_collision(droot: Path):
    from agent_cli import features as features_mod

    f = features_mod.resolve(droot, "1")
    assert dispatch.allocate_seq(droot, f, "mac") == 1
    hdir = droot / "features/001-alpha-work/handoffs"
    hdir.mkdir()
    (hdir / "mac-03-alpha.md").write_text("---\nid: 001-mac-03\n---\n")
    assert dispatch.allocate_seq(droot, f, "mac") == 4
    (droot / ".agent/dispatches").mkdir(parents=True)
    (droot / ".agent/dispatches/001-mac-05.json").write_text("{}")
    assert dispatch.allocate_seq(droot, f, "mac") == 6
    assert dispatch.make_id(f, 6, "mac") == "001-mac-06"


def test_worktree_path_branch_and_seq_suffix(droot: Path, fake_claude):
    """d1 is left dirty on purpose. A clean, done d1 would make d2 reuse its worktree (1.5), so
    this is also the dirty-worktree-is-not-reused case, and seq suffixing of a fresh worktree."""
    fake_claude.scenario([one(envelope(), dirty=True)])
    d1 = _new_fast(droot)
    d2 = _new_fast(droot)
    assert d1["worktree"] == str(droot / "repos/_worktrees/alpha/001-alpha-work-mac-01")
    assert d1["branch"] == "feature/alpha-work-mac-01"
    assert d2["id"] == "001-mac-02"
    assert d2["worktree"] == str(droot / "repos/_worktrees/alpha/001-alpha-work-mac-02")
    assert d2["branch"] == "feature/alpha-work-mac-02"
    assert d2["reused"] is False
    assert d2["handoff"] == "features/001-alpha-work/handoffs/mac-02-alpha.md"
    branches = git(["branch", "--list", "feature/*"], droot / "repos/alpha-api")
    assert "feature/alpha-work-mac-01" in branches and "feature/alpha-work-mac-02" in branches


# ------------------------------------------------------------------ worktree reuse (1.5)


def test_second_dispatch_reuses_a_clean_done_worktree_and_keeps_an_existing_branchs_commits(
    droot: Path, fake_claude
):
    fake_claude.scenario([one(envelope())])
    d1 = _new_fast(droot)
    assert d1["reused"] is False
    d2 = _new_fast(droot)
    assert d2["worktree"] == d1["worktree"]
    assert d2["reused"] is True
    assert d2["branch"] != d1["branch"]
    main = droot / "repos/alpha-api"
    listing = git(["worktree", "list"], main)
    assert listing.count(Path(d1["worktree"]).as_posix()) == 1
    assert git(["rev-parse", "--abbrev-ref", "HEAD"], Path(d1["worktree"])).strip() == d2["branch"]
    # Resuming d1 into that same directory would work on d2's branch without saying so. It
    # refuses instead (ADR-0008).
    with pytest.raises(AgentError, match="reused"):
        dispatch.resume(droot, dispatch_id="001-mac-01", answer="one more thing")

    # `--branch` naming a branch that already exists (a review dispatched onto the build
    # branch) checks it out as it is. `checkout -B` used to reset it to base. 002-10 left both
    # PR 2 commits dangling (2026-09-10 10:19).
    wt = Path(d2["worktree"])
    (wt / "built.txt").write_text("from the build worker\n")
    git(["add", "built.txt"], wt)
    git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "build"], wt)
    head_before = git(["rev-parse", "HEAD"], wt).strip()
    d3 = _new_fast(droot, branch=d2["branch"])
    assert d3["worktree"] == d2["worktree"] and d3["reused"] is True
    assert d3["branch"] == d2["branch"]
    assert git(["rev-parse", "HEAD"], wt).strip() == head_before
    assert (wt / "built.txt").exists()


def test_running_or_blocked_clean_worktree_is_not_reused(droot: Path, fake_claude):
    """A blocked or question dispatch's worktree is the one the owner is told to resume into. A
    later dispatch stealing it would switch its branch underneath them (ADR-0008)."""
    fake_claude.scenario([one(envelope())])
    d1 = _new_fast(droot)
    d1["state"] = "running"
    dispatch.save(droot, d1)
    d2 = _new_fast(droot)
    assert d2["worktree"] != d1["worktree"]
    assert d2["reused"] is False
    for d in (d1, d2):
        current = dispatch.load(droot, d["id"])
        current["state"] = "blocked"
        dispatch.save(droot, current)
    d3 = _new_fast(droot)
    assert d3["worktree"] not in (d1["worktree"], d2["worktree"])
    assert d3["reused"] is False


def test_worktrees_dir_null_falls_back_to_clones_sibling(droot: Path, fake_claude, run):
    _set_alpha_dispatch(droot, worktrees_dir=None)
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    assert d["worktree"] == str(droot / "repos" / "_worktrees" / "alpha" / "001-alpha-work-mac-01")
    assert Path(d["worktree"]).is_dir()
    r = run(["--json", "doctor"])
    assert any(c["check"] == "repo alpha worktrees_dir" and c["level"] == "warn" for c in r.json)


def test_one_transport_for_every_repo(droot: Path, fake_claude):
    """`dispatch.stream` used to pick between a single-shot `-p` run and a long-lived
    stream-json one, and only one repo had opted in. Both drivers are gone (ADR-0009): there is one
    path, and agent.yaml no longer has a flag to set."""
    fake_claude.scenario([one(envelope())])
    assert "stream" not in repos_mod.load_registry(droot).require("alpha").dispatch
    base = {
        "version": 1,
        "repos": [{"name": "x", "path": "../x", "remote": "r", "dispatch": {"stream": True}}],
    }
    assert repos_mod.validate_registry_data(base) == ["repos[0].dispatch: unknown key 'stream'"]
    assert repos_mod.validate_registry_data(base, unknown_keys=False) == []
    _set_alpha_dispatch(droot, stream=True)
    assert _new_fast(droot)["state"] == "done"


def test_worker_options_carry_the_schema_budget_and_pinned_session(droot: Path, fake_claude):
    """What the old argv assertions were about: the return schema, the budget cap and a
    pinned session id all reach the worker. `--bg` would have dropped all three, the budget
    silently. That is why this harness never used it."""
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    opts = _options(fake_claude)
    assert opts.output_format == {"type": "json_schema", "schema": dispatch.RETURN_SCHEMA}
    assert opts.max_budget_usd == 1.0
    assert opts.session_id == d["session_id"] and opts.resume is None
    assert opts.extra_args["name"] == "001-mac-01"
    assert opts.cli_path == shutil.which("claude"), "the PATH claude, never the SDK's bundled CLI"
    assert d["state"] == "done" and d["result"]["summary"] == "added the line"
    # No wire format of ours sits in between any more. The composed work order is the string
    # handed to `client.query()`. A later `dispatch say` is the same kind of thing.
    assert "# Work order 001-mac-01" in fake_claude.calls()[0]["prompt"]
    # finished: say cannot reach it, and the error names the command that can
    with pytest.raises(AgentError, match="action resume"):
        dispatch.say(droot, dispatch_id="001-mac-01", message="hello")
    with pytest.raises(AgentError, match="nothing to say"):
        dispatch.say(droot, dispatch_id="001-mac-01", message="   ")


def test_a_steer_answers_a_question_in_the_same_session(droot: Path, fake_claude, monkeypatch):
    """A question no longer ends the attempt and relaunches with --resume. The connection
    stays open. The answer arrives on it, and the worker finishes in ONE session with its
    context intact. A worker once burned three attempts on exactly that relaunch."""
    monkeypatch.setattr(dispatch.state, "QUESTION_IDLE_SECONDS", 20.0)
    q = envelope("question", open_questions=["which port?"], summary="need the port")
    fake_claude.scenario([{"turns": [turn(q), turn(envelope(summary="used 8080"))]}])
    t = _steer_when(droot, "which port?", "use 8080")
    d = _new_fast(droot)
    t.join(timeout=5)

    assert len(fake_claude.calls()) == 1, "one session, not a --resume relaunch"
    assert d["attempt"] == 1
    assert d["state"] == "done" and d["result"]["summary"] == "used 8080"
    assert fake_claude.calls()[0]["steers"] == ["use 8080"]


def test_a_slow_answer_after_a_steer_is_not_cut_by_the_watchdog(droot: Path, fake_claude, monkeypatch):
    """Found in review of the first migration: the watchdog ran from the FIRST result. A
    worker answering a question after a steer was stopped WATCHDOG_SECONDS later, mid-answer.
    A steer resets that clock. Only the attempt's timeout bounds the answer."""
    monkeypatch.setattr(dispatch.state, "QUESTION_IDLE_SECONDS", 20.0)
    monkeypatch.setattr(dispatch.state, "WATCHDOG_SECONDS", 0.3)
    q = envelope("question", open_questions=["which port?"], summary="need the port")
    fake_claude.scenario([{"turns": [turn(q), turn(envelope(summary="used 8080"), sleep=1.0)]}])
    t = _steer_when(droot, "which port?", "use 8080")
    d = _new_fast(droot)
    t.join(timeout=5)

    assert d["state"] == "done" and d["result"]["summary"] == "used 8080"
    assert d["stop_reason"] == "result", d["stop_reason"]


def test_an_empty_result_from_a_queue_drain_is_not_the_return(droot: Path, fake_claude):
    """2026-09-09 12:50: a resumed session whose previous attempt left a background task
    running gets its queued task notifications delivered first. The CLI emits a result for
    that empty turn (success, zero turns, zero cost, no structured output) before the new
    message is processed. 002-08 and 003-01 both ended two seconds after launch on it. The
    reader skips such a record and waits for the real return."""
    drain = envelope()
    drain.update({"num_turns": 0, "total_cost_usd": 0, "structured_output": None, "result": ""})
    fake_claude.scenario([one(drain, envelope(summary="after the drain"))])
    d = _new_fast(droot)
    assert d["state"] == "done" and d["result"]["summary"] == "after the drain"
    assert d["stop_reason"] == "result" and d["cost_usd"] == 0.42
    assert "queued notifications drained" in (droot / ".agent/dispatches/001-mac-01.err").read_text()


def test_a_steer_folds_into_the_running_turn_and_the_loop_still_ends(droot: Path, fake_claude):
    """The finding that shaped this whole design, and the bug it caused. A steer sent with
    `client.query()` while a turn is open folds into THAT turn. "Work order + steer" then
    produces ONE result, not two. The spike's loop waited for a second one and sat idle for 55
    minutes. Nothing counts results now. The first non-question result ends the attempt.
    """
    fake_claude.scenario([one(says("on it"), envelope(summary="did both"), await_steer=True)])
    t = _steer_when(droot, "on it", "also do the other thing")
    started = time.monotonic()
    d = _new_fast(droot)
    t.join(timeout=5)

    assert time.monotonic() - started < 20, "the loop must not wait for a second result"
    assert d["state"] == "done" and d["result"]["summary"] == "did both"
    assert d["stop_reason"] == "result"
    assert fake_claude.calls()[0]["steers"] == ["also do the other thing"]


def test_a_worker_that_never_returns_after_a_steer_hits_the_attempt_timeout(
    droot: Path, fake_claude, monkeypatch
):
    """The worker answers `question`, is steered, and then never produces another result.
    Under the SDK a steered turn always ends in a result. A worker that never returns is a
    true hang. The bound for that is the attempt's timeout, not a watchdog counted from the
    question. A watchdog would cut every real answer short (found in review of the first
    migration). The timeout then gets its one auto-resume, at the same budget."""
    monkeypatch.setattr(dispatch.state, "QUESTION_IDLE_SECONDS", 30.0)
    monkeypatch.setattr(dispatch.state, "WATCHDOG_SECONDS", 0.5)
    _set_alpha_dispatch(droot, timeout_minutes=0.03)
    q = envelope("question", open_questions=["which port?"], summary="need the port")
    fake_claude.scenario(
        [{"turns": [turn(q), turn(says("still thinking"))]}, one(envelope(summary="second attempt"))]
    )
    t = _steer_when(droot, "which port?", "use 8080")
    started = time.monotonic()
    d = _new_fast(droot)
    t.join(timeout=5)

    assert time.monotonic() - started < 25, "the attempt timeout, not the 45-minute default, ended it"
    calls = fake_claude.calls()
    assert calls[0]["steers"] == ["use 8080"] and calls[0]["interrupts"] == 1
    assert d["auto_resumes"][0]["reason"] == "timeout"
    assert d["state"] == "done" and d["attempt"] == 2 and d["result"]["summary"] == "second attempt"


def test_say_into_a_queued_dispatch_waits_in_the_inbox(droot: Path, fake_claude, monkeypatch):
    """`say` used to refuse for any repo without `dispatch.stream`, sending the owner to `@001-mac-01`
    instead. Every worker is a live connection now. A steer sent a moment early waits on disk
    for the supervisor's first poll."""
    monkeypatch.setattr(
        dispatch.supervisor,
        "popen",
        type("P", (), {"pid": os.getpid(), "__init__": lambda self, *a, **k: None}),
    )
    _new_fast(droot, watch=False)  # queued, never launched
    r = dispatch.say(droot, dispatch_id="001-mac-01", message="mind the config")
    assert r["pending"] is False
    assert dispatch.inbox_path(droot, "001-mac-01").read_text().strip() == "mind the config"


def test_a_resumed_worker_keeps_its_name_and_resumes_the_session(droot: Path, fake_claude, monkeypatch):
    """Two things a resume has to get right. The name is the address the owner steers to, so it has
    to survive. Attempt 2 must CONTINUE the session (`resume=`) rather than pin a new one. The
    composed follow-up is its first query."""
    monkeypatch.setattr(dispatch.state, "QUESTION_IDLE_SECONDS", 0.2)
    q = envelope("question", open_questions=["which port?"], summary="need the port")
    fake_claude.scenario([one(q), one(envelope())])
    d = _new_fast(droot)
    assert d["state"] == "question"
    dispatch.resume(droot, dispatch_id="001-mac-01", answer="use port 8080", watch=True)
    first, second = _options(fake_claude, 0), _options(fake_claude, 1)
    assert first.session_id == d["session_id"] and first.resume is None
    assert second.resume == d["session_id"] and second.session_id is None
    assert first.extra_args["name"] == second.extra_args["name"] == "001-mac-01"
    assert "use port 8080" in fake_claude.calls()[1]["prompt"]


def test_worker_contract_tells_a_worker_a_message_is_an_instruction(droot: Path):
    """A probe worker treated a peer message as something to acknowledge rather than obey.
    Steering that gets politely noted is not steering."""
    contract = (Path(__file__).resolve().parent.parent / "harness/worker-contract.md").read_text()
    assert "instruction, not a notification" in contract
    assert "surface it for awareness" in contract


def test_worktree_setup_runs_in_a_fresh_worktree(droot: Path, fake_claude):
    """A new worktree has no installed dependencies. Without this, every verification step in a
    work order is unreachable."""
    # `Bash(printf:*)` is not one of BASE_ALLOW's tools. The preflight added in 1.2 would
    # otherwise refuse this dispatch on its own setup command. The fixture repo needs the
    # rule the way a real one would.
    # Setup also gets the repo env. The install is exactly what `dispatch.env` exists to
    # configure, so the repo's variables reach it.
    _set_alpha_dispatch(
        droot,
        env={"MARKER_VALUE": "from-repos-yaml"},
        setup=[
            "printf ran > setup-marker.txt",
            'printf %s "%MARKER_VALUE%" > env-marker.txt'
            if os.name == "nt"
            else 'printf %s "$MARKER_VALUE" > env-marker.txt',
        ],
        allowed_tools=["Bash(printf:*)"],
    )
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    marker = Path(d["worktree"]) / "setup-marker.txt"
    assert marker.exists() and marker.read_text().strip() == "ran"
    assert (Path(d["worktree"]) / "env-marker.txt").read_text().strip() == "from-repos-yaml"
    assert [s["exit"] for s in d["setup"]] == [0, 0]
    assert "setup_pending" not in d, "the flag is consumed, so a resume does not reinstall"


def test_setup_runs_in_the_supervisor_so_new_returns_at_once(droot: Path, fake_claude, monkeypatch):
    """`dispatch new` detaches and hands back the record. That is its whole contract. Running
    setup inline broke it. A cold dependency install blocked the command for minutes before the
    worker even launched."""
    monkeypatch.setattr(
        dispatch.supervisor, "popen", type("P", (), {"pid": 1, "__init__": lambda self, *a, **k: None})
    )
    _set_alpha_dispatch(droot, setup=["printf ran > setup-marker.txt"], allowed_tools=["Bash(printf:*)"])
    d = _new_fast(droot, watch=False)  # detached: the supervisor never runs in this test
    assert d["setup_pending"] is True, "queued for the supervisor, not done yet"
    assert not (Path(d["worktree"]) / "setup-marker.txt").exists(), "setup must not run inline"


def test_worktree_setup_failure_is_reported_not_fatal(droot: Path, fake_claude):
    """The worker can retry the install itself. It can return a better description of the
    problem than we could. A setup failure must not sink the dispatch before it starts."""
    _set_alpha_dispatch(droot, setup=["exit 3"], allowed_tools=["Bash(exit:*)"])
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    assert d["state"] == "done", "a failed setup must not block the worker"
    assert Path(d["worktree"]).is_dir()


# `worker_env` reads only the record's id and feature. The launch that hands it to the worker is
# `test_done_path_options_env_prompt_files_and_records`.
WORKER = {"id": "001-mac-01", "feature": 1}


def test_repo_env_reaches_the_worker(droot: Path):
    """A worker cannot set an environment variable. Its allow-list is a prefix match, so
    `env FOO=1 pnpm install` starts with `env` and matches nothing. Declaring it here beats
    widening the allow-list to `Bash(env:*)`. That would let a worker prefix any command."""
    _set_alpha_dispatch(droot, env={"EXAMPLE_FLAG": "1", "NUMERIC": 7})
    d = WORKER
    repo = repos_mod.load_registry(droot).require("alpha")
    env = dispatch.worker_env(droot, d, repo)
    assert env["EXAMPLE_FLAG"] == "1"
    assert env["NUMERIC"] == "7", "non-string values are coerced, not dropped"
    # the manager's own session must still never leak through
    assert not any(k.startswith("CLAUDE") for k in env)
    assert dispatch.worker_env(droot, d)["AGENT_DISPATCH_ID"] == d["id"], "repo is optional"


def test_anthropic_base_url_does_not_reach_the_worker(droot: Path, monkeypatch):
    """A proxy set in the manager's shell would otherwise carry every worker's traffic, and the
    product source it reads, to that host — invisibly, since neither the dry run nor the handoff
    names the environment. A repo that wants one declares it in `dispatch.env`, which is on the
    record."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.test")
    d = WORKER
    assert "ANTHROPIC_BASE_URL" not in dispatch.worker_env(droot, d)

    _set_alpha_dispatch(droot, env={"ANTHROPIC_BASE_URL": "https://declared.example.test"})
    repo = repos_mod.load_registry(droot).require("alpha")
    env = dispatch.worker_env(droot, d, repo)
    assert env["ANTHROPIC_BASE_URL"] == "https://declared.example.test", (
        "a repo can still declare one on purpose"
    )


def test_worker_env_is_an_allowlist(droot: Path, monkeypatch):
    """The old rule dropped CLAUDE* and ANTHROPIC_BASE_URL and
    passed everything else, so any credential in the manager's shell reached every worker — and
    a worker has WebFetch in BASE_ALLOW while reading untrusted content by design, which is the
    exfiltration chain the standard names. A deny-list can only drop the names someone thought
    of."""
    monkeypatch.setenv("FAKE_TOKEN", "sk-not-a-real-secret")
    monkeypatch.setenv("EXAMPLE_API_KEY", "also-not-real")
    monkeypatch.setenv("PATH", os.environ["PATH"])
    env = dispatch.worker_env(droot, WORKER)
    assert "FAKE_TOKEN" not in env
    assert "EXAMPLE_API_KEY" not in env
    assert env["PATH"], "the base is still there or nothing runs"


def test_worker_env_base_covers_what_a_build_needs(droot: Path, monkeypatch):
    """SSH_AUTH_SOCK earns its place: the clones use SSH remotes and `Bash(git fetch:*)` is
    allowed, so a worker without the agent socket cannot fetch."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-test.sock")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    env = dispatch.worker_env(droot, WORKER)
    assert env["SSH_AUTH_SOCK"] == "/tmp/ssh-test.sock"
    assert env["LC_ALL"] == "en_US.UTF-8", "LC_* is a prefix entry"


def test_repo_env_allow_lets_a_toolchain_var_through(droot: Path, monkeypatch):
    """The base is POSIX-only on purpose, because core ships public and a toolchain list is not
    generic. A repo names what it needs, and the grant is legible in the registry."""
    monkeypatch.setenv("PIP_CACHE_DIR", "/cache/pip")
    monkeypatch.setenv("NPM_CONFIG_LOGLEVEL", "warn")
    monkeypatch.setenv("UNRELATED_SECRET", "nope")
    _set_alpha_dispatch(droot, env_allow=["PIP_CACHE_DIR", "NPM_CONFIG_*"])
    d = WORKER
    repo = repos_mod.load_registry(droot).require("alpha")
    env = dispatch.worker_env(droot, d, repo)
    assert env["PIP_CACHE_DIR"] == "/cache/pip"
    assert env["NPM_CONFIG_LOGLEVEL"] == "warn"
    assert "UNRELATED_SECRET" not in env
    assert "PIP_CACHE_DIR" not in dispatch.worker_env(droot, d), "without the repo, not allowed"


def test_env_allow_can_never_leak_the_managers_session(droot: Path, monkeypatch):
    """A worker that inherited the manager's session would resume it. No repo may ask for that,
    however the pattern is spelled."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "manager-session")
    _set_alpha_dispatch(droot, env_allow=["CLAUDE_*", "CLAUDE_CODE_SESSION_ID"])
    repo = repos_mod.load_registry(droot).require("alpha")
    assert not any(k.startswith("CLAUDE") for k in dispatch.worker_env(droot, WORKER, repo))


def test_registry_rejects_env_allow_with_a_value(droot: Path):
    from agent_cli import repos as repos_module

    problems = repos_module.validate_registry_data(
        {
            "version": 1,
            "repos": [{"name": "x", "path": "../x", "remote": "r", "dispatch": {"env_allow": ["FOO=bar"]}}],
        }
    )
    assert any("env_allow" in p for p in problems)


def test_registry_validates_setup_and_env(droot: Path):
    base = {
        "version": 1,
        "repos": [{"name": "x", "path": "../x", "remote": "r", "dispatch": {"setup": "pnpm install"}}],
    }
    assert any("setup" in p for p in repos_mod.validate_registry_data(base))
    base["repos"][0]["dispatch"] = {"env": ["FOO=1"]}
    assert any("env" in p for p in repos_mod.validate_registry_data(base))
    base["repos"][0]["dispatch"] = {"env": {"FOO": True}}
    assert any("env" in p for p in repos_mod.validate_registry_data(base)), "a bool is not a value"
    base["repos"][0]["dispatch"] = {"setup": ["pnpm install --prefer-offline"], "env": {"FOO": "1", "N": 2}}
    assert repos_mod.validate_registry_data(base) == []
