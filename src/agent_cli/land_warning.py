"""Identify live sessions that must restart after wiring, MCP schema or state-format changes.
Source-only changes run fresh on the next tool call; cached contracts need explicit restart
guidance.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path, PurePosixPath
from typing import Any

from . import land_observe as obs
from .core import agent_path
from .records.narrow import as_dict_or_none, dict_list, str_list

WIRING = (
    ".claude/settings.json",
    ".codex/hooks.json",
    ".codex/config.toml",
    ".mcp.json",
    ".mcp.example.json",
    "bin/agent-hook.cjs",
    # How a Windows manager's MCP server starts (`.mcp.json` -> `bin/agent` -> `agent.cmd`);
    # a running server keeps the old launch until it restarts.
    "bin/agent.cmd",
)
HOOK_NAMES = ("src/agent_cli/hooks/run.py", "src/agent_cli/hooks/groups.py")
MCP = "src/agent_cli/mcp/**"
STATE = (
    "src/agent_cli/records/**",
    "src/agent_cli/dispatch/state.py",
    "src/agent_cli/land_journal.py",
    "src/agent_cli/leases.py",
    "src/agent_cli/sessions.py",
    "src/agent_cli/session_identity.py",
    "src/agent_cli/status.py",
    "src/agent_cli/vault/live.py",
    "src/agent_cli/telemetry/schema.py",
)
LIVE_JSON = agent_path(Path(), "live.json")
SHOWN = 12


def _matches(path: str, pattern: str) -> bool:
    return PurePosixPath(path).full_match(pattern)


def _source(git: obs.Git, commit: str, path: str) -> ast.Module | None:
    text = git.run("show", f"{commit}:{path}", ok=(0, 128))
    if not text:
        return None
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _alias_names(value: ast.expr) -> set[str]:
    """The names an `ALIASES` mapping holds: a dict literal's string keys, or the strings a dict
    comprehension iterates over (`{name: why for name in (...)}`)."""

    def strings(items: list[ast.expr | None]) -> set[str]:
        return {e.value for e in items if isinstance(e, ast.Constant) and isinstance(e.value, str)}

    if isinstance(value, ast.Dict):
        return strings(list(value.keys))
    if isinstance(value, ast.DictComp):
        found: set[str] = set()
        for gen in value.generators:
            if isinstance(gen.iter, (ast.Tuple, ast.List, ast.Set)):
                found |= strings(list(gen.iter.elts))
        return found
    return set()


def hook_names(git: obs.Git, commit: str) -> set[str]:
    """The hook and group names `agent hook <name>` accepts at `commit`: the strings of
    `run.py`'s `NAMES` tuple and the first argument of each `Group(...)` in `groups.py`."""
    names: set[str] = set()
    tree = _source(git, commit, HOOK_NAMES[0])
    for node in tree.body if tree else []:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "NAMES" for t in node.targets
        ):
            if isinstance(node.value, ast.Tuple):
                names |= {
                    e.value
                    for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
        elif (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and node.value is not None
            and any(
                isinstance(t, ast.Name) and t.id == "ALIASES"
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
        ):
            # A removed hook's no-op alias is still callable, so old wiring keeps working until
            # it goes.
            names |= _alias_names(node.value)
    tree = _source(git, commit, HOOK_NAMES[1])
    nodes: list[ast.AST] = list(ast.walk(tree)) if tree else []
    for node in nodes:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Group":
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
    return names


def _literal_values(node: ast.expr) -> list[str]:
    """The string values of `Literal["a", "b"]`."""
    if not (
        isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "Literal"
    ):
        return []
    inner = node.slice
    items = inner.elts if isinstance(inner, ast.Tuple) else [inner]
    return [e.value for e in items if isinstance(e, ast.Constant) and isinstance(e.value, str)]


def mcp_surface(git: obs.Git, commit: str, paths: list[str]) -> dict[str, set[str]]:
    """`{"Name": {"value", ...}, "tool fn": {"param", ...}}` for the MCP modules in `paths`:
    each module-level `Literal` alias's values, and the parameter names of each public function
    in a tool-spec module (`specs_*.py`; the others hold helpers, not tools)."""
    surface: dict[str, set[str]] = {}
    for path in paths:
        specs = PurePosixPath(path).name.startswith("specs_")
        tree = _source(git, commit, path)
        for node in tree.body if tree else []:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                values = _literal_values(node.value)
                if values:
                    surface[node.targets[0].id] = set(values)
            elif specs and isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                a = node.args
                params = [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
                surface[f"tool {node.name}"] = {p for p in params if p not in ("ctx", "self")}
    return surface


def moved(before: dict[str, set[str]], after: dict[str, set[str]]) -> list[str]:
    """What a client holding the old schema can no longer call: removed tools, parameters and
    `Literal` values. A rename reads as a removal; an added alias keeps it callable."""
    out: list[str] = []
    for name, old in sorted(before.items()):
        if name not in after:
            out.append(f"{name} removed")
            continue
        gone = sorted(old - after[name])
        if gone:
            what = "parameter" if name.startswith("tool ") else "value"
            out.append(f"{name}: {what}{'s' if len(gone) > 1 else ''} {', '.join(gone)} removed or moved")
    return out


def live_sessions(root: Path) -> tuple[list[str], list[str]]:
    """`(managers, workers)` named in `.agent/live.json`: every manager listed, and the workers
    still running. A missing or unreadable file names none; the text then says so."""
    try:
        live = as_dict_or_none(json.loads((root / LIVE_JSON).read_text(encoding="utf-8")))
    except OSError, ValueError:
        return [], []
    if live is None:
        return [], []
    managers = [
        f"{m.get('name') or 'manager'} ({str(m.get('session_id') or '?')[:8]})"
        for m in dict_list(live.get("managers"))
        if m.get("state") != "ended"
    ]
    workers = [
        str(w.get("id"))
        for w in dict_list(live.get("workers"))
        if w.get("state") in ("queued", "running") and w.get("id")
    ]
    return managers, workers


def notice(root: Path, git: obs.Git, old: str, cand: str, paths: list[str]) -> dict[str, Any] | None:
    """What `old -> cand` changes that a live session holds from its start, and who must restart;
    None when it changes none of it. Best effort: a failure to read reads as no finding."""
    wiring = sorted(p for p in paths if p in WIRING)
    hooks_gone: list[str] = []
    if any(p in HOOK_NAMES for p in paths):
        hooks_gone = sorted(hook_names(git, old) - hook_names(git, cand))
    mcp_paths = sorted(p for p in paths if _matches(p, MCP))
    mcp_moved: list[str] = []
    if mcp_paths:
        modules = [p for p in mcp_paths if p.endswith(".py")]
        mcp_moved = moved(mcp_surface(git, old, modules), mcp_surface(git, cand, modules))
    state = sorted(p for p in paths if any(_matches(p, g) for g in STATE))
    if not (wiring or hooks_gone or mcp_paths or state):
        return None
    managers, workers = live_sessions(root)
    return {
        "wiring": wiring,
        "hooks_removed": hooks_gone,
        "mcp": mcp_paths,
        "mcp_moved": mcp_moved,
        "state": state,
        "managers": managers,
        "workers": workers,
    }


def _few(items: list[str]) -> str:
    return ", ".join(items[:SHOWN]) + (f" and {len(items) - SHOWN} more" if len(items) > SHOWN else "")


def text(found: dict[str, Any] | None) -> str:
    """The warning `land` prints, or "" when there is nothing to say."""
    if not found:
        return ""
    lines = ["RESTART WARNING: this landing changes what live sessions loaded when they started."]
    if found.get("wiring"):
        lines.append(f"- hook or MCP wiring: {_few(found['wiring'])}")
    if found.get("hooks_removed"):
        lines.append(f"- hook names gone (old wiring still calls them): {_few(found['hooks_removed'])}")
    if found.get("mcp"):
        lines.append(f"- MCP tools changed: {_few(found['mcp'])}")
    if found.get("mcp_moved"):
        lines.append(f"- MCP calls a connected session can no longer make: {_few(found['mcp_moved'])}")
    if found.get("state"):
        lines.append(f"- state formats may have changed: {_few(found['state'])}")
    managers, workers = str_list(found.get("managers")), str_list(found.get("workers"))
    if managers:
        lines.append(f"Restart these managers: {_few(managers)}.")
    else:
        lines.append(
            f"No live manager is listed in {LIVE_JSON.as_posix()}; restart any manager started before this."
        )
    if workers:
        lines.append(f"Running workers use main's hooks and MCP too: {_few(workers)}.")
    return "\n".join(lines)
