"""Closing a manager tab on Windows left PowerShell, node and Codex running:
the terminal plugin kills only its direct child, the session conhost. The agent-terminal-focus
plugin sweeps for manager PowerShell chains whose console is already gone and ends each with
`taskkill /T`, and never touches a chain whose conhost is alive."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "harness/obsidian/plugins/agent-terminal-focus/main.js"

MARK = "0f8e2c4a-1b2c-4d3e-8f90-123456789abc"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

# Stubs `obsidian` and `child_process`, loads the plugin on the given platform, and runs one
# scenario: `pure` calls orphanChains, `load` runs onload and lets the load-time sweep finish.
HARNESS = r"""
const Module = require('module');
const [pluginPath, platform, mode, payload] = process.argv.slice(1);
Object.defineProperty(process, 'platform', {value: platform});
const calls = [], notices = [], intervals = [], events = [];
const input = JSON.parse(payload);
// each process query answers with the next snapshot; the last one repeats
const snapshots = input.snapshots || [input.processes];
let queries = 0;
class Plugin {
  constructor(app) { this.app = app; }
  addRibbonIcon() {} registerEvent() {} registerDomEvent() {} register() {}
  registerObsidianProtocolHandler() {}
  registerInterval(id) { return id; }
}
const load = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'obsidian') return {addIcon() {}, Modal: class {}, Notice: class { constructor(m) { notices.push(m); } }, Plugin};
  if (request === 'child_process') return {execFile(cmd, args, opts, cb) {
    calls.push({cmd, args, timeout: opts.timeout});
    const child = {pid: input.selfPid};
    if (cmd === 'powershell.exe') {
      setImmediate(() => input.queryFails
        ? cb(Object.assign(new Error('Command failed'), {code: 1}), '', 'Access denied')
        : cb(null, JSON.stringify(snapshots[Math.min(queries++, snapshots.length - 1)]), ''));
    } else {
      setImmediate(() => cb(null, 'SUCCESS', ''));
    }
    return child;
  }};
  return load.call(this, request, ...rest);
};
globalThis.window = {setInterval(fn, ms) { intervals.push(ms); return intervals.length; }};
globalThis.document = {};
const app = {workspace: {getLeavesOfType: () => [], onLayoutReady(fn) { fn(); },
                         on(name) { events.push(name); return {}; }},
             vault: {adapter: {getBasePath: () => 'C:\\vault'}}};
const Klass = require(pluginPath);
(async () => {
  if (mode === 'pure') {
    console.log(JSON.stringify({pids: Klass.orphanChains(input.processes, input.selfPid).map((c) => c.p)}));
    return;
  }
  if (mode === 'manager') {
    const leaf = {getViewState: () => ({type: 'terminal:terminal', state: {'terminal:terminal': {profile: input.profile}}})};
    console.log(JSON.stringify({manager: Klass.isManagerLeaf(leaf)}));
    return;
  }
  const plugin = new Klass(app);
  plugin.onload();  // the stub layout is ready at once, so the load-time sweep runs now
  await new Promise((r) => setTimeout(r, 30));
  console.log(JSON.stringify({calls, notices, intervals, events}));
})();
"""


def _node(platform: str, mode: str, payload: dict) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        ["node", "-e", HARNESS, str(PLUGIN), platform, mode, json.dumps(payload)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])  # after any console.info line


def _line(client: str, marker: str) -> str:
    # the exact shape Windows reported for a ribbon tab's PowerShell (2026-09-30)
    return (
        '"powershell.exe"  "-NoLogo" "-NoExit" "-Command" '
        f"\"$env:AGENT_RIBBON_LAUNCH='{client}:{marker}'; {client}\" "
    )


BAT = "C:\\WINDOWS\\system32\\cmd.exe /c C:\\Users\\owner\\AppData\\Local\\Temp\\tmp-1234-AbCdEf123456-.bat"


def _ps(pid: int, ppid: int, marker: str = MARK, client: str = "codex", t: int = 300) -> dict:
    return {"p": pid, "pp": ppid, "n": "powershell.exe", "t": t, "c": _line(client, marker)}


def _cmd(pid: int, ppid: int, t: int = 200, line: str | None = BAT) -> dict:
    return {"p": pid, "pp": ppid, "n": "cmd.exe", "t": t, "c": line}


def _conhost(pid: int, t: int = 100) -> dict:
    return {"p": pid, "pp": 1, "n": "conhost.exe", "t": t, "c": None}


def _pure(processes: list[dict], self_pid: int = 9999) -> list[int]:
    return _node("win32", "pure", {"processes": processes, "selfPid": self_pid})["pids"]


def test_a_chain_whose_conhost_is_gone_is_selected():
    assert _pure([_cmd(20, 10), _ps(30, 20)]) == [30]


def test_a_live_tab_is_never_selected():
    assert _pure([_conhost(10), _cmd(20, 10), _ps(30, 20)]) == []


def test_a_reused_conhost_pid_counts_as_gone():
    # pid 10 now belongs to a process started after the cmd
    assert _pure([_conhost(10, t=500), _cmd(20, 10), _ps(30, 20)]) == [30]


GOOD = _line("codex", MARK)


@pytest.mark.parametrize(
    "line",
    [
        '"powershell.exe"  "-NoLogo"',  # no marker
        _line("codex", "0f8e2c4a-1b2c-1d3e-8f90-123456789abc"),  # not a v4 uuid
        GOOD.replace('; codex"', '; claude"'),  # client mismatch
        "echo x " + GOOD,  # embedded in another command
        GOOD.replace('; codex"', '; codex; Remove-Item x"'),  # an extra command
        GOOD.replace('"-NoExit" ', '"-NoExit" "-File" "x.ps1" '),  # extra arguments
        GOOD.replace('"-NoLogo"', "-NoLogo"),  # malformed quoting
        GOOD.replace("'codex:", "codex:"),
        GOOD + "x",  # trailing text
    ],
    ids=[
        "no-marker",
        "uuid-v1",
        "client-mismatch",
        "embedded",
        "extra-command",
        "extra-args",
        "unquoted-arg",
        "unquoted-marker",
        "trailing",
    ],
)
def test_a_foreign_or_malformed_shell_is_left_alone(line: str):
    assert _pure([_cmd(20, 10), _ps(30, 20) | {"c": line}]) == []


def test_pwsh_is_not_a_manager_shell():
    assert _pure([_cmd(20, 10), _ps(30, 20) | {"n": "pwsh.exe"}]) == []


@pytest.mark.parametrize(
    "line",
    [
        None,
        "C:\\WINDOWS\\system32\\cmd.exe /c C:\\x\\build.bat",
        BAT + " & calc",
        "C:\\WINDOWS\\system32\\cmd.exe /k " + BAT.split(" /c ", 1)[1],
    ],
    ids=["unknown", "other-bat", "extra-command", "not-slash-c"],
)
def test_a_cmd_that_did_not_run_the_terminal_bat_is_left_alone(line: str | None):
    assert _pure([_cmd(20, 10, line=line), _ps(30, 20)]) == []


def test_the_chain_shape_read_on_windows():
    """The shape of the four ribbon chains read natively (2026-09-30), with synthetic pids and
    markers: three Codex tabs orphaned by a close, and one live Claude tab whose conhost is alive."""
    procs = [
        _conhost(100, t=90),
        _cmd(110, 100, t=100),
        _ps(111, 110, marker="11111111-1111-4111-8111-111111111111", client="claude", t=101),
        _cmd(210, 200, t=200),
        _ps(211, 210, marker="22222222-2222-4222-9222-222222222222", t=201),
        _cmd(310, 300, t=300),
        _ps(311, 310, marker="33333333-3333-4333-a333-333333333333", t=301),
        _cmd(410, 400, t=400),
        _ps(411, 410, marker="44444444-4444-4444-b444-444444444444", t=401),
    ]
    assert sorted(_pure(procs)) == [211, 311, 411]


def test_a_non_cmd_parent_is_left_alone():
    assert _pure([{"p": 20, "pp": 10, "n": "explorer.exe", "t": 200, "c": None}, _ps(30, 20)]) == []


def test_ambiguous_matches_are_refused():
    # two marked shells under one cmd, and one marker seen twice under different cmds
    assert _pure([_cmd(20, 10), _ps(30, 20), _ps(31, 20, marker=OTHER)]) == []
    assert _pure([_cmd(20, 10), _ps(30, 20), _cmd(21, 11), _ps(31, 21)]) == []


def test_the_query_process_itself_is_excluded():
    assert _pure([_cmd(20, 10), _ps(30, 20)], self_pid=30) == []


def test_the_load_sweep_ends_each_orphan_with_one_tree_kill():
    processes = [_cmd(20, 10), _ps(30, 20), _conhost(11), _cmd(21, 11), _ps(31, 21, marker=OTHER)]
    out = _node("win32", "load", {"processes": processes, "selfPid": 9999})
    query, _, *kills = out["calls"]  # two snapshots, then the kills
    assert query["cmd"] == "powershell.exe" and query["args"][:3] == [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    ]
    assert all(c["timeout"] == 15000 for c in out["calls"])
    assert kills == [{"cmd": "taskkill.exe", "args": ["/PID", "30", "/T", "/F"], "timeout": 15000}]
    assert 60000 in out["intervals"] and "layout-change" in out["events"]
    assert out["notices"] == []


def test_the_load_sweep_queries_twice_before_a_kill():
    out = _node("win32", "load", {"processes": [_cmd(20, 10), _ps(30, 20)], "selfPid": 9999})
    assert [c["cmd"] for c in out["calls"]] == ["powershell.exe", "powershell.exe", "taskkill.exe"]


def test_no_orphan_means_one_query_and_no_second():
    out = _node("win32", "load", {"processes": [_conhost(10), _cmd(20, 10), _ps(30, 20)], "selfPid": 9999})
    assert [c["cmd"] for c in out["calls"]] == ["powershell.exe"]


@pytest.mark.parametrize(
    "processes",
    [
        [_cmd(20, 10), _ps(30, 20, t=0)],
        [_cmd(20, 10, t=0), _ps(30, 20)],
        [_conhost(10, t=0), _cmd(20, 10), _ps(30, 20)],  # a host with no known birth is not gone
    ],
    ids=["shell", "cmd", "host"],
)
def test_an_unknown_creation_time_is_never_reaped(processes: list[dict]):
    assert _pure(processes) == []


def test_a_parent_missing_from_one_snapshot_only_is_left_alone():
    orphan = [_cmd(20, 10), _ps(30, 20)]
    live = [_conhost(10), *orphan]
    out = _node("win32", "load", {"snapshots": [orphan, live], "selfPid": 9999})
    assert [c["cmd"] for c in out["calls"]] == ["powershell.exe", "powershell.exe"]


def test_a_pid_reused_between_the_snapshots_is_left_alone():
    # pid 30 exits and a new manager PowerShell takes the pid between the two queries
    first = [_cmd(20, 10), _ps(30, 20)]
    second = [_cmd(20, 10), _ps(30, 20, marker=OTHER, t=400)]
    out = _node("win32", "load", {"snapshots": [first, second], "selfPid": 9999})
    assert "taskkill.exe" not in [c["cmd"] for c in out["calls"]]


def test_a_failed_query_warns_once_and_kills_nothing():
    out = _node("win32", "load", {"processes": [], "selfPid": 1, "queryFails": True})
    assert [c["cmd"] for c in out["calls"]] == ["powershell.exe"]
    assert out["notices"] == ["Manager cleanup failed: process query failed: Access denied"]


def test_other_platforms_never_sweep():
    out = _node("darwin", "load", {"processes": [_cmd(20, 10), _ps(30, 20)], "selfPid": 1})
    assert out["calls"] == [] and 60000 not in out["intervals"]


def _worker_profile(state: str, monkeypatch, tmp_path: Path, session: str = "worker-sid") -> dict:
    # the profile `dispatch open` builds on Windows
    from agent_cli.dispatch import opener

    record = {"id": "059-windows-49", "state": state, "session_id": session, "worktree": str(tmp_path)}
    # scoped: a later shutil.which("node") in _node breaks on macOS while sys.platform says win32
    with monkeypatch.context() as m:
        m.setattr(opener.sys, "platform", "win32")
        return opener._windows_profile(tmp_path, opener.open_command(tmp_path, record)[1])


def _as_reported(args: list[str]) -> str:
    # the manager shape: each argument quoted, two spaces after the executable, a trailing space
    return '"powershell.exe"  ' + " ".join(f'"{a}"' for a in args) + " "


def _worker(pid: int, ppid: int, line: str, t: int = 300) -> dict:
    return {"p": pid, "pp": ppid, "n": "powershell.exe", "t": t, "c": line}


@pytest.mark.parametrize("state", ["running", "blocked"], ids=["watch", "resume"])
def test_a_closed_worker_tab_chain_is_selected_and_a_live_one_is_not(state: str, monkeypatch, tmp_path: Path):
    line = _as_reported(_worker_profile(state, monkeypatch, tmp_path)["args"])
    assert _pure([_cmd(20, 10), _worker(30, 20, line)]) == [30]
    assert _pure([_conhost(10), _cmd(20, 10), _worker(30, 20, line)]) == []
    assert _pure([_cmd(20, 10, line="C:\\x\\cmd.exe /c C:\\x\\build.bat"), _worker(30, 20, line)]) == []
    assert _pure([_cmd(20, 10), _worker(30, 20, line, t=0)]) == []


def test_worker_and_manager_chains_are_reaped_together(monkeypatch, tmp_path: Path):
    line = _as_reported(_worker_profile("running", monkeypatch, tmp_path)["args"])
    procs = [_cmd(20, 10), _worker(30, 20, line), _cmd(21, 11), _ps(31, 21)]
    assert sorted(_pure(procs)) == [30, 31]
    # one cmd with a worker and a manager shell under it is ambiguous
    assert _pure([_cmd(20, 10), _worker(30, 20, line), _ps(31, 20)]) == []
    # the same worker marker twice
    assert _pure([_cmd(20, 10), _worker(30, 20, line), _cmd(21, 11), _worker(31, 21, line)]) == []


WORKER = (
    '"powershell.exe"  "-NoLogo" "-NoExit" "-Command" '
    f"\"$env:AGENT_WORKER_LAUNCH='worker:{MARK}'; ./bin/agent.cmd dispatch watch '059-windows-49' --follow\" "
)


@pytest.mark.parametrize(
    "line",
    [
        WORKER.replace("'worker:", "'codex:"),  # a manager client under the worker variable
        WORKER.replace("AGENT_WORKER_LAUNCH", "AGENT_RIBBON_LAUNCH"),  # worker client, manager variable
        WORKER.replace("./bin/agent.cmd", "./bin/other.cmd"),
        WORKER.replace("dispatch watch", "dispatch push"),
        WORKER.replace(" --follow", " --follow; Remove-Item x"),
        WORKER.replace("'059-windows-49'", "059-windows-49"),  # unquoted id
        WORKER.replace("'059-windows-49'", "'059 windows'"),
        WORKER.replace("-4d3e-", "-1d3e-"),  # not a v4 uuid
        WORKER.replace('"-NoExit" ', ""),
        WORKER + "x",
    ],
    ids=[
        "codex-client",
        "ribbon-variable",
        "other-script",
        "other-verb",
        "extra-command",
        "unquoted-id",
        "space-in-id",
        "uuid-v1",
        "no-noexit",
        "trailing",
    ],
)
def test_a_near_miss_worker_shell_is_left_alone(line: str):
    assert _pure([_cmd(20, 10), _worker(30, 20, WORKER)]) == [30]
    assert _pure([_cmd(20, 10), _worker(30, 20, line)]) == []


def test_a_worker_tab_is_not_a_manager_tab(monkeypatch, tmp_path: Path):
    # the restart close and the Codex right-click guard key off managerClient
    for state in ("running", "blocked"):
        profile = _worker_profile(state, monkeypatch, tmp_path)
        assert _node("win32", "manager", {"profile": profile})["manager"] is False
        manager = profile | {
            "args": [*profile["args"][:3], f"$env:AGENT_RIBBON_LAUNCH='codex:{MARK}'; codex"]
        }
        assert _node("win32", "manager", {"profile": manager})["manager"] is True


def test_the_query_carries_no_marker_and_the_profile_launches_powershell():
    source = PLUGIN.read_text(encoding="utf-8")
    query = source.split("const PROCESS_QUERY = ", 1)[1].split(";\n", 1)[0]
    assert "AGENT_RIBBON_LAUNCH" not in query
    profile = source.split("function windowsManagerProfile", 1)[1].split("\n}\n", 1)[0]
    assert "executable: 'powershell.exe'" in profile
    assert "`$env:AGENT_RIBBON_LAUNCH='${launchMarker}'; ${client}`" in profile
