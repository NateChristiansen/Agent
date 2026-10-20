"""The verifier's locale is pinned, so a pass record applies from any shell (C12).

A record keyed on an inherited locale did not apply in a shell with LANG alone after it was made in
one with LANG and LC_CTYPE, though nothing the tests saw had changed."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import repos as repos_mod
from agent_cli import verify_record
from agent_cli.dispatch.options import VERIFIER_LOCALE, verifier_env


def _repo(env: dict[str, object] | None = None) -> repos_mod.Repo:
    dispatch: dict[str, object] = {"env": env} if env else {}
    return repos_mod.Repo(name="Harness", short="harness", path=".", remote="x", dispatch=dispatch)


def test_the_shells_locale_never_reaches_the_verifier(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_CTYPE", "en_US.UTF-8")
    env = verifier_env(_repo())
    assert env["LANG"] == env["LC_ALL"] == VERIFIER_LOCALE
    assert "LC_CTYPE" not in env


def test_a_repo_may_still_declare_its_own_locale():
    env = verifier_env(_repo({"LC_ALL": "de_DE.UTF-8"}))
    assert env["LC_ALL"] == "de_DE.UTF-8"


def test_the_record_environment_is_the_same_from_either_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _repo()
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_CTYPE", "en_US.UTF-8")
    one = verify_record.environment(repo)["digest"]
    monkeypatch.delenv("LC_CTYPE")
    monkeypatch.setenv("LANG", "C")
    assert verify_record.environment(repo)["digest"] == one
