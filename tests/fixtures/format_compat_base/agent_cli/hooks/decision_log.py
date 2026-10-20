# ruff: noqa
from __future__ import annotations
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any
from ..records.narrow import as_dict, dig

MAX_REASON = 300
PROBE_ENV = "AGENT_HOOK_PROBE"
_SECRET_RE = re.compile(
    "(?i)\\b([A-Z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD|AUTH)[A-Z0-9_]*)=\\S+|\\b(?:authorization|proxy-authorization|cookie|x-api-key|api[-_]key)\\s*:\\s*\\S+(?:\\s+\\S+)?|\\b(?:bearer|basic|token)\\s+\\S+|[A-Za-z0-9+/_\\-]{32,}"
)
_ASSIGNMENT = re.compile("^[A-Za-z_][A-Za-z0-9_]*=")


def denial(root: Path, name: str, payload: dict[str, Any], out: str, seconds: float) -> None:
    reason = _denied(out)
    if reason is not None:
        if name == "ask-guard":
            from .ask_guard import logged_reason

            reason = logged_reason(reason)
        elif name == "peer-stop":
            from .peer_stop import logged_reason as peer_logged_reason

            reason = peer_logged_reason(reason)
        _record(root, "hook.denial", "deny", name, payload, reason, seconds)


def error(root: Path, name: str, payload: dict[str, Any], exc: BaseException, seconds: float) -> None:
    _record(root, "hook.error", "error", name, payload, type(exc).__name__, seconds)


def redact(text: str) -> str:
    return redact_all(text)[:MAX_REASON]


def redact_all(text: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=[redacted]" if m.group(1) else "[redacted]", text)


def shown(text: str) -> str:
    from ..permissions import shown_text

    return redact_all(shown_text(text))


def summary(payload: dict[str, Any]) -> str:
    from ..permissions import shown_text, shown_tool

    command = str(dig(payload, "tool_input", "command") or "")
    if not command:
        return shown_tool(str(payload.get("tool_name") or ""))
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    words = [w for w in words if not _ASSIGNMENT.match(w)]
    if not words:
        return ""
    flags = [
        w.split("=", 1)[0] if w.startswith("--") else w[:2]
        for w in words[1:]
        if w.startswith("-") and w != "-"
    ]
    return redact(" ".join([shown_text(words[0]).rsplit("/", 1)[-1], *flags]))[:MAX_REASON]


def _denied(out: str) -> str | None:
    if not out or ('"permissionDecision": "deny"' not in out and '"decision": "block"' not in out):
        return None
    try:
        data = as_dict(json.loads(out))
    except ValueError:
        return ""
    spec = as_dict(data.get("hookSpecificOutput"))
    return str(spec.get("permissionDecisionReason") or data.get("reason") or "")


def _record(
    root: Path, kind: str, outcome: str, name: str, payload: dict[str, Any], reason: str, seconds: float
) -> None:
    try:
        from .. import telemetry
        from ..session_identity import resolve_provider
        from ..telemetry import fence

        sink = _sink(root)
        if sink is None or not fence.allows(sink):
            return
        telemetry.record(
            sink,
            kind,
            outcome=outcome,
            duration_s=round(seconds, 4),
            session=str(payload.get("session_id") or "") or None,
            provider=resolve_provider(),
            dispatch=os.environ.get("AGENT_DISPATCH_ID") or None,
            data={
                "hook": name,
                "role": _role(payload),
                "reason": redact(reason),
                "command": summary(payload),
                "class": denial_class(name, reason),
            },
        )
    except Exception:
        return


CONTINUATION_HOOKS = {"peer-stop", "codex-prompt", "manager-prompt", "journal-pending"}


def denial_class(hook: str, reason: str) -> str:
    if hook == "host-decline":
        return "host-decline"
    if "already delivered" in reason:
        return "wake-suppression"
    if hook in CONTINUATION_HOOKS:
        return "continuation"
    return "policy"


def _role(payload: dict[str, Any]) -> str:
    if os.environ.get("AGENT_DISPATCH_ID"):
        return "worker"
    return "subagent" if payload.get("agent_id") or payload.get("agent_type") else "session"


def worker_denial(root: Path, dispatch_id: str, provider: str, hook: str, command: str, reason: str) -> None:
    try:
        from .. import telemetry
        from ..permissions import shown_tool
        from ..telemetry import fence

        sink = _sink(root)
        if sink is None or not fence.allows(sink):
            return
        shown_command = (
            shown_tool(command)
            if command.startswith("mcp__")
            else summary({"tool_input": {"command": command}})
        )
        telemetry.record(
            sink,
            "hook.denial",
            outcome="deny",
            duration_s=0.0,
            session=None,
            provider=provider,
            dispatch=dispatch_id,
            data={
                "hook": hook,
                "role": "worker",
                "reason": shown(reason)[:MAX_REASON],
                "command": shown_command,
                "class": denial_class(hook, reason),
            },
        )
    except Exception:
        return


def _sink(root: Path) -> Path | None:
    from .. import telemetry

    probe = os.environ.get(PROBE_ENV)
    return Path(probe) if probe else telemetry.sink_root(root)
