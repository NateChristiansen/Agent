"""Worker MCP profiles: extra MCP servers a dispatch can hand its worker (`--mcp <profile>`).

A profile is a named entry in the `worker_mcp:` block of agent.yaml. The harness knows nothing
about any particular server. A browser or a docs search server is configuration,
not code (the ADR that superseded ADR-0010). The owner's registry says which servers exist.
The manager's own session never loads them. Only the dispatch that names a profile does.

    worker_mcp:
      qa-staging:
        server: playwright            # the key in the worker's mcp config; tools are mcp__<server>__*
        command: npx
        args: [--yes, "@playwright/mcp@0.0.81", --headless, --isolated,
               --allowed-origins, "https://staging.example.com",
               --storage-state, "{root}/.agent/browser/staging.json", --output-dir, "{output_dir}"]
        allow: [mcp__playwright__*]   # permission rules added to the worker's allow list
        deny: [mcp__playwright__browser_evaluate, mcp__playwright__browser_run_code]
        requires_file: .agent/browser/staging.json
        requires_hint: "npx playwright open --save-storage .agent/browser/staging.json https://staging.example.com"

`{root}`, `{output_dir}` and `{dispatch_id}` expand at launch. `requires_file` is checked
before a worktree or a record exists, so a dispatch whose login was never saved refuses to
start instead of failing on its first tool call. `deny` rules join the worker's deny list and
win over `allow`, so a profile can grant a whole server and still withhold its riskiest tools.
Its `mcp__` rules name its own server as `mcp__<server>__*` or `mcp__<server>__<tool>`, the two
spellings Claude and the Codex hook read alike; with a bare `mcp__<server>` the worker's settings
do not build, so the dispatch refuses to launch.
The example pins the package and limits navigation to one origin: a browser worker runs
unattended, so an unpinned package or an open web is a supply-chain and prompt-injection risk.

The worker's mcp config does not run the command directly. It runs `bin/agent worker-mcp exec
<profile>`, which changes into the dispatch's output directory and execs. Claude Code's mcp
config has no cwd field, and a worker's cwd is a product repo, whose own package.json can make
npm refuse to start the server. The output directory has no package.json, and files a
server writes by relative name land next to the dispatch's other files.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import launcher
from .config_file import registry_path
from .core import AgentError
from .permissions import shown_text
from .records.narrow import as_dict, as_dict_or_none, as_list, as_list_or_none

BLOCK = "worker_mcp"


@dataclass(frozen=True)
class Profile:
    name: str
    server: str
    command: str
    args: list[str] = field(default_factory=list[str])
    allow: list[str] = field(default_factory=list[str])
    deny: list[str] = field(default_factory=list[str])
    env: dict[str, str] = field(default_factory=dict[str, str])
    requires_file: str = ""
    requires_hint: str = ""


def validate(data: object) -> list[str]:
    """Problems with a `worker_mcp:` block, in the same shape `repos.validate_registry_data`
    reports them. An absent block is fine."""
    if data is None:
        return []
    block = as_dict_or_none(data)
    if block is None:
        return [f"'{BLOCK}' must be a mapping of profile name to server config"]
    problems: list[str] = []
    for name, raw in block.items():
        where = f"{BLOCK}.{_shown(name)}"
        cfg = as_dict_or_none(raw)
        if cfg is None:
            problems.append(f"{where} must be a mapping")
            continue
        for key in ("server", "command"):
            text = cfg.get(key)
            if not isinstance(text, str) or not text.strip():
                problems.append(f"{where}.{key} must be a non-empty string")
        for key in ("args", "allow", "deny"):
            val = cfg.get(key)
            items = as_list_or_none(val)
            if val is not None and (items is None or not all(isinstance(v, str) for v in items)):
                problems.append(f"{where}.{key} must be a list of strings")
        env = cfg.get("env")
        env_map = as_dict_or_none(env)
        pairs: list[tuple[object, object]] = list(env_map.items()) if env_map is not None else []
        if env is not None and (
            env_map is None or not all(isinstance(k, str) and isinstance(v, str) for k, v in pairs)
        ):
            problems.append(f"{where}.env must be a mapping of NAME to a string")
        for key in ("requires_file", "requires_hint"):
            val = cfg.get(key)
            if val is not None and not isinstance(val, str):
                problems.append(f"{where}.{key} must be a string")
    return problems


def _shown(name: object) -> str:
    """A profile or server name as an error may print it (`mcp_policy._label`): a repo may name
    either by its endpoint."""
    from .dispatch.mcp_policy import _label  # pyright: ignore[reportPrivateUsage]

    return _label(str(name))


def _parse(path: Path) -> object:
    """The registry's YAML. A parse error keeps its line, column and problem but not the source
    line PyYAML quotes, which may hold an endpoint or a credential."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        at = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise AgentError(
            f"{path}: invalid YAML{at}: {shown_text(exc.problem or exc.context or '')}"
        ) from None
    except yaml.YAMLError:
        raise AgentError(f"{path}: invalid YAML") from None


def load(root: Path) -> dict[str, Profile]:
    path = registry_path(root)
    if not path.exists():
        return {}
    block = as_dict(_parse(path)).get(BLOCK)
    problems = validate(block)
    if problems:
        raise AgentError(f"{path}: " + "; ".join(problems))
    out: dict[str, Profile] = {}
    for name, raw in as_dict(block).items():
        cfg = as_dict(raw)
        out[str(name)] = Profile(
            name=str(name),
            server=str(cfg["server"]),
            command=str(cfg["command"]),
            args=[str(a) for a in as_list(cfg.get("args"))],
            allow=[str(a) for a in as_list(cfg.get("allow"))],
            deny=[str(a) for a in as_list(cfg.get("deny"))],
            env={str(k): str(v) for k, v in as_dict(cfg.get("env")).items()},
            requires_file=str(cfg.get("requires_file") or ""),
            requires_hint=str(cfg.get("requires_hint") or ""),
        )
    return out


def require(root: Path, names: list[str] | tuple[str, ...]) -> list[Profile]:
    """The named profiles, each checked: known, no two on the same server key, and its
    `requires_file` present. Raises with the fix before anything is created; names print through
    `_shown` and the file and hint through `permissions.shown_text`."""
    if not names:
        return []
    known = load(root)
    out: list[Profile] = []
    servers: set[str] = set()
    for name in names:
        if name not in known:
            listed = (
                ", ".join(sorted(_shown(k) for k in known)) or "none; add a `worker_mcp:` block to agent.yaml"
            )
            raise AgentError(f"unknown worker mcp profile {_shown(name)!r}; agent.yaml knows: {listed}")
        prof = known[name]
        if prof.server in servers or prof.server == "agent":
            raise AgentError(
                f"worker mcp profile {_shown(name)!r} reuses the server name {_shown(prof.server)!r}"
            )
        servers.add(prof.server)
        if prof.requires_file and not (root / prof.requires_file).exists():
            hint = f"; {shown_text(prof.requires_hint)}" if prof.requires_hint else ""
            raise AgentError(
                f"worker mcp profile {_shown(name)!r} needs {shown_text(prof.requires_file)}, "
                f"which is missing{hint}"
            )
        out.append(prof)
    return out


def allow_rules(root: Path, names: list[str] | tuple[str, ...]) -> list[str]:
    return _rules(root, names, "allow")


def _rules(root: Path, names: list[str] | tuple[str, ...], kind: str) -> list[str]:
    rules: list[str] = []
    for prof in require(root, names):
        for rule in getattr(prof, kind):
            if rule not in rules:
                rules.append(rule)
    return rules


def expand(value: str, *, root: Path, output_dir: Path, dispatch_id: str) -> str:
    """Fill the three placeholders. Anything else in braces is left alone, so an argument
    that legitimately contains `{` does not break."""
    for key, val in (("root", str(root)), ("output_dir", str(output_dir)), ("dispatch_id", dispatch_id)):
        value = value.replace("{" + key + "}", val)
    return value


def output_dir(root: Path, dispatch_id: str, profile: str) -> Path:
    from .dispatch.state import file

    return file(root, dispatch_id, f".mcp-{profile}")


def servers(root: Path, d: dict[str, Any]) -> dict[str, Any]:
    """The worker mcp config entries for a dispatch record's `mcp` profiles. Each runs the
    launcher, which execs the real command from the output directory (module docstring)."""
    return {prof.server: server_config(root, d, prof) for prof in require(root, list(d.get("mcp") or []))}


def server_config(root: Path, d: dict[str, Any], prof: Profile) -> dict[str, Any]:
    """Launcher definition for an already checked profile; does not re-read the registry."""
    command, prefix = launcher.command_and_args(root)
    return {
        "command": command,
        "args": [
            *prefix,
            "worker-mcp",
            "exec",
            prof.name,
            "--output-dir",
            str(output_dir(root, d["id"], prof.name)),
            "--dispatch",
            str(d["id"]),
        ],
    }


def exec_argv(root: Path, name: str, out_dir: Path, dispatch_id: str) -> tuple[list[str], dict[str, str]]:
    (prof,) = require(root, [name])
    argv = [
        expand(a, root=root, output_dir=out_dir, dispatch_id=dispatch_id) for a in [prof.command, *prof.args]
    ]
    env = {k: expand(v, root=root, output_dir=out_dir, dispatch_id=dispatch_id) for k, v in prof.env.items()}
    return argv, env


def exec_profile(
    root: Path,
    name: str,
    out_dir: Path,
    dispatch_id: str,
    *,
    chdir: Callable[[str], object] = os.chdir,
    execvpe: Callable[[str, list[str], dict[str, str]], object] = os.execvpe,
) -> None:
    argv, env = exec_argv(root, name, out_dir, dispatch_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    chdir(str(out_dir))
    execvpe(argv[0], argv, {**os.environ, **env})


def listing(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for prof in load(root).values():
        row: dict[str, Any] = {"profile": prof.name, "server": prof.server, "command": prof.command}
        if prof.requires_file:
            row["requires_file"] = prof.requires_file
            row["ready"] = (root / prof.requires_file).exists()
            if not row["ready"] and prof.requires_hint:
                row["fix"] = prof.requires_hint
        rows.append(row)
    return rows


def format_listing(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no worker mcp profiles (agent.yaml `worker_mcp:`)"
    lines: list[str] = []
    for r in rows:
        state = ""
        if "ready" in r:
            state = "  ready" if r["ready"] else f"  missing {r['requires_file']}"
            if r.get("fix"):
                state += f"  → {r['fix']}"
        lines.append(f"{r['profile']:<16} {r['server']:<14} {r['command']}{state}")
    return "\n".join(lines)
