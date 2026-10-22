"""Tree-keyed full pass records (ADR-0041): a record comes only from a full run of every verify
command in an isolated checkout of the exact tree, is reused for an identical tree, and fails
closed on anything it cannot check."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent_cli import verify_record, verify_run
from agent_cli.core import AgentError
from land_helpers import git as _git
from land_helpers import recorded

PASS = "echo 3 passed in 0.01s"
REGISTRY = """version: 1
repos:
  - name: Harness
    short: harness
    path: .
    remote: git@github.com:example/harness.git
    default_branch: main
    harness: "none"
    dispatch:
      verify:
{verify}
      verify_timeout_seconds: 30
    notes: ""
"""


def _registry(root: Path, *commands: str) -> None:
    rows = "\n".join(f'        - command: "{c}"\n          timeout_seconds: 20' for c in commands)
    (root / "agent.yaml").write_text(REGISTRY.format(verify=rows), encoding="utf-8")


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _land_template: Path) -> Path:
    """`_land_template` only places `recorded`'s session record cache."""
    root = tmp_path / "harness"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "agent").write_text("")
    (root / ".gitignore").write_text("/agent.yaml\n.agent/\n")
    (root / "tracked.txt").write_text("v1\n")
    (root / "harness" / "lessons").mkdir(parents=True)
    (root / "harness" / "lessons" / "queue.md").write_text("# queue\n")
    _registry(root, "echo lint ok", PASS)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    monkeypatch.setenv("AGENT_ROOT", str(root))
    return root


def _recorded(root: Path) -> dict:
    """HEAD's pass record: every test's initial tree is the same, so all but the first per
    session get a copy of a real one."""
    recorded(root, "HEAD")
    found, why = verify_record.lookup(root, "HEAD")
    assert found is not None, why
    return found


def test_a_full_run_records_the_tree_and_an_identical_tree_reuses_it(harness: Path, monkeypatch):
    """The record is written under `agent land` (session ids, telemetry routing, a fixed clock) and
    still applies to the owner's later push from a plain shell."""
    for name, value in (
        ("AGENT_NOW", "2026-10-05T00:00:00Z"),
        ("AGENT_TELEMETRY_SINK", "/tmp/sink"),
        ("CLAUDE_CODE_SESSION_ID", "s1"),
        ("CODEX_THREAD_ID", "t1"),
    ):
        monkeypatch.setenv(name, value)
    first = verify_record.record(harness, "HEAD")
    assert first["reused"] is False and first["tier"] == "full" and first["command_count"] == 2
    assert all(c["exit_code"] == 0 for c in first["commands"])
    assert "retry" not in first and not (verify_record.store(harness) / "failed").exists()
    assert "AGENT_NOW" in first["recorded_only"]["agent_vars"]
    for name in ("AGENT_NOW", "AGENT_TELEMETRY_SINK", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name)
    _git(harness, "commit", "-q", "--allow-empty", "-m", "same tree, new commit")
    again = verify_record.record(harness, "HEAD")
    assert again["reused"] is True and again["run_id"] == first["run_id"]
    shas = [p["sha"] for p in again["provenance"]]
    assert shas == [first["sha"], _git(harness, "rev-parse", "HEAD")]
    found, why = verify_record.lookup(harness, "HEAD")
    assert found is not None and why == "ok"
    # the checkout and its pytest temp root are gone; only the record and its evidence remain
    assert _git(harness, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_a_tree_that_differs_only_in_the_lessons_queue_has_no_record(harness: Path):
    _recorded(harness)
    (harness / "harness" / "lessons" / "queue.md").write_text("# queue\n- a lesson\n")
    _git(harness, "commit", "-q", "-am", "queue only")
    found, why = verify_record.lookup(harness, "HEAD")
    assert found is None and "no full pass record" in why and "agent verify record" in why


def test_a_changed_tool_environment_or_policy_does_not_apply(harness: Path, monkeypatch):
    _recorded(harness)
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    assert verify_record.lookup(harness, "HEAD")[0] is None
    monkeypatch.delenv("TZ")
    real = verify_record._tool
    monkeypatch.setattr(
        verify_record, "_tool", lambda name, env, bash: "uv 9.9.9" if name == "uv" else real(name, env, bash)
    )
    assert verify_record.lookup(harness, "HEAD")[0] is None
    monkeypatch.setattr(verify_record, "_tool", real)
    assert verify_record.lookup(harness, "HEAD")[0] is not None
    _registry(harness, "echo lint ok", PASS, "echo another check")
    assert verify_record.lookup(harness, "HEAD")[0] is None


@pytest.mark.parametrize(
    "command,why",
    [
        ("echo 1 failed, 2 passed; exit 1", "a command did not pass"),
        ("echo 2 passed, 1 error in 0.1s", "no clean pytest summary"),
        ("echo nothing ran", "no clean pytest summary"),
        ("echo x >> tracked.txt; echo 3 passed", "not clean"),
    ],
)
def test_a_run_that_did_not_cleanly_pass_writes_no_record(harness: Path, command: str, why: str):
    _registry(harness, command)
    with pytest.raises(AgentError) as caught:
        verify_record.record(harness, "HEAD")
    assert why in str(caught.value) or "changed the isolated checkout" in str(caught.value)
    assert verify_record.lookup(harness, "HEAD")[0] is None
    assert not list((verify_record.store(harness) / "v1").glob("*/*.json"))


@pytest.mark.parametrize(
    "command,subset",
    [
        ("uv run pytest", False),
        ("uv run pytest -q -n 4", False),
        ("python -m pytest", False),
        ("uv run pytest -k slow", True),
        ("uv run pytest -m 'not live'", True),
        ("uv run pytest --ignore=tests/test_x.py", True),
        ("uv run pytest tests/test_x.py", True),
        ("uv run pytest tests/test_x.py::test_y", True),
        ("uv run pytest --lf", True),
        ("uv run ruff check src tests", False),
    ],
)
def test_a_filtered_pytest_command_is_never_full(command: str, subset: bool):
    assert verify_run.filtered(command) is subset


def test_a_configured_subset_refuses_to_record(harness: Path):
    _registry(harness, "uv run pytest -k fast")
    with pytest.raises(AgentError, match="subset"):
        verify_record.record(harness, "HEAD")


def _write(harness: Path, record: dict | str) -> Path:
    sha, tree = verify_record.resolve(harness, "HEAD")
    rp = verify_record.harness_repo(harness)
    policy_digest, _ = verify_record.policy(rp)
    path = verify_record._path(harness, tree, verify_record.environment(rp)["digest"], policy_digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record) if isinstance(record, dict) else record)
    return path


def test_a_hand_written_or_tampered_record_fails_closed(harness: Path):
    real = _recorded(harness)
    run_dir = verify_record.store(harness) / "runs" / real["run_id"]
    forged = {**real, "run_id": "f" * 32}
    _write(harness, forged)
    assert "missing" in verify_record.lookup(harness, "HEAD")[1]
    _write(harness, real)
    log = run_dir / "output.log"
    log.write_text(log.read_text() + "1 failed\n")
    assert "does not match" in verify_record.lookup(harness, "HEAD")[1]
    _write(harness, "{not json")
    assert "unreadable" in verify_record.lookup(harness, "HEAD")[1]
    _write(harness, {**real, "tier": "scoped"})
    assert "not a full pass record" in verify_record.lookup(harness, "HEAD")[1]


def test_malformed_record_fields_fail_closed(harness: Path):
    real = _recorded(harness)
    _write(harness, {**real, "command_count": str(real["command_count"])})
    assert "does not cover every configured command" in verify_record.lookup(harness, "HEAD")[1]
    _write(harness, {**real, "evidence": list(real["evidence"])})
    assert "names no run evidence" in verify_record.lookup(harness, "HEAD")[1]
    _write(harness, "[]")
    assert "not a full pass record" in verify_record.lookup(harness, "HEAD")[1]


def test_old_small_and_notes_only_records_are_ignored(harness: Path):
    """Records the removed landing tiers left under the store certify nothing and break no reader."""
    from agent_cli import land_journal, land_recover, push_records
    from agent_cli.push_input import Update

    sha, tree = verify_record.resolve(harness, "HEAD")
    store = verify_record.store(harness)
    (store / "small").mkdir(parents=True)
    (store / "small" / f"{sha}.json").write_text(json.dumps({"kind": "small", "sha": sha, "tree": tree}))
    (store / f"{sha}.json").write_text(json.dumps({"kind": "notes-only", "sha": sha, "on": sha}))
    found, why = verify_record.lookup(harness, "HEAD")
    assert found is None and "no full pass record" in why
    shown = verify_record.show(harness, "HEAD")
    assert shown["applies"] is False and set(shown) == {"sha", "tree", "applies", "why", "record", "kept"}
    update = Update(
        local_oid=sha, remote_oid="0" * 40, local_ref="refs/heads/main", remote_ref="refs/heads/main"
    )
    assert push_records.problems(harness, [update])
    land_journal.land_dir(harness).mkdir(parents=True, exist_ok=True)
    assert any("no full pass record" in line for line in land_recover.report(harness))
    _recorded(harness)
    assert push_records.problems(harness, [update]) == []


def test_consistent_evidence_of_a_failed_run_is_still_refused(harness: Path):
    """Evidence hashes that match prove nothing on their own: the evidence itself must show a
    complete, clean, passing run."""
    real = _recorded(harness)
    run_dir = verify_record.store(harness) / "runs" / real["run_id"]
    terminal = json.loads((run_dir / "terminal.json").read_text())
    terminal["state"] = "interrupted"
    (run_dir / "terminal.json").write_text(json.dumps(terminal))
    hashes = {**real["evidence"], "terminal.json": verify_record._file_sha(run_dir / "terminal.json")}
    _write(harness, {**real, "evidence": hashes})
    assert "the run is interrupted" in verify_record.lookup(harness, "HEAD")[1]


def test_a_tree_that_names_a_filter_is_refused_and_no_filter_runs(harness: Path, tmp_path: Path):
    marker = tmp_path / "filter-ran"
    (harness / ".gitattributes").write_text("*.txt filter=probe\n")
    _git(harness, "add", ".gitattributes")
    _git(harness, "commit", "-q", "-m", "attributes")
    probe = f"sh -c 'touch {marker}; cat'"
    _git(harness, "config", "filter.probe.clean", probe)
    _git(harness, "config", "filter.probe.smudge", probe)
    with pytest.raises(AgentError, match="filter"):
        verify_record.record(harness, "HEAD")
    assert not marker.exists()


def test_an_unpopulated_gitlink_records_as_reverify_would(harness: Path):
    head = _git(harness, "rev-parse", "HEAD")
    _git(harness, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/sub")
    _git(harness, "commit", "-q", "-m", "a submodule pointer")
    assert verify_record.record(harness, "HEAD")["reused"] is False


def test_the_cli_shows_and_records(harness: Path, run):
    """A new run's record is `record`'s own subject above; here the CLI reuses the session's."""
    shown = run(["verify", "show", "HEAD"])
    assert shown.code != 0 and "no full pass record" in shown.out + shown.err
    _recorded(harness)
    assert run(["verify", "show", "HEAD"]).code == 0
    assert json.loads(run(["--json", "verify", "record", "HEAD"]).out)["reused"] is True


ENV_REGISTRY = """version: 1
repos:
  - name: Harness
    short: harness
    path: .
    remote: git@github.com:example/harness.git
    default_branch: main
    harness: "none"
    dispatch:
      verify:
        - command: "test x$REVIEW_REQUIRED = xgood && echo 3 passed in 0.01s"
          timeout_seconds: 20
      verify_timeout_seconds: 30
      env_allow: [MY_FLAG]
      env:
        REVIEW_REQUIRED: {value}
    notes: ""
"""


def test_the_verifier_environment_keys_the_record(harness: Path, monkeypatch):
    """Codex review: changing `dispatch.env` left the key unchanged while the command now failed."""
    monkeypatch.setenv("MY_FLAG", "1")
    (harness / "agent.yaml").write_text(ENV_REGISTRY.format(value="good"))
    made = verify_record.record(harness, "HEAD")
    assert "REVIEW_REQUIRED" in made["verifier_inputs"] and "MY_FLAG" in made["verifier_inputs"]
    (harness / "agent.yaml").write_text(ENV_REGISTRY.format(value="bad"))
    assert verify_record.lookup(harness, "HEAD")[0] is None
    (harness / "agent.yaml").write_text(ENV_REGISTRY.format(value="good"))
    monkeypatch.setenv("MY_FLAG", "2")
    assert verify_record.lookup(harness, "HEAD")[0] is None
    monkeypatch.setenv("MY_FLAG", "1")
    for name, value in (("TERM", "dumb"), ("AGENT_SESSION_ID", "s9"), ("CLAUDE_CODE_SESSION_ID", "x")):
        monkeypatch.setenv(name, value)
    assert verify_record.lookup(harness, "HEAD")[0] is not None


DECLARED_REGISTRY = """version: 1
repos:
  - name: Harness
    short: harness
    path: .
    remote: git@github.com:example/harness.git
    default_branch: main
    harness: "none"
    dispatch:
      verify:
        - command: '{command}'
          timeout_seconds: 20
      verify_timeout_seconds: 30
      env:
        {variable}: '{value}'
    notes: ""
"""


@pytest.mark.parametrize("variable", ["PATH", "HOME"])
def test_a_declared_path_or_home_keys_the_record(harness: Path, variable: str):
    """`dispatch.env` setting PATH or HOME changed the commands' outcome
    while both digests stayed the same, because plumbing names were never keyed."""
    import subprocess

    from agent_cli.dispatch.options import verifier_env

    good, bad = harness.parent / "good", harness.parent / "bad"
    for directory, code in ((good, 0), (bad, 1)):
        directory.mkdir()
        if sys.platform == "win32":
            (directory / "review-tool.cmd").write_text(f"@exit /b {code}\n")
        else:
            tool = directory / "review-tool"
            tool.write_text(f"#!/bin/sh\nexit {code}\n")
            tool.chmod(0o755)
    (good / "review-good").write_text("good")
    command = (
        "review-tool && echo 3 passed in 0.01s"
        if variable == "PATH"
        else 'test -f "$HOME/review-good" && echo 3 passed in 0.01s'
    )
    if sys.platform == "win32" and variable == "HOME":
        command = 'if exist "%HOME%\\review-good" (echo 3 passed in 0.01s) else (exit /b 1)'

    def key(directory: Path) -> tuple[object, str, int]:
        value = f"{directory}{os.pathsep}{os.environ['PATH']}" if variable == "PATH" else str(directory)
        (harness / "agent.yaml").write_text(
            DECLARED_REGISTRY.format(command=command, variable=variable, value=value)
        )
        rp = verify_record.harness_repo(harness)
        shell = {"shell": True} if sys.platform == "win32" else {}
        argv = command if sys.platform == "win32" else ["/bin/sh", "-c", command]
        run = subprocess.run(argv, cwd=harness, env=verifier_env(rp), check=False, **shell)
        return verify_record.policy(rp), verify_record.environment(rp)["digest"], run.returncode

    *before, passed = key(good)
    *after, failed = key(bad)
    assert (passed, failed) == (0, 1)
    assert after != before


def test_tools_are_measured_where_the_commands_run(harness: Path):
    """A `dispatch.env` PATH that finds another uv changes the environment digest. Windows runs the
    commands under Git Bash, which runs this extensionless script too and would never find a
    `uv.cmd`."""
    fake = harness.parent / "fakebin"
    fake.mkdir()
    uv = fake / "uv"
    uv.write_text("#!/bin/sh\necho uv 0.0.1-fake\n")
    uv.chmod(0o755)
    (harness / "agent.yaml").write_text(
        DECLARED_REGISTRY.format(
            command="echo 3 passed in 0.01s", variable="PATH", value=f"{fake}{os.pathsep}{os.environ['PATH']}"
        )
    )
    facts = verify_record.environment(verify_record.harness_repo(harness))["facts"]
    assert facts["uv"] == "uv 0.0.1-fake"


@pytest.mark.skipif(sys.platform == "win32", reason="an extensionless script on PATH")
def test_a_tool_version_is_probed_once_per_request_only(tmp_path: Path):
    calls = tmp_path / "calls"
    tool = tmp_path / "bin" / "probe-tool"
    tool.parent.mkdir()
    tool.write_text(f"#!/bin/sh\necho x >> {calls}\necho probe 1\n")
    tool.chmod(0o755)
    env = {"PATH": str(tool.parent)}
    argv = ["probe-tool", "--version"]
    with verify_record.probe_scope():
        assert [verify_record._version(argv, env) for _ in range(3)] == ["probe 1"] * 3
        assert calls.read_text().count("x") == 1
        assert verify_record._version(argv, {**env, "TZ": "UTC"}) == "probe 1"
        assert calls.read_text().count("x") == 2
    assert [verify_record._version(argv, env) for _ in range(2)] == ["probe 1"] * 2
    assert calls.read_text().count("x") == 4


@pytest.mark.skipif(sys.platform == "win32", reason="an extensionless script on PATH")
def test_a_tool_changed_behind_its_launcher_between_records_is_measured_again(harness: Path):
    fake = harness.parent / "fakebin"
    fake.mkdir()
    impl = harness.parent / "uv-impl"
    impl.write_text("echo uv 1.0\n")
    launcher = fake / "uv"
    launcher.write_text(f"#!/bin/sh\nexec /bin/sh {impl}\n")
    launcher.chmod(0o755)
    (harness / "agent.yaml").write_text(
        DECLARED_REGISTRY.format(
            command=PASS, variable="PATH", value=f"{fake}{os.pathsep}{os.environ['PATH']}"
        )
    )
    first = verify_record.record(harness, "HEAD")
    assert first["environment"]["uv"] == "uv 1.0"
    impl.write_text("echo uv 2.0\n")
    again = verify_record.record(harness, "HEAD")
    assert again["reused"] is False and again["run_id"] != first["run_id"]
    assert again["environment"]["uv"] == "uv 2.0" and again["env_digest"] != first["env_digest"]


def test_a_copied_run_log_keeps_the_run_own_time(harness: Path, monkeypatch: pytest.MonkeyPatch):
    """The suite-health report places a kept run log by its mtime, so the copy must not restamp it."""
    from agent_cli.dispatch import verification

    then = 1_700_000_000.0
    wait = verification.wait

    def backdated(handle):
        status = wait(handle)
        # The run checkout is `handle.root`; on Windows it sits under the drive's short Temp.
        logs = list(handle.root.glob(".agent/dispatches/*.verification/*/*"))
        assert any(log.name == "output.log" for log in logs)
        for log in logs:
            os.utime(log, (then, then))
        return status

    monkeypatch.setattr(verification, "wait", backdated)
    rec = verify_record.record(harness, "HEAD")
    copied = verify_record.store(harness) / "runs" / rec["run_id"] / "output.log"
    assert copied.stat().st_mtime == then


# A stand-in pytest: prints the summary it is given and, unless told `-`, writes the suite stats
# the runner names for this command.
FAKE_PYTEST = """#!/bin/sh
if [ -n "$AGENT_SUITE_STATS" ] && [ "$2" != - ]; then
  sed "s/SPAWNS/$2/" tools/stats.json > "$AGENT_SUITE_STATS"
fi
echo "$1"
"""
STATS = {
    "totals": {
        "tests": 2,
        "spawns": "SPAWNS",
        "test_seconds": 1.5,
        "cpu_children_s": 2.5,
        "opaque_share": 0.1,
    },
    "workers": {},
    "tests": [
        {"test": "tests/test_a.py::test_few", "spawns": 1, "s": 0.9},
        {"test": "tests/test_a.py::test_many", "spawns": 30, "s": 0.1},
    ],
}


def _fake_pytest(root: Path, summary: str, spawns: str) -> str:
    if not (root / "tools" / "pytest").is_file():
        (root / "tools").mkdir(exist_ok=True)
        (root / "tools" / "pytest").write_text(FAKE_PYTEST)
        (root / "tools" / "stats.json").write_text(json.dumps(STATS).replace('"SPAWNS"', "SPAWNS"))
        _git(root, "add", "tools")
        _git(root, "commit", "-q", "-m", "a stand-in pytest")
    return f"sh tools/pytest '{summary}' {spawns}"


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    from agent_cli import telemetry

    seen: list[dict] = []
    monkeypatch.setattr(telemetry, "record", lambda root, kind, **kw: seen.append({"kind": kind, **kw}))
    return seen


def test_a_new_record_carries_its_suite_cost_and_its_event_the_figures(harness: Path, events: list[dict]):
    _registry(harness, "echo lint ok", _fake_pytest(harness, "3 passed, 2 warnings in 0.01s", "40"))
    made = verify_record.record(harness, "HEAD")
    suite = made["suite"]
    assert (suite["command"], suite["tests"], suite["spawns"], suite["missing"]) == (1, 3, 40, [])
    assert (suite["test_seconds"], suite["cpu_children_s"], suite["opaque_share"]) == (1.5, 2.5, 0.1)
    assert suite["wall_s"] == made["commands"][1]["seconds"] and suite["extras"] == []
    assert [t["test"] for t in suite["top_spawns"]] == [
        "tests/test_a.py::test_many",
        "tests/test_a.py::test_few",
    ]
    assert [t["test"] for t in suite["top_seconds"]] == [
        "tests/test_a.py::test_few",
        "tests/test_a.py::test_many",
    ]
    assert sys.platform == "win32" or len(suite["loadavg"]) == 3
    run_dir = verify_record.store(harness) / "runs" / made["run_id"]
    stats = run_dir / "suite-1.json"
    assert made["evidence"]["suite-1.json"] == verify_record._file_sha(stats)
    assert "suite-0.json" not in made["evidence"]  # the lint command wrote none
    (data,) = [e["data"] for e in events if e["kind"] == "verify.record"]
    assert (data["tests"], data["spawns"], data["cpu_children_s"]) == (3, 40, 2.5)
    assert data["wall_s"] == suite["wall_s"]
    stats.write_text(stats.read_text().replace("40", "4"))
    assert "suite-1.json" in verify_record.lookup(harness, "HEAD")[1]


def test_missing_suite_stats_are_named_not_zeroed(harness: Path):
    _registry(harness, "echo lint ok", _fake_pytest(harness, "3 passed in 0.01s", "-"))
    suite = verify_record.record(harness, "HEAD")["suite"]
    assert (suite["tests"], suite["spawns"], suite["stats"]) == (3, None, "missing")
    assert suite["missing"] == ["command 1 wrote no suite stats"]


def test_an_extra_is_kept_apart_and_a_reuse_still_reports_the_full_run(harness: Path, events: list[dict]):
    """The extra's summary is the log's last and its stats would have overwritten the full run's."""
    _registry(harness, "echo lint ok", _fake_pytest(harness, "3 passed in 0.01s", "40"))
    extra = (_fake_pytest(harness, "12 passed in 0.01s", "5"),)
    made = verify_record.record(harness, "HEAD", extra=extra)
    again = verify_record.record(harness, "HEAD", extra=extra)
    assert again["reused"] is True and again["run_id"] == made["run_id"]
    for suite in (made["suite"], again["suite"]):
        assert (suite["tests"], suite["spawns"]) == (3, 40)
        (added,) = suite["extras"]
        assert (added["index"], added["tests"], added["spawns"], added["stats"]) == (2, 12, 5, "suite-2.json")
    reused = [e["data"] for e in events if e.get("outcome") == "reused"]
    assert [(d["tests"], d["spawns"]) for d in reused] == [(3, 40)]


def test_a_full_command_without_a_summary_never_borrows_an_extra_one(harness: Path):
    """`-qq` prints no summary; the extra's, the only one in the log, is not the full suite's."""
    _registry(harness, "echo lint ok", _fake_pytest(harness, "", "40"))
    made = verify_record.record(harness, "HEAD", extra=("echo 1 passed in 0.01s",))
    suite = made["suite"]
    assert (suite["tests"], suite["spawns"]) == (None, 40)
    assert suite["missing"] == ["command 1's output holds 0 pytest summaries, not one"]
    assert suite["extras"][0]["tests"] == 1


def test_a_summary_counts_failed_and_error_tests_but_not_warnings():
    log = (
        "== 1 failed, 5 passed, 2 skipped, 1 xfailed, 1 xpassed, 2 errors, 3 warnings in 3.21s ==\n"
        "12 passed, 1 deselected in 0.5s\n"
    )
    assert verify_record.summary_counts(log) == [12, 12]


def test_a_file_system_that_rejects_the_mtime_still_gets_the_evidence_bytes(
    harness: Path, monkeypatch: pytest.MonkeyPatch
):
    def refuse(*args, **kwargs):
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(verify_run.shutil, "copystat", refuse)
    rec = verify_record.record(harness, "HEAD")
    run_dir = verify_record.store(harness) / "runs" / rec["run_id"]
    assert (run_dir / "output.log").is_file() and rec["tier"] == "full"
