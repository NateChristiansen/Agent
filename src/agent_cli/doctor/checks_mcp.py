"""Check MCP registration, configuration and cached schema freshness."""

from __future__ import annotations

import json
from pathlib import Path

from .. import launcher
from ..records.narrow import as_dict, as_dict_or_none, as_list, as_list_or_none, dig, str_list
from . import checks_notes
from .check import Check, new_check

MCP_SERVER_NAME = "agent"
MCP_ALLOW_RULE = "mcp__agent__*"


def launcher_command_ok(root: Path, command: str) -> bool:
    """Accept this root's launcher, venv interpreter or legacy console shim. The shim remains
    compatible but holds agent.exe open while the server runs.
    """
    candidates = [
        (launcher.argv_prefix(root, windows=False)[0], False),
        (launcher.argv_prefix(root, windows=True)[0], True),
        (str(root / launcher.LEGACY_WINDOWS_ENTRY), True),
    ]
    for absolute, windows in candidates:
        relative = Path(absolute).relative_to(root).as_posix()
        accepted = {absolute, relative, f"./{relative}"}
        accepted |= {f"${{CLAUDE_PROJECT_DIR:-.}}/{relative}", f"${{CLAUDE_PROJECT_DIR}}/{relative}"}
        if windows:
            if command.replace("\\", "/").casefold() in {a.replace("\\", "/").casefold() for a in accepted}:
                return True
        elif command in accepted:
            return True
    return False


def check_codex_mcp_wiring(root: Path) -> list[Check]:
    """Check Codex's project registration and a real read-only manager tool call."""
    import tomllib

    from .. import codex_mcp

    path = root / codex_mcp.CONFIG
    if not path.exists():
        return [new_check("Codex agent MCP", "warn", f"no {codex_mcp.CONFIG}; run `bin/agent setup`")]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [new_check("Codex agent MCP", "fail", f"cannot read {codex_mcp.CONFIG}: {exc}")]
    entry = as_dict_or_none(dig(data, "mcp_servers", "agent"))
    wanted = codex_mcp.expected(root)
    if entry is None or any(entry.get(key) != value for key, value in wanted.items()):
        return [
            new_check(
                "Codex agent MCP",
                "fail",
                f"{codex_mcp.CONFIG} has no matching mcp_servers.agent; run `bin/agent setup`",
            )
        ]
    if entry.get("enabled") is False:
        return [new_check("Codex agent MCP", "fail", "mcp_servers.agent is disabled in Codex config")]
    enabled_value = entry.get("enabled_tools")
    enabled_tools = as_list_or_none(enabled_value)
    disabled_tools = as_list_or_none(entry.get("disabled_tools") or [])
    read_tools = {"feature_list", "feature_show"}
    if (
        (enabled_value is not None and enabled_tools is None)
        or disabled_tools is None
        or (enabled_tools is not None and not all(isinstance(v, str) for v in enabled_tools))
        or not all(isinstance(v, str) for v in disabled_tools)
        or (enabled_tools is not None and not read_tools <= set(enabled_tools))
        or read_tools & set(disabled_tools)
    ):
        return [new_check("Codex agent MCP", "fail", "manager read tools are filtered in Codex config")]
    if not Path(str(entry["command"])).is_file():
        return [new_check("Codex agent MCP", "fail", f"{entry['command']} is missing; run `uv sync`")]
    ok, detail = codex_mcp.probe(root, entry)
    return [new_check("Codex agent MCP", "ok" if ok else "fail", detail)]


def check_mcp_wiring(root: Path) -> list[Check]:
    """Check agent registration, enabling, tool permissions and MCP importability, then check every
    other configured server. Missing config warns for fresh or test roots; present invalid
    configuration or a missing package fails. Registration without enabling or permission coverage
    does not make a server reachable.
    """
    import importlib.util

    out: list[Check] = []
    if importlib.util.find_spec("mcp") is None:
        out.append(new_check("mcp wiring", "fail", "the `mcp` package is not importable; run `uv sync`"))
    mcp_json = root / ".mcp.json"
    if not mcp_json.exists():
        out.append(
            new_check(
                "mcp wiring", "warn", "no .mcp.json; the agent MCP server is not registered with Claude Code"
            )
        )
        return out
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return out + [new_check("mcp wiring", "fail", f".mcp.json is not valid JSON: {exc}")]
    servers = as_dict(dig(data, "mcpServers"))
    server = as_dict_or_none(servers.get(MCP_SERVER_NAME))
    problems: list[str] = []
    if server is None:
        problems.append(f".mcp.json has no mcpServers.{MCP_SERVER_NAME} entry")
    else:
        command = str(server.get("command") or "")
        if not launcher_command_ok(root, command):
            problems.append(
                f"command {command!r} should be this harness's launcher: bin/agent or .venv/Scripts/python.exe"
            )
        args = server.get("args")
        argv = as_list(args)
        module = launcher.WINDOWS_MODULE
        if argv[: len(module)] == module:  # `python.exe -I -m agent_cli mcp ...`
            argv = argv[len(module) :]
        if not argv or argv[0] != "mcp":
            problems.append(f"args {args!r} should start with 'mcp'")
        if server.get("alwaysLoad") is not True:
            problems.append("alwaysLoad should be true (deferred tools tempt the model back to bash)")
    settings = root / ".claude" / "settings.json"
    sdata: dict[str, object] | None = None
    if settings.exists():
        try:
            loaded = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = None
        sdata = as_dict(loaded)
        # merged with settings.local.json: since ADR-0012 `.mcp.json` comes in from the notes
        # repo while the enablement that matches it is per machine and gitignored
        enabled = checks_notes.enabled_servers(root)
        if MCP_SERVER_NAME not in enabled:
            problems.append(f"settings.json enabledMcpjsonServers lacks {MCP_SERVER_NAME!r}")
        if MCP_ALLOW_RULE not in as_list(dig(sdata, "permissions", "allow")):
            problems.append(f"settings.json permissions.allow lacks {MCP_ALLOW_RULE!r}")
    else:
        problems.append("no .claude/settings.json to enable the server in")
    if problems:
        out.append(new_check("mcp wiring", "fail", "; ".join(problems)))
    elif not out:
        out.append(
            new_check("mcp wiring", "ok", f"{MCP_SERVER_NAME} server registered, enabled, and allowed")
        )
    return out + _mcp_servers(servers, sdata, checks_notes.enabled_servers(root), _local_rules(root))


def _local_rules(root: Path) -> list[str]:
    """allow and ask rules from the gitignored `.claude/settings.local.json`. Claude Code
    merges them with settings.json, so a server one machine allows there is reachable."""
    path = root / ".claude" / "settings.local.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return []
    perms = as_dict(dig(data, "permissions"))
    return [str(r) for r in as_list(perms.get("allow")) + as_list(perms.get("ask"))]


def _mcp_servers(
    servers: dict[str, object],
    sdata: dict[str, object] | None,
    enabled: list[str],
    local_rules: list[str] | None = None,
) -> list[Check]:
    """Every server in .mcp.json must be enabled and have at least one permission rule.

    Nothing may be enabled that is no longer registered. This check is generic on purpose:
    the next server gets checked without anyone remembering to add it here."""
    others = sorted(n for n in servers if n != MCP_SERVER_NAME)
    if sdata is None:
        return []
    permissions = as_dict(sdata.get("permissions"))
    rules = str_list(permissions.get("allow")) + str_list(permissions.get("ask")) + list(local_rules or [])
    out: list[Check] = []

    # A warn, not a fail: a server registered in the owner's .mcp.json but not enabled in
    # `.claude/settings.local.json` on this machine is a normal state (see ADR-0012), not a wiring
    # error.
    not_enabled = [n for n in others if n not in enabled]
    if not_enabled:
        out.append(
            new_check(
                "mcp servers",
                "warn",
                f"registered in .mcp.json but missing from enabledMcpjsonServers: "
                f"{', '.join(not_enabled)} — a server that is not enabled never loads; "
                f"enable it in .claude/settings.local.json when this machine uses it",
            )
        )

    unreachable = [
        n for n in others if not any(r == f"mcp__{n}" or r.startswith(f"mcp__{n}__") for r in rules)
    ]
    if unreachable:
        out.append(
            new_check(
                "mcp servers",
                "warn",
                f"no permissions.allow or .ask rule matches: {', '.join(unreachable)} — every "
                "call to them prompts the owner",
            )
        )

    stale = [n for n in enabled if n not in servers]
    if stale:
        out.append(
            new_check(
                "mcp servers",
                "warn",
                f"enabled in settings.json but not in .mcp.json: {', '.join(sorted(stale))}",
            )
        )

    if not out:
        listed = ", ".join(others) if others else "none besides agent"
        out.append(new_check("mcp servers", "ok", f"enabled and reachable: {listed}"))
    return out
