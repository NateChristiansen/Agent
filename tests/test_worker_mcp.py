"""`dispatch new --mcp <profile>` and `agent worker-mcp list|exec`: extra MCP servers a worker
gets from an agent.yaml `worker_mcp:` profile (worker_mcp.py).

The contracts worth pinning: a dispatch refuses to start when a profile is unknown or its
required file (a saved login) is missing, before any worktree or record exists; the profile's
server and allow rules reach the worker and nothing reaches a plain dispatch; the server runs
through the launcher from its output directory, with the placeholders filled.
"""

import json
import subprocess
from pathlib import Path

import pytest

from agent_cli import dispatch, launcher, worker_mcp
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from dispatch_helpers import droot  # noqa: F401 -- pytest fixture, used only as a parameter name

pytestmark = pytest.mark.usefixtures("notes_remote")

PROFILE_BLOCK = """
worker_mcp:
  qa-staging:
    server: playwright
    command: npx
    args: [--yes, "@playwright/mcp@0.0.81", --headless, --isolated,
           --allowed-origins, "https://staging.example.com",
           --storage-state, "{root}/.agent/browser/staging.json", --output-dir, "{output_dir}"]
    allow: [mcp__playwright__*]
    deny: [mcp__playwright__browser_evaluate, mcp__playwright__browser_run_code]
    env: {RUN_ID: "{dispatch_id}"}
    requires_file: .agent/browser/staging.json
    requires_hint: save a login with npx playwright open --save-storage
  search:
    server: docs
    command: docs-server
"""

CONTRACT = Path(__file__).resolve().parents[1] / "harness" / "worker-contract.md"


def _configure(root: Path, block: str = PROFILE_BLOCK) -> None:
    reg = root / "agent.yaml"
    reg.write_text(reg.read_text(encoding="utf-8") + block, encoding="utf-8")


def _login(root: Path) -> Path:
    path = root / ".agent" / "browser" / "staging.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


def _feature_with_alpha(root: Path, run) -> None:
    # Checked, so a refused `feature new` fails here with its own reason rather than later as
    # "no feature with id 1".
    made = run(["feature", "new", "alpha-work", "--title", "Alpha work", "--repo", "alpha"])
    assert made.code == 0, made.out + made.err
    added = run(["task", "add", "1", "do the thing"])
    assert added.code == 0, added.out + added.err
    (root / "harness").mkdir(exist_ok=True)
    (root / "harness" / "worker-contract.md").write_text(
        CONTRACT.read_text(encoding="utf-8"), encoding="utf-8"
    )


# ------------------------------------------------------------------ config


def test_profiles_load_from_the_registry(tmp_root: Path):
    assert worker_mcp.load(tmp_root) == {}
    _configure(tmp_root)
    profiles = worker_mcp.load(tmp_root)
    assert sorted(profiles) == ["qa-staging", "search"]
    assert profiles["qa-staging"].server == "playwright"
    assert profiles["qa-staging"].allow == ["mcp__playwright__*"]
    assert profiles["search"].args == [] and profiles["search"].requires_file == ""


def test_the_tracked_example_registry_and_documented_profile_are_valid():
    import yaml

    root = Path(__file__).resolve().parents[1]
    text = (root / "agent.example.yaml").read_text(encoding="utf-8")
    assert repos_mod.validate_registry_data(yaml.safe_load(text)) == []
    assert "harness/agent-yaml.md" in text
    guide = (root / "harness/agent-yaml.md").read_text(encoding="utf-8")
    block = guide.split("```yaml\nworker_mcp:\n", 1)[1].split("```", 1)[0]
    data = yaml.safe_load("worker_mcp:\n" + block)
    assert repos_mod.validate_registry_data({"repos": [], **data}) == []
    assert worker_mcp.validate(data["worker_mcp"]) == []
    assert data["worker_mcp"]["qa-staging"]["server"] == "playwright"


def test_a_bad_block_is_a_registry_problem(tmp_root: Path):
    assert worker_mcp.validate(None) == []
    problems = worker_mcp.validate({"x": {"server": "", "command": "c", "args": "not a list"}})
    assert "worker_mcp.x.server must be a non-empty string" in problems
    assert "worker_mcp.x.args must be a list of strings" in problems
    _configure(tmp_root, "\nworker_mcp:\n  broken: {command: c}\n")
    with pytest.raises(AgentError, match="worker_mcp.broken.server"):
        repos_mod.load_registry(tmp_root)


def test_require_names_the_fix(tmp_root: Path):
    _configure(tmp_root)
    with pytest.raises(
        AgentError, match="unknown worker mcp profile 'nope'; agent.yaml knows: qa-staging, search"
    ):
        worker_mcp.require(tmp_root, ["nope"])
    with pytest.raises(AgentError, match=r"needs \.agent/browser/staging\.json"):
        worker_mcp.require(tmp_root, ["qa-staging"])
    _login(tmp_root)
    assert [p.name for p in worker_mcp.require(tmp_root, ["qa-staging", "search"])] == [
        "qa-staging",
        "search",
    ]


def test_two_profiles_on_one_server_name_refuse(tmp_root: Path):
    _configure(tmp_root, "\nworker_mcp:\n  a: {server: s, command: x}\n  b: {server: s, command: y}\n")
    with pytest.raises(AgentError, match="reuses the server name 's'"):
        worker_mcp.require(tmp_root, ["a", "b"])
    _configure(tmp_root, "  c: {server: agent, command: z}\n")
    with pytest.raises(AgentError, match="reuses the server name 'agent'"):
        worker_mcp.require(tmp_root, ["c"])


# ------------------------------------------------------------------ the worker's server


def test_servers_run_the_launcher(tmp_root: Path):
    _configure(tmp_root)
    _login(tmp_root)
    d = {"id": "001-01", "mcp": ["qa-staging"]}
    cfg = worker_mcp.servers(tmp_root, d)
    assert list(cfg) == ["playwright"]
    command, prefix_args = launcher.command_and_args(tmp_root)
    assert cfg["playwright"]["command"] == command
    assert cfg["playwright"]["args"] == [
        *prefix_args,
        "worker-mcp",
        "exec",
        "qa-staging",
        "--output-dir",
        str(dispatch.file(tmp_root, "001-01", ".mcp-qa-staging")),
        "--dispatch",
        "001-01",
    ]
    assert worker_mcp.servers(tmp_root, {"id": "001-01"}) == {}


def test_exec_fills_placeholders_changes_to_the_output_dir_and_execs(tmp_root: Path):
    _configure(tmp_root)
    state = _login(tmp_root)
    out = tmp_root / "out"
    seen: dict = {}
    worker_mcp.exec_profile(
        tmp_root,
        "qa-staging",
        out,
        "001-01",
        chdir=lambda p: seen.setdefault("cwd", p),
        execvpe=lambda f, argv, env: seen.setdefault("exec", (f, argv, env)),
    )
    # the output dir is the cwd: no package.json there for npm's devEngines check to trip on
    assert seen["cwd"] == str(out) and out.is_dir()
    f, argv, env = seen["exec"]
    assert f == "npx" and argv[:3] == ["npx", "--yes", "@playwright/mcp@0.0.81"]
    assert argv[argv.index("--storage-state") + 1] == f"{tmp_root}/.agent/browser/staging.json"
    assert Path(argv[argv.index("--storage-state") + 1]) == state
    assert argv[argv.index("--output-dir") + 1] == str(out)
    assert env["RUN_ID"] == "001-01"


def test_expand_leaves_other_braces_alone(tmp_root: Path):
    assert (
        worker_mcp.expand('{"a": 1} {root}', root=tmp_root, output_dir=tmp_root, dispatch_id="x")
        == f'{{"a": 1}} {tmp_root}'
    )


# ------------------------------------------------------------------ dispatch --mcp


def test_a_dispatch_refuses_an_unready_profile_and_a_ready_one_reaches_the_worker(
    tmp_root: Path, run, fake_claude
):
    _configure(tmp_root)
    _feature_with_alpha(tmp_root, run)
    with pytest.raises(AgentError, match="needs .agent/browser/staging.json"):
        dispatch.new(tmp_root, repo="alpha", feature="1", prompt="qa pass", mcp=["qa-staging"], dry_run=True)
    with pytest.raises(AgentError, match="unknown worker mcp profile"):
        dispatch.new(tmp_root, repo="alpha", feature="1", prompt="qa pass", mcp=["nope"], dry_run=True)
    assert not (tmp_root / ".agent" / "dispatches").exists()

    plain = dispatch.new(tmp_root, repo="alpha", feature="1", prompt="plain", dry_run=True)
    assert plain["mcp"] == []
    assert "playwright" not in plain["options"]["mcp_servers"]

    _login(tmp_root)
    p = dispatch.new(tmp_root, repo="alpha", feature="1", prompt="qa pass", mcp=["qa-staging"], dry_run=True)
    assert p["mcp"] == ["qa-staging"]
    assert "playwright" in p["options"]["mcp_servers"]
    alpha = repos_mod.load_registry(tmp_root).require("alpha")
    with_profile = dispatch.build_settings(tmp_root, alpha, mcp=["qa-staging"])["permissions"]["allow"]
    without = dispatch.build_settings(tmp_root, alpha)["permissions"]["allow"]
    assert "mcp__playwright__*" in with_profile and "mcp__playwright__*" not in without
    denied = dispatch.build_settings(tmp_root, alpha, mcp=["qa-staging"])["permissions"]["deny"]
    assert "mcp__playwright__browser_evaluate" in denied
    assert (
        "mcp__playwright__browser_evaluate"
        not in dispatch.build_settings(tmp_root, alpha)["permissions"]["deny"]
    )


def test_deny_must_be_a_list_of_strings():
    assert worker_mcp.validate({"x": {"server": "s", "command": "c", "deny": "one"}}) == [
        "worker_mcp.x.deny must be a list of strings"
    ]


def test_cli_list_and_dispatch_flag(tmp_root: Path, run, fake_claude):
    _configure(tmp_root)
    _feature_with_alpha(tmp_root, run)
    r = run(["worker-mcp", "list"])
    assert r.code == 0, r.err
    assert "missing .agent/browser/staging.json" in r.out
    _login(tmp_root)
    r = run(["--json", "worker-mcp", "list"])
    assert r.code == 0 and {row["profile"]: row.get("ready") for row in r.json}["qa-staging"] is True
    r = run(
        [
            "dispatch",
            "new",
            "alpha",
            "--feature",
            "1",
            "--prompt",
            "qa pass",
            "--mcp",
            "qa-staging",
            "--dry-run",
        ]
    )
    assert r.code == 0, r.err
    assert "mcp: ['qa-staging']" in r.out


ENDPOINT = "https://helper.internal/v1/zq7suffix"
ENDPOINT_BLOCK = f"""
worker_mcp:
  "{ENDPOINT}":
    server: hidden
    command: hidden-server
  helper:
    server: "{ENDPOINT}"
    command: helper-server
  helper-again:
    server: "{ENDPOINT}"
    command: helper-server
  login:
    server: login
    command: login-server
    requires_file: ".agent/{ENDPOINT}.json"
    requires_hint: "save a login with npx playwright open --save-storage .agent/login.json {ENDPOINT}"
"""


def _refused(run, *mcp: str) -> str:
    args = ["dispatch", "new", "alpha", "--feature", "1", "--prompt", "qa pass", "--dry-run"]
    r = run([*args, *[a for name in mcp for a in ("--mcp", name)]])
    assert r.code != 0, r.out
    shown = r.out + r.err
    for secret in ("helper.internal", "zq7suffix"):
        assert secret not in shown, shown
    return shown


def test_a_launch_names_the_profile_fix_without_an_endpoint(tmp_root: Path, run, fake_claude):
    _feature_with_alpha(tmp_root, run)
    reg = tmp_root / "agent.yaml"
    base = reg.read_text(encoding="utf-8")
    _configure(tmp_root, ENDPOINT_BLOCK)
    assert (
        "unknown worker mcp profile '[redacted-name]'; agent.yaml knows: [redacted-name], helper, "
        "helper-again, login"
    ) in _refused(run, f"{ENDPOINT}/other")
    assert "unknown worker mcp profile 'nope'; agent.yaml knows: [redacted-name], helper" in _refused(
        run, "nope"
    )
    assert "worker mcp profile 'helper-again' reuses the server name '[redacted-name]'" in _refused(
        run, "helper", "helper-again"
    )
    unready = (
        "worker mcp profile 'login' needs .agent/[redacted-endpoint], which is missing; save a login "
        "with npx playwright open --save-storage .agent/login.json [redacted-endpoint]"
    )
    assert unready in _refused(run, "login")
    # the profile launcher reads the same check
    with pytest.raises(AgentError) as exc:
        worker_mcp.exec_argv(tmp_root, "login", tmp_root / "out", "001-01")
    assert exc.value.message == unready
    assert not (tmp_root / ".agent" / "dispatches").exists()

    # a broken profile block is named without its source text
    reg.write_text(base, encoding="utf-8")
    _configure(tmp_root, f'\nworker_mcp:\n  "{ENDPOINT}": {{command: c}}\n')
    assert f"{reg}: worker_mcp.[redacted-name].server must be a non-empty string" in _refused(run, "x")
    reg.write_text(
        base + f'\nworker_mcp:\n  broken:\n    server: s\n    command: "{ENDPOINT}\n', encoding="utf-8"
    )
    line = len(reg.read_text(encoding="utf-8").splitlines()) + 1  # the stream ends past the last line
    assert f"{reg}: invalid YAML at line {line}, column 1: found unexpected end of stream" in _refused(
        run, "x"
    )


GRANT_BLOCK = f"""
worker_mcp:
  bare: {{server: bare, command: s, allow: [mcp__bare]}}
  bare-deny: {{server: bare, command: s, allow: ["mcp__bare__*"], deny: [mcp__bare]}}
  pattern: {{server: bare, command: s, allow: ["mcp__bare__read_*"]}}
  elsewhere: {{server: bare, command: s, allow: [mcp__other__read]}}
  hidden: {{server: "{ENDPOINT}", command: s, allow: ["mcp__{ENDPOINT}"]}}
  exact: {{server: bare, command: s, allow: [mcp__bare__read, "Bash(stat x:*)"], deny: ["Bash(stat x y:*)"]}}
  whole: {{server: bare, command: s, allow: ["mcp__bare__*"], deny: [mcp__bare__write]}}
"""
BARE = "is a bare server rule, which Claude reads as the whole server and the Codex hook matches to no tool"
OTHER = "is not an exact tool or wildcard rule for the profile's server 'bare'"


def test_a_bare_or_malformed_profile_grant_refuses_and_exact_rules_launch(tmp_root: Path, run, fake_claude):
    _feature_with_alpha(tmp_root, run)
    _configure(tmp_root, GRANT_BLOCK)
    for profile, problem in [
        ("bare", f"allow 'mcp__bare' {BARE}; write `mcp__bare__*` for the whole server"),
        ("bare-deny", f"deny 'mcp__bare' {BARE}; write `mcp__bare__*`"),
        ("pattern", f"allow 'mcp__bare__read_*' {OTHER}"),
        ("elsewhere", f"allow 'mcp__other__read' {OTHER}"),
        ("hidden", f"allow [redacted-mcp-rule] {BARE}; write `mcp__<server>__*`"),
    ]:
        assert f"dispatch.mcp profile {profile!r} {problem}" in _refused(run, profile)
    assert not (tmp_root / ".agent" / "dispatches").exists()

    for profile in ("exact", "whole"):
        p = dispatch.new(tmp_root, repo="alpha", feature="1", prompt="qa", mcp=[profile], dry_run=True)
        assert "bare" in p["options"]["mcp_servers"]
    rules = dispatch.build_settings(
        tmp_root, repos_mod.load_registry(tmp_root).require("alpha"), mcp=["exact"]
    )
    assert {"mcp__bare__read", "Bash(stat x:*)"} <= set(rules["permissions"]["allow"])
    assert "Bash(stat x y:*)" in rules["permissions"]["deny"]


# ------------------------------------------------------------------ a recorded profile in the policy hooks

SECRET = "https://alpha.internal/token"
HELPER_BLOCK = f"""
worker_mcp:
  helper:
    server: helper
    command: helper-server
    allow: [mcp__helper__*]
    deny: [mcp__helper__private, "Bash(stat {SECRET}:*)"]
  extra:
    server: extra
    command: extra-server
    allow: [mcp__extra__*]
    deny: [mcp__extra__drop]
"""


def _profile_dispatch(root: Path, monkeypatch, mcp: list[str]) -> dict:
    """A queued dispatch `new` recorded with `mcp` profiles, product server alpha selected and
    granted next to the profile's whole server, as agent.yaml and the repo configure them."""
    from dispatch_helpers import _new_fast, _set_alpha_dispatch  # pyright: ignore[reportPrivateUsage]

    _configure(root, HELPER_BLOCK)
    _set_alpha_dispatch(root, repo_mcp=["alpha", SECRET], allowed_tools=["mcp__alpha__*", "mcp__helper__*"])
    servers = {"alpha": {"command": "alpha-server"}, SECRET: {"type": "http", "url": SECRET}}
    clone = root / "repos/alpha-api"
    (clone / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    for git in (["add", ".mcp.json"], ["commit", "-qm", "product mcp"]):
        subprocess.run(["git", "-C", str(clone), *git], check=True, capture_output=True)
    monkeypatch.setattr(
        dispatch.supervisor, "popen", type("P", (), {"pid": 1, "__init__": lambda self, *a, **k: None})
    )
    d = _new_fast(root, watch=False, mcp=mcp, base="HEAD")
    assert dispatch.load(root, d["id"])["mcp"] == mcp
    return d


def _codex(root: Path, d: dict, tool: str, **tool_input: str) -> tuple[str, str]:
    from agent_cli.dispatch import codex_permissions

    return codex_permissions.decision(root, d["id"], {"tool_name": tool, "tool_input": tool_input})


def test_the_codex_hook_holds_the_recorded_profiles_denies(droot: Path, monkeypatch):
    d = _profile_dispatch(droot, monkeypatch, ["helper", "extra"])
    verdicts = {
        "mcp__helper__private": "deny",
        "mcp__helper__read": "allow",
        "mcp__alpha__read": "allow",
        "mcp__extra__drop": "deny",
        "mcp__extra__read": "allow",
        "mcp__other__read": "deny",
    }
    assert {tool: _codex(droot, d, tool)[0] for tool in verdicts} == verdicts
    assert _codex(droot, d, "Bash", command="rm -rf build") == (
        "deny",
        "Denied by dispatch rule Bash(rm -r*)",
    )
    assert _codex(droot, d, "Bash", command=f"stat {SECRET} -L") == (
        "deny",
        "Denied by dispatch rule [redacted-rule]",
    )

    from agent_cli.dispatch import codex_permissions

    monkeypatch.setenv("AGENT_ROOT", str(droot))
    monkeypatch.setenv("AGENT_DISPATCH_ID", d["id"])
    out = codex_permissions.run_event({"tool_name": "Bash", "tool_input": {"command": f"stat {SECRET}"}})
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "Denied by dispatch rule [redacted-rule]"
    [event] = dispatch.load(droot, d["id"])["permission_events"]
    assert event["decision"] == "deny" and "alpha.internal" not in json.dumps(event)

    # the fallback decision keeps the profile's Bash denies and withholds the rule
    rec = dispatch.load(droot, d["id"])
    alpha = repos_mod.load_registry(droot).require("alpha")
    behavior, reason = dispatch.rules.decide("Bash", {"command": f"stat {SECRET}"}, rec, alpha, droot)
    assert behavior == "deny" and reason.startswith("`[redacted-rule]` is never a worker's"), reason
    behavior, reason = dispatch.rules.decide("Bash", {"command": "rm -rf build"}, rec, alpha, droot)
    assert behavior == "deny" and reason.startswith("`Bash(rm -r*)` is never a worker's"), reason
    assert dispatch.rules.decide("Bash", {"command": "stat README.md"}, rec, alpha, droot)[0] == "allow"

    # without a profile the hook keeps the repo grants
    rec["mcp"] = []
    dispatch.save(droot, rec)
    assert _codex(droot, d, "mcp__helper__private")[0] == "allow"
    assert _codex(droot, d, "mcp__extra__read")[0] == "deny"
    assert _codex(droot, d, "Bash", command=f"stat {SECRET}")[0] == "allow"


def test_a_malformed_or_unknown_recorded_profile_refuses_every_tool(droot: Path, monkeypatch, capsys):
    import io
    import sys

    from agent_cli.dispatch import codex_permissions

    d = _profile_dispatch(droot, monkeypatch, ["helper"])
    alpha = repos_mod.load_registry(droot).require("alpha")
    monkeypatch.setenv("AGENT_ROOT", str(droot))
    monkeypatch.setenv("AGENT_DISPATCH_ID", d["id"])
    for recorded in ["helper", {"helper": 1}, ["helper", "helper"], [1], ["gone"], [""]]:
        rec = dispatch.load(droot, d["id"])
        rec["mcp"] = recorded
        dispatch.save(droot, rec)
        for tool in ("mcp__helper__read", "mcp__alpha__read", "Bash"):
            with pytest.raises(AgentError, match="selected worker MCP profiles are unknown"):
                _codex(droot, d, tool, command="stat README.md")
        with pytest.raises(AgentError, match="selected worker MCP profiles are unknown"):
            dispatch.rules.decide("Bash", {"command": "stat README.md"}, rec, alpha, droot)
        monkeypatch.setattr(
            sys, "stdin", io.StringIO(json.dumps({"tool_name": "mcp__alpha__read", "tool_input": {}}))
        )
        capsys.readouterr()
        assert codex_permissions.main() == 0, recorded
        assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"
