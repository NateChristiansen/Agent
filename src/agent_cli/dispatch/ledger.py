"""The PR ledger: what a branch's pull request is, how a stacked branch inherits one,
and the deterministic outcome table over finished dispatches."""

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

# ------------------------------------------------------------------ PR ledger
#
# `d["pr"]` is `{"number", "url", "state" (OPEN/MERGED/CLOSED), "merged_at", "checked_at",
# "inherited_from"}` or `None`. Older records simply lack the key. Every reader here treats a
# missing key the same as `None`: no migration. `refresh_pr` is the only thing that ever calls
# `gh`. `ledger_rows`/`render_ledger` only ever read what is already on the records.

_GITHUB_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|https://github\.com/)(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)
# A `checked_at` younger than this is trusted without another `gh` call unless `force=True`.
PR_STALE_SECONDS = 6 * 3600


def _github_owner_repo(root: Path, rp: repos_mod.Repo) -> tuple[str, str] | None:
    """(owner, repo) for `gh --repo owner/repo`, parsed from `Repo.remote` (`git@github.com:`
    or `https://github.com/`, `.git` optional). Falls back to `git remote get-url origin` in
    the clone when the registry string itself does not parse that way. A real clone's origin
    is the more trustworthy source when the two disagree. `None` when neither parses, or the
    clone does not exist."""
    m = _GITHUB_REMOTE_RE.match(rp.remote.strip())
    if m:
        return m.group("owner"), m.group("repo")
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
    """Re-check `d["pr"]` against GitHub for `d["branch"]` and persist it. Never raises: a `gh`
    failure (the binary missing, a non-zero exit, a timeout, or output that is not the JSON
    asked for) leaves the field exactly as it was and returns the unchanged value. A refresh's
    job is to not make a launch or a ledger read fail because a network call did. The caller
    reports the failure.

    Skips the `gh` call and returns the current value unchanged when: `checked_at` is under six
    hours old and `force` is not set; the record has no `branch`; or the repo's remote does not
    resolve to a `github.com` owner/repo. On success the first PR (any state) matching the
    branch becomes the new value, `inherited_from` cleared to `None`, `checked_at` stamped now;
    no PR found for the branch clears the field to `None` (also stamped, so a bare re-run does
    not retry it immediately). `apply_pr_inheritance` is how a stacked branch still gets a PR
    from its descendant even though its own field reads `None`.
    """
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
    # The work leaving the machine, and it merging, are both milestones the journal may want.
    # Only a change fires: `refresh_pr` runs on every ledger read, and an
    # unchanged PR is not news. The marker is a question, not a note.
    was = as_dict(existing).get("state")
    if was != pr["state"]:
        from ..journal import pending as journal_pending

        journal_pending.record(
            root, "pr", f"{d['short']} PR {pr['number']} {str(pr['state']).lower()}", d["id"]
        )
    return pr


def _strip_origin_prefix(ref: str | None) -> str | None:
    """`base` is routinely recorded as `origin/<branch>` (the base ref a worktree was fetched
    from) while the record it is stacked on stores its own `branch` with no such prefix. Real
    records have both spellings depending on how the base was passed at dispatch time.
    Strip a leading `origin/` before comparing the two; `branch` itself is never prefixed."""
    if not ref:
        return None
    return ref[len("origin/") :] if ref.startswith("origin/") else ref


def apply_pr_inheritance(root: Path, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For every record with `pr is None`, find the nearest relative in the same feature+repo
    stack (an ancestor, whose `base`, `origin/`-stripped, matches another record's `branch`,
    or a descendant, whose `base` matches its `branch`) that currently has a `pr`, and copy it
    here with `inherited_from` set to that relative's id. Nearest wins by hop count; an
    ancestor wins a tie against a descendant at the same distance. A record with its own real
    PR is only ever a source, never a target: its own PR is kept even when its base
    branch carries another. `inherited_from` always names the immediate
    neighbor on the shortest path, not the ultimate source, so a value cascades: 001-08 (whose
    base is 001-07's branch) inherits through 001-07, which itself inherited from 001-02, not
    from 001-02 directly, because 001-07 is 001-08's *nearest* relative. A donor record is
    never written to, only the record borrowing its PR. Returns the records that changed.

    Implemented as a multi-source breadth-first flood fill rather than a per-record walk: every
    record that already has a `pr` is a source. Each round, every source propagates to its
    unresolved neighbors (parent and children alike). A node resolved this round becomes a
    source for the next. Rounds are what make "nearest wins" correct: a record is resolved by
    whichever neighbor reaches it first, not by whichever this function happens to visit first.
    That is also what makes the ancestor-tie-break meaningful: it only applies among neighbors
    that reach a record in the very same round, i.e. genuinely equidistant.
    """
    by_id = {r["id"]: r for r in records if r.get("id")}
    branch_index: dict[tuple[Any, Any, str], str] = {}
    for r in records:
        if r.get("branch"):
            branch_index[(r.get("feature"), r.get("repo"), r["branch"])] = r["id"]
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
                continue  # already resolved earlier this same round, via another candidate
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
    """Finished dispatches (`finished` set), grouped by PR (default), feature, or ISO week: the
    deterministic outcome ledger. `by='pr'` groups on the PR's number, state and repo short
    (`#42 (MERGED, web)`); a record with no `pr` (real or inherited) groups under `(no pr)`.
    `by='feature'` groups on the zero-padded feature number (the dispatch-id/feature-dir
    convention). `by='week'` groups on the ISO week of `finished`. Each row:
    `period, dispatches (count), attempts (sum), spend_usd (sum, rounded 2), merged (bool for
    `by='pr'` rows, state MERGED, else None), features (sorted ids), ids (sorted dispatch
    ids)`, rows sorted by period."""
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
            # Eval spend is real money but not work: kept apart so it never inflates a PR's
            # or a week's cost.
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
    """A markdown table, `| <by> | dispatches | attempts | spend | merged | features |`, with a
    final total row; `_none_` when there are no rows."""
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
        features = ", ".join(str(f) for f in r["features"]) if r["features"] else "—"
        spend = f"${r['spend_usd']:.2f}" if r["spend_usd"] is not None else "unknown"
        lines.append(
            f"| {r['period']} | {r['dispatches']} | {r['attempts']} | {spend} | {merged} | {features} |"
        )
    total_label = "unknown" if incomplete else f"${round(total_spend, 2):.2f}"
    lines.append(f"| **total** | {total_dispatches} | {total_attempts} | {total_label} | | |")
    if total_probe:
        lines.append(f"\neval probes, not counted above: ${round(total_probe, 2):.2f}")
    return "\n".join(lines)
