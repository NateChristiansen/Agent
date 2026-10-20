"""Audit 10 plan §A1: replay W8 infrastructure faults through real supervisor acceptance."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import yaml

from agent_cli import ops, verify_record
from agent_cli.dispatch import verification
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.state import load
from agent_cli.dispatch.verification_evidence import CommandOutcome, Evidence, assess
from dispatch_helpers import _set_alpha_dispatch, droot  # noqa: F401
from test_dispatch_verification import _one_command, finish, ready  # noqa: F401
from test_verify_record import harness  # noqa: F401
from verification_fixtures import config, fixture, python_command  # noqa: F401


@pytest.fixture(params=["label", "path", "home", "assertion"])
def w8_case(request):
    if request.param == "label":
        return (
            "raise RuntimeError('no machine label was reserved')",
            "machine label reservation",
            "incomplete",
        )
    if request.param == "path":
        path = "C:/" + "x" * 264
        assert len(path) == 267
        return f"raise OSError(36, 'Filename too long', {path!r})", "path too long (MAX_PATH)", "incomplete"
    if request.param == "home":
        # Git Bash, which runs every Windows verify command, never passes an empty HOME on.
        return (
            "import os; e = os.environ; assert os.name == 'nt' or not e.get('HOME');"
            " assert not e.get('USERNAME') and not e.get('USERPROFILE'); raise KeyError('HOME')",
            "missing HOME",
            "incomplete",
        )
    return "assert 1 == 2", "exited 1", "failed"


def test_w8_replay_reaches_dispatch_show_and_verdict(ready, w8_case, run):
    """Plan §A1 probe: only infrastructure retires the supervisor-pass gate."""
    root, d = ready
    code, cause, expected = w8_case
    _set_alpha_dispatch(
        root,
        verify=[{"command": python_command(code), "timeout_seconds": 10}],
        verify_timeout_seconds=20,
        env={"HOME": "", "USERNAME": "", "USERPROFILE": ""},
    )
    gate.execute(root, d["id"], 1)
    completed = finish(root, load(root, d["id"]))
    assert completed["verification_result"]["status"] == expected
    assert cause in "; ".join(completed["verification_result"]["reasons"])
    if expected == "incomplete":
        assert completed["state"] == "done"
        assert "WARNING: verification incomplete" in completed["result"]["notes"]
    else:
        assert completed["state"] == "blocked"
    shown = run(["--root", str(root), "dispatch", "show", d["id"]])
    assert shown.code == 0
    assert cause in shown.out and f"recorded supervisor verification: {expected}" in shown.out
    verdict = ops.dispatch_show(root, dispatch_id=d["id"], consume=False).text
    assert cause in verdict
    if expected == "incomplete":
        assert "WARNING" in shown.out and "WARNING" in verdict
    assert f"[{completed['state']}]" in shown.out


def test_infrastructure_never_mints_a_landing_pass_record(harness, w8_case):
    code, cause, expected = w8_case
    path = harness / "agent.yaml"
    registry = yaml.safe_load(path.read_text())
    registry["repos"][0]["dispatch"].update(
        verify=[{"command": python_command(code), "timeout_seconds": 10}],
        env={"HOME": "", "USERNAME": "", "USERPROFILE": ""},
    )
    path.write_text(yaml.safe_dump(registry))
    with pytest.raises(verify_record.RecordFailed) as caught:
        verify_record.record(harness, "HEAD")
    assert caught.value.category == expected
    if expected == "incomplete":
        assert cause in str(caught.value)
    assert verify_record.lookup(harness, "HEAD")[0] is None


@pytest.mark.parametrize("bound", ["command", "total"])
def test_a_bound_hit_never_mints_a_record_even_when_commands_exited_zero(harness, monkeypatch, bound):
    run = verify_record.verify_run.run

    def exceeded(*args, **kwargs):
        outcome = run(*args, **kwargs)
        evidence = outcome["evidence"]
        assert evidence is not None and evidence.state == "complete"
        if bound == "total":
            outcome["evidence"] = replace(evidence, elapsed_seconds=31)
        else:
            commands = (*evidence.commands[:-1], replace(evidence.commands[-1], elapsed_seconds=21))
            outcome["evidence"] = replace(evidence, commands=commands, elapsed_seconds=25)
        return outcome

    monkeypatch.setattr(verify_record.verify_run, "run", exceeded)
    with pytest.raises(verify_record.RecordFailed, match="time bound") as caught:
        verify_record.record(harness, "HEAD")
    assert caught.value.category == "incomplete"
    assert verify_record.lookup(harness, "HEAD")[0] is None


def test_reverify_names_infrastructure_without_rewriting_history(ready, w8_case):
    from agent_cli.dispatch import reverify
    from test_dispatch_reverify import _blocked_by_dirt

    root, d = ready
    _blocked_by_dirt(root, d)
    code, cause, expected = w8_case
    _set_alpha_dispatch(
        root,
        verify=[{"command": python_command(code), "timeout_seconds": 10}],
        verify_timeout_seconds=20,
        env={"HOME": "", "USERNAME": "", "USERPROFILE": ""},
    )
    result = reverify.reverify(root, d["id"])["result"]
    assert result["status"] == expected and cause in "; ".join(result["reasons"])
    assert load(root, d["id"])["state"] == "blocked"


@pytest.mark.parametrize(
    "output",
    [
        "fatal: git exit 128",
        "pytest-of-unknown",
        "no machine label was reserved\nAssertionError: wrong answer",
        "Filename too long\nsrc/a.py:1:1: F401 unused import",
        "missing HOME\nF401 unused import",
        "MAX_PATH\nsrc/a.py:1:2 - error: wrong type\n1 error, 0 warnings",
        "FAILED tests/test_a.py::test_one - TypeError: missing required argument\n"
        "FAILED tests/test_b.py::test_two - OSError: [WinError 206] MAX_PATH exceeded",
        "E   AttributeError: 'NoneType' object has no attribute 'x'\nno machine label was reserved",
        "E   Failed: DID NOT RAISE <class 'AgentError'>\nmatch='no machine label was reserved'",
        "E   Failed: expected warning\nFilename too long",
        "src/a.py:1:1: UP035 deprecated import\nFilename too long",
        "UP035 deprecated import\nFilename too long",
        "MAX_PATH\nFAILED tests/test_a.py::test_one - RuntimeError: wrong answer",
        "no machine label was reserved\nERROR tests/test_a.py::test_one - ValueError: wrong answer",
        "FAILED tests/test_a.py::test_one - Failed: DID NOT RAISE <class 'AgentError'> "
        "with match='no machine label was reserved'",
        "FAILED tests/test_a.py::test_one - Failed: expected warning\nFilename too long",
        "FAILED tests/test_a.py::test_one - OSError: Filename too long\n"
        "ERROR tests/test_b.py::test_two - RuntimeError: wrong answer",
    ],
)
def test_unknown_or_mixed_failures_stay_failed(output):
    settings = config("pytest")
    from agent_cli.dispatch.verification_evidence import Binding, config_digest

    binding = Binding("001-01", 1, "a" * 40, config_digest(settings), "fixture")
    evidence = Evidence(1, binding, "interrupted", None, None, (CommandOutcome(0, "exited", 1, 1),), 2)
    assert (
        assess(evidence, expected=binding, config=settings, current_tree=None, output=output).status
        == "failed"
    )


@pytest.mark.parametrize(
    "output",
    [
        "FAILED tests/test_a.py::test_one - RuntimeError: no machine label was reserved",
        "ERROR tests/test_a.py::test_one - KeyError: 'HOME'",
        "FAILED tests/test_a.py::test_one - OSError: Filename too long\n"
        "ERROR tests/test_b.py::test_two - RuntimeError: no machine label was reserved",
    ],
)
def test_only_infrastructure_failures_in_the_summary_are_incomplete(output):
    from agent_cli.dispatch.verification_evidence import Binding, config_digest

    settings = config("pytest")
    binding = Binding("001-01", 1, "a" * 40, config_digest(settings), "fixture")
    evidence = Evidence(1, binding, "interrupted", None, None, (CommandOutcome(0, "exited", 1, 1),), 2)
    result = assess(evidence, expected=binding, config=settings, current_tree=None, output=output)
    assert result.infrastructure


@pytest.mark.parametrize("name", ["HOME", "USERNAME", "USERPROFILE"])
def test_identity_faults_and_w8_unknown_user_git_error_are_named(name):
    from agent_cli.dispatch.verification_evidence import Binding, config_digest

    settings = config("pytest")
    binding = Binding("001-01", 1, "a" * 40, config_digest(settings), "fixture")
    evidence = Evidence(1, binding, "interrupted", None, None, (CommandOutcome(0, "exited", 1, 1),), 2)
    for output in (f"KeyError: '{name}'", "pytest-of-unknown/repo: git returned non-zero exit status 128."):
        result = assess(evidence, expected=binding, config=settings, current_tree=None, output=output)
        assert result.infrastructure and name in result.reasons[0]


def test_pre_activation_interruption_is_durable_and_named(fixture, monkeypatch):
    from agent_cli.dispatch import verification_process as procs

    real_readable = procs.readable

    def interrupted(stream, timeout):
        if real_readable(stream, timeout):
            raise RuntimeError("controlled interruption after watchdog readiness, before activation")
        return False

    monkeypatch.setattr(procs, "readable", interrupted)
    result, life, launched, _, _, _ = _one_command(fixture, monkeypatch)
    assert result.outcome == "error" and life["phase"] == "cleanup"
    assert all(process.returncode is not None for process in launched.values())
    output = (fixture[0] / "output.log").read_text()
    from agent_cli.dispatch.verification_evidence import Binding, config_digest

    settings = config("printf ok")
    binding = Binding("001-01", 1, fixture[2], config_digest(settings), "fixture")
    evidence = Evidence(1, binding, "interrupted", None, None, (result,), result.elapsed_seconds)
    checked = assess(evidence, expected=binding, config=settings, current_tree=None, output=output)
    assert checked.infrastructure and "before command activation" in checked.reasons[0]


@pytest.mark.parametrize("bound", ["command", "total", "activation"])
def test_command_and_total_bounds_finish_done_with_a_warning(ready, bound):
    """Use real terminal evidence, replacing only the measured bound outcome."""
    root, d = ready
    _set_alpha_dispatch(root, verify=[{"command": "true", "timeout_seconds": 10}], verify_timeout_seconds=20)
    gate.execute(root, d["id"], 1)
    current = load(root, d["id"])
    metadata = gate.metadata(current)
    assert metadata is not None
    binding, _ = metadata
    status = verification.load_status(root, binding)
    assert status.evidence is not None
    if bound == "command":
        evidence = replace(
            status.evidence, state="interrupted", commands=(CommandOutcome(0, "timeout", None, 10),)
        )
    elif bound == "total":
        evidence = replace(status.evidence, elapsed_seconds=21)
    else:
        evidence = replace(
            status.evidence, state="interrupted", commands=(CommandOutcome(0, "error", None, 1),)
        )
        (root / current["run_area"] / "output.log").write_text(
            "verification interrupted before command activation\n"
        )
    path = root / current["run_area"] / "terminal.json"
    path.write_text(json.dumps(evidence.to_dict()))
    result = finish(root, current)
    assert result["state"] == "done" and result["verification_result"]["status"] == "incomplete"
    assert ("before command activation" if bound == "activation" else "time bound") in result["result"][
        "notes"
    ]
