"""Setup's first-run question about where the notes repo goes (default to
../Agent-notes, but let people choose another path or point at an existing notes repo)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import notes
from agent_cli.cli import infra as cli_infra


@pytest.mark.parametrize(("answer", "expected"), [("", None), ("  ../Elsewhere  ", "../Elsewhere")])
def test_first_interactive_setup_asks_where_the_notes_repo_goes(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, answer: str, expected
):
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or answer)
    assert cli_infra.ask_notes_path(tmp_root) == expected
    assert prompts == ["Notes repo: "]


def test_the_prompt_offers_to_use_an_existing_default_repo(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """After a reclone the notes repo is already there; "create it" read wrong."""
    (notes.default_notes_root(tmp_root) / ".git").mkdir(parents=True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    cli_infra.ask_notes_path(tmp_root)
    printed = capsys.readouterr().out
    assert "use the existing one" in printed
    assert "create it at" not in printed


def test_a_later_interactive_setup_does_not_ask_again(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    notes.write_notes_root(tmp_root, tmp_root.parent / "harness-notes")
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked again"))
    assert cli_infra.ask_notes_path(tmp_root) is None


# #61: setup asks for the owner's name and a new agent.yaml starts with no example repo.


def _placeholder_yaml(path: Path) -> None:
    path.write_text(
        "version: 1\n# who this vault works for\nowner:\n  name: Your Name\n\nrepos: []\n", encoding="utf-8"
    )


def test_setup_asks_for_the_name_only_while_it_is_unset(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_cli import config_file

    _placeholder_yaml(tmp_root / "agent.yaml")
    monkeypatch.setattr("builtins.input", lambda prompt="": "  Ada   Lovelace ")
    assert cli_infra.ask_owner_name(tmp_root, None) == "Ada   Lovelace"
    assert config_file.set_owner_name(tmp_root, "Ada   Lovelace") == ("done", "owner is Ada Lovelace")
    text = (tmp_root / "agent.yaml").read_text(encoding="utf-8")
    assert '  name: "Ada Lovelace"\n' in text and "# who this vault works for" in text
    assert config_file.owner_name(tmp_root) == "Ada Lovelace"
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked again"))
    assert cli_infra.ask_owner_name(tmp_root, None) is None
    assert config_file.set_owner_name(tmp_root, "Someone Else")[0] == "already"


def test_a_name_in_the_notes_repo_is_not_asked_for_on_a_reclone(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    notes_repo = tmp_root.parent / "notes-here"
    notes_repo.mkdir()
    (notes_repo / "agent.yaml").write_text("version: 1\nowner:\n  name: Ada\nrepos: []\n", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked"))
    assert cli_infra.ask_owner_name(tmp_root, str(notes_repo)) is None


def test_a_new_agent_yaml_starts_with_no_example_repo(tmp_path: Path):
    import shutil

    import yaml

    from agent_cli import config_file, repos

    dest = tmp_path / "agent.yaml"
    shutil.copy(Path(__file__).resolve().parents[1] / "agent.example.yaml", dest)
    config_file.drop_example_repos(dest)
    data = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert data["repos"] == []
    assert repos.validate_registry_data(data) == []
    assert "example-service" not in dest.read_text(encoding="utf-8")


def test_an_unwritable_agent_yaml_is_a_failed_step_not_a_crash(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_cli import config_file

    _placeholder_yaml(tmp_root / "agent.yaml")

    def refuse(self, *args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "write_text", refuse)
    state, detail = config_file.set_owner_name(tmp_root, "Ada")
    assert state == "failed" and "cannot write agent.yaml" in detail
