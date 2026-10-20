"""Keep the suite off the owner's real Obsidian.

On 2026-10-03 the owner saw several Obsidian icons appear in the macOS Dock while the suite
ran. The Obsidian CLI is the app binary itself (`/Applications/Obsidian.app/Contents/MacOS/
obsidian`, on PATH once the CLI is enabled), so any test that reached `obsidian eval`,
`obsidian version` or `obsidian open` without a stub started the app. `obsidian.cli_path()`
found it through PATH, `$OBSIDIAN_CLI` or the macOS install spots, and the tab drainer's
detached process (`tab_drain._spawn`) resolves it again outside every monkeypatch.

The guard, autouse from conftest:

- removes Obsidian app-bundle and Windows install directories from PATH;
- puts a recording `obsidian` and `open` first on PATH and points `$OBSIDIAN_CLI` at the
  recording `obsidian`;
- empties `obsidian.CLI_FALLBACKS`, the macOS install spots a GUI process cannot see on PATH;
- refuses, in process, any `subprocess.Popen` whose `executable=` or first argument is a real
  Obsidian, or that hands `open`/`xdg-open` an Obsidian target, and fails the test that tried.

Limits. "A real Obsidian" is what `REAL_OBSIDIAN` matches: a path inside a macOS
`Obsidian.app` bundle, or a Windows `obsidian.exe` or `obsidian.com`. A plain Linux `obsidian` binary or an
AppImage is not recognised by path; there the PATH stub and `$OBSIDIAN_CLI` are the only
protection. Child processes are covered only while they inherit the guard's environment and
PATH, as the detached tab drainer does. A child started with a replaced `env=` or PATH, outside
this process's Popen, is not covered.

A test that wants the CLI points `$OBSIDIAN_CLI` (or `cli_path`) at its own fake, as before.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# A real Obsidian: a path inside a macOS `Obsidian.app` bundle, or a Windows `obsidian.exe` or
# the `obsidian.com` console CLI beside it. Not a plain Linux `obsidian` binary or an AppImage
# (see the module docstring).
REAL_OBSIDIAN = re.compile(r"obsidian\.app([/\\]|$)|(^|[/\\])obsidian\.(exe|com)$", re.IGNORECASE)
OPENERS = {"open", "xdg-open", "start", "explorer", "explorer.exe", "cmd", "cmd.exe"}

_real_which = shutil.which
_RealPopen = subprocess.Popen

# Records every call as <name>-<pid>-<n>.json in $OBSIDIAN_STUB_DIR and exits 0 with no output,
# the answer a CLI gives when it found nothing to do.
RECORDING_STUB = """#!{python}
import json, os, sys
d = os.environ.get("OBSIDIAN_STUB_DIR") or os.path.dirname(os.path.abspath(sys.argv[0]))
name = os.path.basename(sys.argv[0])
n = len([f for f in os.listdir(d) if f.startswith(name + "-")]) + 1
with open(os.path.join(d, "%s-%d-%d.json" % (name, os.getpid(), n)), "w") as fh:
    json.dump({{"argv": sys.argv}}, fh)
"""


@dataclass
class ObsidianGuard:
    """What a test reached: `stub_calls()` read the recording stubs, `blocked` the in-process
    attempts on a real Obsidian that were refused."""

    bin_dir: Path
    calls_dir: Path
    blocked: list[list[str]] = field(default_factory=list)

    @property
    def stub(self) -> Path:
        return self.bin_dir / "obsidian"

    def stub_calls(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.calls_dir.glob("*.json"))]


def _argv(args: Any) -> list[str]:
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    return [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in args]


def _real_executable(exe: str, env: dict[str, str] | None) -> str:
    """`exe`, or what it resolves to, when that is a real Obsidian; else ""."""
    resolved = exe
    # Only a bare `obsidian` can resolve into the app through PATH. Resolving every bare name
    # would also break the tests that set sys.platform to "win32", where `shutil.which` calls
    # the Windows API.
    if "obsidian" in exe.lower() and not ("/" in exe or "\\" in exe):
        try:
            resolved = _real_which(exe, path=(env or {}).get("PATH") if env is not None else None) or exe
        except Exception:  # noqa: BLE001 — a guard never breaks the spawn it inspects
            resolved = exe
    for candidate in (exe, resolved, os.path.realpath(resolved)):
        if REAL_OBSIDIAN.search(candidate):
            return candidate
    return ""


def obsidian_target(args: Any, executable: Any = None, env: dict[str, str] | None = None) -> str:
    """The real Obsidian this spawn would start, or "".

    An explicit `executable=` is what runs, whatever argv says, so it is checked first and on
    its own. Then a shell string is checked as text, and an argv by its first element."""
    if executable is not None and (found := _real_executable(os.fsdecode(executable), env)):
        return found
    argv = _argv(args)
    if not argv:
        return ""
    if len(argv) == 1 and " " in argv[0]:  # shell=True or a command line
        line = argv[0]
        return line if re.search(r"obsidian\.(app|exe|com)\b|obsidian://|-a\s+obsidian", line, re.I) else ""
    if found := _real_executable(argv[0], env):
        return found
    if Path(argv[0]).name.lower() in OPENERS and any("obsidian" in a.lower() for a in argv[1:]):
        return " ".join(argv)
    return ""


def _without_obsidian(path: str) -> str:
    """PATH without Obsidian's own directories, such as /Applications/Obsidian.app/Contents/MacOS
    or the Windows folder holding `obsidian.com`, which `shutil.which("obsidian")` finds through
    PATHEXT ahead of the extensionless stub."""
    return os.pathsep.join(p for p in path.split(os.pathsep) if p and not _holds_obsidian(p))


def _holds_obsidian(entry: str) -> bool:
    if REAL_OBSIDIAN.search(entry):
        return True
    try:
        return any((Path(entry) / name).is_file() for name in ("obsidian.com", "obsidian.exe"))
    except OSError:
        return False


def install(bin_dir: Path, calls_dir: Path, monkeypatch: pytest.MonkeyPatch) -> ObsidianGuard:
    """Put the guard in place for one test; the caller fails the test on `guard.blocked`."""
    from agent_cli import obsidian

    calls_dir.mkdir(exist_ok=True)
    for name in ("obsidian", "open"):
        script = bin_dir / name
        script.write_text(RECORDING_STUB.format(python=sys.executable), encoding="utf-8")
        script.chmod(0o755)
    guard = ObsidianGuard(bin_dir, calls_dir)
    path = _without_obsidian(os.environ.get("PATH", ""))
    if not path.split(os.pathsep)[:1] == [str(bin_dir)]:
        path = f"{bin_dir}{os.pathsep}{path}"
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("OBSIDIAN_CLI", str(guard.stub))
    monkeypatch.setenv("OBSIDIAN_STUB_DIR", str(calls_dir))
    monkeypatch.setattr(obsidian, "CLI_FALLBACKS", ())

    class GuardedPopen(_RealPopen):  # type: ignore[type-arg]
        def __init__(self, args: Any, *rest: Any, **kw: Any) -> None:
            target = obsidian_target(args, kw.get("executable"), kw.get("env"))
            if target:
                guard.blocked.append(_argv(args))
                raise OSError(f"test guard: refused to start the real Obsidian ({target})")
            super().__init__(args, *rest, **kw)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)

    def startfile(target: Any, *_args: Any, **_kw: Any) -> None:  # Windows' native launch path
        guard.blocked.append(["startfile", str(target)])
        raise OSError(f"test guard: refused to start {target}")

    monkeypatch.setattr(os, "startfile", startfile, raising=False)

    # `obsidian_run` only calls the CLI while the app runs. Tests treat it as running, which is
    # what every CLI stub assumes, unless a test stubs `obsidian.app_running` to say otherwise.
    from agent_cli import obsidian_run

    real_app_running = obsidian.app_running
    monkeypatch.setattr(
        obsidian_run,
        "running",
        lambda: True if obsidian.app_running is real_app_running else obsidian.app_running(),
    )
    return guard
