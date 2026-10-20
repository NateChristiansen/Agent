"""Work-order composition (the text a worker is launched with, and the follow-up a resume
sends) and the schema check over what it returns."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .. import features as features_mod
from .. import repos as repos_mod
from ..records.dispatch import result_of, thread_of
from ..records.narrow import as_dict, as_dict_or_none, as_float, as_list, str_list
from . import conventions as conventions_mod
from .return_schema import RETURN_SCHEMA

# ------------------------------------------------------------------ prompt composition


def plan_section(body: str, name: str) -> str:
    """Text under `## <name>` up to the next `## ` heading, stripped."""
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == f"## {name}":
            start = i
            break
    if start is None:
        return ""
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        out.append(line)
    return "\n".join(out).strip("\n")


def compose_prompt(
    *,
    dispatch_id: str,
    nonce: str,
    repo: repos_mod.Repo,
    feature: features_mod.Feature,
    prompt: str,
    tasks: list[features_mod.Task],
    worktree: Path,
    branch: str,
    base: str,
    root: Path,
    thread: list[dict[str, Any]] | None = None,
    escalated_from: tuple[str, str] | None = None,
    forked_from: str | None = None,
) -> str:
    """The work order on stdin (A3): task verbatim, feature context, assigned tasks, repo
    notes, workspace, and the thread on resumes. `escalated_from` (item 2, `--escalate-from`)
    is `(old_id, old_id's last manager message)`, shown before the new task so the worker
    reads what the previous one was last told. `forked_from` is the same escalation when the
    old session could be forked instead: the whole conversation is already in this
    worker's context, so nothing is pasted. But the old work order is in there too, nonce and
    all, and this is where the new one is told to win."""
    context = plan_section(feature.body, "Context") or "(the plan has no Context yet)"
    lines = [
        f"# Work order {dispatch_id} — {repo.name}",
        "",
        f"nonce: {nonce}",
        "Echo this nonce verbatim in the `nonce` field of your return.",
        "",
    ]
    if forked_from:
        lines += [
            f"## Forked from {forked_from}",
            "",
            f"This session is a fork of dispatch {forked_from}: everything above this message is "
            "that attempt's own history, and it is yours to read. Two things about it are now "
            f"stale — the work order it opened with, which the Task below replaces, and the nonce "
            f"it carried. The nonce for THIS dispatch is {nonce}; echo that one, not the earlier "
            "one.",
            "",
        ]
    if escalated_from:
        old_id, old_text = escalated_from
        lines += [
            f"## Escalated from {old_id}: the last instruction to the previous worker",
            "",
            old_text.strip("\n") or "(no manager message recorded)",
            "",
        ]
    lines += [
        "## Task",
        "",
        prompt.strip("\n"),
        "",
        "## Feature context",
        "",
        f"Feature {feature.number} {feature.slug}: {feature.meta.get('title', '')}".rstrip(": "),
        "",
        context,
        "",
        "## Tasks assigned to you",
        "",
    ]
    if tasks:
        lines.append("Say in your return what you did for each; the manager ticks them once it is verified.")
        lines.append("")
        for t in tasks:
            lines.append(f"{t.index}. (#{t.index}) {t.text}")
    else:
        lines.append("None assigned; the Task section above is the whole job.")
    lines += ["", "## Repo notes", ""]
    notes = (repo.notes or "").strip()
    harness = (repo.harness or "").strip()
    lines.append(notes or "(no notes in agent.yaml)")
    if harness and harness.lower() != "none":
        lines.append(f"Harness in this repo: {harness}")
    try:
        rules = repo.diff_rules
    except ValueError:
        rules = []
    conv = conventions_mod.prompt_section(repo.abs_path(root), prompt, rules)
    if conv:
        lines += ["", *conv]
    lines += [
        "",
        "## Workspace",
        "",
        f"- branch: {branch}",
        f"- path: {worktree}",
        f"- base: {base}",
        "- commit here as you go; never push, never open a PR",
        f"- the feature folder is readable at {root / feature.rel_dir(root)} (read-only)",
    ]
    if thread:
        lines += ["", "## Thread", ""]
        lines += _thread_lines(thread)
    return "\n".join(lines).rstrip("\n") + "\n"


def compose_followup(d: dict[str, Any]) -> str:
    """Stdin for an attempt that resumes its own history (`resume_mode.resumes`). The session
    already holds the work order and every earlier turn, so this sends only what is new: the
    instructions since the last return, the supervisor's findings on that return, the nonce and
    a short reminder. Replaying the whole thread re-sent turns the worker had already read."""
    thread = thread_of(d)
    last = max((i for i, e in enumerate(thread) if e.get("from") == "worker"), default=-1)
    lines = [
        f"# Work order {d['id']} — {d['repo']} (attempt {d['attempt']})",
        "",
        f"nonce: {d['nonce']}",
        "Echo this nonce verbatim in the `nonce` field of your return.",
        "",
    ]
    findings = _findings(d)
    if findings:
        lines += [
            "Weigh review findings: do they reproduce, does real use take the path, and is it worth "
            "building around? Fix the real ones; name each declined finding and its reason in your return. "
            "A failed verification command is not a review finding: fix it.",
            "",
            "## Findings on your last return",
            "",
            *findings,
            "",
        ]
    lines += [
        "## New instructions",
        "",
        *(_thread_lines(thread[last + 1 :]) or ["(none: continue the task)"]),
    ]
    lines += [
        "",
        "## Reminder",
        "",
        f"You are in {d['worktree']} on branch {d['branch']}. Commit here; never push.",
        "Your final message must be the JSON return with the nonce.",
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def _findings(d: dict[str, Any]) -> list[str]:
    """What the supervisor found wrong with the last return: its verification and its blocks."""
    out: list[str] = []
    verdict = as_dict(d.get("verification_result"))
    if verdict.get("reasons") == ["new attempt"]:  # resume's placeholder, not a verdict on the last return
        verdict = as_dict(d.get("prior_verification"))
        if verdict.get("reasons") == ["new attempt"]:
            verdict = {}
    if verdict.get("status") not in (None, "verified", "exempt"):
        out.append(
            f"- verification {verdict['status']}: " + "; ".join(map(str, as_list(verdict.get("reasons"))))
        )
    out += [f"- {step}" for step in str_list(result_of(d).get("next_steps"))]
    if d.get("error"):
        out.append(f"- {d['error']}")
    return out


def compose_restart(d: dict[str, Any], original_prompt: str) -> str:
    """Start a fresh CLI session after a failed launch, retaining later manager turns.

    The original prompt carries the full feature context and worker contract. A resume
    prompt only carries the thread, so sending it to a fresh session loses that context.
    """
    thread = thread_of(d)
    original = next((entry for entry in thread if entry.get("from") == "manager"), None)
    followups = [entry for entry in thread if entry is not original and entry.get("from") != "worker"]
    if not followups:
        return original_prompt
    lines = [
        original_prompt.rstrip("\n"),
        "",
        "## Instructions received after the failed launch",
        "",
        *_thread_lines(followups),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def _thread_lines(thread: Sequence[Mapping[str, object]]) -> list[str]:
    out: list[str] = []
    for entry in thread:
        out.append(f"### {thread_heading(entry)}")
        out.append("")
        out.append(str(entry.get("text") or "").strip("\n"))
        out.append("")
    return out[:-1] if out else out


def thread_heading(entry: Mapping[str, object]) -> str:
    sender = str(entry.get("from") or "manager")
    ts = str(entry.get("ts") or "")
    if sender == "worker":
        meta = f"{entry.get('status')}, attempt {entry.get('attempt')}"
        if entry.get("cost_usd") is not None:
            meta += f", ${as_float(entry['cost_usd']):.2f}"
        return f"{ts} worker → manager ({meta})"
    # A `dispatch say` steer landed inside a turn that was already running. Say so,
    # because a reader of the thread otherwise cannot tell it from the work order or an answer.
    if entry.get("steer"):
        return f"{ts} {sender} → worker (steer, mid-turn)"
    return f"{ts} {sender} → worker"


# ------------------------------------------------------------------ return checking (A4)


def check_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Hand-written subset of JSON Schema: type, required, properties,
    additionalProperties=false, items, enum. Enough for RETURN_SCHEMA."""
    problems: list[str] = []
    kind = schema.get("type")
    if kind == "object":
        obj = as_dict_or_none(value)
        if obj is None:
            return [f"{path}: expected object"]
        for key in as_list(schema.get("required")):
            if key not in obj:
                problems.append(f"{path}.{key}: missing")
        props = as_dict(schema.get("properties"))
        for key, sub in props.items():
            if key in obj:
                problems += check_schema(obj[key], as_dict(sub), f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            for key in obj:
                if key not in props:
                    problems.append(f"{path}.{key}: unexpected field")
        return problems
    if kind == "array":
        elements = as_list(value)
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        items = schema.get("items")
        if items:
            for i, item in enumerate(elements):
                problems += check_schema(item, items, f"{path}[{i}]")
        return problems
    if kind == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string"]
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{path}: expected integer"]
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{path}: expected number"]
    elif kind == "boolean":
        if not isinstance(value, bool):
            return [f"{path}: expected boolean"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} not in {schema['enum']}")
    return problems


def verify_return(d: dict[str, Any], ret: Any) -> list[str]:
    problems = check_schema(ret, RETURN_SCHEMA)
    if not problems and ret.get("nonce") != d["nonce"]:
        problems.append(f"nonce mismatch: expected {d['nonce']}, got {ret.get('nonce')!r}")
    return problems


def manager_prompt(d: dict[str, Any]) -> str:
    first = next((e for e in thread_of(d) if e.get("from") == "manager"), None)
    return str(first.get("text") if first else "")


def last_manager_message(d: dict[str, Any]) -> str:
    """The last instruction sent to a worker (item 2, `--escalate-from`): the newest
    `from: manager` thread entry, which is the work order itself when nothing was ever said
    after it."""
    entries = [e for e in thread_of(d) if e.get("from") == "manager"]
    return str(entries[-1].get("text") if entries else "")
