# ruff: noqa
from __future__ import annotations
from pathlib import Path
from .check import Check, new_check

TAB_FAIL_WARN = 0.02


def rows(root: Path) -> list[Check]:
    return (
        tab_paints(root)
        + plugin_paints(root)
        + blocked_with_work(root)
        + render_failures(root)
        + machine_label(root)
    )


def machine_label(root: Path) -> list[Check]:
    from ..dispatch import machine

    resolved = machine.resolve(root)
    if resolved.tag is None:
        return [new_check("machine label", "warn", f"{resolved.problem}; `agent setup` sets one")]
    return []


def plugin_paints(root: Path) -> list[Check]:
    from .. import tab_plugin

    rate = tab_plugin.paint_rate(root)
    attempted, failed, unread = (rate["attempted"], rate["failed"], rate["read_errors"])
    active = tab_plugin.active(root)
    if not attempted and (not unread) and (not tab_plugin.heartbeat(root)):
        return []
    share = failed / attempted if attempted else 0.0
    detail = f"{failed}/{attempted} plugin tab paints failed in the last 24 h ({share:.1%})"
    detail += f"; {unread} polls could not read live.json" if unread else ""
    detail += "; heartbeat fresh" if active else "; heartbeat stale, so the eval push paints"
    outdated = tab_plugin.outdated(root)
    detail += f"; {tab_plugin.OUTDATED}" if outdated else ""
    problem = tab_plugin.read_problem(root)
    detail += f"; plugin cannot read {problem}" if problem else ""
    status = "warn" if share > TAB_FAIL_WARN or unread or outdated or problem else "ok"
    return [new_check("plugin paints", status, detail)]


def tab_paints(root: Path) -> list[Check]:
    from ..tab_drain import INSTALLER_REMEDY, failure_rate, installer_outdated

    rate = failure_rate(root)
    evals, failed = (rate["evals"], rate["failed"])
    if not evals:
        return []
    share = failed / evals
    how = ", ".join((f"{n} {k}" for k, n in sorted(rate["ended"].items(), key=lambda kv: -kv[1])))
    detail = f"{failed}/{evals} Obsidian evals failed in the last 24 h ({share:.1%})"
    detail += f": {how}" if how else ""
    outdated = installer_outdated(root)
    if outdated:
        detail += f"; Obsidian says its installer is out of date, so every eval reloads the app and may time out: {INSTALLER_REMEDY}"
    return [new_check("tab paints", "warn" if share > TAB_FAIL_WARN or outdated else "ok", detail)]


def blocked_with_work(root: Path) -> list[Check]:
    from .. import blocked
    from .. import features as features_mod
    from ..core import AgentError

    try:
        feats = [f for f in features_mod.load_all(root) if f.status == "blocked"]
    except AgentError, OSError:
        return []
    if not feats:
        return []
    from ..dispatch import state as dispatch_state

    try:
        dispatches = dispatch_state.load_all(root)
    except AgentError, OSError:
        dispatches = []
    out: list[Check] = []
    for f in feats:
        work = blocked.active_work(root, f, dispatches)
        if work:
            out.append(
                new_check(
                    f"blocked {f.number}",
                    "warn",
                    f"marked blocked with active work: {'; '.join(work)}; {blocked.CLEAR_HINT}",
                )
            )
    return out


def render_failures(root: Path) -> list[Check]:
    from ..status import render_failure

    failure = render_failure(root)
    if not failure:
        return []
    detail = (
        f"{failure.get('source')} at {failure.get('at')}: {failure.get('error')}; run `agent status render`"
    )
    return [new_check("render", "warn", f"the last view render failed, none since: {detail}")]
