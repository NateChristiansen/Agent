# ruff: noqa
from __future__ import annotations
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from .. import adrs as adrs_mod
from .. import features as features_mod
from .. import frontmatter
from .. import log as log_mod
from .. import repos as repos_mod
from ..capture import UNASSIGNED, read_jsonl
from ..core import AgentError, agent_path, atomic_write
from .block import dispatches_today
from .paths import JOURNAL_DIR

HEAD_CHARS = 120


def collect(root: Path, day: date) -> dict[str, Any]:
    day_s = day.isoformat()
    feats_out: list[dict[str, Any]] = []
    for f in features_mod.load_all(root):
        entries = [e for e in (log_mod.parse(f.log_path) if f.log_path.exists() else []) if e.date == day_s]
        raw = _raw_summary(f.raw_dir / f"{day_s}.jsonl")
        opened_today = f.meta.get("opened") == day
        closed_today = f.meta.get("closed") == day
        if not (entries or raw["records"] or opened_today or closed_today):
            continue
        feats_out.append(
            {
                "id": f.id,
                "slug": f.slug,
                "title": f.meta.get("title", ""),
                "status": f.status,
                "opened_today": opened_today,
                "closed_today": closed_today,
                "log_entries": [{"ts": e.ts, "kind": e.kind, "text": e.text} for e in entries],
                "raw": raw,
            }
        )
    unassigned = _raw_summary(root / UNASSIGNED / f"{day_s}.jsonl")
    adrs_today = [
        {"id": a.id, "title": a.meta.get("title", ""), "status": a.meta.get("status"), "scope": scope}
        for scope in ("harness", "notes")
        if scope == "harness" or adrs_mod.notes_ready(root)
        for a in adrs_mod.load_all(root, scope)
        if a.meta.get("date") == day
    ]
    return {
        "date": day_s,
        "features": feats_out,
        "unassigned_raw": unassigned,
        "adrs_today": adrs_today,
        "git": _git_activity(root, day),
        "dispatches": dispatches_today(root, day),
        "retros": _retros_today(root, day),
        "harness_git": _harness_git(root, day),
    }


def _retros_today(root: Path, day: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in (d / "retro.md" for d in features_mod.feature_dirs(root)):
        if not p.is_file():
            continue
        try:
            meta, body = frontmatter.load(p)
        except AgentError:
            continue
        if meta.get("date") != day:
            continue
        headline = ""
        for line in body.splitlines():
            if line.strip():
                headline = line.strip().lstrip("#").strip()
                break
        out.append(
            {"feature": meta.get("feature"), "path": p.relative_to(root).as_posix(), "headline": headline}
        )
    return out


def _harness_git(root: Path, day: date) -> list[str]:
    if not (root / ".git").exists():
        return []
    since = f"{day.isoformat()} 00:00:00"
    until = f"{(day + timedelta(days=1)).isoformat()} 00:00:00"
    out = _run(["git", "log", f"--since={since}", f"--until={until}", "--format=%s"], root)
    return out.splitlines() if out else []


def _raw_summary(path: Path) -> dict[str, Any]:
    records = read_jsonl(path)
    prompts = [r for r in records if r.get("kind") == "prompt"]
    stops = [r for r in records if r.get("kind") == "stop"]
    sessions = {r.get("session_id") for r in records if r.get("session_id")}
    heads: list[str] = []
    for r in prompts:
        text = (r.get("prompt") or "").strip().replace("\n", " ")
        if text:
            heads.append(text[:HEAD_CHARS])
    return {
        "records": len(records),
        "sessions": len(sessions),
        "prompts": len(prompts),
        "stops": len(stops),
        "first_ts": records[0].get("ts") if records else None,
        "last_ts": records[-1].get("ts") if records else None,
        "prompt_heads": heads,
    }


def _git_activity(root: Path, day: date) -> list[dict[str, Any]]:
    try:
        registry = repos_mod.load_registry(root)
    except AgentError:
        return []
    out: list[dict[str, Any]] = []
    seen_main: set[str] = set()
    for repo in registry.repos:
        main = repo.abs_path(root)
        if not (main / ".git").exists():
            out.append(
                {
                    "repo": repo.name,
                    "worktree": repo.path,
                    "not_cloned": True,
                    "commits": [],
                    "dirty_files": None,
                    "branch": None,
                }
            )
            continue
        seen_main.add(repo.name)
    for repo, path in repos_mod.all_worktree_paths(root, registry):
        out.append(_scan(root, repo, path, day))
    return out


def _scan(root: Path, repo: repos_mod.Repo, path: Path, day: date) -> dict[str, Any]:
    email = _run(["git", "config", "user.email"], path)
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
    since = f"{day.isoformat()} 00:00:00"
    until = f"{day.isoformat()} 23:59:59"
    args = ["git", "log", "--all", f"--since={since}", f"--until={until}", "--format=%h%x09%aI%x09%s"]
    if email:
        args.insert(3, f"--author={email}")
    log = _run(args, path) or ""
    commits: list[dict[str, str]] = []
    for line in log.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            commits.append({"sha": parts[0], "ts": parts[1], "subject": parts[2]})
    porcelain = _run(["git", "status", "--porcelain"], path)
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = str(path)
    return {
        "repo": repo.name,
        "worktree": rel,
        "not_cloned": False,
        "branch": branch,
        "commits": commits,
        "dirty_files": len(porcelain.splitlines()) if porcelain is not None else None,
    }


def _run(args: list[str], cwd: Path) -> str | None:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def render_markdown(bundle: dict[str, Any]) -> str:
    lines = [f"# Journal collect — {bundle['date']}", ""]
    lines.append("## Features")
    if not bundle["features"]:
        lines.append("_no feature activity_")
    for f in bundle["features"]:
        flags: list[str] = []
        if f["opened_today"]:
            flags.append("opened today")
        if f["closed_today"]:
            flags.append("closed today")
        suffix = f" ({', '.join(flags)})" if flags else ""
        lines += ["", f"### {f['id']:03d} {f['slug']} — {f['title']} [{f['status']}]{suffix}"]
        for e in f["log_entries"]:
            lines.append(f"- **{e['ts'][11:]} {e['kind']}** — {e['text']}")
        r = f["raw"]
        if r["records"]:
            lines.append(
                f"- raw: {r['sessions']} session(s), {r['prompts']} prompt(s), {r['stops']} stop(s), {r['first_ts']} → {r['last_ts']}"
            )
            for h in r["prompt_heads"]:
                lines.append(f"  - {h}")
    u = bundle["unassigned_raw"]
    lines += ["", "## Unassigned captures"]
    if u["records"]:
        lines.append(f"{u['sessions']} session(s), {u['prompts']} prompt(s), {u['stops']} stop(s)")
        for h in u["prompt_heads"]:
            lines.append(f"- {h}")
    else:
        lines.append("_none_")
    lines += ["", "## Decisions"]
    if bundle["adrs_today"]:
        for a in bundle["adrs_today"]:
            label = "ADR" if a["scope"] == "harness" else "Decision"
            lines.append(f"- {label}-{a['id']:04d} {a['title']} [{a['status']}]")
    else:
        lines.append("_none_")
    lines += ["", "## Git activity"]
    if not bundle["git"]:
        lines.append("_no registry or no repos cloned_")
    for g in bundle["git"]:
        if g.get("not_cloned"):
            lines.append(f"- {g['repo']} ({g['worktree']}): (not cloned)")
            continue
        lines.append(
            f"- {g['repo']} @ {g['worktree']} [{g['branch']}] — {len(g['commits'])} commit(s), {g['dirty_files']} dirty file(s)"
        )
        for c in g["commits"]:
            lines.append(f"  - {c['sha']} {c['subject']}")
    lines += ["", "## Dispatches"]
    if bundle.get("dispatches"):
        for d in bundle["dispatches"]:
            cost = float(d.get("cost_usd") or 0.0)
            probe = " eval probe," if d.get("kind") == "probe" else ""
            lines.append(
                f"- {d['id']} {d['short']} [{d['state']}]{probe} ${cost:.2f}, attempt {d['attempt']}, {d.get('model') or 'default'}/{d.get('effort') or 'default'} — {d['summary']} ({d['handoff']})"
            )
    else:
        lines.append("_none_")
    lines += ["", "## Retros"]
    if bundle.get("retros"):
        for r in bundle["retros"]:
            lines.append(f"- {r['feature']}: {r['headline']} ({r['path']})")
    else:
        lines.append("_none_")
    lines += ["", "## Harness git"]
    if bundle.get("harness_git"):
        for subject in bundle["harness_git"]:
            lines.append(f"- {subject}")
    else:
        lines.append("_none_")
    lines.append("")
    return "\n".join(lines)


def write(root: Path, bundle: dict[str, Any], as_json: bool) -> Path:
    import json

    base = agent_path(root, JOURNAL_DIR)
    base.mkdir(parents=True, exist_ok=True)
    if as_json:
        path = base / f"{bundle['date']}.collect.json"
        atomic_write(path, json.dumps(bundle, indent=2, default=str) + "\n")
    else:
        path = base / f"{bundle['date']}.collect.md"
        atomic_write(path, render_markdown(bundle))
    stale_dir = root / JOURNAL_DIR
    for ext in ("md", "json"):
        stale = stale_dir / f"{bundle['date']}.collect.{ext}"
        if stale.exists():
            stale.unlink()
    return path
