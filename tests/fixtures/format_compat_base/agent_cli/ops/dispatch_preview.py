# ruff: noqa
from __future__ import annotations
from typing import Any
from ..records.narrow import as_dict_or_none, as_str, dict_list, str_list


def pickup_lines(p: dict[str, Any]) -> list[str]:
    from .. import task_check as task_check_mod

    lines = [f"  warning: {w}" for w in str_list(p.get("writes_warnings"))]
    checked = as_dict_or_none(p.get("task_check"))
    if checked is None:
        return lines
    if "skipped" in checked:
        return [*lines, f"  task check skipped: {checked['skipped']}"]
    if checked.get("open"):
        lines += [f"  {line}" for line in task_check_mod.render(checked).splitlines()]
    return lines


def budget_line(p: dict[str, Any]) -> str:
    budget = float(p["budget_usd"])
    default = p.get("budget_default_usd")
    source = p.get("budget_source")
    text = f"  budget: ${budget:.2f}"
    if source == "explicit":
        text += " (explicit budget_usd"
        if default is not None:
            text += f"; repo default ${float(default):.2f}"
        reason = p.get("budget_reason")
        text += f"; reason: {reason})" if reason else ")"
    elif source == "row":
        text += f" (row band; repo default ${float(default):.2f})" if default is not None else " (row band)"
    elif source == "repo":
        text += " (repo default, dispatch.max_budget_usd)"
    elif source == "default":
        text += " (harness default)"
    if p.get("provider") == "codex":
        text += "; soft cap, Codex has no hard maximum"
    return text


def verification_preview_lines(verification: dict[str, Any] | None) -> list[str]:
    if not verification:
        return ["  supervisor verification: unverified (launch requirements unavailable)"]
    exempt = verification.get("status") == "exempt"
    lines = [
        "  supervisor verification: exempt (explicit request; not verified)"
        if exempt
        else "  supervisor verification: unverified (planned checks have not run)",
        f"    configuration: {verification['configuration']}",
        "    verification prerequisite: " + ("satisfied" if verification["launch_allowed"] else "BLOCKED"),
        f"    {verification['reason']}",
    ]
    commands = dict_list(verification.get("commands"))
    if commands:
        lines.append(f"    planned supervisor checks ({len(commands)}):")
        for i, command in enumerate(commands, 1):
            lines.append(f"      {i}. timeout {command['timeout_seconds']:g}s:")
            lines += [f"         {line}" for line in as_str(command["command"]).splitlines()]
        lines.append(f"    total verification timeout: {verification['timeout_seconds']:g}s")
    env = verification.get("env")
    if env is not None:
        lines.append(f"    verifier env ({len(env)}): {', '.join(env)}")
    return lines
