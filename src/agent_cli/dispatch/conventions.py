"""Compare the real base-to-head diff with repository conventions and list added comments for review.

diff_rules_violations reads configured path globs, change kinds, forbidden added-line patterns and
quoted governing sentences. Context or worker claims cannot establish that a diff follows those
rules.

added_comments lists every added comment and any optional return-row reason. The handoff displays
them for manager and reviewer judgment before publication. Missing reasons do not block a comment.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import repos as repos_mod
from ..records.narrow import dict_list

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
GWT_RE = re.compile(r"^(Given|When|Then|And)\b")
_XML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
# A self-closing reference tag in a doc comment is replaced by the name in its attribute, so
# the handoff keeps the word the tag stood for instead of a gap where the tag was.
_XML_REF_RE = re.compile(r"<(?:see|seealso|paramref|typeparamref)\s+(?:cref|name|langword)=\"([^\"]*)\"\s*/>")

_SLASH_EXT = {
    ".cs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".go", ".swift",
    ".scss", ".css", ".c", ".cc", ".cpp", ".h", ".hpp", ".rs", ".php",
}  # fmt: skip
_HASH_EXT = {".py", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".rb", ".ps1", ".tf"}
_ANGLE_EXT = {".html", ".xml", ".cshtml", ".razor", ".vue", ".svelte", ".csproj", ".props"}


@dataclass
class FileDiff:
    path: str
    status: str  # "A" added, "M" modified, "D" deleted
    added: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])


def squash(text: str) -> str:
    return " ".join(text.split())


def parse_diff(text: str) -> list[FileDiff]:
    """`git diff --unified=0 --no-renames` output as one `FileDiff` per file, with each added
    line and its line number in the new file."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    line_no = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            cur = FileDiff(path="", status="M")
            files.append(cur)
            continue
        if cur is None:
            continue
        if raw.startswith("new file mode"):
            cur.status = "A"
        elif raw.startswith("deleted file mode"):
            cur.status = "D"
        elif raw.startswith("+++ "):
            target = raw[4:]
            if target.startswith("b/"):
                cur.path = target[2:]
        elif raw.startswith("--- "):
            source = raw[4:]
            if cur.status == "D" and source.startswith("a/"):
                cur.path = source[2:]
        elif m := _HUNK_RE.match(raw):
            line_no = int(m.group(1))
        elif raw.startswith("+"):
            cur.added.append((line_no, raw[1:]))
            line_no += 1
    return [f for f in files if f.path]


def _diff_text(worktree: Path, base: str) -> str | None:
    """`git diff` text from the merge base of `base` to HEAD, `None` when git cannot produce it."""
    return repos_mod.git_or_none(
        ["diff", "--no-color", "--no-ext-diff", "--unified=0", "--no-renames", f"{base}...HEAD"], worktree
    )


def read_diff(worktree: Path, base: str) -> list[FileDiff]:
    """The committed diff from the merge base of `base` to HEAD. Three dots, so a branch that
    merged its base in later is judged on its own lines only."""
    return parse_diff(_diff_text(worktree, base) or "")


def own_lines(worktree: Path, start: str) -> set[tuple[str, str]]:
    """Each (path, line) added by a non-merge commit on HEAD's first-parent chain since
    `start`. A worker that fast-forwards to a remote tip holding a merge of main brings main's
    commits into `start..HEAD`; they sit behind the merge's second parent, so they are left out.
    Lines a merge commit itself adds (a conflict resolution) are left out too."""
    out = repos_mod.git_or_none(
        [
            "log", "--first-parent", "--no-merges", "--format=", "-p", "--no-color", "--no-ext-diff",
            "--unified=0", "--no-renames", f"{start}..HEAD",
        ],
        worktree,
    )  # fmt: skip
    return {(f.path, text) for f in parse_diff(out or "") for _, text in f.added}


def own_paths(worktree: Path, start: str) -> set[str]:
    """Every path a non-merge commit on HEAD's first-parent chain since `start` touched: the
    commit set `own_lines` reads, but by name, so a delete or a removal-only edit counts too.
    `own_diff` is no substitute: it keeps every file of the branch diff and only filters lines."""
    out = repos_mod.git_or_none(
        [
            "log", "--first-parent", "--no-merges", "--format=", "--name-only", "--no-renames",
            f"{start}..HEAD",
        ],
        worktree,
    )  # fmt: skip
    return {line.strip() for line in (out or "").splitlines() if line.strip()}


def own_diff(worktree: Path, start: str) -> list[FileDiff]:
    """The diff since `start`, keeping only the added lines this branch's own commits wrote."""
    own = own_lines(worktree, start)
    files = read_diff(worktree, start)
    for f in files:
        f.added = [(n, text) for n, text in f.added if (f.path, text) in own]
    return files


# ------------------------------------------------------------------ diff rules


def _path_matches(path: str, glob: str) -> bool:
    """`fnmatch` semantics, where `*` also crosses `/`, so `tests/*test_*.py` covers nested files."""
    return fnmatch.fnmatchcase(path, glob)


def diff_rules_violations(files: list[FileDiff], rules: list[repos_mod.DiffRule]) -> list[dict[str, Any]]:
    """One row per added line that a rule forbids, with the doc and sentence it breaks."""
    rows: list[dict[str, Any]] = []
    for rule in rules:
        rx = re.compile(rule.forbid)
        for f in files:
            if f.status == "D" or not _path_matches(f.path, rule.paths):
                continue
            if rule.change == "added" and f.status != "A":
                continue
            if rule.change == "modified" and f.status != "M":
                continue
            for line_no, text in f.added:
                if rx.search(text):
                    rows.append(
                        {
                            "path": f.path,
                            "line": line_no,
                            "text": text.strip(),
                            "doc": rule.doc,
                            "rule": rule.rule,
                        }
                    )
    return rows


# ------------------------------------------------------------------ comments


def _comment_text(path: str, line: str) -> str | None:
    """The text of `line` if it is a comment line for this file type, markers stripped;
    `None` for code. Only whole-line comments count: a trailing comment after code is rare in
    the repos here and too easy to confuse with a URL or a string."""
    ext = Path(path).suffix.lower()
    s = line.strip()
    if ext in _SLASH_EXT:
        if s.startswith("//"):
            body = s.lstrip("/")
        elif s.startswith("/*") or s.startswith("*"):
            body = s.lstrip("/*").rstrip("*/")
        else:
            return None
        return _XML_TAG_RE.sub(" ", _XML_REF_RE.sub(r"\1", body)).strip()
    if ext in _HASH_EXT:
        if s.startswith("#") and not s.startswith("#!"):
            return s.lstrip("#").strip()
        return None
    if ext in _ANGLE_EXT:
        if s.startswith("<!--"):
            return s.removeprefix("<!--").removesuffix("-->").strip()
        return None
    return None


def added_comments(files: list[FileDiff]) -> list[dict[str, Any]]:
    """Every added comment, consecutive lines in one file joined into one block, so a phrase
    that wraps a line break is still one run of words."""
    blocks: list[dict[str, Any]] = []
    for f in files:
        prev = -2
        for line_no, text in f.added:
            body = _comment_text(f.path, text)
            if body is None:
                prev = -2
                continue
            if blocks and line_no == prev + 1 and blocks[-1]["path"] == f.path:
                blocks[-1]["text"] = (blocks[-1]["text"] + " " + body).strip()
                blocks[-1]["end"] = line_no
            else:
                blocks.append({"path": f.path, "line": line_no, "end": line_no, "text": body})
            prev = line_no
    return [b for b in blocks if b["text"]]


def _row_for(comment: dict[str, Any], rows: list[Any] | None) -> str:
    """The reason of the first row covering `comment`, or "". A row names a path and a `line`,
    and optionally an `end`, so one row can answer for a run of like comments (a run of
    one-line docstrings on sibling functions). It covers a block when the two line
    ranges overlap. A row with an empty reason covers nothing."""
    for r in dict_list(rows):
        start, end = r.get("line"), r.get("end")
        if not isinstance(start, int):
            continue
        why = str(r.get("reason") or "").strip()
        end = end if isinstance(end, int) else start
        if (
            why
            and str(r.get("path") or "") == comment["path"]
            and start <= comment["end"]
            and comment["line"] <= end
        ):
            return why
    return ""


def annotate_reasons(comments: list[dict[str, Any]], rows: list[Any] | None) -> None:
    """Set each comment's `reason` from an optional `comments` row in the return. Display only:
    no comment needs a row, and none is blocked for lacking one."""
    for c in comments:
        c["reason"] = _row_for(c, rows)


# ------------------------------------------------------------------ governing docs

_PATHISH_RE = re.compile(
    r"[A-Za-z0-9_.\-/]*[A-Za-z0-9_\-]\.[A-Za-z0-9]{1,8}\b|[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+/?"
)


def named_paths(repo_dir: Path, text: str) -> list[str]:
    """Resolve named repo paths against tracked files: accept a full path that exists, a new file
    under an existing parent, or a bare name matching exactly one tracked file. Ambiguous bare
    names establish no path.
    """
    tracked = (repos_mod.git_or_none(["ls-files"], repo_dir) or "").splitlines()
    by_name: dict[str, list[str]] = {}
    dirs: set[str] = set()
    for t in tracked:
        by_name.setdefault(t.rsplit("/", 1)[-1], []).append(t)
        parts = t.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    tracked_set = set(tracked)
    found: list[str] = []
    for m in _PATHISH_RE.finditer(text):
        cand = m.group(0).strip("./").rstrip("/")
        hit: str | None = None
        if cand in tracked_set or cand in dirs:
            hit = cand
        elif "/" in cand and cand.rsplit("/", 1)[0] in dirs:
            hit = cand
        elif "/" not in cand and len(by_name.get(cand, [])) == 1:
            hit = by_name[cand][0]
        if hit and hit not in found:
            found.append(hit)
    return found


def governing_docs(repo_dir: Path, paths: list[str]) -> list[str]:
    """Every AGENTS.md or CLAUDE.md in the directories above each path, the repo root's own
    excluded because Claude Code loads it already. A CLAUDE.md that only imports its sibling
    AGENTS.md is not listed twice."""
    docs: list[str] = []
    for p in paths:
        parts = p.split("/")
        is_dir = (repo_dir / p).is_dir()
        top = len(parts) if is_dir else len(parts) - 1
        for i in range(1, top + 1):
            d = "/".join(parts[:i])
            agents = repo_dir / d / "AGENTS.md"
            claude = repo_dir / d / "CLAUDE.md"
            names: list[str] = []
            if agents.is_file():
                names.append(f"{d}/AGENTS.md")
            if claude.is_file() and not (
                agents.is_file()
                and claude.read_text(encoding="utf-8", errors="replace").strip() == "@AGENTS.md"
            ):
                names.append(f"{d}/CLAUDE.md")
            for n in names:
                if n not in docs:
                    docs.append(n)
    return docs


def prompt_section(repo_dir: Path, text: str, rules: list[repos_mod.DiffRule]) -> list[str]:
    """The work-order block that names the convention docs in play and the rules checked at
    return. Generated, so the manager cannot forget it or word it as a suggestion."""
    docs = governing_docs(repo_dir, named_paths(repo_dir, text)) if repo_dir.is_dir() else []
    if not docs and not rules:
        return []
    lines = ["## Conventions that govern this work", ""]
    if docs:
        lines.append(
            "These docs govern the paths this work order names. Read each before you write in its "
            "directory. They outrank this work order and any file it tells you to mirror: where a "
            "sibling breaks one of them, follow the doc and list the difference in your return."
        )
        lines.append("")
        lines += [f"- `{d}`" for d in docs]
        lines.append("")
    if rules:
        lines.append("Checked on your diff at return; a `done` that breaks one comes back `blocked`:")
        lines.append("")
        lines += [f'- `{r.paths}` ({r.change} files): {r.doc} says "{r.rule}"' for r in rules]
        lines.append("")
    lines.append(
        "Every `AGENTS.md` or `CLAUDE.md` above a file you change, the repo root's excepted, governs "
        "that file too, including directories this work order does not name: read it first. If a doc "
        "forbids what this work order asks, return `question` instead."
    )
    return lines


# ------------------------------------------------------------------ the gate


_OUTSIDE = object()


def _read_doc(wt: Path, doc: str) -> Any:
    """A quoted file's text, None when absent, or `_OUTSIDE`. `doc` comes from the worker, so the
    manager reads it only when it resolves inside the worktree: no absolute path, no `..`, no
    symlink out. Any file name may be quoted, such as a repo's own `SKILL.md`; only
    AGENTS.md and CLAUDE.md are ever required."""
    if not doc:
        return None
    rel = Path(doc)
    if rel.is_absolute() or ".." in rel.parts:
        return _OUTSIDE
    try:
        # A symlink loop raises RuntimeError from resolve(); fail closed, never abort evaluate().
        root = wt.resolve()
        path = (wt / rel).resolve()
    except OSError, RuntimeError:
        return _OUTSIDE
    if not path.is_relative_to(root):
        return _OUTSIDE
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    except OSError:
        return None


def check_convention_rows(
    wt: Path, required: list[str], rows: list[Any] | None
) -> tuple[list[dict[str, str]], list[str], list[dict[str, str]]]:
    """(answered, missing_docs, bad_quotes) for the `conventions` rows a return carries.

    A row is `{doc, quote}`. It answers for its doc only when the quote appears in that doc,
    whitespace ignored. The gate cannot tell a true quote from a motivated one, but it can tell
    an invented one: a quote that appears nowhere in the doc it names cannot be claimed as the
    rule that allowed a change. A bad quote is reported and its doc stays missing."""
    answered: list[dict[str, str]] = []
    bad: list[dict[str, str]] = []
    for r in dict_list(rows):
        doc = str(r.get("doc") or "").strip().removeprefix("./")
        quote = str(r.get("quote") or "").strip()
        body = _read_doc(wt, doc)
        if body is _OUTSIDE:
            bad.append({"doc": doc, "quote": quote, "reason": "doc outside the worktree"})
            continue
        if quote and body is not None and squash(quote) in squash(body):
            answered.append({"doc": doc, "quote": quote})
        else:
            reason = "doc not found" if body is None else "quote not found in the doc"
            bad.append({"doc": doc, "quote": quote, "reason": reason if quote else "empty quote"})
    done = {a["doc"] for a in answered}
    return answered, [doc for doc in required if doc not in done], bad


def pr_base(wt: Path, d: dict[str, Any], rp: repos_mod.Repo | None) -> str:
    """What the branch's PR is judged against: `origin/<default>` when the worktree has it, else
    the dispatch base. The three-dot diff runs from the merge base, so a branch that merged a
    newer default branch is judged on its own files only. Against the dispatch base, a branch
    that merged a far newer main would be judged on files it never touched."""
    if rp is not None:
        ref = f"origin/{rp.default_branch}"
        if repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], wt):
            return ref
    return str(d["base"])


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError, ValueError:
        return None


def gate(d: dict[str, Any], rp: repos_mod.Repo | None, ret: dict[str, Any] | None) -> dict[str, Any] | None:
    """The checks over a finished attempt's diff, or `None` when there is no worktree to read or
    git cannot read it. `violations` and `noise` block a `done`. `comments`, the docs that govern
    the touched paths and any `conventions` rows the return carried are for the handoff and the
    reviewer: a quote is no longer required, and a wrong one no longer blocks."""
    wt = Path(d.get("worktree") or "")
    if not d.get("worktree") or not wt.exists():
        return None
    text = _diff_text(wt, pr_base(wt, d, rp))
    if text is None:
        return None
    files = parse_diff(text)
    rules = rp.diff_rules if rp is not None else []
    # Rules hold the whole branch, since a PR has to pass them. Comments count only when this
    # dispatch added them; a follow-up on a long branch is not judged on the rest, nor on the main
    # a merge brought in (over a third of one run's findings were merged-in main).
    from .comment_noise import added_docstrings, noisy

    start = d.get("start_sha")
    own = own_diff(wt, start) if start else files
    comments = added_comments(own) + added_docstrings(own, lambda path: _read(wt / path))
    annotate_reasons(comments, (ret or {}).get("comments"))
    branch_paths = [f.path for f in files]
    touched = [p for p in branch_paths if p in own_paths(wt, start)] if start else branch_paths
    governing = governing_docs(wt, touched)
    answered, _, bad = check_convention_rows(wt, governing, (ret or {}).get("conventions"))
    return {
        "violations": diff_rules_violations(files, rules),
        "noise": noisy(comments),
        "comments": comments,
        "governing_docs": governing,
        "answered": answered,
        "bad_quotes": bad,
    }


def block_lines(result: dict[str, Any]) -> list[str]:
    """The `next_steps` lines a blocked return carries, one per rule violation and noisy comment."""
    lines = [
        f'convention: {v["path"]}:{v["line"]} breaks {v["doc"]}: "{v["rule"]}" (line: {v["text"]})'
        for v in dict_list(result.get("violations"))
    ]
    lines += [
        f'comment noise: {c["path"]}:{c["line"]} {c["why"]}: "{str(c["text"])[:80]}". Delete it, or keep '
        "it only if it tells a reader something the code does not; ids belong in your return."
        for c in dict_list(result.get("noise"))
    ]
    return lines
