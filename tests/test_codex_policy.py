"""Preview and RPC launch settings use one resolved Codex policy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent_cli.core import AgentError
from agent_cli.dispatch import codex_policy
from agent_cli.dispatch.options import codex_options_preview


def _repo(**dispatch: Any) -> Any:
    return SimpleNamespace(dispatch=dispatch)


def test_programmatic_repo_cannot_enable_network_with_a_string():
    with pytest.raises(AgentError, match="dispatch.codex_network_access"):
        codex_policy.policy_for({}, _repo(codex_network_access="false"), writable=[])


def test_preview_names_codex_and_matches_rpc_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_policy, "command", lambda *args: ["/trusted/codex", *args])
    d = {"budget_usd": 1, "model": "gpt-6-luna"}
    repo = _repo(codex_network_access=True)
    policy = codex_policy.policy_for(d, repo, writable=[])
    preview = codex_options_preview(d, repo, resume=True)
    assert preview["cli_path"] == "/trusted/codex"
    assert preview["command"] == ["/trusted/codex", "app-server", "--stdio"]
    assert preview["transport"] == "app-server --stdio"
    assert preview["approval_policy"] == policy.thread_params()["approvalPolicy"]
    assert preview["sandbox_policy"] == policy.turn_params()["sandboxPolicy"]
    assert preview["sandbox_policy"]["networkAccess"] is True
    assert preview["resume"] is True


def test_missing_binary_is_visible_and_cannot_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    def absent(*_args: str) -> list[str]:
        raise FileNotFoundError("Codex is not on PATH")

    monkeypatch.setattr(codex_policy, "command", absent)
    policy = codex_policy.policy_for({}, _repo(), writable=[])
    assert policy.preview()["cli_path"] is None
    assert policy.preview()["executable_error"] == "Codex is not on PATH"
    with pytest.raises(AgentError, match="not on PATH"):
        policy.server_command()


def test_named_profile_omits_rpc_sandbox_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_policy, "command", lambda *args: ["/trusted/codex", *args])
    overrides = ("-c", 'default_permissions="worker"')
    policy = codex_policy.policy_for(
        {},
        _repo(),
        writable=["/candidate"],
        filesystem_permissions=(("/candidate", "write"), ("/main", "read")),
        permissions_profile="worker",
        approval_policy="never",
        config_overrides=overrides,
    )
    assert policy.thread_params() == {"approvalPolicy": "never"}
    assert policy.turn_params() == {"approvalPolicy": "never"}
    assert policy.server_command("-c", "hooks={}") == (
        "/trusted/codex",
        "app-server",
        "--stdio",
        *overrides,
        "-c",
        "hooks={}",
    )
    preview = policy.preview()
    assert preview["sandbox_policy"] is None
    assert preview["permissions_profile"] == "worker"
    assert preview["filesystem_permissions"] == {"/candidate": "write", "/main": "read"}
