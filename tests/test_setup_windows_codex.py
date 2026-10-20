"""Windows-only MXC trial, durable rollback and opt-in inline TUI; no real client home."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agent_cli import codex_profile
from agent_cli.cli import build_parser
from agent_cli.ops import infra


def _config(root: Path) -> dict:
    return tomllib.loads((root / codex_profile.CODEX_CONFIG_FILE).read_text(encoding="utf-8"))


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_platform_profile_keeps_permissions(tmp_root: Path, monkeypatch, system):
    monkeypatch.setattr(codex_profile.platform, "system", lambda: system)
    assert codex_profile.configure_codex_notes_root(tmp_root)[0] == "done"
    data = _config(tmp_root)
    if system == "Windows":
        assert data["windows"] == {"sandbox": "mxc"}
    else:
        assert "windows" not in data
    assert "tui" not in data
    assert data["features"] == {"network_proxy": True}
    assert data["default_permissions"] == "agent-manager"
    profile = data["permissions"]["agent-manager"]
    assert profile["extends"] == ":workspace"
    assert profile["network"]["enabled"] is True
    assert profile["filesystem"]
    assert codex_profile.configure_codex_notes_root(tmp_root)[0] == "already"


def test_rollback_and_inline_survive_setup(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(codex_profile.platform, "system", lambda: "Windows")
    infra._codex_config_step(tmp_root)
    assert _config(tmp_root)["windows"]["sandbox"] == "mxc"
    step = infra._codex_config_step(tmp_root, codex_sandbox="elevated", codex_inline=True)
    assert step["state"] == "done"
    assert 'sandbox = "elevated" under [windows] in the user config' in step["detail"]
    assert infra._codex_config_step(tmp_root)["state"] == "already"
    data = _config(tmp_root)
    assert data["windows"] == {"sandbox": "elevated"}
    assert data["tui"] == {"alternate_screen": "never"}
    assert infra._codex_config_step(tmp_root, codex_sandbox="mxc", codex_inline=False)["state"] == "done"
    assert _config(tmp_root)["windows"] == {"sandbox": "mxc"}
    assert "tui" not in _config(tmp_root)


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_windows_options_do_not_write_on_other_platforms(tmp_root: Path, monkeypatch, system):
    monkeypatch.setattr(codex_profile.platform, "system", lambda: system)
    codex_profile.configure_codex_notes_root(tmp_root, codex_sandbox="elevated", codex_inline=True)
    assert "windows" not in _config(tmp_root)
    assert "tui" not in _config(tmp_root)


@pytest.mark.parametrize("mode", ["unelevated", "disabled"])
def test_unsupported_sandbox_is_refused(tmp_root: Path, mode):
    assert codex_profile.configure_codex_notes_root(tmp_root, codex_sandbox=mode)[0] == "refused"
    assert not (tmp_root / codex_profile.CODEX_CONFIG_FILE).exists()


def test_owner_tables_survive_and_conflicts_refuse(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(codex_profile.platform, "system", lambda: "Windows")
    path = tmp_root / codex_profile.CODEX_CONFIG_FILE
    path.parent.mkdir(exist_ok=True)
    original = '[tui]\nalternate_screen = "auto"\nscreen_reader_detection_done = true\n'
    path.write_text(original, encoding="utf-8")
    assert codex_profile.configure_codex_notes_root(tmp_root)[0] == "done"
    before = path.read_bytes()
    assert codex_profile.configure_codex_notes_root(tmp_root, codex_inline=True)[0] == "refused"
    assert path.read_bytes() == before
    assert _config(tmp_root)["tui"]["alternate_screen"] == "auto"
    path.write_text('[windows]\nsandbox = "elevated"\n', encoding="utf-8")
    before = path.read_bytes()
    assert codex_profile.configure_codex_notes_root(tmp_root)[0] == "refused"
    assert path.read_bytes() == before


def test_setup_cli_forwards_windows_options(tmp_root: Path, monkeypatch):
    from agent_cli.core import Output

    args = build_parser().parse_args(["setup", "--codex-sandbox", "elevated", "--codex-inline"])
    seen = {}

    def setup(root, **kwargs):
        seen.update(kwargs)
        return infra.Op({}, "")

    monkeypatch.setattr("agent_cli.cli.infra.ops.setup", setup)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    from agent_cli.cli.infra import setup_cmd

    setup_cmd(tmp_root, args, Output())
    assert seen["codex_sandbox"] == "elevated"
    assert seen["codex_inline"] is True
    assert build_parser().parse_args(["setup", "--no-codex-inline"]).codex_inline is False
    defaults = build_parser().parse_args(["setup"])
    assert defaults.codex_sandbox is None and defaults.codex_inline is None
