"""Compare open feature tasks with current main before pickup and first dispatch. Surface evidence of
landed or stale work for an owner decision; never mark a task done automatically.
"""

from __future__ import annotations

import contextvars
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import feature_tree, task_md
from . import features as features_mod
from . import repos as repos_mod
from .core import AgentError, coerce_date
from .records.narrow import as_dict, as_list, dig, str_list

# A path a task names: one with a directory part, or a file name with a known extension. URLs,
# wikilinks and globs are not paths here; a token that never existed in a repo simply finds nothing.
_EXT = r"(?:py|pyi|js|mjs|ts|tsx|jsx|md|toml|ya?ml|json|jsonl|sh|ps1|psm1|cs|csproj|sln|go|rs|txt|cfg|ini|html|css)"
PATH_RE = re.compile(
    r"(?<![\w/.:\[-])("
    r"(?:[\w.-]+/)+[\w.-]*\w"  # with a directory part: src/agent_cli/hooks/groups.py, hooks/
    r"|[\w-][\w.-]*\." + _EXT + r"(?![\w])"  # a bare file name: terminal.py
    r")"
)
# A backticked code name: one word of six or more characters with `_`, `.` or `-` in it.
SYMBOL_RE = re.compile(r"`([A-Za-z_][\w]*[_.-][\w.-]*\w)`")
# Shown per flagged task; the rest are counted.
MAX_COMMITS = 2
MAX_LINE = 140
# The check is advisory and runs inside `feature_use` and a dispatch preflight, so it must never
# hold either. The whole check gets BUDGET_S; each git call gets
# at most GIT_TIMEOUT_S of what is left. In practice the whole check took about 4 s.
BUDGET_S = 10.0
GIT_TIMEOUT_S = 5.0
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("task_check_deadline", default=None)


class OutOfTime(Exception):
    """The check's budget ran out; `check` returns what it has, marked incomplete."""


def _tick() -> None:
    """Raise OutOfTime once the budget is spent. Called around git and inside every Python scan
    (other features' tasks, commits, mentions), so the budget bounds the whole check and not only
    its git calls (13.9 s on a 10 s budget scanning long lines)."""
    deadline = _DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise OutOfTime


@dataclass(frozen=True)
class RepoRef:
    """A repo's checkout and the ref the check reads (its default branch)."""

    name: str
    path: Path
    ref: str


def task_paths(text: str) -> list[str]:
    """The repo paths a task's text names, in order, without line suffixes or duplicates."""
    out: list[str] = []
    for m in PATH_RE.finditer(text):
        token = m.group(1).rstrip("/.")
        start = m.start(1)
        if "://" in text[max(0, start - 8) : start + 3] or text[max(0, start - 2) : start] == "[[":
            continue
        if token not in out:
            out.append(token)
    return out


def task_symbols(text: str) -> list[str]:
    """Backticked code names a task cites (`terminal.finalize`, `CODEX_ABSENT_HOOKS`,
    `plan-mode-allow`, a test name), other than the paths `task_paths` already reads."""
    paths = task_paths(text)
    out: list[str] = []
    for m in SYMBOL_RE.finditer(text):
        token = m.group(1)
        if len(token) < 6 or token in out or any(token in p for p in paths):
            continue
        if not PATH_RE.fullmatch(token):
            out.append(token)
    return out


def task_mention_re(feature_id: int, index: int) -> re.Pattern[str]:
    """`005 #6`, `005#6`, `005 task 6`, `from 002 task 6`, `005-example #6`, `005 T6`. The
    number must stand alone, so a date such as `2026-10-09 task 2` is no mention of 005 #2."""
    return re.compile(
        rf"(?<![\w.-])0*{feature_id}(?![\d.])[\w-]*\s*(?:task\s*#?|#|\bT)\s*{index}(?!\d)",
        re.IGNORECASE,
    )


def _git(args: list[str], cwd: Path) -> str | None:
    """`git args` in `cwd`: stdout, or None when git fails. Bounded by GIT_TIMEOUT_S and by what
    is left of the check's budget; past either it kills git's whole process group (a textconv or
    pager it started too) and raises OutOfTime. stderr is discarded, so no grandchild can hold a
    pipe open after the kill."""
    deadline = _DEADLINE.get()
    timeout = GIT_TIMEOUT_S if deadline is None else min(GIT_TIMEOUT_S, deadline - time.monotonic())
    if timeout <= 0:
        raise OutOfTime
    try:
        proc = subprocess.Popen(
            ["git", *args],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="surrogateescape",
            start_new_session=os.name != "nt",
        )
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        raise OutOfTime from None
    return out if proc.returncode == 0 else None


def _kill(proc: subprocess.Popen[str]) -> None:
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if proc.stdout is not None:
        proc.stdout.close()


def feature_repos(root: Path, feature: features_mod.Feature) -> list[RepoRef]:
    """The feature's registered repos that are checked out here, each with its default branch
    (local, else `origin/`). A repo missing on this machine is skipped, not an error."""
    names = str_list(feature.meta.get("repos"))
    if not names:
        return []
    try:
        registry = repos_mod.load_registry(root)
    except AgentError:
        return []
    out: list[RepoRef] = []
    for name in names:
        repo = registry.find(name)
        if repo is None:
            continue
        path = repo.abs_path(root)
        if not path.exists():
            continue
        for ref in (repo.default_branch, f"origin/{repo.default_branch}"):
            if _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], path):
                out.append(RepoRef(repo.name, path, ref))
                break
    return out


HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,\d+)? @@", re.MULTILINE)


def _committed_task_line(committed: str, text: str, occurrence: int) -> int | None:
    """1-based file line of the `occurrence`-th counted task whose text is `text` in the
    committed plan file, frontmatter included in the numbering, or None."""
    from . import frontmatter

    file_lines = task_md.split_lines(committed)
    try:
        body = frontmatter.split(committed)[1]
    except AgentError:
        body = committed  # no frontmatter: the whole file is the body
    body_lines = task_md.split_lines(body)
    offset = len(file_lines) - len(body_lines)
    if offset < 0 or file_lines[offset:] != body_lines:
        return None  # the body is not the file's tail; no line number can be trusted
    same = [t for t in features_mod.list_tasks(body) if t.text == text]
    if occurrence >= len(same):
        return None
    return offset + same[occurrence].line_no + 1


def added_at(plan: Path, text: str, occurrence: int = 0) -> tuple[str, str] | None:
    """(sha, ISO author date) of the commit that added the task line whose text is `text` (its
    `occurrence`-th such line) in `plan` as committed at HEAD, or None when that cannot be told.

    It follows the line's own history (`git log -L n,n`), not its text, so tick notes,
    qualifiers and suffixes on the line do not matter (a text search once dated an unticked task to its tick). `-L` walks newest to oldest; the line was
    born in the commit whose hunk has an empty old side (`@@ -n,0 …`). None when the task is not
    in HEAD's plan (uncommitted) or no such hunk shows (the line was moved or rewritten whole);
    the caller then falls back to the feature's opened date and calls the birth approximate.

    The committed line is found with `features.list_tasks` on the committed body, the same
    counted-task set `occurrence` was counted in: a same-text checkbox outside Tasks, in a fenced
    example or nested as a sub-step is not a task and is never followed."""
    committed = _git(["show", "--no-textconv", f"HEAD:./{plan.name}"], plan.parent)
    if committed is None:
        return None
    line_no = _committed_task_line(committed, text, occurrence)
    if line_no is None:
        return None
    args = [
        "log",
        "--no-textconv",
        "--no-ext-diff",
        "--format=%x00%h%x09%aI",
        f"-L{line_no},{line_no}:{plan.name}",
    ]
    for chunk in (_git(args, plan.parent) or "").split("\x00")[1:]:
        _tick()
        head, _, patch = chunk.partition("\n")
        hunk = HUNK_RE.search(patch)
        if hunk and hunk.group(1) == "0":
            sha, _, when = head.partition("\t")
            return (sha, when.strip()) if when.strip() else None
    return None


def context_edited(plan: Path) -> str | None:
    """ISO date of the last commit that changed the plan's `## Context` section, or None."""
    try:
        lines = plan.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    start = next((i for i, line in enumerate(lines) if line.strip() == "## Context"), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    # `-L` takes 1-based inclusive file lines: the heading through the section's last line.
    out = _git(
        [
            "log",
            "-1",
            "--no-textconv",
            "--no-ext-diff",
            "--format=%aI",
            "-s",
            f"-L{start + 1},{end}:{plan.name}",
        ],
        plan.parent,
    )
    return (out or "").strip() or None


def _commits(repo: RepoRef, since: str, pathspecs: list[str]) -> list[str]:
    out = _git(["log", repo.ref, f"--since={since}", "--format=%h %as %s", "--", *pathspecs], repo.path)
    return [line for line in (out or "").splitlines() if line.strip()]


@dataclass(frozen=True)
class Commit:
    when: str  # ISO author date
    line: str  # `<sha> <date> <subject>`
    changed: str  # the added and removed lines of its patch; '' when patches were not read


def _history(repo: RepoRef, since: str, patches: bool) -> list[Commit]:
    """Commits on `repo.ref` since `since`, read once for every task. With `patches`, each one's
    changed lines too, so a cited symbol is one scan rather than one `git log -S` per symbol
    (about 10 MB and 2 s for the harness's 470 commits from 10-01 to 10-12). No textconv or
    external diff: a repo's own diff driver must not run, or stall, inside a pickup check."""
    args = [
        "log",
        repo.ref,
        f"--since={since}",
        "--no-color",
        "--no-textconv",
        "--no-ext-diff",
        "--format=%x00%aI%x09%h %as %s",
    ]
    out = _git([*args, "-p", "-U0"] if patches else args, repo.path) or ""
    commits: list[Commit] = []
    for chunk in out.split("\x00")[1:]:
        _tick()
        head, _, patch = chunk.partition("\n")
        when, _, line = head.partition("\t")
        changed = "\n".join(
            row for row in patch.splitlines() if row[:1] in "+-" and not row.startswith(("+++", "---"))
        )
        commits.append(Commit(when, line, changed))
    return commits


def _shown(lines: list[str]) -> str:
    more = len(lines) - MAX_COMMITS
    return "; ".join(lines[:MAX_COMMITS]) + (f"; +{more} more" if more > 0 else "")


def _tree(repo: RepoRef) -> list[str]:
    out = _git(["ls-tree", "-r", "--name-only", repo.ref], repo.path)
    return (out or "").splitlines()


def _exists(token: str, tree: list[str]) -> bool:
    return any(
        p == token or p.endswith("/" + token) or p.startswith(token + "/") or f"/{token}/" in p for p in tree
    )


def _path_signals(token: str, since: str, repos: list[RepoRef], trees: dict[str, list[str]]) -> list[str]:
    specs = [f":(glob)**/{token}", f":(glob)**/{token}/**"]
    lines: list[str] = []
    for repo in repos:
        commits = _commits(repo, since, specs)
        if not commits:
            continue
        where = f"{token}" if len(repos) == 1 else f"{repo.name}:{token}"
        shown = _shown(commits)
        if not _exists(token, trees[repo.name]):
            lines.append(f"{where} is gone from {repo.ref} ({shown})")
        else:
            lines.append(f"{where}: {len(commits)} commit(s) since added ({shown})")
    return lines


def _landed(root: Path, feature_id: int) -> dict[int, list[str]]:
    """Task index -> this feature's dispatches that carried it and have landed or finished."""
    from .dispatch import state as dispatch_state

    out: dict[int, list[str]] = {}
    try:
        records = dispatch_state.load_all(root)
    except AgentError, OSError:
        return out
    for d in records:
        if d.get("feature") != feature_id:
            continue
        landed = dig(d, "integration", "state") == "landed"
        if not landed and d.get("state") != "done":
            continue
        label = f"dispatch {d.get('id')} {'landed' if landed else 'returned done'}"
        for t in as_list(d.get("tasks")):
            if isinstance(t, int):
                out.setdefault(t, []).append(label)
    return out


def _elsewhere(root: Path, feature: features_mod.Feature) -> list[tuple[str, str]]:
    """(source, text) for every ticked or superseded task line in other features that could
    mention this one: a mention holds the feature's number, so other lines are dropped here
    rather than run through every task's mention pattern."""
    rows: list[tuple[str, str]] = []
    number = str(feature.id)
    try:
        dirs = [d for d in feature_tree.walk(root).features]
    except AgentError, OSError:
        return rows
    for path in dirs:
        _tick()
        try:
            other = features_mod.load_dir(path)
        except AgentError, OSError:
            continue
        if other.id == feature.id or number not in other.body:
            continue
        for t in features_mod.list_tasks(other.body):
            if not t.is_open and number in t.text:
                rows.append((f"{other.number} #{t.index} {t.state}", t.text))
    return rows


def _clip(text: str, limit: int = MAX_LINE) -> str:
    indent = text[: len(text) - len(text.lstrip())]
    text = indent + " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def check(
    root: Path,
    feature: features_mod.Feature,
    repos: list[RepoRef] | None = None,
    budget_s: float | None = None,
) -> dict[str, Any]:
    """Evidence per open task. `repos` overrides the feature's registered repos (probes, tests).
    Bounded by `budget_s` (default BUDGET_S): past it the result holds the tasks checked so far
    and `incomplete` says so, and nothing waits longer."""
    open_tasks = [t for t in features_mod.list_tasks(feature.body) if t.is_open]
    result: dict[str, Any] = {
        "feature": feature.id,
        "number": feature.number,
        "slug": feature.slug,
        "open": len(open_tasks),
        "repos": [],
        "context": None,
        "tasks": [],
        "incomplete": None,
    }
    budget = BUDGET_S if budget_s is None else budget_s
    token = _DEADLINE.set(time.monotonic() + budget)
    try:
        if open_tasks:
            _check_into(result, root, feature, open_tasks, repos)
    except OutOfTime:
        result["incomplete"] = (
            f"timed out after {budget:g} s; checked {len(result['tasks'])} of {len(open_tasks)} open task(s)"
        )
    finally:
        _DEADLINE.reset(token)
    result["flagged"] = sum(1 for t in result["tasks"] if t["verdict"] != "no signal")
    return result


def _check_into(
    result: dict[str, Any],
    root: Path,
    feature: features_mod.Feature,
    open_tasks: list[features_mod.Task],
    repos: list[RepoRef] | None,
) -> None:
    """Fill `result` task by task, so whatever is done when the budget runs out is kept."""
    repos = feature_repos(root, feature) if repos is None else repos
    result["repos"] = [{"name": r.name, "ref": r.ref} for r in repos]
    plan = feature.plan_path
    opened = coerce_date(feature.meta.get("opened"))
    fallback = opened.isoformat() if opened else None
    edited = context_edited(plan)
    context: dict[str, Any] = {"edited": edited, "commits_since": {}}
    if edited:
        for repo in repos:
            count = _git(["rev-list", "--count", f"--since={edited}", repo.ref], repo.path)
            context["commits_since"][repo.name] = (
                int(count.strip()) if count and count.strip().isdigit() else None
            )
    result["context"] = context
    trees = {repo.name: _tree(repo) for repo in repos}
    landed = _landed(root, feature.id)
    elsewhere = _elsewhere(root, feature)
    all_texts = [t.text for t in features_mod.list_tasks(feature.body)]
    births: dict[int, tuple[str, str] | None] = {}
    for t in open_tasks:
        _tick()
        births[t.index] = added_at(plan, t.text, all_texts[: t.index - 1].count(t.text))
    known = [b[1] for b in births.values() if b] + ([fallback] if fallback else [])
    earliest = min(known) if known else None
    symbols = {t.index: task_symbols(t.text) for t in open_tasks}
    patches = any(symbols.values())
    history = {repo.name: _history(repo, earliest, patches) for repo in repos} if earliest else {}
    for t in open_tasks:
        _tick()
        birth = births[t.index]
        since = birth[1] if birth else fallback
        done_signals: list[str] = list(landed.get(t.index, []))
        mention = task_mention_re(feature.id, t.index)
        for source, text in elsewhere:
            _tick()
            if mention.search(text):
                done_signals.append(f"{source}: {_clip(text, 80)}")
        changed: list[str] = []
        for repo_name, commits in history.items():
            _tick()
            prefix = "" if len(repos) == 1 else f"{repo_name} "
            later = [c for c in commits if not since or c.when >= since]
            # Match subjects naming the task or a branch or path the task cites.
            cited = [s for s in task_paths(t.text) + symbols[t.index] if "/" in s or "_" in s]
            done_signals += [
                f"{prefix}commit {c.line}"
                for c in later
                if mention.search(c.line) or any(s in c.line.split(" ", 2)[-1] for s in cited)
            ]
            for symbol in symbols[t.index]:
                _tick()
                hits = [c.line for c in later if symbol in c.changed]
                if hits:
                    changed.append(f"{prefix}`{symbol}`: {len(hits)} commit(s) since added ({_shown(hits)})")
        if since:
            for token in task_paths(t.text):
                changed += _path_signals(token, since, repos, trees)
        verdict = "possibly done" if done_signals else "changed under it" if changed else "no signal"
        result["tasks"].append(
            {
                "index": t.index,
                "text": t.text,
                # No line history: the opened date stands in, and says so.
                "added": {"sha": birth[0], "date": birth[1], "approximate": False}
                if birth
                else ({"sha": None, "date": fallback, "approximate": True} if fallback else None),
                "verdict": verdict,
                "done_signals": done_signals,
                "changed": changed,
            }
        )


def _age(iso_ts: str) -> str:
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts
    days = (datetime.now(then.tzinfo) - then).days
    return f"{then.date().isoformat()} ({days}d ago)"


def render(result: dict[str, Any]) -> str:
    """Short and actionable: one header, the Context age, two lines at most per flagged task,
    and the unflagged ones as a list of numbers."""
    head = f"task check {result['number']} {result['slug']}: {result['open']} open"
    if not result["open"]:
        return head
    flagged = [t for t in result["tasks"] if t["verdict"] != "no signal"]
    quiet = [t for t in result["tasks"] if t["verdict"] == "no signal"]
    if not flagged:
        lines = [head + (" (read-only)." if result.get("incomplete") else ", none flagged (read-only).")]
    else:
        lines = [
            f"{head}, {len(flagged)} flagged (read-only): tick (task_done), supersede (task_supersede) "
            "or keep each before dispatching it"
        ]
    if result.get("incomplete"):
        lines.append(f"  incomplete: {result['incomplete']}")
    context = as_dict(result.get("context"))
    edited = context.get("edited")
    if isinstance(edited, str):
        counts = as_dict(context.get("commits_since"))
        since = ", ".join(f"{n} has {c} commit(s) since" for n, c in counts.items() if c is not None)
        lines.append(f"  Context last edited {_age(edited)}" + (f"; {since}" if since else ""))
    for t in flagged:
        born = t["added"]
        added = ""
        if born and born["date"]:
            added = (
                f" (added ~{born['date'][:10]}, birth approximate)"
                if born.get("approximate")
                else f" (added {born['date'][:10]})"
            )
        lines.append(_clip(f"  #{t['index']} {t['verdict']}{added}: {t['text']}"))
        for row in (t["done_signals"] + t["changed"])[:2]:
            lines.append(_clip(f"     {row}"))
        extra = len(t["done_signals"]) + len(t["changed"]) - 2
        if extra > 0:
            lines.append(f"     +{extra} more signal(s)")
    if quiet and flagged:
        lines.append("  no signal: " + ", ".join(f"#{t['index']}" for t in quiet))
    return "\n".join(lines)
