"""Build the forced SSH command from the real versioned interpreter with -I. Windows key-auth logons
can refuse uv's minor-version Python junction as an untrusted mount point; bin/peer-net-receive
adds the harness to sys.path.

Support only cmd.exe on Windows and sh, bash or zsh elsewhere. Refuse path characters requiring
escaping beyond double quotes, rather than guess shell quoting.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import valid_alias
from .errors import PeerNetError

SAFE_PATH_RE = re.compile(r"[A-Za-z0-9 _.:\\/()@-]+")
POSIX_SHELLS = {"sh", "bash", "zsh"}
LAUNCHER_REL = Path("bin") / "peer-net-receive"


@dataclass(frozen=True)
class Launch:
    interpreter: str
    script: str
    shell: str  # "cmd" or "posix"


def check_path(value: str) -> str:
    if not SAFE_PATH_RE.fullmatch(value):
        raise PeerNetError("unsafe-path", "launcher")
    return value


def windows_shell() -> str:
    """OpenSSH for Windows' DefaultShell registry value; cmd.exe when unset.

    Only a missing key or value means sshd falls back to cmd.exe. Any other failure (access
    denied, a value of the wrong type) leaves the shell unknown, and enrollment stops. Read
    through `winreg`, so no localized `reg` output is parsed."""
    import winreg  # type: ignore[import-not-found]  # Windows only

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\OpenSSH") as key:  # type: ignore[attr-defined]
            value, kind = winreg.QueryValueEx(key, "DefaultShell")  # type: ignore[attr-defined]
    except FileNotFoundError:
        return "cmd"
    except OSError:
        raise PeerNetError("shell-unknown", "shell") from None
    if kind not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} or not isinstance(value, str):  # type: ignore[attr-defined]
        raise PeerNetError("shell-unknown", "shell")
    if Path(value.strip()).name.lower() != "cmd.exe":
        raise PeerNetError("shell-unsupported", "shell")
    return "cmd"


def posix_shell() -> str:
    import pwd

    shell = Path(pwd.getpwuid(os.getuid()).pw_shell).name
    if shell not in POSIX_SHELLS:
        raise PeerNetError("shell-unsupported", "shell")
    return "posix"


def resolve(root: Path) -> Launch:
    """The interpreter and launcher the forced command names, checked."""
    base = os.path.realpath(getattr(sys, "_base_executable", sys.executable))
    st = os.lstat(base)
    reparse = getattr(st, "st_file_attributes", 0) & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if not os.path.isfile(base) or os.path.islink(base) or (os.name == "nt" and reparse):
        raise PeerNetError("interpreter", "launcher")
    venv = _venv_version(root)
    if venv and venv != f"{sys.version_info.major}.{sys.version_info.minor}":
        raise PeerNetError("interpreter", "launcher")
    script = str((root / LAUNCHER_REL).resolve())
    shell = windows_shell() if os.name == "nt" else posix_shell()
    return Launch(check_path(base), check_path(script), shell)


def _venv_version(root: Path) -> str:
    cfg = root / ".venv" / "pyvenv.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "version_info":
                return ".".join(value.strip().split(".")[:2])
    except OSError:
        return ""
    return ""


def forced_command(launch: Launch, alias: str) -> str:
    """The exact string for `command="..."`. Both paths are double-quoted; with the path check,
    that quoting means the same argv in cmd.exe and in a POSIX shell."""
    if not valid_alias(alias):
        raise PeerNetError("bad-value", "alias")
    return f'"{check_path(launch.interpreter)}" -I "{check_path(launch.script)}" --from-device {alias}'
