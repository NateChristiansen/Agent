"""Shared config spelling diagnostics, strict validation, and example setup compatibility."""

import re
from pathlib import Path

import pytest
import yaml
from mcp import Client

from agent_cli import config_file, repos, repos_validate
from agent_cli.core import AgentError
from agent_cli.doctor import checks_repos
from agent_cli.mcp import server as mcp_server


@pytest.mark.parametrize(
    ("data", "path", "key", "hint"),
    [
        ({"wroker_mcp": {}}, "agent.yaml", "wroker_mcp", "worker_mcp"),
        ({"repos": [{"default_brach": "main"}]}, "repos[0]", "default_brach", "default_branch"),
        (
            {"repos": [{"dispatch": {"max_concurent": 1}}]},
            "repos[0].dispatch",
            "max_concurent",
            "max_concurrent",
        ),
    ],
)
def test_unknown_keys_name_the_level_and_closest_key(data, path, key, hint):
    [problem] = repos_validate.registry_unknown_keys(data)
    assert problem == f"{path}: unknown key {key!r}; did you mean {hint!r}?"


def test_dispatch_typo_is_rejected_instead_of_silently_using_the_cap_default():
    assert repos_validate.validate_dispatch({"max_concurent": 1}) == [
        "dispatch: unknown key 'max_concurent'; did you mean 'max_concurrent'?"
    ]


def test_unknown_diagnostics_do_not_print_values_and_handle_nonstring_keys():
    problems = repos_validate.registry_unknown_keys({42: "private-value", "zzzzzz": "private-value"})
    assert problems == ["agent.yaml: unknown key 42", "agent.yaml: unknown key 'zzzzzz'"]


def test_only_schema_keys_are_checked_not_environment_names_or_profile_names():
    data = {
        "repos": [{"name": "x", "path": "../repos/x", "dispatch": {"env": {"CUSTOM_NAME": "x"}}}],
        "worker_mcp": {"custom-profile": {"server": "custom", "command": "test-tool"}},
        "hooks": {"thrash-guard": False},
    }
    assert repos_validate.validate_registry_data(data) == []


def test_retired_unused_keys_are_unknown():
    problems = repos_validate.validate_registry_data(
        {"repos": [], "github_org": "unused", "evals": {"fixture_repo": "unused"}}
    )
    assert len(problems) == 2
    assert any("unknown key 'github_org'" in p for p in problems)
    assert any("unknown key 'evals'" in p for p in problems)


PUBLIC = {"name": "Pat Public", "email": "1+pat@users.noreply.github.com"}


def test_export_identity_is_a_shared_key():
    assert repos_validate.validate_registry_data({"repos": [], "export": {"identity": PUBLIC}}) == []


@pytest.mark.parametrize(
    "export",
    [
        "Pat Public",
        {"identity": "Pat Public"},
        {"identity": {"name": "Pat Public"}},
        {"identity": {"email": PUBLIC["email"]}},
        {"identity": {**PUBLIC, "name": 7}},
        {"identity": {**PUBLIC, "email": ["x@y"]}},
        {"identity": {**PUBLIC, "name": " "}},
        {"identity": {**PUBLIC, "name": "Pat <x>"}},
        {"identity": {**PUBLIC, "email": "no-at-sign"}},
        {"identity": {**PUBLIC, "extra": "x"}},
        {"identity": PUBLIC, "remote": "x"},
    ],
)
def test_a_malformed_export_identity_is_rejected(export: object):
    problems = repos_validate.validate_registry_data({"repos": [], "export": export})
    assert problems == [config_file.EXPORT_IDENTITY_SHAPE]


def test_export_identity_is_not_a_machine_override(tmp_path: Path):
    from agent_cli.ops.repos import repos_validate as validate

    (tmp_path / "agent.yaml").write_text("repos: []\n", encoding="utf-8")
    (tmp_path / "agent.local.yaml").write_text(
        "export: {identity: {name: Pat, email: p@x.example}}\n", encoding="utf-8"
    )
    with pytest.raises(AgentError, match="only.*supported"):
        validate(tmp_path)


def _typos(root: Path) -> None:
    (root / "agent.yaml").write_text(
        "frge: github\nrepos:\n  - name: x\n    path: ../repos/x\n"
        "    default_brach: main\n    dispatch: {max_concurent: 1}\n",
        encoding="utf-8",
    )


def test_doctor_warns_while_normal_loading_stays_compatible(tmp_path: Path, monkeypatch):
    _typos(tmp_path)
    monkeypatch.setattr(checks_repos, "closed_leftovers", lambda root: [])
    rows = checks_repos.check_registry(tmp_path)
    warnings = [r for r in rows if r["check"] == "agent.yaml unknown key"]
    assert len(warnings) == 3
    assert all(r["level"] == "warn" and "did you mean" in r["detail"] for r in warnings)
    assert not any(r["level"] == "fail" for r in rows)
    assert repos.load_registry(tmp_path).require("x").default_branch == "main"


def test_repos_validate_cli_errors_for_each_level_and_passes_after_correction(tmp_root: Path, run):
    _typos(tmp_root)
    result = run(["--json", "repos", "validate"])
    assert result.code == 1
    assert result.json["ok"] is False
    assert len(result.json["problems"]) == 3
    assert all("did you mean" in p for p in result.json["problems"])
    path = tmp_root / "agent.yaml"
    path.write_text(
        path.read_text()
        .replace("frge", "forge")
        .replace("default_brach", "default_branch")
        .replace("max_concurent", "max_concurrent"),
        encoding="utf-8",
    )
    result = run(["--json", "repos", "validate"])
    assert result.code == 0 and result.json == {"ok": True, "problems": []}


def test_repos_validate_checks_local_overrides_too(tmp_path: Path):
    from agent_cli.ops.repos import repos_validate as validate

    (tmp_path / "agent.yaml").write_text("repos: []\n", encoding="utf-8")
    (tmp_path / "agent.local.yaml").write_text("not_a_local_key: true\n", encoding="utf-8")
    with pytest.raises(AgentError, match="only.*supported"):
        validate(tmp_path)


@pytest.mark.anyio
async def test_repos_validate_mcp_is_read_only_and_returns_diagnostics(tmp_root: Path):
    _typos(tmp_root)
    server = mcp_server.build_server(tmp_root, "manager", ["repos_validate"])
    async with Client(server) as client:
        [tool] = (await client.list_tools()).tools
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        result = await client.call_tool("repos_validate", {})
    assert result.structured_content["ok"] is False
    assert len(result.structured_content["problems"]) == 3


def test_example_and_commented_dispatch_keys_validate_and_setup_keeps_no_example_repo(tmp_path: Path):
    text = (Path(__file__).resolve().parents[1] / "agent.example.yaml").read_text(encoding="utf-8")
    assert repos_validate.validate_registry_data(yaml.safe_load(text)) == []
    # Uncomment the optional dispatch settings to catch misspellings in the editing guide too.
    expanded = re.sub(r"^      # (?=[a-z_]+:)", "      ", text, flags=re.MULTILINE)
    assert repos_validate.validate_registry_data(yaml.safe_load(expanded)) == []
    path = tmp_path / "agent.yaml"
    path.write_text(text, encoding="utf-8")
    config_file.drop_example_repos(path)
    assert yaml.safe_load(path.read_text())["repos"] == []
    assert config_file.set_owner_name(tmp_path, "Example Owner")[0] == "done"
    assert config_file.owner_name(tmp_path) == "Example Owner"
