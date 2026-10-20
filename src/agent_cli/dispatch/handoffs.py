"""The committed handoff record and log callouts written when a dispatch is sent and returns.

Plans read handoff files through a fixed view; dispatch never writes plan.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import features as features_mod
from .. import frontmatter
from ..core import agent_display, atomic_write, write_if_changed
from ..records.dispatch import GitFacts, WorkerReturn, git_of, result_of, thread_of
from ..records.narrow import as_float, as_list, dict_list
from ..vault.dashboard import view_block
from .ownership import foreign_reason
from .prompt import manager_prompt, thread_heading
from .state import (
    TYPE_TAG,
    denied_permission_events,
    display_summary,
    first_line,
    is_live,
    load_all,
    one_line,
    permission_denials,
    permission_events,
    prompt_text,
    verification_summary,
)


def _cwd_slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def transcript_path(d: dict[str, Any]) -> Path | None:
    if d.get("transcript_path"):
        return Path(str(d["transcript_path"]))
    if d.get("worktree") and d.get("session_id"):
        return (
            Path.home() / ".claude" / "projects" / _cwd_slug(str(d["worktree"])) / f"{d['session_id']}.jsonl"
        )
    return None


# ------------------------------------------------------------------ handoff file (A5)


def render_handoff(root: Path, d: dict[str, Any], feature: features_mod.Feature) -> str:
    meta = {
        "id": d["id"],
        "feature": d["feature"],
        "repo": d["repo"],
        "worktree": d["worktree"],
        "branch": d["branch"],
        "session_id": d["session_id"],
        "status": d["state"],
        "attempt": d["attempt"],
        "cost_usd": round(float(d.get("cost_usd") or 0.0), 4),
        "budget_usd": d["budget_usd"],
        "provider": d.get("provider") or "claude",
        "row": d.get("row"),
        "model": d.get("model"),
        "resolved_model": d.get("resolved_model"),
        "effort": d.get("effort"),
        "override": d.get("override"),
        "choice_reason": d.get("choice_reason"),
        "started": d.get("started"),
        "finished": d.get("finished"),
        "tasks": list(d.get("tasks") or []),
        "verification_status": verification_summary(d)["status"],
        # ADR-0036 provenance: the label and install that wrote this, and when, for doctor's
        # duplicate-label check. A legacy record has none and its handoff keeps its old shape.
        **({k: d.get(k) for k in ("machine", "install", "created")} if d.get("machine") else {}),
        **live_fields(root, d, feature),
        "tags": [TYPE_TAG],
        "links": [feature.wikilink(root)],
    }
    lines = [
        f"# Handoff {d['id']} — {d['short']}",
        "",
        f"Worker for {feature.wikilink(root)} in `{d['repo']}`. Runtime state: `{agent_display('dispatches', str(d['id']) + '.json')}`.",
        "",
        "## Live",
        "",
        # the worker's state and the tail of its stream, redrawn while it runs
        view_block("worker", f'{{ id: "{d["id"]}" }}'),
        "",
        "## Prompt",
        "",
        "```markdown",
        _fence_safe(prompt_text(root, d)),
        "```",
        "",
        "## Return",
        "",
    ]
    if d.get("verification_cleanup_pending"):
        lines += [
            "> [!warning] Cleanup incomplete",
            "> The repo lease is retained until worker and verifier cleanup is confirmed. Run dispatch collect to recover.",
            "",
        ]
    result = d.get("result")
    if result:
        lines += ["```json", json.dumps(result, indent=2, ensure_ascii=False), "```", ""]
        lines += return_lines(result, git_of(d))
    elif d.get("error"):
        lines += [f"> [!failure] {d['state']}", f"> {one_line(d['error'])}", ""]
    else:
        lines += ["_no return yet_", ""]
    lines += _verification_lines(d)
    lines += _denial_lines(d)
    lines += _conventions_lines(d.get("conventions"))
    lines += ["## Thread", ""]
    for entry in thread_of(d):
        lines.append(f"### {thread_heading(entry)}")
        lines.append("")
        lines.append(str(entry.get("text") or "").strip("\n"))
        lines.append("")
    return printable(frontmatter.join(meta, "\n".join(lines).rstrip("\n") + "\n"))


ASK_CHARS = 160


def handoff_ask(d: dict[str, Any]) -> str | None:
    """What the dispatch is waiting on the owner for, in one line: the first open question, the
    reason it stopped, or what to do with a finished one. None while it runs or once closed."""
    if d.get("verification_cleanup_pending"):
        return "cleanup incomplete; repo lease retained — run dispatch collect"
    state = d.get("state")
    result = result_of(d)
    if state == "question":
        text = next(iter(result.get("open_questions") or []), None) or result.get("summary")
    elif state in ("blocked", "failed"):
        text = d.get("error") or result.get("notes") or result.get("summary")
    elif state == "done":
        text = next(iter(result.get("next_steps") or []), None) or done_next_action(d)
    else:
        return None
    return display_summary(one_line(text), ASK_CHARS) if text else None


def done_next_action(d: dict[str, Any]) -> str:
    """A finished dispatch's next step, by kind. "Review, then
    push" fitted only a build with work on its branch; a review's or a probe's return is the
    result, and a branch with no diff has nothing to push."""
    from .state import is_probe
    from .verdicts import has_diff

    if is_probe(d):
        return "read the result; nothing to land"
    if d.get("writes") == [] or d.get("row") == "review":
        return "read the verdict (dispatch_read show), act on it, then dispatch consume"
    if has_diff(d):
        return "review, then push or ack"
    return "no changes on its branch; read the result, then ack"


def live_fields(root: Path, d: dict[str, Any], feature: features_mod.Feature) -> dict[str, Any]:
    """The handoff frontmatter the Workers view filters and shows. `live` is
    `is_live` plus an open feature: a dispatch on a closed feature waits on nobody."""
    from .opener import open_uri

    return {
        "open": open_uri(root, str(d["id"])),
        # The manager's own work order, not the composed prompt, whose first line is always the
        # "Work order NNN — repo" header. Same line the dispatch log entry names as the goal.
        "goal": display_summary(first_line(manager_prompt(d)), ASK_CHARS),
        "ask": handoff_ask(d),
        "live": bool(is_live(d, set() if feature.is_open else {feature.id})),
    }


def sync_live_fields(root: Path, skipped: list[dict[str, Any]] | None = None) -> int:
    """Refresh `open`, `goal`, `ask` and `live` on every handoff from its dispatch record, touching only
    those keys. Called from `status render`, so a feature closing or an ack elsewhere reaches the
    view without rewriting handoff bodies older code wrote. Returns the handoffs changed.

    A handoff this record does not own (`ownership.foreign_reason`) is skipped and appended to
    `skipped` as `{id, handoff, reason}`; doctor reports it too. This contains a dispatch id
    collision between machines; it does not fix id allocation."""
    changed = 0
    for d in load_all(root):
        path = root / str(d.get("handoff") or "")
        if not d.get("handoff") or not path.is_file():
            continue
        if reason := foreign_reason(path, d):
            if skipped is not None:
                skipped.append({"id": d.get("id"), "handoff": d["handoff"], "reason": reason})
            continue
        try:
            feature = features_mod.resolve(root, str(d["feature"]))
            meta, body = frontmatter.load(path)
        except Exception:  # noqa: BLE001, S112 — one unreadable handoff must not fail the render; doctor reports it
            continue
        fields = live_fields(root, d, feature)
        if all(meta.get(k) == v for k, v in fields.items()):
            continue
        meta.update(fields)
        if write_if_changed(path, frontmatter.join(meta, body)):
            changed += 1
    return changed


def return_lines(result: WorkerReturn, git: GitFacts | None) -> list[str]:
    """The return's lists. Commits and files come from git (`git`); a legacy record without
    them shows what its worker reported, and its `commands` rows if it has any."""
    lines = [f"**{result.get('status')}** — {one_line(result.get('summary') or '')}", ""]
    commits = git["commits"] if git is not None else result.get("commits") or []
    lines.append("commits:" if commits else "commits: none")
    for c in commits:
        lines.append(f"- `{str(c.get('sha', ''))[:10]}` {c.get('subject', '')}")
    files = git["files"] if git is not None else result.get("files_touched") or []
    lines.append("")
    lines.append("files:" if files else "files: none")
    lines += [f"- `{f}`" for f in files]
    if commands := result.get("commands") or []:
        lines += ["", "commands:"]
        for c in commands:
            note = f" — {c.get('note')}" if c.get("note") else ""
            lines.append(f"- `{c.get('cmd', '')}` → exit {c.get('exit')}{note}")
    for label, items in (
        ("open questions", result.get("open_questions") or []),
        ("next steps", result.get("next_steps") or []),
    ):
        if items:
            lines.append("")
            lines.append(f"{label}:")
            lines += [f"- {x}" for x in items]
    if notes := result.get("notes"):
        lines += ["", "notes:", "", str(notes).strip()]
    lines.append("")
    return lines


def _verification_lines(d: dict[str, Any]) -> list[str]:
    verification = verification_summary(d)
    lines = ["## Supervisor verification", "", f"Recorded status: **{verification['status']}**.", ""]
    lines += [f"- {one_line(reason)}" for reason in verification["reasons"]]
    if verification["status"] == "exempt":
        lines += ["", "Exemption does not make worker permissions read-only."]
    lines += [
        "",
        "This displays the recorded assessment; viewing does not rerun checks or revalidate the tree.",
        "",
    ]
    return lines


def _denial_lines(d: dict[str, Any]) -> list[str]:
    """Show denials across attempts and sources, followed by first-hand callback events; stay silent
    when empty. Mask transport endpoints and credentials. Callback events use [tool arguments
    omitted]; legacy records may retain argument heads. Fix missing coverage in agent.yaml.
    """
    denials = permission_denials(d)
    events, denied = permission_events(d), denied_permission_events(d)
    if not denials and not events:
        return []
    lines = ["## Permission denials", ""]
    if denials:
        lines += [f"permission denials: {len(denials)}", "", *[f"- `{c}`" for c in denials], ""]
    if events:
        lines += [f"permission events: {len(events)} (denied {len(denied)})", ""]
        lines += [f"- `{e.get('input')}` — {e.get('reason')}" for e in denied]
        lines.append("")
    return lines


def _conventions_lines(conv: dict[str, Any] | None) -> list[str]:
    """Show convention findings and added diff comments so the manager can review each before
    publication.
    """
    if not conv:
        return []
    lines = ["## Convention gate", ""]
    if conv.get("error"):
        return lines + [f"diff_rules unreadable: {conv['error']}", ""]
    for v in dict_list(conv.get("violations")):
        lines.append(f'- **breaks** `{v["doc"]}` at `{v["path"]}:{v["line"]}`: "{v["rule"]}"')
    for c in dict_list(conv.get("noise")):
        lines.append(f"- **comment noise** at `{c['path']}:{c['line']}`: {c['why']}")
    for c in dict_list(conv.get("unjustified")):  # records from before rows were dropped
        lines.append(f"- **comment without a reason** at `{c['path']}:{c['line']}`")
    for doc in as_list(conv.get("missing_docs")):  # records from before quotes were dropped
        lines.append(f"- **doc not answered for** `{doc}`")
    if not any(conv.get(k) for k in ("violations", "noise", "unjustified", "missing_docs")):
        lines.append("no findings")
    governing = as_list(conv.get("governing_docs"))
    if governing:
        lines += ["", f"### Docs that govern the touched paths ({len(governing)})", ""]
        lines += [f"- `{doc}`" for doc in governing]
    answered = dict_list(conv.get("answered"))
    if answered:
        lines += ["", f"### Convention docs quoted ({len(answered)})", ""]
        lines += [f'- `{a["doc"]}`: "{one_line(str(a["quote"]))}"' for a in answered]
    for b in dict_list(conv.get("bad_quotes")):
        lines.append(
            f'- quote not checked out ({b["reason"]}) for `{b["doc"]}`: "{one_line(str(b["quote"]))}"'
        )
    comments = dict_list(conv.get("comments"))
    lines += ["", f"### Comments and docstrings added ({len(comments)})", ""]
    lines += [
        f"- `{c['path']}:{c['line']}` {one_line(str(c['text']))}"
        + (f" — _{one_line(str(c['reason']))}_" if c.get("reason") else "")
        + (f" (from attempt {c['reason_attempt']})" if c.get("reason_attempt") else "")
        for c in comments
    ] or ["none"]
    lines.append("")
    return lines


def printable(text: str) -> str:
    """Git text keeps invalid bytes as lone surrogates so names compare exactly; a file written
    as UTF-8 cannot hold them, so rendered output shows U+FFFD instead."""
    return re.sub("[\ud800-\udfff]", "�", text)


def _fence_safe(text: str) -> str:
    return text.replace("```", "'''")


def write_handoff(root: Path, d: dict[str, Any], feature: features_mod.Feature) -> Path | None:
    """Re-render the whole handoff file, or return None without writing when `d` does not own
    it (`ownership.foreign_reason`; doctor reports the skip). Contains an id collision between
    machines; it does not fix id allocation."""
    path = root / d["handoff"]
    if foreign_reason(path, d):
        return None
    atomic_write(path, render_handoff(root, d, feature))
    return path


def parse_handoff(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """(frontmatter, {section name: text}) with top-level `## ` headings split outside fences."""
    meta, body = frontmatter.load(path)
    sections: dict[str, str] = {}
    current = "_head"
    buf: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            sections[current] = "\n".join(buf).strip("\n")
            current, buf = line[3:].strip(), []
            continue
        buf.append(line)
    sections[current] = "\n".join(buf).strip("\n")
    return meta, sections


def plan_link_line(root: Path, d: dict[str, Any]) -> str:
    target = d["handoff"].removesuffix(".md")
    pending = " (cleanup incomplete; lease retained)" if d.get("verification_cleanup_pending") else ""
    return f"- [[{target}|{d['id']} {d['short']}]] — {d['state']}{pending}"


# ------------------------------------------------------------------ log callouts (A5)


def dispatch_log_text(d: dict[str, Any], budget_reason: str | None, approval: str) -> str:
    goal = first_line(manager_prompt(d))
    budget = f"budget ${float(d['budget_usd']):.2f}"
    if float(d["budget_usd"]) > float(d["budget_default_usd"]):
        budget += (
            f" (raised from ${float(d['budget_default_usd']):.2f}: {budget_reason or 'no reason given'})"
        )
    else:
        budget += " (repo default)"
    tasks = ", ".join(f"#{t}" for t in as_list(d.get("tasks"))) or "none"
    line = (
        f"dispatch {d['id']} → {d['short']}: {goal}\n"
        f"branch {d['branch']} from {d['base']}; {budget}; model {d.get('model') or 'default'}; "
        f"effort {d.get('effort') or 'default'}; "
        f"tasks {tasks}; notify {'+'.join(d.get('notify') or []) or 'none'}; approval: {approval}"
    )
    if d.get("choice_reason"):
        line += f"; why: {d['choice_reason']}"
    return line


def handoff_log_text(root: Path, d: dict[str, Any]) -> str:
    result = result_of(d)
    cost = float(d.get("cost_usd") or 0.0)
    cost_label = " local estimate" if d.get("cost_estimated_attempts") else ""
    head = f"handoff {d['id']} ← {d['short']}: {d['state']} (attempt {d['attempt']}, {int(d.get('num_turns') or 0)} turns, ${cost:.2f}{cost_label})"
    lines = [head]
    summary = display_summary(one_line(result.get("summary") or ""))
    if summary:
        lines.append(summary)
    git = git_of(d)
    commits = git["commits"] if git is not None else result.get("commits") or []
    if commits:
        lines.append(
            "commits: " + "; ".join(f"{str(c.get('sha', ''))[:7]} {c.get('subject', '')}" for c in commits)
        )
    steps = result.get("next_steps") or []
    if steps:
        lines.append(f"next: {steps[0]}")
    if d["state"] == "question":
        questions = result.get("open_questions") or []
        if questions:
            lines.append(f"asks: {questions[0]}")
        lines.append(f"answer with: the dispatch tool, action resume, dispatch_id {d['id']}, and `answer`")
    elif d["state"] in ("blocked", "failed", "cancelled"):
        reason = (
            d.get("error")
            or (result.get("notes") if d["state"] == "blocked" else None)
            or summary
            or "(no reason recorded)"
        )
        lines.append(f"reason: {one_line(reason)}")
        lines.append(f"worktree kept at {d['worktree']}")
    for auto in dict_list(d.get("auto_resumes")):
        lines.append(
            f"auto-resumed once after {auto['reason']} (budget ${as_float(auto['from_budget']):.2f} → ${as_float(auto['to_budget']):.2f})"
        )
    return printable("\n".join(lines))
