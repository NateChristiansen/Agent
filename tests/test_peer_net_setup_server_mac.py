"""`peer-net setup-server` on the Mac: Remote Login read from launchd, one visible sudo.

Split from test_peer_net_setup_server.py at the 500-line limit; the stub runner lives there.
"""

from __future__ import annotations

import sys

import pytest

from agent_cli.peer_net import server_setup, system
from agent_cli.peer_net.errors import PeerNetError
from agent_cli.peer_net.server_setup import Result
from test_peer_net_setup_server import Stub, _ok, _OsAs

ON = '\tdisabled services = {\n\t\t"com.openssh.sshd" => enabled\n\t}\n'
OFF = '\tdisabled services = {\n\t\t"com.openssh.sshd" => disabled\n\t}\n'
NO_OVERRIDE = '\tdisabled services = {\n\t\t"com.apple.ftpd" => disabled\n\t}\n'


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(system, "host_fingerprints", lambda: ["SHA256:host"])
    monkeypatch.setattr(server_setup.launcher, "posix_shell", lambda: "posix")


def test_mac_with_remote_login_on_changes_nothing(mac):
    runner = Stub({"launchctl": _ok(ON)})
    steps = {s["step"]: s for s in server_setup._mac(runner)}
    assert all(s["status"] == "ok" for s in steps.values()) and runner.interactive_calls == []


def test_mac_turns_remote_login_on_through_an_interactive_sudo_and_rereads(mac):
    runner = Stub({"launchctl": [_ok(OFF), _ok(ON)]})
    steps = {s["step"]: s for s in server_setup._mac(runner)}
    assert steps["remote login"]["status"] == "changed"
    assert runner.interactive_calls == [["sudo", "systemsetup", "-f", "-setremotelogin", "on"]]


def test_mac_sudo_that_does_not_take_effect_fails_with_the_setting(mac):
    runner = Stub({"launchctl": _ok(OFF), "systemsetup": Result(1, "", "")})
    [step] = server_setup._mac(runner)
    assert step["status"] == "failed"
    assert "Remote Login" in step["detail"] and "Full Disk Access" in step["detail"]


@pytest.mark.parametrize(
    "out",
    [
        "something else entirely\n",  # no header
        '\tdisabled services = {\n\t\t"com.openssh.sshd" => maybe\n\t}\n',  # unfamiliar value
        '\tdisabled services = {\n\t\t"com.openssh.sshd" => enabled\n\t\t"com.openssh.sshd" => disabled\n\t}\n',
    ],
)
def test_mac_unrecognised_launchd_output_is_unknown_and_changes_nothing(mac, out):
    runner = Stub({"print-disabled": _ok(out)})
    [step] = server_setup._mac(runner)
    assert step["status"] == "failed" and runner.interactive_calls == []


def test_mac_unreadable_launchd_state_changes_nothing(mac):
    runner = Stub({"launchctl": Result(1, "", "")})
    [step] = server_setup._mac(runner)
    assert step["status"] == "failed" and runner.interactive_calls == []


@pytest.mark.parametrize(("code", "state"), [(0, "on"), (113, "off"), (5, "unknown")])
def test_mac_without_an_override_asks_launchd_for_the_job(code, state):
    runner = Stub({"print-disabled": _ok(NO_OVERRIDE), "print system/com.openssh.sshd": Result(code, "", "")})
    assert server_setup._remote_login(runner) == state


def test_mac_refuses_root(monkeypatch):
    monkeypatch.setattr(server_setup, "os", _OsAs("posix"))
    monkeypatch.setattr(system.os, "geteuid", lambda: 0, raising=False)
    if sys.platform == "win32":
        monkeypatch.setattr(system, "os", _OsAs("posix"))
    with pytest.raises(PeerNetError) as caught:
        server_setup.setup_server(Stub({}))
    assert caught.value.code == "root"


def test_the_interactive_runner_leaves_the_terminal_attached(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr(server_setup.subprocess, "run", fake_run)
    assert server_setup.Runner().interactive(["sudo", "true"]).code == 0
    assert "capture_output" not in seen and "stdout" not in seen
    assert seen["timeout"] == server_setup.SUDO_TIMEOUT_S
