"""Serve typed manager and worker tools over stdio, enforcing worker-profile scope. Tools call shared
ops on the same dictionaries the CLI renders (see ADR-0005). Load MCP lazily so hooks and ordinary
CLI calls avoid its import cost. Stdout is wire-only; call_op redirects operation output to stderr
as a backstop.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import features as features_mod
from ..core import AgentError, json_default
from ..records.narrow import as_list_or_none
from .coverage import mcp_wording
from .specs_dispatch import DISPATCH_TOOLS, WORKER_TOOLS
from .specs_features import FEATURE_TOOLS
from .specs_harness import HARNESS_TOOLS
from .specs_reconcile import RECONCILE_TOOLS
from .specs_repos import REPO_TOOLS
from .tools import Ctx, Op, ToolSpec

MANAGER_TOOLS: dict[str, ToolSpec] = {
    **FEATURE_TOOLS,
    **RECONCILE_TOOLS,
    **REPO_TOOLS,
    **DISPATCH_TOOLS,
    **HARNESS_TOOLS,
}

PROFILES: dict[str, dict[str, ToolSpec]] = {"manager": MANAGER_TOOLS, "worker": WORKER_TOOLS}


# ---------------------------------------------------------------- wiring


def worker_ctx(root: Path) -> Ctx:
    """The worker profile refuses to start half-configured. AGENT_ROOT, AGENT_FEATURE and
    AGENT_DISPATCH_ID must be set, and the feature must be open."""
    missing = [v for v in ("AGENT_ROOT", "AGENT_FEATURE", "AGENT_DISPATCH_ID") if not os.environ.get(v)]
    if missing:
        raise AgentError(
            "worker profile needs " + ", ".join(missing) + " in the environment (dispatch sets them)"
        )
    feature = features_mod.resolve(root, os.environ["AGENT_FEATURE"])
    if not feature.is_open:
        raise AgentError(
            f"feature {feature.number} {feature.slug} is {feature.status}; a worker cannot record into a closed feature"
        )
    tasks_env = os.environ.get("AGENT_DISPATCH_TASKS")
    tasks: list[int] | None = None
    if tasks_env is not None and tasks_env.strip():
        try:
            tasks = [int(t) for t in tasks_env.replace(" ", "").split(",") if t]
        except ValueError as exc:
            raise AgentError(
                f"AGENT_DISPATCH_TASKS must be comma-separated task numbers; got {tasks_env!r}"
            ) from exc
    return Ctx(
        root=root,
        profile="worker",
        feature=str(feature.id),
        dispatch_id=os.environ["AGENT_DISPATCH_ID"],
        dispatch_tasks=tasks,
    )


def to_jsonable(data: Any) -> Any:
    return json.loads(json.dumps(data, default=json_default))


def call_op(fn: Callable[..., Op], ctx: Ctx, /, **kw: Any) -> dict[str, Any]:
    """Run one op for a tool call. Stdout is the wire, so anything that prints goes to
    stderr. An AgentError is the model's problem (ToolError → is_error result). A list
    result becomes {items, count, summary}; a dict gains `summary`."""
    from mcp.server.mcpserver.exceptions import ToolError

    with contextlib.redirect_stdout(sys.stderr):
        try:
            op = fn(ctx, **kw)
        except AgentError as exc:
            raise ToolError(mcp_wording(exc.message)) from None
    return shape_result(op)


def shape_result(op: Op) -> dict[str, Any]:
    """The tool result. The summary is the CLI's text with each CLI reference rewritten into
    the tool that does it (over MCP, a model is never pointed at the shell)."""
    data = to_jsonable(op.data)
    summary = mcp_wording(op.text)
    items = as_list_or_none(data)
    if items is not None:
        return {"items": items, "count": len(items), "summary": summary}
    if not isinstance(data, dict):
        return {"result": data, "summary": summary}
    return {**data, "summary": summary}


def _call_fresh(tool_name: str, ctx: Ctx, /, **kw: Any) -> dict[str, Any]:
    """Use a positional-only operation name so typed tool arguments named name pass through kw without
    colliding.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    from .fresh import run_fresh

    try:
        return run_fresh(ctx.root, ctx.profile, tool_name, kw)
    except AgentError as exc:
        raise ToolError(mcp_wording(exc.message)) from None


def bind_tool(name: str, spec: ToolSpec, ctx: Ctx, fresh: bool = False) -> Callable[..., dict[str, Any]]:
    """A tool function with `ctx` closed over and hidden from the schema. With `fresh`, the
    call runs in a new process on the current code (`fresh.py`); the served stdio server always
    passes it."""
    sig = inspect.signature(spec.fn, eval_str=True)
    params = list(sig.parameters.values())[1:]

    def tool(**kw: Any) -> dict[str, Any]:
        if fresh:
            return _call_fresh(name, ctx, **kw)
        return call_op(spec.fn, ctx, **kw)

    tool.__name__ = name
    tool.__qualname__ = name
    tool.__doc__ = spec.description
    tool.__signature__ = sig.replace(parameters=params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict[str, Any]}
    return tool


def select_tools(profile: str, tools: list[str] | None) -> dict[str, ToolSpec]:
    try:
        table = PROFILES[profile]
    except KeyError:
        raise AgentError(f"profile must be one of {', '.join(PROFILES)}; got {profile!r}") from None
    if not tools:
        return dict(table)
    unknown = [t for t in tools if t not in table]
    if unknown:
        raise AgentError(
            f"unknown tool(s) for the {profile} profile: {', '.join(unknown)}; known: {', '.join(table)}"
        )
    return {t: table[t] for t in tools}


def build_server(
    root: Path, profile: str = "manager", tools: list[str] | None = None, fresh: bool = False
) -> Any:
    """An MCPServer named `agent` with the profile's tools registered. Raises AgentError for
    a bad profile, an unknown tool name, or a half-configured worker. `fresh` runs each call
    in a new process; `serve` turns it on."""
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    specs = select_tools(profile, tools)
    ctx = worker_ctx(root) if profile == "worker" else Ctx(root=root, profile=profile)
    server = MCPServer("agent", log_level="WARNING")
    for name, spec in specs.items():
        server.add_tool(
            bind_tool(name, spec, ctx, fresh=fresh),
            name=name,
            title=spec.title,
            description=spec.description,
            annotations=ToolAnnotations(
                title=spec.title,
                read_only_hint=spec.read_only,
                destructive_hint=spec.destructive,
                idempotent_hint=spec.idempotent or spec.read_only,
                open_world_hint=False,
            ),
            structured_output=True,
        )
    return server


def serve(root: Path, profile: str = "manager", tools: list[str] | None = None) -> None:
    """Run over stdio until the client hangs up. Auto-open is off. A headless server must
    never launch Obsidian."""
    os.environ["AGENT_NO_AUTOOPEN"] = "1"
    # Stamp the start, so anything can tell later whether this process predates the code it
    # would run (`staleness.py`). Never fails the serve.
    from .staleness import write_stamp

    write_stamp(root, profile, fresh=True)
    server = build_server(root, profile, tools, fresh=True)
    server.run(transport="stdio")
