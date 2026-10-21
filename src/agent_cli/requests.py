"""Request records: an ask, review, research or bounded fix too small for a feature.

A request lives in the feature store (`features/NNN-slug/`, `record_kind: request`) and keeps its
numbering, session mapping, log, raw captures, handoffs and dispatches. What differs is the
contract: the ask (in Context) and the delivered result (under `## Result`) replace a task graph,
completion checks the request's dispatch work instead of the lesson queue, and no retro is due.
A captured bug or idea starts `proposed`; capture is not an authorization to execute it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import features as features_mod
from . import templates
from .core import AgentError
from .records.narrow import str_list

CATEGORIES = ("ask", "review", "research", "fix", "bug", "idea")
CAPTURE_CATEGORIES = ("bug", "idea")
ASK_CHARS = 100
_ASK_PREFIX = re.compile(r"^Ask \([a-z]+\): ")
# A heading or fence line inside the ask or the result would end or swallow a plan section.
_STRUCTURE = re.compile(r"^ {0,3}(#{1,6}(\s|$)|`{3,}|~{3,})")


def _block(text: str | None, what: str) -> str:
    value = (text or "").strip()
    if not value:
        raise AgentError(f"{what} cannot be empty")
    if any(_STRUCTURE.match(line) for line in value.splitlines()):
        raise AgentError(
            f"{what} cannot hold a heading or a code fence line; put long material in a vault note"
        )
    return value


def new(
    root: Path,
    *,
    slug: str,
    title: str,
    ask: str,
    category: str,
    repos: list[str] | None = None,
    status: str = "active",
) -> features_mod.Feature:
    if category not in CATEGORIES:
        raise AgentError(f"category must be one of {', '.join(CATEGORIES)}; got {category!r}")
    request = {"category": category, "ask": _block(ask, "the ask")}
    return features_mod.new(root, slug, title, status, None, list(repos or []), None, request=request)


def capture(
    root: Path, *, slug: str, title: str, ask: str, category: str, repos: list[str] | None = None
) -> features_mod.Feature:
    """A bug or idea recorded for later, `proposed` until the owner asks to execute it."""
    if category not in CAPTURE_CATEGORIES:
        raise AgentError(
            f"capture records a bug or an idea; got {category!r}. Use `agent request new` for work the owner asked for now."
        )
    return new(root, slug=slug, title=title, ask=ask, category=category, repos=repos, status="proposed")


def require(f: features_mod.Feature) -> features_mod.Feature:
    if not f.is_request:
        raise AgentError(f"{f.label.lower()} is a feature, not a request; use the feature tools for it")
    return f


def section(body: str, name: str) -> str:
    """Text under `## <name>` up to the next `## ` heading, stripped; '' when absent."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == f"## {name}":
            out: list[str] = []
            for rest in lines[i + 1 :]:
                if rest.startswith("## "):
                    break
                out.append(rest)
            return "\n".join(out).strip()
    return ""


def ask_of(f: features_mod.Feature) -> str:
    return _ASK_PREFIX.sub("", section(f.body, "Context"), count=1)


def result_of(f: features_mod.Feature) -> str:
    return section(f.body, templates.REQUEST_RESULT)


def short(text: str, limit: int = ASK_CHARS) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def with_result(body: str, result: str) -> str:
    """`body` with `## Result` holding `result`. A plan that lost the heading gets it back right
    before Review rounds or Handoffs, so the CLI-owned sections keep their bytes."""
    text = _block(result, "the result")
    lines = body.splitlines()
    heading = f"## {templates.REQUEST_RESULT}"
    if heading in (line.strip() for line in lines):
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
        end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        lines[start + 1 : end] = ["", *text.splitlines(), ""]
    else:
        anchors = (f"## {templates.REQUEST_ROUNDS}", "## Handoffs")
        at = next((i for i, line in enumerate(lines) if line.strip() in anchors), len(lines))
        lines[at:at] = [heading, "", *text.splitlines(), ""]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- completion


def unfinished_work(root: Path, f: features_mod.Feature) -> list[str]:
    """Each dispatch of this record that still owes something, as `id: reason, ...`.

    Read from the raw records before any status changes: a running or queued attempt, cleanup a
    worker, verifier or reverify run left pending, and `vault.live.dispatch_obligation`'s owner
    question or block, unread verdict and unresolved branch. Nothing here filters closed records,
    so a request already marked closed still shows what it owes."""
    from . import dispatch as dispatch_mod
    from .dispatch.reverify import cleanup_pending
    from .vault.live import dispatch_obligation

    labels = {"unread": "verdict unread", "unresolved-branch": "branch not landed, acked or abandoned"}
    rows: list[str] = []
    for d in dispatch_mod.load_all(root):
        if d.get("feature") != f.id:
            continue
        why: list[str] = []
        state = d.get("state")
        if state in ("queued", "running"):
            why.append(str(state))
        if d.get("worker_cleanup_pending") or (
            d.get("stop_reason") == "cleanup_failed" and d.get("worker_cleanup_confirmed") is not True
        ):
            why.append("worker cleanup pending")
        if d.get("verification_cleanup_pending"):
            why.append("verifier cleanup pending")
        if cleanup_pending(d):
            why.append("reverify cleanup pending")
        owed = dispatch_obligation(d)
        if owed is not None:
            who, reasons = owed
            if who == "owner":
                why += [f"{r}, unanswered" for r in reasons]
            else:
                why += [labels[r] for r in reasons if r in labels]
        if why:
            rows.append(f"{d.get('id')}: {', '.join(dict.fromkeys(why))}")
    return rows


def refuse_unfinished(root: Path, f: features_mod.Feature, status: str) -> None:
    """Refuse a request's terminal status while its dispatch work is unfinished, and `done`
    without a delivered result. `features.refuse_terminal` calls this for every route."""
    owed = unfinished_work(root, f)
    if owed:
        raise AgentError(
            f"{f.label.lower()} still has dispatch work: "
            + "; ".join(owed)
            + ". Answer, read, land, ack or abandon each, or finish its cleanup (`agent dispatch collect`), "
            "then complete it. Nothing was changed."
        )
    if status == "done" and not result_of(f):
        raise AgentError(
            f"{f.label.lower()} has no delivered result; complete it with `agent request complete "
            f"{f.number} --result <what was delivered>`."
        )


def next_action(f: features_mod.Feature) -> str:
    if not f.is_open:
        return "resolved"
    if f.status == "proposed":
        return "captured: execute only when the owner asks, then set it active"
    if f.status == "blocked":
        return "blocked: see the log for what it waits on"
    return "deliver the ask, then complete it with the result"


def row(root: Path, f: features_mod.Feature) -> dict[str, Any]:
    """The compact identity, ask, status and next action every request view shows."""
    return {
        "id": f.id,
        "number": f.number,
        "label": f.label,
        "slug": f.slug,
        "dir": f.rel_dir(root),
        "title": f.meta.get("title", ""),
        "status": f.status,
        "category": f.meta.get("category"),
        "ask": short(ask_of(f)),
        "has_result": bool(result_of(f)),
        "next_action": next_action(f),
    }


# ---------------------------------------------------------------- promotion


def promote(root: Path, f: features_mod.Feature, reason: str) -> features_mod.Feature:
    """Make the request an ordinary feature in place: same folder, id, log, raw captures,
    handoffs and result. Its status is kept, so promotion authorizes no work by itself."""
    require(f)
    if not f.is_open:
        raise AgentError(f"{f.label.lower()} is {f.status}; open a new feature for further work")
    why = features_mod.one_line(reason, "the reason").strip()
    if not why:
        raise AgentError("promotion needs the owner's approval as its reason")
    f.meta.pop(features_mod.KIND_FIELD, None)
    tags = [t for t in str_list(f.meta.get("tags")) if t != features_mod.REQUEST_TAG]
    f.meta["tags"] = features_mod.with_type_tag(tags)
    f.body = _feature_sections(f.body, f.number)
    f.save()
    from . import log as log_mod

    log_mod.append(f, "decision", f"promoted request {f.number} to a feature: {why}")
    return f


def _feature_sections(body: str, number: str) -> str:
    """Add the feature plan sections the request lacks: Decisions before Tasks, Open questions
    and Verification right after it. Inserting at a heading leaves every existing section's
    bytes alone, Tasks and Handoffs included."""
    lines = body.splitlines()
    if lines and lines[0].startswith(f"# Request {number} — "):
        lines[0] = f"# {number} — " + lines[0][len(f"# Request {number} — ") :]
    present = {line[3:].strip() for line in lines if line.startswith("## ")}
    tasks = next((i for i, line in enumerate(lines) if line.strip() == "## Tasks"), None)
    if tasks is None:
        raise AgentError("plan.md has no '## Tasks' section; restore it before promoting")
    after = next((j for j in range(tasks + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    late = [name for name in templates.FEATURE_ONLY_SECTIONS[1:] if name not in present]
    lines[after:after] = [row for name in late for row in (f"## {name}", "")]
    if templates.FEATURE_ONLY_SECTIONS[0] not in present:
        lines[tasks:tasks] = [f"## {templates.FEATURE_ONLY_SECTIONS[0]}", ""]
    return "\n".join(lines) + "\n"
