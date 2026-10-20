"""`peer-net setup-server` reads before it acts, re-reads after, and never makes the owner's trust
decisions.

Every OS call goes through a stub runner keyed by the command it was given, so these tests show
exactly which commands each state leads to: a satisfied machine runs no changing command, a
failed or unreadable read runs none either, and every change is checked by reading again.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_cli.peer_net import server_setup, system
from agent_cli.peer_net.errors import PeerNetError
from agent_cli.peer_net.server_setup import Result

CHANGING = (
    "Add-WindowsCapability",
    "Set-Service",
    "Start-Service",
    "New-NetFirewallRule",
    "Set-NetFirewallRule",
)
GOOD_SERVICE = json.dumps({"Status": "Running", "StartType": "Automatic"})
GOOD_RULE = {
    "Enabled": "True",
    "Profile": "Private",
    "Direction": "Inbound",
    "Action": "Allow",
    "Protocol": "TCP",
    "LocalPort": "22",
}
PRIVATE_NET = json.dumps([{"Name": "home", "Category": "Private"}])


def _ok(out: str = "") -> Result:
    return Result(0, out, "")


def _rule(**over) -> Result:
    return _ok(json.dumps([{**GOOD_RULE, **over}]))


DENIED = Result(3, "", "Access is denied.")


class Stub(server_setup.Runner):
    """Answers by the first matching fragment of the raw script; a list answers in order."""

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.calls: list[str] = []
        self.interactive_calls: list[list[str]] = []

    def ps(self, script: str, timeout: float = 60.0) -> Result:
        return self._answer(script)

    def run(self, argv: list[str], timeout: float = 60.0) -> Result:
        return self._answer(" ".join(argv))

    def interactive(self, argv: list[str], timeout: float = 300.0) -> Result:
        self.interactive_calls.append(argv)
        return self._answer(" ".join(argv))

    def _answer(self, command: str) -> Result:
        self.calls.append(command)
        for fragment, answer in self.answers.items():
            if fragment in command:
                if isinstance(answer, list):
                    return answer.pop(0) if len(answer) > 1 else answer[0]
                return answer
        return _ok()

    def sleep(self, seconds: float) -> None:
        pass

    def changing(self) -> list[str]:
        return [c for c in self.calls if c.split()[0] in CHANGING]


def satisfied() -> dict:
    return {
        "Get-WindowsCapability": _ok('"Installed"'),
        "Get-Service": _ok(GOOD_SERVICE),
        "Get-NetFirewallRule": _rule(),
        "Get-NetConnectionProfile": _ok(PRIVATE_NET),
        "Get-LocalUser": _ok('"Local"'),
    }


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    (tmp_path / "ProgramData" / "ssh").mkdir(parents=True)
    monkeypatch.setattr(system, "host_fingerprints", lambda: ["SHA256:host"])
    monkeypatch.setattr(system, "acl_sids", lambda path: {system.ADMINS_SID, system.SYSTEM_SID})
    monkeypatch.setattr(system, "restrict_admin_file", lambda path: None)
    monkeypatch.setattr(server_setup.launcher, "windows_shell", lambda: "cmd")
    monkeypatch.setattr(server_setup.launcher, "posix_shell", lambda: "posix")
    return tmp_path / "ProgramData" / "ssh" / "administrators_authorized_keys"


def _run(answers: dict, private_only: bool = False):
    runner = Stub(answers)
    return {s["step"]: s for s in server_setup._windows(runner, private_only)}, runner


# --- the PowerShell wrapper ---


def test_every_powershell_call_stops_on_errors_and_exits_nonzero(monkeypatch):
    seen: list[list[str]] = []

    class Spy(server_setup.Runner):
        def run(self, argv, timeout=60.0):
            seen.append(argv)
            return _ok()

    Spy().ps("Get-Thing")
    script = seen[0][-1]
    assert "$ErrorActionPreference = 'Stop'" in script and "exit 3" in script and "Get-Thing" in script
    assert seen[0][:3] == ["powershell", "-NoProfile", "-NonInteractive"]


# --- idempotency and the happy paths ---


def test_a_satisfied_machine_runs_no_changing_command_twice(windows):
    windows.write_text("")
    for _ in range(2):
        steps, runner = _run(satisfied())
        assert all(s["status"] == "ok" for s in steps.values()), steps
        assert runner.changing() == []


def test_a_fresh_machine_is_installed_started_firewalled_and_rechecked(windows):
    steps, runner = _run(
        {
            **satisfied(),
            "Get-WindowsCapability": [_ok('"NotPresent"'), _ok('"Installed"')],
            "Get-Service": [_ok(json.dumps({"Status": "Stopped", "StartType": "Manual"})), _ok(GOOD_SERVICE)],
            "Get-NetFirewallRule": [_ok(""), _rule()],
        }
    )
    assert steps["ssh server installed"]["status"] == "changed"
    assert steps["sshd automatic and running"]["detail"] == "set Automatic, started"
    assert steps["firewall"]["status"] == "changed" and steps["admin keys file"]["status"] == "changed"
    rule = next(c for c in runner.calls if c.startswith("New-NetFirewallRule"))
    for part in ("-Direction Inbound", "-Protocol TCP", "-LocalPort 22", "-Profile Private", "-Action Allow"):
        assert part in rule
    assert "Public" not in rule and "Domain" not in rule
    assert sum("Get-WindowsCapability" in c for c in runner.calls) == 2  # re-read after install


# --- T1: a failed or unreadable read never leads to a change ---


@pytest.mark.parametrize("bad", [DENIED, Result(124, "", "timeout"), _ok("not json"), _ok("")])
def test_an_unreadable_capability_state_installs_nothing(windows, bad):
    steps, runner = _run({**satisfied(), "Get-WindowsCapability": bad})
    assert steps["ssh server installed"]["status"] == "failed" and runner.changing() == []
    assert list(steps) == ["ssh server installed"]  # nothing after an unknown install state


@pytest.mark.parametrize("bad", [DENIED, Result(124, "", "timeout"), _ok("{broken")])
def test_an_unreadable_firewall_rule_is_never_created_or_changed(windows, bad):
    steps, runner = _run({**satisfied(), "Get-NetFirewallRule": bad}, private_only=True)
    assert steps["firewall"]["status"] == "failed"
    assert not [c for c in runner.calls if c.startswith(("New-NetFirewallRule", "Set-NetFirewallRule"))]


def test_an_unreadable_service_is_not_started(windows):
    steps, runner = _run({**satisfied(), "Get-Service": DENIED})
    assert steps["sshd automatic and running"]["status"] == "failed"
    assert not [c for c in runner.calls if c.startswith(("Set-Service", "Start-Service"))]


def test_the_rule_read_never_suppresses_errors():
    """Absence must be a successful empty enumeration; an -ErrorAction override would turn an
    access or provider error into "absent" and lead to New-NetFirewallRule."""
    assert "ErrorAction" not in server_setup.RULE_READ and "SilentlyContinue" not in server_setup.RULE_READ
    assert "Where-Object" in server_setup.RULE_READ


@pytest.mark.parametrize("bad", ["{}", "[]", '["Installed"]', '{"State": "Installed"}', "42", '"Weird"'])
def test_a_well_formed_but_wrong_capability_answer_is_failed(windows, bad):
    steps, runner = _run({**satisfied(), "Get-WindowsCapability": _ok(bad)})
    assert steps["ssh server installed"]["status"] == "failed" and runner.changing() == []


@pytest.mark.parametrize(
    "bad",
    [
        "{}",
        "[]",
        '{"Status": "Running"}',
        '{"Status": 4, "StartType": "Automatic"}',
        '{"Status": "Odd", "StartType": "Manual"}',
    ],
)
def test_a_wrong_service_shape_is_failed_and_starts_nothing(windows, bad):
    steps, runner = _run({**satisfied(), "Get-Service": _ok(bad)})
    assert steps["sshd automatic and running"]["status"] == "failed"
    assert not [c for c in runner.calls if c.startswith(("Set-Service", "Start-Service"))]


@pytest.mark.parametrize(
    "bad",
    [
        '{"Name": "x"}',
        "[1]",
        '[{"Name": "x"}]',
        '[{"Name": 3, "Category": "Private"}]',
        '[{"Name": "x", "Category": []}]',
        '[{"Name": "x", "Category": {}}]',
    ],
)
def test_a_wrong_network_shape_is_failed(windows, bad):
    steps, _ = _run({**satisfied(), "Get-NetConnectionProfile": _ok(bad)})
    assert steps["network"]["status"] == "failed"


@pytest.mark.parametrize("bad", ['[{"Enabled": "True"}]', '[{"Enabled": true, "Profile": "Private"}]', "{}"])
def test_a_wrong_rule_shape_is_failed_and_changes_nothing(windows, bad):
    steps, runner = _run({**satisfied(), "Get-NetFirewallRule": _ok(bad)}, private_only=True)
    assert steps["firewall"]["status"] == "failed" and runner.changing() == []


def test_unreadable_network_and_account_never_claim_private(windows):
    steps, _ = _run({**satisfied(), "Get-NetConnectionProfile": DENIED, "Get-LocalUser": DENIED})
    assert steps["network"]["status"] == "failed" and "Private" not in steps["network"]["detail"]
    assert steps["account"]["status"] == "warn"


# --- T2: re-reads after changes, and the full rule shape ---


def test_install_that_does_not_read_back_installed_fails(windows):
    steps, _ = _run({**satisfied(), "Get-WindowsCapability": [_ok('"NotPresent"'), _ok('"NotPresent"')]})
    assert steps["ssh server installed"]["status"] == "failed"


def test_sshd_that_stops_again_or_stays_manual_fails(windows):
    stopped = _ok(json.dumps({"Status": "Stopped", "StartType": "Automatic"}))
    steps, _ = _run({**satisfied(), "Get-Service": [stopped, stopped]})
    assert "stopped again" in steps["sshd automatic and running"]["detail"]
    manual = _ok(json.dumps({"Status": "Running", "StartType": "Manual"}))
    steps, _ = _run({**satisfied(), "Get-Service": [manual, manual]})
    assert steps["sshd automatic and running"]["status"] == "failed"


@pytest.mark.parametrize(
    "over", [{"Direction": "Outbound"}, {"Action": "Block"}, {"LocalPort": "2222"}, {"Protocol": "UDP"}]
)
def test_a_rule_of_the_wrong_shape_is_never_ok_and_never_edited(windows, over):
    steps, runner = _run({**satisfied(), "Get-NetFirewallRule": _rule(**over)}, private_only=True)
    assert steps["firewall"]["status"] == "warn"
    assert not [c for c in runner.calls if c.startswith(("Set-NetFirewallRule", "New-NetFirewallRule"))]


def test_two_rules_with_the_name_are_a_failed_read(windows):
    two = _ok(json.dumps([GOOD_RULE, GOOD_RULE]))
    steps, runner = _run({**satisfied(), "Get-NetFirewallRule": two}, private_only=True)
    assert steps["firewall"]["status"] == "failed" and runner.changing() == []


@pytest.mark.parametrize("profile", ["Any", "Public", "Domain, Private"])
def test_an_existing_rule_is_reported_never_changed_without_the_flag(windows, profile):
    steps, runner = _run({**satisfied(), "Get-NetFirewallRule": _rule(Profile=profile)})
    assert steps["firewall"]["status"] == "warn" and profile in steps["firewall"]["detail"]
    assert not [c for c in runner.calls if c.startswith("Set-NetFirewallRule")]


def test_the_flag_narrows_and_rereads_the_rule(windows):
    steps, runner = _run(
        {**satisfied(), "Get-NetFirewallRule": [_rule(Profile="Any"), _rule()]}, private_only=True
    )
    assert steps["firewall"]["status"] == "changed"
    assert any(c.startswith("Set-NetFirewallRule") and "-Profile Private" in c for c in runner.calls)
    steps, _ = _run(
        {**satisfied(), "Get-NetFirewallRule": [_rule(Profile="Any"), _rule(Profile="Any")]}, True
    )
    assert steps["firewall"]["status"] == "failed"  # the change did not read back


# --- T5: steps never discard each other; network categories reported accurately ---


def test_a_public_or_domain_network_is_named_as_outside_the_rule(windows):
    nets = json.dumps(
        [{"Name": "cafe", "Category": "Public"}, {"Name": "work", "Category": "DomainAuthenticated"}]
    )
    steps, runner = _run({**satisfied(), "Get-NetConnectionProfile": _ok(nets)})
    assert steps["network"]["status"] == "warn"
    assert (
        "cafe (Public)" in steps["network"]["detail"]
        and "work (DomainAuthenticated)" in steps["network"]["detail"]
    )
    assert not [c for c in runner.calls if "Set-NetConnectionProfile" in c]


def test_an_unreadable_host_key_is_a_failed_step_and_later_steps_still_report(windows, monkeypatch):
    def unreadable():
        raise PeerNetError("host-key-unreadable", "run init from an elevated shell")

    monkeypatch.setattr(system, "host_fingerprints", unreadable)
    steps, _ = _run(satisfied())
    assert steps["host keys"]["status"] == "failed" and "host-key-unreadable" in steps["host keys"]["detail"]
    assert "firewall" in steps and "account" in steps


def test_an_existing_admin_file_with_a_wider_acl_is_reported_not_rewritten(windows, monkeypatch):
    windows.write_text("ssh-rsa AAAA owner\n")
    monkeypatch.setattr(system, "acl_sids", lambda path: {system.ADMINS_SID, system.SYSTEM_SID, "S-1-5-11"})
    steps, _ = _run(satisfied())
    assert steps["admin keys file"]["status"] == "warn" and windows.read_text() == "ssh-rsa AAAA owner\n"


def test_the_admin_file_gets_its_acl_before_it_exists(windows, monkeypatch):
    seen: list[bool] = []
    monkeypatch.setattr(system, "restrict_admin_file", lambda path: seen.append(windows.exists()))
    assert server_setup._admin_keys_file()["status"] == "changed"
    assert seen == [False] and windows.exists() and not list(windows.parent.glob("*.tmp-*"))


def test_an_acl_command_failure_publishes_nothing(windows, monkeypatch):
    def fails(path):
        raise PeerNetError("system", "acl")

    monkeypatch.setattr(system, "restrict_admin_file", fails)
    steps, _ = _run(satisfied())
    assert steps["admin keys file"]["status"] == "failed" and not windows.exists()
    assert not list(windows.parent.glob("*.tmp-*"))


def test_a_file_that_appears_meanwhile_is_left_alone(windows, monkeypatch):
    def race(path):
        windows.write_text("someone else's\n")

    monkeypatch.setattr(system, "restrict_admin_file", race)
    step = server_setup._admin_keys_file()
    assert step["status"] == "warn" and windows.read_text() == "someone else's\n"
    assert not list(windows.parent.glob("*.tmp-*"))


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs a privilege the Windows test user lacks")
def test_a_linked_admin_file_is_refused(windows, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("")
    windows.symlink_to(target)
    steps, _ = _run(satisfied())
    assert (
        steps["admin keys file"]["status"] == "failed" and "unsafe-path" in steps["admin keys file"]["detail"]
    )


def test_a_microsoft_account_and_an_unsupported_shell_warn(windows, monkeypatch):
    def powershell_shell():
        raise PeerNetError("shell-unsupported", "shell")

    monkeypatch.setattr(server_setup.launcher, "windows_shell", powershell_shell)
    steps, _ = _run({**satisfied(), "Get-LocalUser": _ok('"MicrosoftAccount"')})
    assert (
        steps["account"]["status"] == "warn"
        and "start `pair` from this machine" in steps["account"]["detail"]
    )
    assert steps["ssh shell"]["status"] == "warn"


# --- T3: the overall result and the CLI exit ---


class _OsAs:
    """`os` as another platform sees it, for one module only (pathlib keeps its flavour)."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr: str):
        import os

        return getattr(os, attr)


@pytest.mark.parametrize(
    ("statuses", "result", "code"),
    [
        (["ok", "ok"], "ok", 0),
        (["ok", "changed"], "changed", 0),
        (["changed", "warn"], "warn", 0),
        (["warn", "failed"], "failed", 1),
    ],
)
def test_the_worst_step_decides_the_result_and_only_failure_exits_nonzero(
    monkeypatch, statuses, result, code
):
    from agent_cli.cli.peer_net import setup_exit, setup_report

    monkeypatch.setattr(server_setup, "os", _OsAs("posix"))
    monkeypatch.setattr(
        server_setup, "_mac", lambda runner: [server_setup._step(f"s{i}", s) for i, s in enumerate(statuses)]
    )
    report = server_setup.setup_server(Stub({}))
    assert report["result"] == result and setup_exit(report) == code
    assert setup_report(report).endswith(f"result: {result}") and len(report["steps"]) == len(statuses)


def test_windows_needs_elevation(monkeypatch):
    monkeypatch.setattr(server_setup, "os", _OsAs("nt"))
    monkeypatch.setattr(system, "is_elevated", lambda: False)
    with pytest.raises(PeerNetError) as caught:
        server_setup.setup_server(Stub({}))
    assert caught.value.code == "needs-elevation"


# The Mac cases are in test_peer_net_setup_server_mac.py (split at the 500-line limit).
