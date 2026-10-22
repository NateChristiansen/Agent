"""Per-hook switches: a removable process or style hook can be turned off by config.

    hooks:                 # in agent.yaml (every machine) or agent.local.yaml (this one; wins)
      thrash-guard: false

Only the hooks in `TOGGLEABLE` can be switched. The safety core (pattern-kill-guard,
subagent-revert-guard, commit-guard, push-guard, sandbox-help-guard, verify-pipe, cli-route,
protect-generated, no-chaining and the rest) cannot: a name outside the
list is a config error that doctor reports, and the hook keeps running. A hook never raises on a
bad block; it runs as if no switch were set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..records.narrow import as_dict_or_none, dig

TOGGLEABLE = frozenset(
    {
        "thrash-guard",
        "peer-route",
        "plan-gate",
        "mcp-stale",
        "pull-warn",
        "journal-pending",
        "ask-guard",
        "record-guard",
    }
)
#: Hooks that no longer exist. A leftover switch for one is ignored, not a config error: an error
#: would void the whole block and turn the owner's other switches back on (`probe-cwd-guard` was switchable
#: until it was removed).
RETIRED = frozenset({"plan-gate-stop", "style-guard", "done-unticked", "lease-nudge", "probe-cwd-guard"})


def problems(raw: Any, where: str) -> list[str]:
    if raw is None:
        return []
    switches = as_dict_or_none(raw)
    if switches is None:
        return [f"{where} `hooks:` must map hook names to true or false"]
    out: list[str] = []
    for name, value in switches.items():
        if name in RETIRED:
            continue
        if name not in TOGGLEABLE:
            out.append(f"{where} `hooks.{name}` cannot be switched; only {', '.join(sorted(TOGGLEABLE))} can")
        elif not isinstance(value, bool):
            out.append(f"{where} `hooks.{name}` must be true or false")
    return out


def settings(root: Path) -> dict[str, bool]:
    """The effective switches, local over shared; empty when either block is invalid."""
    from .. import config_file

    merged: dict[str, bool] = {}
    for raw, where in (
        (_shared(root), "agent.yaml"),
        (config_file.machine_section(root, "hooks"), "agent.local.yaml"),
    ):
        if problems(raw, where):
            return {}
        merged.update(raw or {})
    return merged


def disabled(root: Path, name: str) -> bool:
    try:
        return name in TOGGLEABLE and settings(root).get(name) is False
    except Exception:  # noqa: BLE001 — a switch never breaks the hook it controls
        return False


def report(root: Path) -> tuple[list[str], list[str]]:
    """`(disabled names, problems)` for doctor."""
    from .. import config_file

    found = problems(_shared(root), "agent.yaml") + problems(
        config_file.machine_section(root, "hooks"), "agent.local.yaml"
    )
    return sorted(n for n, on in settings(root).items() if on is False and n not in RETIRED), found


def _shared(root: Path) -> Any:
    import yaml

    from ..config_file import registry_path

    try:
        data = yaml.safe_load(registry_path(root).read_text(encoding="utf-8"))
    except OSError, ValueError, yaml.YAMLError:
        return None
    return dig(data, "hooks")
