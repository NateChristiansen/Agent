"""Explicit MCP source selection only: no processes, accounts or native permission claims."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_cli import repos, worker_mcp
from agent_cli.core import AgentError
from agent_cli.dispatch import mcp_policy
from agent_cli.dispatch.mcp_policy import resolve

SECRET = "fixture-secret-never-in-diagnostics"


@pytest.fixture
def repo(tmp_path: Path) -> repos.Repo:
    clone = tmp_path / "clone"
    clone.mkdir()
    return repos.Repo("Product", "product", str(clone), "unused")


@pytest.fixture
def record() -> dict[str, Any]:
    return {"id": "001-01", "feature": 1, "tasks": [2, 3]}


def product(repo: repos.Repo, servers: Any) -> None:
    (Path(repo.path) / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))


def profiles(root: Path, value: Any) -> None:
    (root / "agent.yaml").write_text(yaml.safe_dump({"worker_mcp": value}))


@pytest.mark.parametrize(
    ("dispatch", "origin"),
    [({}, "default-absent"), ({"repo_mcp": None}, "default-null"), ({"repo_mcp": []}, "explicit-empty")],
)
def test_no_product_selection_does_not_read_even_broken_config(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict[str, Any],
    dispatch: dict,
    origin: str,
    monkeypatch: pytest.MonkeyPatch,
):
    repo.dispatch = dispatch
    (Path(repo.path) / ".mcp.json").write_text("invalid " + SECRET)
    original = mcp_policy._product_text

    def guarded(path, *args, **kwargs):
        assert Path(path).name != ".mcp.json", "unselected configuration must not be read"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(mcp_policy, "_product_text", guarded)
    result = resolve(tmp_path, repo, record)
    assert list(result.servers) == ["agent"]
    assert result.product_selection == origin
    assert result.preview()["strict_mcp_config"] is True
    assert result.servers["agent"]["env"]["AGENT_DISPATCH_TASKS"] == "2,3"


def test_exact_composition_provenance_and_no_tool_grant(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict[str, Any],
):
    profiles(
        tmp_path,
        {
            "qa": {
                "server": "browser",
                "command": "fixture-command",
                "env": {"TOKEN": SECRET},
                "allow": ["mcp__browser"],
            }
        },
    )
    repo.dispatch = {"repo_mcp": ["api"]}
    record["mcp"] = ["qa"]
    definition = {
        "type": "http",
        "url": f"https://fixture.invalid/{SECRET}",
        "headers": {"Authorization": SECRET},
    }
    product(repo, {"api": definition, "ignored": {"command": "never-loaded"}})
    result = resolve(tmp_path, repo, record)
    assert list(result.servers) == ["agent", "browser", "api"]
    assert result.servers["browser"] == worker_mcp.servers(tmp_path, record)["browser"]
    assert result.servers["api"] == definition
    assert result.sources[-1].selection_source == "agent.yaml#repos.product.dispatch.repo_mcp"
    assert result.sources[-1].definition_source == "clone:.mcp.json#mcpServers.api"
    assert result.sources[1].selection_source == "dispatch.mcp:qa"
    assert result.sources[1].definition_source == "agent.yaml#worker_mcp.qa"
    diagnostics = repr(result) + json.dumps(result.preview())
    assert SECRET not in diagnostics and "fixture.invalid" not in diagnostics
    assert "Authorization" not in diagnostics and "mcp__browser" not in diagnostics
    detached = result.config()
    detached["mcpServers"]["api"]["headers"]["Authorization"] = "changed"
    assert result.servers["api"]["headers"]["Authorization"] == SECRET
    assert json.loads((Path(repo.path) / ".mcp.json").read_text())["mcpServers"]["api"] == definition


@pytest.mark.parametrize("kind", ["stdio", "http", "sse"])
def test_supported_definition_shapes(tmp_path: Path, repo: repos.Repo, record: dict, kind: str):
    repo.dispatch = {"repo_mcp": ["selected"]}
    value = {"type": kind, "command": "fixture", "args": ["literal $ ` text"], "env": {"X": SECRET}}
    if kind != "stdio":
        value = {"type": kind, "url": "http://127.0.0.1:1/mcp", "headers": {"X": SECRET}}
    product(repo, {"selected": value, "invalid-but-unselected": None})
    assert resolve(tmp_path, repo, record).servers["selected"] == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "bad",
        {},
        {"command": ""},
        {"command": 1},
        {"command": "fixture", "args": SECRET},
        {"command": "fixture", "env": {"X": 1}},
        {"type": "http", "url": None},
        {"type": "http", "url": ""},
        {"type": "http", "url": "https://fixture.invalid", "headers": {"X": 1}},
        {"type": "unknown", "url": SECRET},
        {"type": "http", "url": "https://fixture.invalid", "command": "x"},
    ],
)
def test_invalid_selected_definition_is_sanitized(tmp_path: Path, repo: repos.Repo, record: dict, value):
    repo.dispatch = {"repo_mcp": ["bad"]}
    product(repo, {"bad": value})
    with pytest.raises(AgentError, match="invalid clone definition") as exc:
        resolve(tmp_path, repo, record)
    assert SECRET not in str(exc.value)


# Distinct shapes: a duplicate entry, a bare string where a list belongs, and an empty name.
# The five-value matrix was ten cases over one validator (2026-09-24).
@pytest.mark.parametrize("selection", [["a", "a"], "a", [""]])
@pytest.mark.parametrize("source", ["repo", "profiles"])
def test_bad_selections_reject_before_source_reads(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
    selection,
    source: str,
):
    if source == "repo":
        repo.dispatch = {"repo_mcp": selection}
    else:
        record["mcp"] = selection
    with pytest.raises(AgentError, match="selection|non-empty"):
        resolve(tmp_path, repo, record)


@pytest.mark.parametrize("selected", [["unknown"], ["a", "b"], ["reserved"], ["login"]])
def test_profile_unknown_collision_and_readiness_reject_without_leaking_hints(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
    selected: list[str],
):
    profiles(
        tmp_path,
        {
            "a": {"server": "same", "command": "x"},
            "b": {"server": "same", "command": "x"},
            "reserved": {"server": "agent", "command": "x"},
            "login": {"server": "login", "command": "x", "requires_file": SECRET, "requires_hint": SECRET},
        },
    )
    record["mcp"] = selected
    with pytest.raises(AgentError, match="selected worker MCP") as exc:
        resolve(tmp_path, repo, record)
    assert SECRET not in str(exc.value)


def test_ready_profile_with_no_product_selection(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    auth = tmp_path / "fixture-auth"
    auth.write_text(SECRET)
    profiles(tmp_path, {"ready": {"server": "browser", "command": "fixture", "requires_file": str(auth)}})
    record["mcp"] = ["ready"]
    original = Path.read_text

    def no_auth_read(path: Path, *args, **kwargs):
        assert path != auth, "readiness checks presence, not credential contents"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_auth_read)
    result = resolve(tmp_path, repo, record)
    assert list(result.servers) == ["agent", "browser"]
    assert SECRET not in repr(result)


@pytest.mark.parametrize(
    "contents", ["worker_mcp: [" + SECRET, "worker_mcp:\n  bad:\n    command: " + SECRET]
)
def test_profile_parse_and_validation_errors_do_not_echo_values(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
    contents: str,
):
    (tmp_path / "agent.yaml").write_text(contents)
    record["mcp"] = ["bad"]
    with pytest.raises(AgentError) as exc:
        resolve(tmp_path, repo, record)
    assert SECRET not in str(exc.value) and exc.value.__suppress_context__


@pytest.mark.parametrize("name", ["agent", "browser"])
def test_product_collisions_reject_even_identical_definitions(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
    name: str,
):
    profiles(tmp_path, {"qa": {"server": "browser", "command": "x"}})
    record["mcp"] = ["qa"]
    definition = resolve(tmp_path, repo, record).servers[name]
    product(repo, {name: definition})
    repo.dispatch = {"repo_mcp": [name]}
    with pytest.raises(AgentError, match="collides"):
        resolve(tmp_path, repo, record)


def test_no_user_or_manager_config_inheritance(tmp_path: Path, repo: repos.Repo, record: dict):
    (tmp_path / ".mcp.json").write_text('{"mcpServers":{"ambient":{"command":"never"}}}')
    (tmp_path / ".claude.json").write_text('{"mcpServers":{"ambient":{"command":"never"}}}')
    assert list(resolve(tmp_path, repo, record).servers) == ["agent"]


def test_unsafe_identifiers_are_redacted_in_preview_and_errors(
    tmp_path: Path, repo: repos.Repo, record: dict
):
    name = "https://fixture.invalid/" + SECRET
    repo.dispatch = {"repo_mcp": [name]}
    product(repo, {name: {"command": "fixture"}})
    result = resolve(tmp_path, repo, record)
    assert name in result.servers
    assert SECRET not in repr(result) + json.dumps(result.preview())
    product(repo, {})
    with pytest.raises(AgentError) as exc:
        resolve(tmp_path, repo, record)
    assert SECRET not in str(exc.value)


def test_registry_rejects_duplicate_product_selection():
    data = {
        "version": 1,
        "repos": [
            {
                "name": "Product",
                "short": "product",
                "path": "clone",
                "remote": "fixture",
                "dispatch": {"repo_mcp": ["a", "a"]},
            }
        ],
    }
    assert "repos[0] dispatch.repo_mcp must not repeat server names" in repos.validate_registry_data(data)


def test_each_resolution_observes_current_sources_and_returns_owned_data(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
):
    repo.dispatch = {"repo_mcp": ["a"]}
    product(repo, {"a": {"command": "first"}})
    first = resolve(tmp_path, repo, record)
    product(repo, {"a": {"command": "second"}})
    second = resolve(tmp_path, repo, record)
    assert first.servers["a"]["command"] == "first"
    assert second.servers["a"]["command"] == "second"
    record["tasks"].append(4)
    assert first.servers["agent"]["env"]["AGENT_DISPATCH_TASKS"] == "2,3"
    product(repo, {})
    with pytest.raises(AgentError, match="unknown"):
        resolve(tmp_path, repo, record)


def test_native_interpolation_stays_literal_without_environment_reads(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("FIXTURE_URL", SECRET)
    repo.dispatch = {"repo_mcp": ["api"]}
    definition = {"type": "http", "url": "${FIXTURE_URL}", "headers": {"X": "${FIXTURE_TOKEN}"}}
    product(repo, {"api": definition})
    result = resolve(tmp_path, repo, record)
    assert result.servers["api"] == definition
    assert SECRET not in repr(result.config())


@pytest.mark.parametrize(
    "definition",
    [
        {"command": "bad\0command"},
        {"command": "fixture", "args": ["bad\0argument"]},
        {"command": "fixture", "env": {"BAD\0NAME": "value"}},
        {"command": "fixture", "env": {"NAME": "bad\0value"}},
        {"command": "fixture", "env": {"BAD=NAME": "value"}},
        {"command": "fixture", "env": {"": "value"}},
    ],
)
@pytest.mark.parametrize("source", ["product", "profile"])
def test_impossible_process_values_refuse_before_compilation(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
    definition: dict,
    source: str,
):
    if source == "product":
        product(repo, {"a": definition})
        repo.dispatch = {"repo_mcp": ["a"]}
    else:
        profiles(tmp_path, {"a": {"server": "a", **definition}})
        record["mcp"] = ["a"]
    with pytest.raises(AgentError, match="invalid"):
        resolve(tmp_path, repo, record)
