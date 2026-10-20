"""Windows diagnostics read fixture configs and registry values, never the host client home."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import doctor
from agent_cli.doctor import checks_windows


@pytest.fixture
def windows(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(checks_windows.platform, "system", lambda: "Windows")
    monkeypatch.setattr(checks_windows, "windows_build", lambda: (26200, 9457))
    return home / ".codex" / "config.toml"


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("user", "project", "winner", "mode", "warning"),
    [
        ("elevated", "mxc", "project", "mxc", True),
        ("mxc", "elevated", "project", "elevated", False),
        ("elevated", "elevated", "project", "elevated", False),
        ("elevated", None, "user", "elevated", False),
        (None, "mxc", "project", "mxc", False),
        (None, None, "neither", "client default", True),
    ],
)
def test_config_layers(tmp_root: Path, windows, user, project, winner, mode, warning):
    if user:
        _write(windows, f'[windows]\nsandbox = "{user}"\n')
    if project:
        _write(tmp_root / ".codex/config.toml", f'[windows]\nsandbox = "{project}"\n')
    row = checks_windows.rows(tmp_root)[0]
    assert row["level"] == ("warn" if warning else "ok")
    assert f"effective sandbox = {mode}" in row["detail"]
    assert winner in row["detail"]
    assert f"user ({windows}) = {user or 'unset'}" in row["detail"]
    if user == "elevated" and project == "mxc":
        assert "project key may not win" in row["detail"]
        assert "an assumption, verify" in row["detail"]


def test_codex_home_override(tmp_root: Path, tmp_path: Path, windows, monkeypatch):
    _write(windows, '[windows]\nsandbox = "elevated"\n')
    custom = tmp_path / "custom-codex"
    _write(custom / "config.toml", '[windows]\nsandbox = "mxc"\n')
    monkeypatch.setenv("CODEX_HOME", str(custom))
    row = checks_windows.rows(tmp_root)[0]
    assert "effective sandbox = mxc" in row["detail"]
    assert str(custom) in row["detail"]
    assert windows.read_text(encoding="utf-8").endswith('sandbox = "elevated"\n')


@pytest.mark.parametrize("layer", ["user", "project"])
@pytest.mark.parametrize("text", ["[windows", 'windows = "wrong"', "[windows]\nsandbox = 42"])
def test_bad_config_does_not_claim_an_effective_mode(tmp_root: Path, windows, layer, text):
    path = windows if layer == "user" else tmp_root / ".codex/config.toml"
    _write(path, text)
    row = checks_windows.rows(tmp_root)[0]
    assert row["level"] == "warn"
    assert "effective sandbox unknown" in row["detail"]


@pytest.mark.parametrize(
    ("build", "supported"),
    [
        ((26100, 9277), False),
        ((26100, 9278), True),
        ((26100, 9457), True),
        ((26200, 9277), False),
        ((26200, 9278), True),
        ((26200, 9457), True),
        ((26000, 9999), False),
        (None, False),
    ],
)
def test_os_threshold(tmp_root: Path, windows, monkeypatch, build, supported):
    monkeypatch.setattr(checks_windows, "windows_build", lambda: build)
    row = checks_windows.rows(tmp_root)[1]
    assert row["level"] == ("ok" if supported else "warn")
    assert "26100.9278" in row["detail"] and "26200.9278" in row["detail"]
    if build:
        assert f"{build[0]}.{build[1]}" in row["detail"]
    else:
        assert "unknown" in row["detail"]


def test_registry_reads_ubr_not_osversion(monkeypatch):
    queried = []

    def query(key, name):
        queried.append(name)
        return {"CurrentBuild": "26200", "UBR": 9457}[name], 1

    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE=1,
        KEY_READ=2,
        KEY_WOW64_64KEY=4,
        OpenKey=lambda *args: nullcontext("key"),
        QueryValueEx=query,
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    assert checks_windows.windows_build() == (26200, 9457)
    assert queried == ["CurrentBuild", "UBR"]

    def denied(*args):
        raise PermissionError

    registry.OpenKey = denied
    assert checks_windows.windows_build() is None


def test_doctor_includes_windows_rows(tmp_root: Path, windows):
    names = {r["check"] for r in doctor.run(tmp_root)}
    assert {"Codex Windows sandbox", "Codex MXC support"} <= names


def test_non_windows_never_reads_configs(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(checks_windows.platform, "system", lambda: "Darwin")

    def forbidden():
        pytest.fail("non-Windows diagnostics must not read a Codex home")

    monkeypatch.setattr(Path, "home", forbidden)
    assert checks_windows.rows(tmp_root) == []
