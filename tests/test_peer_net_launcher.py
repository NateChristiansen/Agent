"""The forced command runs the real launcher with the exact argv in the account's real shell.

Import origins are inspected only in this wrapper, never by a switch in the launcher.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli.peer_net import authorized, launcher
from agent_cli.peer_net.errors import PeerNetError
from peer_net_helpers import envelope, raw

REPO = Path(__file__).resolve().parents[1]
BASE = os.path.realpath(getattr(sys, "_base_executable", sys.executable))


def test_unsafe_paths_and_aliases_are_refused():
    for bad in ('/a"b', "/a%PATH%", "/a!b", "/a&b", "/a|b", "/a$b", "/a`b", "/a\nb", "/a^b", "/a<b"):
        with pytest.raises(PeerNetError):
            launcher.forced_command(launcher.Launch(bad, "/s", "posix"), "mac")
    with pytest.raises(PeerNetError):
        launcher.forced_command(launcher.Launch("/py", "/s", "posix"), "mac; rm")


def test_homebrew_versioned_interpreter_passes_resolve_and_forced_command(tmp_path, monkeypatch):
    interpreter = tmp_path / "Cellar" / "python@3.14" / "3.14.2" / "bin" / "python3.14"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(launcher.sys, "_base_executable", str(interpreter))
    monkeypatch.setattr(launcher, "posix_shell", lambda: "posix")
    monkeypatch.setattr(launcher, "windows_shell", lambda: "cmd")
    launch = launcher.resolve(tmp_path)
    assert launch.interpreter == str(interpreter.resolve())
    assert launcher.forced_command(launch, "mac") == (
        f'"{launch.interpreter}" -I "{launch.script}" --from-device mac'
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
def test_the_command_string_gives_the_exact_argv_through_sh(tmp_path):
    spaced = tmp_path / "python@3.14 with space"
    spaced.mkdir()
    script = spaced / "peer-net-receive"
    script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    interpreter = spaced / "python@3.14"
    interpreter.symlink_to(BASE)
    command = launcher.forced_command(launcher.Launch(str(interpreter), str(script), "posix"), "mac")
    line = authorized.managed_line(command, b"\0" * 51, "mac", "a" * 32)
    entry = authorized.parse_line(line)
    unescaped = authorized.option_command(entry) if entry else None
    assert unescaped is not None and unescaped == command
    out = subprocess.run(["/bin/sh", "-c", unescaped], capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(out.stdout) == ["--from-device", "mac"]


@pytest.mark.skipif(os.name != "nt", reason="Windows sshd's default shell is cmd.exe")
def test_the_command_string_gives_the_exact_argv_through_cmd(tmp_path):
    spaced = tmp_path / "dir with space"
    spaced.mkdir()
    script = spaced / "peer-net-receive"
    script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    command = launcher.forced_command(launcher.Launch(BASE, str(script), "cmd"), "mac")
    out = subprocess.run(f'cmd.exe /s /c "{command}"', capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(out.stdout) == ["--from-device", "mac"]


def _link_dir(link: Path, target: Path) -> None:
    """A directory link; on Windows a junction, which needs no symlink privilege."""
    if sys.platform == "win32":
        subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
        )
    else:
        link.symlink_to(target)


WRAPPER = """
import json, runpy, sys
sys.argv = [sys.argv[1], "--from-device", "mac"]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as exc:
    code = exc.code
import sqlite3, threading
import agent_cli.peer_net.receiver as receiver
sys.stderr.write(json.dumps({"code": code, "sqlite3": sqlite3.__file__, "threading": threading.__file__,
                             "receiver": receiver.__file__}))
"""


@pytest.mark.skipif(not (REPO / ".venv").is_dir(), reason="needs the synced venv")
def test_the_real_launcher_answers_status_with_no_ambient_python_path(tmp_root, tmp_path):
    from peer_net_helpers import enable

    enable(tmp_root)
    # The launcher takes its root from its own location, so it runs from the fixture root, with
    # the real source and venv linked in (the harness guard refuses a linked `.agent`).
    copy = tmp_root
    shutil.copy(REPO / "bin" / "peer-net-receive", copy / "bin" / "peer-net-receive")
    if not (copy / "src").exists():
        _link_dir(copy / "src", REPO / "src")
    # The fixture root has a stub `.venv`; give it the real packages.
    packages = "Lib" if os.name == "nt" else "lib"
    (copy / ".venv").mkdir(exist_ok=True)
    if sys.platform == "win32":
        shutil.rmtree(copy / ".venv" / packages, ignore_errors=True)  # the stub holds only a `.pth`
    if not (copy / ".venv" / packages).exists():
        _link_dir(copy / ".venv" / packages, REPO / ".venv" / packages)
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(WRAPPER)
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "AGENT_ROOT"}}
    common = {k: envelope()[k] for k in ("v", "id", "sent_at", "expires_at")}
    out = subprocess.run(
        [BASE, "-I", str(wrapper), str(copy / "bin" / "peer-net-receive")],
        input=raw({**common, "verb": "status"}),
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert out.stdout.strip(), out.stderr.decode()[-2000:]
    answer = json.loads(out.stdout.decode().strip().splitlines()[-1])
    origins = json.loads(out.stderr.decode().strip().splitlines()[-1])
    assert answer == {"v": 1, "ok": True, "device": "windows", "enabled": True, "proto": 1}
    assert origins["code"] == 0
    assert origins["receiver"].startswith(str(REPO / "src")) or origins["receiver"].startswith(
        str(copy / "src")
    )
    assert ".venv" not in origins["sqlite3"] and ".venv" not in origins["threading"]  # the stdlib, kept


@pytest.mark.skipif(os.name == "nt", reason="POSIX login shell")
def test_an_unsupported_login_shell_is_refused(monkeypatch):
    import pwd

    class Entry:
        pw_shell = "/usr/local/bin/fish"

    monkeypatch.setattr(pwd, "getpwuid", lambda uid: Entry())
    with pytest.raises(PeerNetError) as caught:
        launcher.posix_shell()
    assert caught.value.code == "shell-unsupported"
