from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_cli.dispatch import options

WINDOWS_KEYS = {
    "USERPROFILE": r"C:\Users\w",
    "HOMEDRIVE": "C:",
    "HOMEPATH": r"\Users\w",
    "SYSTEMROOT": r"C:\WINDOWS",
    "SYSTEMDRIVE": "C:",
    "WINDIR": r"C:\WINDOWS",
    "COMSPEC": r"C:\WINDOWS\system32\cmd.exe",
    "PATHEXT": ".COM;.EXE",
    "TEMP": r"C:\Users\w\AppData\Local\Temp",
    "TMP": r"C:\Users\w\AppData\Local\Temp",
    "APPDATA": r"C:\Users\w\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\w\AppData\Local",
    "PROGRAMDATA": r"C:\ProgramData",
    "PROGRAMFILES": r"C:\Program Files",
    "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
    "PROCESSOR_ARCHITECTURE": "AMD64",
    "NUMBER_OF_PROCESSORS": "8",
    "OS": "Windows_NT",
}
RECORD = {"id": "001-01", "feature": 1}


def _run(monkeypatch: pytest.MonkeyPatch, platform: str, env: dict[str, str], allow: list[str] | None = None):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "environ", env)
    repo: Any = SimpleNamespace(env_allow=allow or [], dispatch={})
    return options.worker_env(Path("/r"), RECORD, repo)


def test_windows_names_reach_the_worker_under_upper_case_keys(monkeypatch: pytest.MonkeyPatch):
    env = _run(monkeypatch, "win32", {**WINDOWS_KEYS, "SECRET_TOKEN": "x"})
    assert set(WINDOWS_KEYS) <= set(env)
    assert "SECRET_TOKEN" not in env


def test_posix_admits_none_of_the_windows_names(monkeypatch: pytest.MonkeyPatch):
    env = _run(monkeypatch, "darwin", WINDOWS_KEYS)
    assert not set(WINDOWS_KEYS) & set(env)


def test_posix_base_match_stays_case_exact(monkeypatch: pytest.MonkeyPatch):
    env = _run(monkeypatch, "darwin", {"path": "/x", "PATH": "/y"}, allow=["Foo"])
    assert env["PATH"] == "/y"
    assert "path" not in env


def test_windows_base_and_env_allow_match_ignoring_case(monkeypatch: pytest.MonkeyPatch):
    env = _run(
        monkeypatch,
        "win32",
        {"Path": "p", "NPM_CONFIG_CACHE": "d", "PIP_CACHE_DIR": "n"},
        allow=["npm_config_*", "pip_cache_dir"],
    )
    assert env["Path"] == "p"
    assert env["NPM_CONFIG_CACHE"] == "d"
    assert env["PIP_CACHE_DIR"] == "n"


def test_env_allow_is_case_exact_on_posix(monkeypatch: pytest.MonkeyPatch):
    assert "NPM_CONFIG_CACHE" not in _run(
        monkeypatch, "linux", {"NPM_CONFIG_CACHE": "d"}, allow=["npm_config_*"]
    )


@pytest.mark.parametrize(
    ("platform", "name"),
    [
        ("win32", "CLAUDE_CODE_GIT_BASH_PATH"),
        ("win32", "claude_code_session_id"),
        ("win32", "Claude_X"),
        ("linux", "CLAUDE_CODE_GIT_BASH_PATH"),
    ],
)
def test_claude_names_stay_excluded(monkeypatch: pytest.MonkeyPatch, platform: str, name: str):
    env = _run(monkeypatch, platform, {name: "v"}, allow=["CLAUDE_*", name])
    assert not any(k.upper().startswith("CLAUDE") for k in env)


def test_codex_names_are_not_copied_by_default(monkeypatch: pytest.MonkeyPatch):
    env = _run(monkeypatch, "win32", {"CODEX_HOME": "h", "CODEX_API_KEY": "k"})
    assert not any(k.startswith("CODEX") for k in env)


def test_verifier_base_still_derives_from_worker_base():
    assert set(options.VERIFIER_ENV_BASE) == set(options.WORKER_ENV_BASE) - {"SSH_AUTH_SOCK"}


CLAUDE_CASES = {
    "claude_session_id": "a",
    "Claude_X": "b",
    "CLAUDE_SESSION_ID": "c",
    "CLAUDE_CODE_GIT_BASH_PATH": "d",
}


def _verifier(monkeypatch: pytest.MonkeyPatch, platform: str, env: dict[str, str], allow: list[str]):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "environ", env)
    return options.verifier_env(SimpleNamespace(env_allow=allow, dispatch={}))  # type: ignore[arg-type]


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_verifier_and_worker_never_admit_upper_case_claude(monkeypatch: pytest.MonkeyPatch, platform: str):
    allow = ["CLAUDE_*", "CLAUDE_CODE_GIT_BASH_PATH"]
    assert not any(
        k.upper() == "CLAUDE_SESSION_ID" for k in _verifier(monkeypatch, platform, CLAUDE_CASES, allow)
    )
    assert not any(k.upper() == "CLAUDE_SESSION_ID" for k in _run(monkeypatch, platform, CLAUDE_CASES, allow))
    assert "CLAUDE_CODE_GIT_BASH_PATH" not in _verifier(monkeypatch, platform, CLAUDE_CASES, allow)


def test_windows_verifier_and_worker_exclude_claude_in_any_case(monkeypatch: pytest.MonkeyPatch):
    allow = ["CLAUDE_*"]
    for env in (
        _verifier(monkeypatch, "win32", CLAUDE_CASES, allow),
        _run(monkeypatch, "win32", CLAUDE_CASES, allow),
    ):
        assert not any(k.upper().startswith("CLAUDE") for k in env)


def test_posix_exclusion_and_matching_stay_case_exact(monkeypatch: pytest.MonkeyPatch):
    env = {"claude_x": "a", "CLAUDE_Y": "b", "Foo": "c", "FOO": "d"}
    for got in (
        _verifier(monkeypatch, "linux", env, ["FOO", "claude_*"]),
        _run(monkeypatch, "linux", env, ["FOO", "claude_*"]),
    ):
        assert "CLAUDE_Y" not in got
        assert got["claude_x"] == "a"
        assert got["FOO"] == "d"
        assert "Foo" not in got
