from __future__ import annotations

from ..launcher import is_windows

#: Added to the worker base on Windows only. Without `SystemRoot` Bun cannot make network
#: requests and without `TEMP`/`TMP` uv falls back to `C:\WINDOWS`, which is not writable.
WINDOWS_ENV_BASE = (
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "SystemRoot",
    "SystemDrive",
    "windir",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "ProgramData",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "OS",
)


def with_platform_names(base: tuple[str, ...]) -> tuple[str, ...]:
    return (*base, *WINDOWS_ENV_BASE) if is_windows() else base


def env_allowed(name: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """An allow entry is an exact variable name, or a `NAME_*` prefix wildcard. Windows variable
    names are case-insensitive and `os.environ` upper-cases them, so matching folds case there."""
    if is_windows():
        name = name.upper()
        patterns = [p.upper() for p in patterns]
    for pattern in patterns:
        if pattern.endswith("*"):
            if name.startswith(pattern[:-1]):
                return True
        elif name == pattern:
            return True
    return False


def claude_named(name: str) -> bool:
    return (name.upper() if is_windows() else name).startswith("CLAUDE")
