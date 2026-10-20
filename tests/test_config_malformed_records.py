from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import features, mcp_servers, obsidian, obsidian_config, repos, tickets


def _registry(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.yaml").write_text(body, encoding="utf-8")
    return root


def test_registry_text_fields_that_yaml_reads_as_numbers_load_as_text(tmp_path: Path):
    root = _registry(tmp_path / "h", "repos:\n  - {name: 123, path: repos/x, remote: 7}\n")
    (repo,) = repos.load_registry(root).repos
    assert (repo.name, repo.short, repo.remote) == ("123", "123", "7")


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("x", 1),  # not an int: the default
        ('"2"', 2),  # a digit string
        ("2.0", 2),  # a whole float
    ],
)
def test_a_registry_version_that_is_not_an_int_reads_as_its_number_or_1(
    tmp_path: Path, text: str, version: int
):
    root = _registry(tmp_path / "h", f"version: {text}\nrepos: []\n")
    assert repos.load_registry(root).version == version


@pytest.mark.parametrize("value", ["soon", [45], True])
def test_a_repo_dispatch_number_that_is_not_a_number_reads_as_the_default(value: object):
    repo = repos.Repo(
        name="a", short="a", path="a", remote="r", dispatch={"timeout_minutes": value, "max_turns": value}
    )
    assert (repo.timeout_minutes, repo.max_turns) == (45.0, repos.DEFAULT_MAX_TURNS)


@pytest.mark.parametrize("key", ["allowed_tools", "env_allow"])
def test_a_repo_dispatch_string_where_a_list_belongs_gives_no_entries(key: str):
    repo = repos.Repo(name="a", short="a", path="a", remote="r", dispatch={key: "Bash"})
    assert getattr(repo, key) == []


def test_terminal_profiles_that_are_not_a_mapping_are_replaced(tmp_root: Path, tmp_path: Path):
    path = tmp_path / obsidian_config.TERMINAL_DATA
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"profiles": ["junk"]}), encoding="utf-8")
    obsidian_config.terminal_settings(tmp_root, tmp_path)
    profiles = json.loads(path.read_text(encoding="utf-8"))["profiles"]
    assert set(profiles) == set(obsidian_config.terminal_profiles("python"))


def test_shell_commands_that_are_not_mappings_are_skipped(tmp_path: Path):
    path = tmp_path / obsidian_config.SHELLCOMMANDS_DATA
    path.parent.mkdir(parents=True)
    good = {"platform_specific_commands": {"default": obsidian_config._BASH_SHIM + " status"}}
    bad_variants = {"platform_specific_commands": "bin/agent status"}
    path.write_text(json.dumps({"shell_commands": ["junk", bad_variants, good]}), encoding="utf-8")
    assert obsidian_config.shellcommands_windows(tmp_path) is True
    rows = json.loads(path.read_text(encoding="utf-8"))["shell_commands"]
    assert rows[2]["platform_specific_commands"]["win32"].startswith(obsidian_config._WINDOWS_SHIM)
    assert rows[:2] == ["junk", bad_variants]


def test_homepages_that_are_not_a_mapping_change_nothing(tmp_path: Path):
    path = tmp_path / obsidian_config.HOMEPAGE_DATA
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"homepages": ["Replace all open notes"]}), encoding="utf-8")
    assert obsidian_config.homepage_keeps_tabs(tmp_path) is False


def test_obsidian_lists_stored_as_strings_are_not_split_into_characters(tmp_root: Path):
    odir = tmp_root / ".obsidian"
    odir.mkdir(exist_ok=True)
    (odir / "appearance.json").write_text(json.dumps({"enabledCssSnippets": "abc"}), encoding="utf-8")
    (odir / "app.json").write_text(json.dumps({"userIgnoreFilters": "xyz"}), encoding="utf-8")
    obsidian.setup(tmp_root)
    snippets = json.loads((odir / "appearance.json").read_text(encoding="utf-8"))["enabledCssSnippets"]
    ignore = json.loads((odir / "app.json").read_text(encoding="utf-8"))["userIgnoreFilters"]
    assert snippets == list(obsidian.SNIPPET_NAMES)
    assert not {"a", "b", "c", "x", "y", "z"} & set(snippets + ignore)


def test_a_tickets_string_is_not_split_into_characters():
    assert tickets.tickets_of({"tickets": "T-1"}) == []


def test_a_links_string_is_not_split_into_characters():
    meta: dict[str, object] = {"links": "[[a]]"}
    features.add_links(meta, ["[[b]]"])
    assert meta["links"] == ["[[b]]"]


def test_manifest_dependencies_that_are_not_a_mapping_declare_nothing(tmp_root: Path):
    target = mcp_servers.manifest_dir(tmp_root)
    target.mkdir(parents=True, exist_ok=True)
    (target / "package.json").write_text(json.dumps({"dependencies": ["x"]}), encoding="utf-8")
    assert mcp_servers.declared(tmp_root) == {}
