# ruff: noqa
from __future__ import annotations
import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from .. import repos as repos_mod
from ..core import AgentError, iso, now
from ..records.narrow import as_dict, as_list
from .spend import current_cost
from .state import is_probe, load_all, save

_GITHUB_REMOTE_RE = re.compile(
    "^(?:git@github\\.com:|https://github\\.com/)(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\\.git)?/?$"
)
PR_STALE_SECONDS = 6 * 3600


def _github_owner_repo(root: Path, rp: repos_mod.Repo) -> tuple[str, str] | None:
    m = _GITHUB_REMOTE_RE.match(rp.remote.strip())
    if m:
        return (m.group("owner"), m.group("repo"))
    clone = rp.abs_path(root)
    if not (clone / ".git").exists():
        return None
    origin = repos_mod.git_or_none(["remote", "get-url", "origin"], clone)
    if not origin:
        return None
    m = _GITHUB_REMOTE_RE.match(origin.strip())
    return (m.group("owner"), m.group("repo")) if m else None


def _pr_fresh(pr: dict[str, Any] | None) -> bool:
    if not pr or not pr.get("checked_at"):
        return False
    try:
        checked = dt.datetime.fromisoformat(str(pr["checked_at"]))
    except ValueError:
        return False
    return (now() - checked).total_seconds() < PR_STALE_SECONDS


def refresh_pr(
    root: Path, d: dict[str, Any], rp: repos_mod.Repo, *, force: bool = False
) -> dict[str, Any] | None:
    existing = d.get("pr")
    if not force and _pr_fresh(existing):
        return existing
    branch = d.get("branch")
    if not branch:
        return existing
    owner_repo = _github_owner_repo(root, rp)
    if owner_repo is None:
        return existing
    owner, name = owner_repo
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                f"{owner}/{name}",
                "--head",
                str(branch),
                "--state",
                "all",
                "--json",
                "number,url,state,mergedAt",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except OSError, subprocess.TimeoutExpired:
        return existing
    if proc.returncode != 0:
        return existing
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return existing
    items = as_list(payload)
    if not isinstance(payload, list):
        return existing
    if not items:
        d["pr"] = None
        save(root, d)
        return None
    item = as_dict(items[0])
    number = item["number"]
    if not isinstance(number, int) or isinstance(number, bool):
        return existing
    pr = {
        "number": number,
        "url": str(item["url"]),
        "state": str(item["state"]),
        "merged_at": item.get("mergedAt"),
        "checked_at": iso(now()),
        "inherited_from": None,
    }
    d["pr"] = pr
    save(root, d)
    was = as_dict(existing).get("state")
    if was != pr["state"]:
        from ..journal import pending as journal_pending

        journal_pending.record(
            root, "pr", f"{d['short']} PR {pr['number']} {str(pr['state']).lower()}", d["id"]
        )
    return pr


def _strip_origin_prefix(ref: str | None) -> str | None:
    if not ref:
        return None
    return ref[len("origin/") :] if ref.startswith("origin/") else ref


def apply_pr_inheritance(root: Path, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {r["id"]: r for r in records if r.get("id")}
    branch_index: dict[tuple[Any, Any, str], str] = {}
    for r in records:
        if r.get("branch"):
            branch_index[r.get("feature"), r.get("repo"), r["branch"]] = r["id"]
    parent_of: dict[str, str] = {}
    children_of: dict[str, list[str]] = {}
    for r in records:
        base = _strip_origin_prefix(r.get("base"))
        if not base:
            continue
        parent_id = branch_index.get((r.get("feature"), r.get("repo"), base))
        if parent_id and parent_id != r["id"]:
            parent_of[r["id"]] = parent_id
            children_of.setdefault(parent_id, []).append(r["id"])
    resolved: dict[str, dict[str, Any]] = {
        rid: rec["pr"] for rid, rec in by_id.items() if rec.get("pr") is not None
    }
    donor_of: dict[str, str] = {}
    frontier = list(resolved)
    while frontier:
        proposals: dict[str, list[str]] = {}
        for rid in frontier:
            parent = parent_of.get(rid)
            if parent is not None and parent not in resolved:
                proposals.setdefault(parent, []).append(rid)
            for child in children_of.get(rid, []):
                if child not in resolved:
                    proposals.setdefault(child, []).append(rid)
        next_frontier: list[str] = []
        for node_id, candidates in proposals.items():
            if node_id in resolved:
                continue
            ancestor = parent_of.get(node_id)
            donor_id = ancestor if ancestor in candidates else sorted(candidates)[0]
            donor_of[node_id] = donor_id
            resolved[node_id] = resolved[donor_id]
            next_frontier.append(node_id)
        frontier = next_frontier
    changed: list[dict[str, Any]] = []
    for node_id, donor_id in donor_of.items():
        r = by_id[node_id]
        inherited = dict(resolved[donor_id])
        inherited["inherited_from"] = donor_id
        r["pr"] = inherited
        save(root, r)
        changed.append(r)
    return changed


def _pr_period(pr: dict[str, Any], short: str) -> str:
    return f"#{pr['number']} ({pr['state']}, {short})"


LEDGER_BY = ("pr", "feature", "week")


def ledger_rows(root: Path, *, by: str = "pr") -> list[dict[str, Any]]:
    if by not in LEDGER_BY:
        raise AgentError(f"by must be one of {', '.join(LEDGER_BY)}; got {by!r}")
    groups: dict[str, dict[str, Any]] = {}
    for d in load_all(root):
        if not d.get("finished"):
            continue
        merged: bool | None = None
        if by == "pr":
            pr = d.get("pr")
            if pr:
                period = _pr_period(pr, str(d.get("short") or d.get("repo") or ""))
                merged = pr.get("state") == "MERGED"
            else:
                period = "(no pr)"
        elif by == "feature":
            period = f"{int(d['feature']):03d}" if d.get("feature") is not None else "?"
        else:
            try:
                when = dt.datetime.fromisoformat(str(d["finished"]))
            except ValueError:
                continue
            iso_year, iso_week, _ = when.date().isocalendar()
            period = f"{iso_year}-W{iso_week:02d}"
        g = groups.setdefault(
            period,
            {
                "period": period,
                "dispatches": 0,
                "attempts": 0,
                "recorded_spend_usd": 0.0,
                "unknown_cost_dispatches": 0,
                "legacy_cost_dispatches": 0,
                "recorded_probe_spend_usd": 0.0,
                "unknown_probe_cost_dispatches": 0,
                "legacy_probe_cost_dispatches": 0,
                "merged": merged,
                "features": set(),
                "ids": [],
            },
        )
        amount, source = current_cost(d)
        if is_probe(d):
            if source == "unknown":
                g["unknown_probe_cost_dispatches"] += 1
            else:
                g["recorded_probe_spend_usd"] += amount
                if source == "legacy-recorded":
                    g["legacy_probe_cost_dispatches"] += 1
            continue
        g["dispatches"] += 1
        g["attempts"] += int(d.get("attempt") or 0)
        if source == "unknown":
            g["unknown_cost_dispatches"] += 1
        else:
            g["recorded_spend_usd"] += amount
            if source == "legacy-recorded":
                g["legacy_cost_dispatches"] += 1
        feature = d.get("feature")
        if feature is not None:
            g["features"].add(feature)
        g["ids"].append(d["id"])
    rows: list[dict[str, Any]] = []
    for period in sorted(groups):
        g = groups[period]
        rows.append(
            {
                "period": g["period"],
                "dispatches": g["dispatches"],
                "attempts": g["attempts"],
                "spend_usd": None
                if g["unknown_cost_dispatches"] or g["legacy_cost_dispatches"]
                else round(g["recorded_spend_usd"], 2),
                "recorded_spend_usd": round(g["recorded_spend_usd"], 2),
                "unknown_cost_dispatches": g["unknown_cost_dispatches"],
                "legacy_cost_dispatches": g["legacy_cost_dispatches"],
                "probe_spend_usd": None
                if g["unknown_probe_cost_dispatches"] or g["legacy_probe_cost_dispatches"]
                else round(g["recorded_probe_spend_usd"], 2),
                "recorded_probe_spend_usd": round(g["recorded_probe_spend_usd"], 2),
                "unknown_probe_cost_dispatches": g["unknown_probe_cost_dispatches"],
                "legacy_probe_cost_dispatches": g["legacy_probe_cost_dispatches"],
                "merged": g["merged"],
                "features": sorted(g["features"]),
                "ids": sorted(g["ids"]),
            }
        )
    return rows


def render_ledger(rows: list[dict[str, Any]], by: str = "pr") -> str:
    if not rows:
        return "_none_"
    lines = [f"| {by} | dispatches | attempts | spend | merged | features |", "|---|---|---|---|---|---|"]
    total_dispatches = total_attempts = 0
    total_spend = total_probe = 0.0
    incomplete = False
    for r in rows:
        total_dispatches += r["dispatches"]
        total_attempts += r["attempts"]
        incomplete = incomplete or r["spend_usd"] is None
        total_spend += r.get("recorded_spend_usd", r.get("spend_usd") or 0.0)
        total_probe += r.get("recorded_probe_spend_usd", r.get("probe_spend_usd") or 0.0)
        merged = "yes" if r["merged"] is True else "no" if r["merged"] is False else "—"
        features = ", ".join((str(f) for f in r["features"])) if r["features"] else "—"
        spend = f"${r['spend_usd']:.2f}" if r["spend_usd"] is not None else "unknown"
        lines.append(
            f"| {r['period']} | {r['dispatches']} | {r['attempts']} | {spend} | {merged} | {features} |"
        )
    total_label = "unknown" if incomplete else f"${round(total_spend, 2):.2f}"
    lines.append(f"| **total** | {total_dispatches} | {total_attempts} | {total_label} | | |")
    if total_probe:
        lines.append(f"\neval probes, not counted above: ${round(total_probe, 2):.2f}")
    return "\n".join(lines)
