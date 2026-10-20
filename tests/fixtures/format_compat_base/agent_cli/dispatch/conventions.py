# ruff: noqa
from __future__ import annotations
import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .. import repos as repos_mod
from ..records.narrow import dict_list

_HUNK_RE = re.compile("^@@ -\\d+(?:,\\d+)? \\+(\\d+)(?:,\\d+)? @@")
GWT_RE = re.compile("^(Given|When|Then|And)\\b")
_XML_TAG_RE = re.compile("</?[A-Za-z][^>]*>")
_XML_REF_RE = re.compile(
    '<(?:see|seealso|paramref|typeparamref)\\s+(?:cref|name|langword)=\\"([^\\"]*)\\"\\s*/>'
)
_SLASH_EXT = {
    ".cs",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".java",
    ".kt",
    ".go",
    ".swift",
    ".scss",
    ".css",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".rs",
    ".php",
}
_HASH_EXT = {".py", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".rb", ".ps1", ".tf"}
_ANGLE_EXT = {".html", ".xml", ".cshtml", ".razor", ".vue", ".svelte", ".csproj", ".props"}


@dataclass
class FileDiff:
    path: str
    status: str
    added: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])


def squash(text: str) -> str:
    return " ".join(text.split())


def parse_diff(text: str) -> list[FileDiff]:
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
    return repos_mod.git_or_none(
        ["diff", "--no-color", "--no-ext-diff", "--unified=0", "--no-renames", f"{base}...HEAD"], worktree
    )


def read_diff(worktree: Path, base: str) -> list[FileDiff]:
    return parse_diff(_diff_text(worktree, base) or "")


def own_lines(worktree: Path, start: str) -> set[tuple[str, str]]:
    out = repos_mod.git_or_none(
        [
            "log",
            "--first-parent",
            "--no-merges",
            "--format=",
            "-p",
            "--no-color",
            "--no-ext-diff",
            "--unified=0",
            "--no-renames",
            f"{start}..HEAD",
        ],
        worktree,
    )
    return {(f.path, text) for f in parse_diff(out or "") for _, text in f.added}


def own_paths(worktree: Path, start: str) -> set[str]:
    out = repos_mod.git_or_none(
        [
            "log",
            "--first-parent",
            "--no-merges",
            "--format=",
            "--name-only",
            "--no-renames",
            f"{start}..HEAD",
        ],
        worktree,
    )
    return {line.strip() for line in (out or "").splitlines() if line.strip()}


def own_diff(worktree: Path, start: str) -> list[FileDiff]:
    own = own_lines(worktree, start)
    files = read_diff(worktree, start)
    for f in files:
        f.added = [(n, text) for n, text in f.added if (f.path, text) in own]
    return files


def _path_matches(path: str, glob: str) -> bool:
    return fnmatch.fnmatchcase(path, glob)


def diff_rules_violations(files: list[FileDiff], rules: list[repos_mod.DiffRule]) -> list[dict[str, Any]]:
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


def _comment_text(path: str, line: str) -> str | None:
    ext = Path(path).suffix.lower()
    s = line.strip()
    if ext in _SLASH_EXT:
        if s.startswith("//"):
            body = s.lstrip("/")
        elif s.startswith("/*") or s.startswith("*"):
            body = s.lstrip("/*").rstrip("*/")
        else:
            return None
        return _XML_TAG_RE.sub(" ", _XML_REF_RE.sub("\\1", body)).strip()
    if ext in _HASH_EXT:
        if s.startswith("#") and (not s.startswith("#!")):
            return s.lstrip("#").strip()
        return None
    if ext in _ANGLE_EXT:
        if s.startswith("<!--"):
            return s.removeprefix("<!--").removesuffix("-->").strip()
        return None
    return None


def added_comments(files: list[FileDiff]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for f in files:
        prev = -2
        for line_no, text in f.added:
            body = _comment_text(f.path, text)
            if body is None:
                prev = -2
                continue
            if blocks and line_no == prev + 1 and (blocks[-1]["path"] == f.path):
                blocks[-1]["text"] = (blocks[-1]["text"] + " " + body).strip()
                blocks[-1]["end"] = line_no
            else:
                blocks.append({"path": f.path, "line": line_no, "end": line_no, "text": body})
            prev = line_no
    return [b for b in blocks if b["text"]]


def _row_for(comment: dict[str, Any], rows: list[Any] | None) -> str:
    for r in dict_list(rows):
        start, end = (r.get("line"), r.get("end"))
        if not isinstance(start, int):
            continue
        why = str(r.get("reason") or "").strip()
        end = end if isinstance(end, int) else start
        if (
            why
            and str(r.get("path") or "") == comment["path"]
            and (start <= comment["end"])
            and (comment["line"] <= end)
        ):
            return why
    return ""


def annotate_reasons(comments: list[dict[str, Any]], rows: list[Any] | None) -> None:
    for c in comments:
        c["reason"] = _row_for(c, rows)


_PATHISH_RE = re.compile(
    "[A-Za-z0-9_.\\-/]*[A-Za-z0-9_\\-]\\.[A-Za-z0-9]{1,8}\\b|[A-Za-z0-9_.\\-]+(?:/[A-Za-z0-9_.\\-]+)+/?"
)


def named_paths(repo_dir: Path, text: str) -> list[str]:
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
            if claude.is_file() and (
                not (
                    agents.is_file()
                    and claude.read_text(encoding="utf-8", errors="replace").strip() == "@AGENTS.md"
                )
            ):
                names.append(f"{d}/CLAUDE.md")
            for n in names:
                if n not in docs:
                    docs.append(n)
    return docs


def prompt_section(repo_dir: Path, text: str, rules: list[repos_mod.DiffRule]) -> list[str]:
    docs = governing_docs(repo_dir, named_paths(repo_dir, text)) if repo_dir.is_dir() else []
    if not docs and (not rules):
        return []
    lines = ["## Conventions that govern this work", ""]
    if docs:
        lines.append(
            "These docs govern the paths this work order names. Read each before you write in its directory. They outrank this work order and any file it tells you to mirror: where a sibling breaks one of them, follow the doc and list the difference in your return."
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
        "Every `AGENTS.md` or `CLAUDE.md` above a file you change, the repo root's excepted, governs that file too, including directories this work order does not name: read it first. If a doc forbids what this work order asks, return `question` instead."
    )
    return lines


_OUTSIDE = object()


def _read_doc(wt: Path, doc: str) -> Any:
    if not doc:
        return None
    rel = Path(doc)
    if rel.is_absolute() or ".." in rel.parts:
        return _OUTSIDE
    try:
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
    answered: list[dict[str, str]] = []
    bad: list[dict[str, str]] = []
    for r in dict_list(rows):
        doc = str(r.get("doc") or "").strip().removeprefix("./")
        quote = str(r.get("quote") or "").strip()
        body = _read_doc(wt, doc)
        if body is _OUTSIDE:
            bad.append({"doc": doc, "quote": quote, "reason": "doc outside the worktree"})
            continue
        if quote and body is not None and (squash(quote) in squash(body)):
            answered.append({"doc": doc, "quote": quote})
        else:
            reason = "doc not found" if body is None else "quote not found in the doc"
            bad.append({"doc": doc, "quote": quote, "reason": reason if quote else "empty quote"})
    done = {a["doc"] for a in answered}
    return (answered, [doc for doc in required if doc not in done], bad)


def pr_base(wt: Path, d: dict[str, Any], rp: repos_mod.Repo | None) -> str:
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
    wt = Path(d.get("worktree") or "")
    if not d.get("worktree") or not wt.exists():
        return None
    text = _diff_text(wt, pr_base(wt, d, rp))
    if text is None:
        return None
    files = parse_diff(text)
    rules = rp.diff_rules if rp is not None else []
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
    lines = [
        f'''convention: {v["path"]}:{v["line"]} breaks {v["doc"]}: "{v["rule"]}" (line: {v["text"]})'''
        for v in dict_list(result.get("violations"))
    ]
    lines += [
        f'''comment noise: {c["path"]}:{c["line"]} {c["why"]}: "{str(c["text"])[:80]}". Delete it, or keep it only if it tells a reader something the code does not; ids belong in your return.'''
        for c in dict_list(result.get("noise"))
    ]
    return lines
