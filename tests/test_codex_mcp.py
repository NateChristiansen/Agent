"""Codex manager MCP registration preserves project config and uses the selected venv."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agent_cli import codex_mcp, notes
from agent_cli.doctor import checks_mcp


def test_configure_preserves_other_settings_and_replaces_only_managed_block(tmp_root: Path):
    path = tmp_root / codex_mcp.CONFIG
    path.parent.mkdir()
    path.write_text('model = "other"\n[mcp_servers.vendor]\nurl = "https://example.test/mcp"\n')

    assert codex_mcp.configure(tmp_root)[0] == "done"
    assert codex_mcp.configure(tmp_root)[0] == "already"
    parsed = tomllib.loads(path.read_text())
    assert parsed["model"] == "other"
    assert parsed["mcp_servers"]["vendor"]["url"] == "https://example.test/mcp"
    assert parsed["mcp_servers"]["agent"] == codex_mcp.expected(tmp_root)
    assert path.read_text().count(codex_mcp.BEGIN) == 1


def test_configure_refuses_unmanaged_agent_without_changing_it(tmp_root: Path):
    path = tmp_root / codex_mcp.CONFIG
    path.parent.mkdir()
    original = '[mcp_servers.agent]\ncommand = "custom"\n'
    path.write_text(original)
    state, detail = codex_mcp.configure(tmp_root)
    assert state == "refused"
    assert "unmanaged" in detail
    assert path.read_text() == original


@pytest.mark.parametrize("windows", [False, True])
def test_configure_upgrades_manager_ssh_agent_forwarding_without_storing_socket(
    tmp_root: Path, monkeypatch, windows: bool
):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/example-agent/socket")
    assert codex_mcp.configure(tmp_root, windows=windows)[0] == "done"
    path = tmp_root / codex_mcp.CONFIG
    original = path.read_text().replace('env_vars = ["SSH_AUTH_SOCK"]\n', "")
    path.write_text(original)

    assert codex_mcp.configure(tmp_root, windows=windows)[0] == "done"
    entry = tomllib.loads(path.read_text())["mcp_servers"]["agent"]
    assert entry["env_vars"] == ["SSH_AUTH_SOCK"]
    assert "/private/example-agent/socket" not in path.read_text()
    assert codex_mcp.configure(tmp_root, windows=windows)[0] == "already"


def test_configure_selects_native_windows_entry_point(tmp_root: Path):
    assert codex_mcp.configure(tmp_root, windows=True)[0] == "done"
    parsed = tomllib.loads((tmp_root / codex_mcp.CONFIG).read_text())
    entry = parsed["mcp_servers"]["agent"]
    # The venv's python, not the agent.exe shim `uv sync` must replace.
    assert entry["command"] == str((tmp_root / ".venv/Scripts/python.exe").resolve())
    assert entry["args"] == [
        "-I",
        "-m",
        "agent_cli",
        "--root",
        str(tmp_root.resolve()),
        "mcp",
        "--profile",
        "manager",
    ]
    posix = codex_mcp.expected(tmp_root, windows=False)
    assert Path(posix["command"]).parts[-2:] == ("bin", "agent") and posix["args"][0] == "--root"
    assert entry["cwd"] == str(tmp_root.resolve())


def test_notes_profile_regeneration_preserves_mcp_registration(tmp_root: Path):
    assert notes.configure_codex_notes_root(tmp_root, tmp_root.parent / "first-notes")[0] == "done"
    assert codex_mcp.configure(tmp_root)[0] == "done"
    assert notes.configure_codex_notes_root(tmp_root, tmp_root.parent / "second-notes")[0] == "done"
    parsed = tomllib.loads((tmp_root / codex_mcp.CONFIG).read_text())
    assert parsed["mcp_servers"]["agent"] == codex_mcp.expected(tmp_root)
    assert (
        parsed["permissions"]["agent-manager"]["filesystem"][
            str((tmp_root.parent / "second-notes").resolve())
        ]
        == "write"
    )


def test_doctor_probes_registered_server_and_reports_failure(tmp_root: Path, monkeypatch):
    assert codex_mcp.configure(tmp_root)[0] == "done"
    entry = codex_mcp.expected(tmp_root)
    executable = Path(str(entry["command"]))
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch()
    seen = []

    def probe(root, registered):
        seen.append((root, registered))
        return False, "read-only probe failed"

    monkeypatch.setattr(codex_mcp, "probe", probe)
    row = checks_mcp.check_codex_mcp_wiring(tmp_root)[0]
    assert row["level"] == "fail"
    assert "read-only probe failed" in row["detail"]
    assert seen == [(tmp_root, entry)]


def test_doctor_rejects_a_disabled_codex_server_before_probe(tmp_root: Path, monkeypatch):
    assert codex_mcp.configure(tmp_root)[0] == "done"
    path = tmp_root / codex_mcp.CONFIG
    path.write_text(
        path.read_text().replace("[mcp_servers.agent]\n", "[mcp_servers.agent]\nenabled = false\n")
    )
    monkeypatch.setattr(codex_mcp, "probe", lambda *_: pytest.fail("disabled server was probed"))
    row = checks_mcp.check_codex_mcp_wiring(tmp_root)[0]
    assert row["level"] == "fail"
    assert "disabled" in row["detail"]
