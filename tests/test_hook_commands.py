"""030 E: portable launch commands, strict probes, and stdin/exit preservation."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli import hooks

ROOT = Path(__file__).resolve().parents[1]
# The hook command both clients' configs carry (ADR-0031).
LOADER = (
    "node -e \"require(require('path').join(process.env.CLAUDE_PROJECT_DIR||require('child_process')"
    ".execFileSync('git',['rev-parse','--show-toplevel'],{encoding:'utf8'}).trim(),'bin','agent-hook.cjs'))\""
)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable stub; native CLI runs in Windows CI")
def test_slow_cli_preserves_bytes_and_exit_past_four_seconds(tmp_path: Path):
    root = tmp_path / "vault with spaces"
    launcher = root / "bin" / "agent-hook.cjs"
    launcher.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "bin/agent-hook.cjs", launcher)
    cli = root / ".venv" / "bin" / "agent"
    cli.parent.mkdir(parents=True)
    cli.write_text(
        f"#!{sys.executable}\nimport sys, time\ntime.sleep(4.2)\nsys.stdout.buffer.write(sys.stdin.buffer.read())\nsys.stderr.buffer.write(b'error bytes\\xff')\nsys.exit(7)\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    payload = b'{"stdin":"passed"}\n\xff'
    for command in (
        ["node", "-e", "require(" + json.dumps(str(launcher)) + ")"],
        ["node", str(launcher)],
    ):
        result = subprocess.run(
            [*command, "agent", "hook", "capture"],
            input=payload,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 7
        assert result.stdout == payload
        assert result.stderr == b"error bytes\xff"


def test_strict_probe_exposes_handler_exception(tmp_root: Path, run, monkeypatch):
    def broken(root, payload):
        raise RuntimeError("probe exposed this")

    monkeypatch.setitem(hooks.HANDLERS, "no-chaining", broken)
    result = run(["hook", "no-chaining", "--probe"])
    assert result.code != 0
    assert "probe exposed this" in result.err


def test_guard_payload_reaches_the_handler():
    command = LOADER + " agent hook no-chaining"
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "AGENT_ROOT")}
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status; git log -1"},
    }
    result = subprocess.run(
        command,
        shell=True,
        cwd=ROOT / "bin",
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_probe_does_not_call_external_effects(tmp_root: Path, run, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a pure probe attempted an external effect")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(Path, "home", forbidden)
    result = run(["hook", "no-chaining", "--probe"])
    assert result.code == 0 and result.json == {"probe": "no-chaining", "ok": True}


def test_lifecycle_handlers_cannot_be_probed(tmp_root: Path, run, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a lifecycle handler was executed by --probe")

    for name in hooks.NAMES:
        if name == "no-chaining":
            continue
        monkeypatch.setitem(hooks.HANDLERS, name, forbidden)
        result = run(["hook", name, "--probe"])
        assert result.code != 0 and "only no-chaining" in result.err


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("installed", [True, False])
def test_launcher_selects_the_native_cli_and_preserves_exit(platform, installed):
    # Drive the actual launcher with a mocked process boundary. A space in the root tests
    # argument arrays rather than shell interpolation. The real command test above covers stdin.
    script = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const [file, platform, installed] = process.argv.slice(1);
const results = [];
for (const direct of [false, true]) {
let called;
const module = {};
const fake = {argv: ['node', ...(direct ? [file] : []), 'agent', 'hook', 'capture', 'argument with spaces'], platform, execPath: '/node', env: {KEEP: 'yes'}, stderr: {write() {}}};
vm.runInNewContext(fs.readFileSync(file, 'utf8'), {
  __dirname: path.join('/vault with spaces', 'bin'), process: fake, module,
  require: Object.assign(function(name) {
    if (name === 'fs') return {existsSync: () => installed === 'true'};
    if (name === 'child_process') return {spawnSync(exe, args, options) {called = {exe, args, options}; return {status: 7};}};
    return require(name);
  }, {main: direct ? module : undefined}),
});
results.push({...called, exit: fake.exitCode});
}
console.log(JSON.stringify(results));
"""
    node = shutil.which("node")
    assert node is not None, "Node is a setup requirement"
    result = subprocess.run(
        [
            node,
            "-e",
            script,
            str(ROOT / "bin/agent-hook.cjs"),
            platform,
            str(installed).lower(),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    inline, direct = json.loads(result.stdout)
    assert direct == inline
    out = direct
    assert out["exit"] == (7 if installed else 0)
    assert out["options"]["stdio"] == ("inherit" if installed else ["inherit", "pipe", "pipe"])
    assert "timeout" not in out["options"], "the client's configured hook timeout governs"
    assert out["options"]["env"]["KEEP"] == "yes"
    assert out["options"]["env"]["AGENT_ROOT"].endswith("vault with spaces")
    if installed:
        # Windows runs the venv's python, never the agent.exe shim.
        assert out["exe"].endswith("python.exe" if platform == "win32" else "agent")
        module = ["-I", "-m", "agent_cli"] if platform == "win32" else []
        assert out["args"] == [*module, "hook", "capture", "argument with spaces"]
    else:
        assert out["exe"] == "uv"
        assert out["args"] == [
            "run",
            "--no-sync",
            "--project",
            out["options"]["env"]["AGENT_ROOT"],
            "agent",
            "hook",
            "capture",
            "argument with spaces",
        ]


@pytest.mark.skipif(os.name != "nt", reason="native Windows shells")
def test_command_also_runs_in_powershell():
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", LOADER + " agent hook no-chaining --probe"],
        cwd=ROOT / "bin",
        input="{}",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"probe": "no-chaining", "ok": True}
