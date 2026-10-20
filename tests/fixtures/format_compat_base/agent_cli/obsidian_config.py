# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Any
from .records.narrow import as_dict, dict_list

TERMINAL_DATA = Path("plugins") / "terminal" / "data.json"
TERMINAL_RENDERER = "dom"
_TERMINAL_COMMON: dict[str, Any] = {
    "type": "integrated",
    "environment": [],
    "followTheme": True,
    "name": "",
    "restoreHistory": False,
    "rightClickAction": "copyPaste",
    "successExitCodes": ["0", "SIGINT", "SIGTERM"],
    "terminalOptions": {"documentOverride": None},
}
SHELLCOMMANDS_DATA = Path("plugins") / "obsidian-shellcommands" / "data.json"
_BASH_SHIM = "bin/agent "
_WINDOWS_SHIM = "bin\\agent.cmd "
DISABLED_PLUGINS = ("show-hidden-files",)
HOMEPAGE_DATA = Path("plugins") / "homepage" / "data.json"
HOMEPAGE_MODE_KEYS = ("openMode", "manualOpenMode")
HOMEPAGE_REPLACE_MODE = "Replace all open notes"
HOMEPAGE_KEEP_MODE = "Keep open notes"


def terminal_profiles(windows_python: str) -> dict[str, dict[str, Any]]:
    return {
        "agent-macos": {
            **_TERMINAL_COMMON,
            "name": "agent (zsh)",
            "executable": "/bin/zsh",
            "args": ["--login"],
            "platforms": {"darwin": True},
            "pythonExecutable": "python3",
            "useWin32Conhost": True,
        },
        "agent-linux": {
            **_TERMINAL_COMMON,
            "name": "agent (bash)",
            "executable": "/bin/bash",
            "args": ["-l"],
            "platforms": {"linux": True},
            "pythonExecutable": "python3",
            "useWin32Conhost": True,
        },
        "agent-windows": {
            **_TERMINAL_COMMON,
            "name": "agent (PowerShell)",
            "executable": "powershell.exe",
            "args": ["-NoLogo"],
            "platforms": {"win32": True},
            "pythonExecutable": windows_python,
            "useWin32Conhost": True,
        },
    }


def terminal_settings(root: Path, odir: Path) -> None:
    from . import installs
    from .obsidian import read_json, write_json

    path = odir / TERMINAL_DATA
    data = read_json(path)
    ours = terminal_profiles(str(installs.terminal_python_path(root, "Windows").resolve()))
    theirs = {k: v for k, v in as_dict(data.get("profiles")).items() if k not in ours}
    updated = {**data, "preferredRenderer": TERMINAL_RENDERER, "profiles": {**ours, **theirs}}
    if updated != data or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, updated)


def shellcommands_windows(odir: Path) -> bool:
    from .obsidian import read_json, write_json

    path = odir / SHELLCOMMANDS_DATA
    data = read_json(path)
    changed = False
    for command in dict_list(data.get("shell_commands")):
        variants = as_dict(command.get("platform_specific_commands"))
        default = variants.get("default")
        if isinstance(default, str) and default.startswith(_BASH_SHIM):
            windows = _WINDOWS_SHIM + default[len(_BASH_SHIM) :]
            if variants.get("win32") != windows:
                variants["win32"] = windows
                command["platform_specific_commands"] = variants
                changed = True
    if changed:
        write_json(path, data)
    return changed


def homepage_keeps_tabs(odir: Path) -> bool:
    from .obsidian import read_json, write_json

    path = odir / HOMEPAGE_DATA
    data = read_json(path)
    changed = False
    for value in as_dict(data.get("homepages")).values():
        page = as_dict(value)
        for key in HOMEPAGE_MODE_KEYS:
            if page.get(key) == HOMEPAGE_REPLACE_MODE:
                page[key] = HOMEPAGE_KEEP_MODE
                changed = True
    if changed:
        write_json(path, data)
    return changed


def enforce(root: Path, odir: Path) -> list[str]:
    from .obsidian import OBSIDIAN_DIR

    terminal_settings(root, odir)
    touched = [TERMINAL_DATA]
    if shellcommands_windows(odir):
        touched.append(SHELLCOMMANDS_DATA)
    if homepage_keeps_tabs(odir):
        touched.append(HOMEPAGE_DATA)
    return [(OBSIDIAN_DIR / rel).as_posix() for rel in touched]
