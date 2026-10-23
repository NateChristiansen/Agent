from pathlib import Path

import pytest

from agent_cli import gitgate
from harness_root import link_notes

pytestmark = pytest.mark.usefixtures("notes_remote")


def _levels(r):
    return {c["check"]: c["level"] for c in r.json}


def test_clean_fixture_passes(tmp_root: Path, run):
    link_notes(tmp_root)  # a notes repo is required
    gitgate.install_review(tmp_root)  # setup's product pre-push hook
    run(["feature", "new", "x", "--title", "X"])
    run(["adr", "new", "a", "--title", "A", "--feature", "1", "--scope", "harness"])
    r = run(["--json", "doctor"])
    assert r.code == 0, r.out
    levels = _levels(r)
    assert levels["agent.yaml"] == "ok"
    assert levels["notes repo"] == "ok"
    assert levels["landing"] in ("ok", "warn")
    assert levels["repo beta on disk"] == "warn"
    # About six checks that matter, not forty.
    assert not {"features", "adrs", "generated files", "hook wiring", "venv"} & set(levels)


def test_mcp_wiring_checks(tmp_root: Path, run):
    run(["status", "render"])
    r = run(["--json", "doctor"])
    assert _levels(r)["mcp wiring"] == "warn"  # no .mcp.json in the fixture
    (tmp_root / ".mcp.json").write_text(
        '{"mcpServers":{"agent":{"command":"${CLAUDE_PROJECT_DIR:-.}/bin/agent","args":["mcp","--profile","manager"],"alwaysLoad":true}}}'
    )
    settings = tmp_root / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"enabledMcpjsonServers":["agent"],"permissions":{"allow":["mcp__agent__*"]}}')
    r = run(["--json", "doctor"])
    assert _levels(r)["mcp wiring"] == "ok"
    settings.write_text('{"permissions":{"allow":["Edit"]}}')
    r = run(["--json", "doctor"])
    checks = [c for c in r.json if c["check"] == "mcp wiring"]
    assert checks[-1]["level"] == "fail"
    assert "enabledMcpjsonServers" in checks[-1]["detail"] and "mcp__agent__*" in checks[-1]["detail"]
    settings.write_text('{"enabledMcpjsonServers":["agent"],"permissions":{"allow":["mcp__agent__*"]}}')
    (tmp_root / ".mcp.json").write_text(
        '{"mcpServers":{"agent":{"command":"npx something","args":["serve"]}}}'
    )
    r = run(["--json", "doctor"])
    detail = [c for c in r.json if c["check"] == "mcp wiring"][-1]
    assert detail["level"] == "fail"
    assert "bin/agent" in detail["detail"] and "alwaysLoad" in detail["detail"]
    (tmp_root / ".mcp.json").write_text("{not json")
    r = run(["--json", "doctor"])
    assert [c for c in r.json if c["check"] == "mcp wiring"][-1]["level"] == "fail"


AGENT_SERVER = '"agent":{"command":"bin/agent","args":["mcp"],"alwaysLoad":true}'


def _wire(tmp_root: Path, servers: str, settings_json: str) -> None:
    (tmp_root / ".mcp.json").write_text('{"mcpServers":{' + servers + "}}")
    settings = tmp_root / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(settings_json)


def test_every_mcp_server_is_checked_not_just_agent(tmp_root: Path, run):
    """A registered server that is not enabled never loads."""
    run(["status", "render"])
    _wire(
        tmp_root,
        AGENT_SERVER + ',"vendor":{"command":"npx","args":["-y","x"]}',
        '{"enabledMcpjsonServers":["agent","vendor"],'
        '"permissions":{"allow":["mcp__agent__*","mcp__vendor__read_thing"]}}',
    )
    r = run(["--json", "doctor"])
    check = [c for c in r.json if c["check"] == "mcp servers"][-1]
    assert check["level"] == "ok"
    assert "vendor" in check["detail"]


def test_a_rule_in_settings_local_makes_a_server_reachable(tmp_root: Path, run):
    """Claude Code merges `.claude/settings.local.json` into settings.json. A server one
    machine allows there, or allows whole with the bare `mcp__<server>` rule, is not a prompt."""
    run(["status", "render"])
    _wire(
        tmp_root,
        AGENT_SERVER + ',"vendor":{"command":"npx"},"browser":{"command":"npx"}',
        '{"enabledMcpjsonServers":["agent","vendor","browser"],"permissions":{"allow":["mcp__agent__*"]}}',
    )
    r = run(["--json", "doctor"])
    check = [c for c in r.json if c["check"] == "mcp servers"][-1]
    assert check["level"] == "warn" and "prompts the owner" in check["detail"]
    (tmp_root / ".claude" / "settings.local.json").write_text(
        '{"permissions":{"allow":["mcp__vendor__read","mcp__browser"]}}'
    )
    r = run(["--json", "doctor"])
    check = [c for c in r.json if c["check"] == "mcp servers"][-1]
    assert check["level"] == "ok", check["detail"]


def test_a_registered_server_that_is_not_enabled_warns(tmp_root: Path, run):
    """A warn, not a fail: the example .mcp.json ships an optional server a machine may never
    enable (ADR-0012)."""
    run(["status", "render"])
    _wire(
        tmp_root,
        AGENT_SERVER + ',"vendor":{"command":"npx","args":["-y","x"]}',
        '{"enabledMcpjsonServers":["agent"],"permissions":{"allow":["mcp__agent__*","mcp__vendor__x"]}}',
    )
    r = run(["--json", "doctor"])
    check = [c for c in r.json if c["check"] == "mcp servers"][-1]
    assert check["level"] == "warn"
    assert "vendor" in check["detail"] and "never loads" in check["detail"]


def test_a_server_with_no_permission_rule_warns(tmp_root: Path, run):
    run(["status", "render"])
    _wire(
        tmp_root,
        AGENT_SERVER + ',"vendor":{"command":"npx","args":["-y","x"]}',
        '{"enabledMcpjsonServers":["agent","vendor"],"permissions":{"allow":["mcp__agent__*"]}}',
    )
    r = run(["--json", "doctor"])
    check = [c for c in r.json if c["check"] == "mcp servers"][-1]
    assert check["level"] == "warn"
    assert "vendor" in check["detail"]


def test_an_ask_rule_counts_as_reachable(tmp_root: Path, run):
    """A write-only server lives entirely in `ask`; that is wired, not broken."""
    run(["status", "render"])
    _wire(
        tmp_root,
        AGENT_SERVER + ',"vendor":{"command":"npx","args":["-y","x"]}',
        '{"enabledMcpjsonServers":["agent","vendor"],'
        '"permissions":{"allow":["mcp__agent__*"],"ask":["mcp__vendor__write_thing"]}}',
    )
    r = run(["--json", "doctor"])
    assert [c for c in r.json if c["check"] == "mcp servers"][-1]["level"] == "ok"


def test_an_enabled_server_that_is_no_longer_registered_warns(tmp_root: Path, run):
    run(["status", "render"])
    _wire(
        tmp_root,
        AGENT_SERVER,
        '{"enabledMcpjsonServers":["agent","removed"],"permissions":{"allow":["mcp__agent__*"]}}',
    )
    r = run(["--json", "doctor"])
    check = [c for c in r.json if c["check"] == "mcp servers"][-1]
    assert check["level"] == "warn"
    assert "removed" in check["detail"]


PENDING = (
    'version = 1\n\n[[quarantine]]\nnode = "tests/test_a.py::test_x"\ndisposition = "timing-flake"\n'
    'owner = "maintainers"\nreason = "lost update"\nexpires = 2099-01-01\nevidence = "this test"\n'
    'owner_approval = "pending"\n'
)


def _quarantined(tmp_root: Path, role: str | None) -> None:
    (tmp_root / "tests").mkdir(exist_ok=True)
    (tmp_root / "tests" / "quarantine.toml").write_text(PENDING)
    if role:
        (tmp_root / "agent.local.yaml").write_text(f"harness:\n  role: {role}\n")


@pytest.mark.parametrize(("role", "shown"), [("consumer", False), ("source", True), (None, True)])
def test_the_flake_quarantine_row_is_for_source_installs_only(tmp_root: Path, run, role, shown):
    link_notes(tmp_root)
    _quarantined(tmp_root, role)
    r = run(["--json", "doctor"])
    assert ("flake quarantine" in _levels(r)) is shown


def test_doctor_opens_with_one_summary_line_of_the_counts(tmp_root: Path, run):
    link_notes(tmp_root)
    r = run(["doctor"])
    levels = [c["level"] for c in run(["--json", "doctor"]).json]
    want = f"{levels.count('ok')} OK, {levels.count('warn')} WARN, {levels.count('fail')} FAIL"
    assert r.out.lstrip().splitlines()[0] == want
    assert r.out.count(" OK, ") == 1
