# ruff: noqa
from __future__ import annotations
import importlib
import os
import platform
import tomllib
from pathlib import Path
from ..records.narrow import dig
from .check import Check, new_check


def windows_build() -> tuple[int, int] | None:
    try:
        registry = importlib.import_module("winreg")
        with registry.OpenKey(
            registry.HKEY_LOCAL_MACHINE,
            "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion",
            0,
            registry.KEY_READ | registry.KEY_WOW64_64KEY,
        ) as key:
            build = int(registry.QueryValueEx(key, "CurrentBuild")[0])
            revision = int(registry.QueryValueEx(key, "UBR")[0])
    except ImportError, OSError, TypeError, ValueError:
        return None
    return (build, revision)


def _sandbox(path: Path) -> tuple[str | None, str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (None, "")
    except OSError, UnicodeError, tomllib.TOMLDecodeError:
        return (None, f"{path}: unreadable or invalid TOML")
    windows = data.get("windows", {})
    if not isinstance(windows, dict):
        return (None, f"{path}: invalid windows table")
    mode = dig(data, "windows", "sandbox")
    if mode is None:
        return (None, "")
    if mode not in ("mxc", "elevated", "unelevated"):
        return (None, f"{path}: unrecognized windows.sandbox")
    return (str(mode), "")


def rows(root: Path) -> list[Check]:
    if platform.system() != "Windows":
        return []
    home = os.environ.get("CODEX_HOME")
    user_path = (Path(home).expanduser() if home else Path.home() / ".codex") / "config.toml"
    project_path = root / ".codex" / "config.toml"
    user, user_error = _sandbox(user_path)
    project, project_error = _sandbox(project_path)
    errors = [e for e in (user_error, project_error) if e]
    detail = f"user ({user_path}) = {user or 'unset'}; project ({project_path}) = {project or 'unset'}; "
    level = "ok"
    if errors:
        level = "warn"
        detail += "effective sandbox unknown; " + "; ".join(errors)
    elif project:
        detail += f"effective sandbox = {project} (expected: project wins over user in a trusted project; an assumption, verify in a fresh Codex session and sandbox logs)"
        if user == "elevated" and project != user:
            level = "warn"
            detail += f'; user config says elevated and the project key may not win. If it does not, set sandbox = "{project}" under [windows] in {user_path} and restart Codex'
    elif user:
        detail += f"effective sandbox = {user} (user wins; project key unset)"
    else:
        level = "warn"
        detail += "effective sandbox = client default (neither layer selects a mode)"
    if "unelevated" in (user, project):
        level = "warn"
        detail += "; unelevated is outside the supported trial; use mxc or elevated"
    detail += "; launch overrides and managed policy are not inspected"
    build = windows_build()
    minimum = "24H2 >= 26100.9278 or 25H2 >= 26200.9278"
    supported = build is not None and build[0] in (26100, 26200) and (build[1] >= 9278)
    build_detail = (
        f"Windows build {build[0]}.{build[1]}: MXC OS threshold {('met' if supported else 'not met')}"
        if build
        else "Windows build unknown: MXC OS support unverified"
    )
    return [
        new_check("Codex Windows sandbox", level, detail),
        new_check(
            "Codex MXC support",
            "ok" if supported else "warn",
            f"{build_detail}; requires {minimum}; capabilities and policy still need a live probe",
        ),
    ]
