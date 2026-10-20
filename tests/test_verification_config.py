from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from agent_cli.core import AgentError
from agent_cli.repos import Repo, load_registry, verification_config
from agent_cli.repos_validate import validate_dispatch


def valid_dispatch():
    return {
        "verify": [
            {"command": "uv run pytest", "timeout_seconds": 20},
            {"command": "uv run ruff check src tests", "timeout_seconds": 10.5},
        ],
        "verify_timeout_seconds": 25,
    }


def test_verification_config_reads_the_runnable_commands():
    dispatch = valid_dispatch()
    repo = Repo("fixture", "fixture", "../fixture", "remote", dispatch=dispatch)
    config = repo.verification
    assert config is not None
    assert config.commands[0].command == "uv run pytest"
    assert config.commands[0].timeout_seconds == 20.0
    assert config.timeout_seconds == 25.0
    assert validate_dispatch(dispatch) == []
    # The total can intentionally stop the sequence before all per-command limits elapse.
    assert sum(command.timeout_seconds for command in config.commands) > config.timeout_seconds
    dispatch["verify"][0]["command"] = "changed"
    assert config.commands[0].command == "uv run pytest"
    with pytest.raises(FrozenInstanceError):
        setattr(config.commands[0], "command", "changed")  # noqa: B010 — test runtime immutability


def test_absent_verification_stays_explicitly_unconfigured():
    assert verification_config({}) is None
    assert validate_dispatch({}) == []


@pytest.mark.parametrize("value", [[r"^uv run pytest"], [], None, "pytest", ["("]])
def test_retired_required_checks_is_rejected_whenever_present(value: object):
    problems = validate_dispatch({**valid_dispatch(), "required_checks": value})
    assert len(problems) == 1
    assert problems[0].startswith("dispatch.required_checks is retired")
    assert "dispatch.verify" in problems[0] and "timeout_seconds" in problems[0]
    # Never translated: the regex does not become a verify command.
    assert verification_config({**valid_dispatch(), "required_checks": value}) == verification_config(
        valid_dispatch()
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "pytest",
        {},
        True,
        ["pytest"],
        [None],
        [{}],
        [{"command": "pytest"}],
        [{"timeout_seconds": 1}],
        [{"command": "pytest", "timeout_seconds": 1, "shell": "zsh"}],
    ],
)
def test_invalid_verify_entries(value):
    dispatch = valid_dispatch() | {"verify": value}
    assert any("dispatch.verify" in problem for problem in validate_dispatch(dispatch))
    with pytest.raises(ValueError):
        verification_config(dispatch)


@pytest.mark.parametrize("command", [None, True, 2, [], "", " \n ", "echo\x00oops"])
def test_invalid_command(command):
    dispatch = valid_dispatch() | {"verify": [{"command": command, "timeout_seconds": 1}]}
    assert "command" in validate_dispatch(dispatch)[0]


# One "finite positive number" branch, so three values covering the distinct failure classes:
# a bool that is also an int, a string that looks numeric, and a non-finite float. The ten-value
# × two-field matrix that used to be here was twenty cases of the same `isinstance` check
# (2026-09-24).
@pytest.mark.parametrize("value", [True, "3", float("nan")])
@pytest.mark.parametrize("field", ["command", "total"])
def test_invalid_verification_bounds(value, field):
    dispatch = valid_dispatch()
    if field == "command":
        dispatch["verify"][0]["timeout_seconds"] = value
    else:
        dispatch["verify_timeout_seconds"] = value
    assert "finite positive" in validate_dispatch(dispatch)[0]


def test_total_and_commands_are_required_together_and_command_cannot_exceed_total():
    assert validate_dispatch({"verify_timeout_seconds": 10})
    assert validate_dispatch({"verify": valid_dispatch()["verify"]})
    dispatch = valid_dispatch() | {"verify_timeout_seconds": 1}
    assert "must not exceed" in validate_dispatch(dispatch)[0]


def test_registry_loads_and_rejects_verification_config(tmp_path: Path):
    path = tmp_path / "agent.yaml"
    repo = {"name": "fixture", "path": "../fixture", "remote": "remote", "dispatch": valid_dispatch()}
    path.write_text(yaml.safe_dump({"repos": [repo]}))
    config = load_registry(tmp_path).repos[0].verification
    assert config and config.commands[0].command == "uv run pytest"
    repo["dispatch"]["verify"][0]["timeout_seconds"] = float("nan")
    path.write_text(yaml.safe_dump({"repos": [repo]}))
    with pytest.raises(AgentError, match=r"repos\[0\].*dispatch.verify"):
        load_registry(tmp_path)
