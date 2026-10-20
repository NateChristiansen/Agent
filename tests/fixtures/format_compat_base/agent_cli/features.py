# ruff: noqa
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from . import feature_tree, frontmatter, task_md, templates
from .core import AgentError, NotFound, agent_path, today
from .records.narrow import as_list
from .tickets import validate_tickets

FEATURES_DIR = feature_tree.FEATURES_DIR
STATUSES = ("proposed", "active", "blocked", "done", "abandoned")
OPEN_STATUSES = ("proposed", "active", "blocked")
TERMINAL_STATUSES = ("done", "abandoned")
SLUG_RE = re.compile("^[a-z0-9]+(-[a-z0-9]+)*$")
TYPE_TAG = "feature"
DIR_RE = feature_tree.FEATURE_DIR_RE
DONE_MARKERS = ("x", "X")
SUPERSEDED_MARKER = "-"
TASK_SUFFIX_RE = re.compile("\\s+(?:\\{[^{}\\n]*\\}\\s+)?\\^t\\d+$")
LINE_SEPARATORS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
RESERVED_SLUGS = {"current", "all", "new", "list", "default"}


@dataclass
class Feature:
    id: int
    slug: str
    dir: Path
    meta: dict[str, Any]
    body: str

    @property
    def number(self) -> str:
        return f"{self.id:03d}"

    @property
    def plan_path(self) -> Path:
        return self.dir / "plan.md"

    @property
    def log_path(self) -> Path:
        return self.dir / "log.md"

    @property
    def raw_dir(self) -> Path:
        return agent_path(feature_tree.vault_root(self.dir), "raw", self.dir.name)

    @property
    def status(self) -> str:
        return str(self.meta.get("status", ""))

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def rel_dir(self, root: Path) -> str:
        return self.dir.relative_to(root).as_posix()

    def wikilink(self, root: Path) -> str:
        return f"[[{self.rel_dir(root)}/plan|{self.number} {self.slug}]]"

    def tasks(self) -> dict[str, Any]:
        return parse_tasks(self.body)

    def save(self) -> None:
        frontmatter.dump(self.plan_path, self.meta, self.body)


@dataclass
class Task:
    index: int
    done: bool
    text: str
    line_no: int
    superseded: bool = False

    @property
    def is_open(self) -> bool:
        return not self.done and (not self.superseded)

    @property
    def state(self) -> str:
        return "done" if self.done else "superseded" if self.superseded else "open"


def list_tasks(body: str) -> list[Task]:
    tasks: list[Task] = []
    for item in task_md.scan(body):
        if item.text:
            done, superseded = (item.marker in DONE_MARKERS, item.marker == SUPERSEDED_MARKER)
            tasks.append(Task(len(tasks) + 1, done, item.text, item.line_no, superseded))
    return tasks


def parse_tasks(body: str) -> dict[str, Any]:
    tasks = list_tasks(body)
    open_tasks = [t for t in tasks if t.is_open]
    return {
        "open": len(open_tasks),
        "done": sum((1 for t in tasks if t.done)),
        "superseded": sum((1 for t in tasks if t.superseded)),
        "next": open_tasks[0].text if open_tasks else None,
        "open_items": [t.text for t in open_tasks],
    }


def resolve_task(body: str, ref: str) -> Task:
    tasks = list_tasks(body)
    if not tasks:
        raise NotFound("this feature has no tasks under ## Tasks")
    ref = ref.strip()
    if ref.isdigit():
        n = int(ref)
        for t in tasks:
            if t.index == n:
                return t
        raise NotFound(f"no task #{n}; there are {len(tasks)}")
    hits = [t for t in tasks if ref.lower() in t.text.lower()]
    if not hits:
        raise NotFound(f"no task matches {ref!r}")
    if len(hits) > 1:
        raise AgentError(
            f"{ref!r} matches {len(hits)} tasks; use the number: " + ", ".join((f"#{t.index}" for t in hits))
        )
    return hits[0]


def split_suffix(text: str) -> tuple[str, str]:
    m = TASK_SUFFIX_RE.search(text)
    return (text[: m.start()], text[m.start() :]) if m else (text, "")


def one_line(value: str, what: str) -> str:
    if any((ch in LINE_SEPARATORS for ch in value)):
        raise AgentError(f"{what} must be one line; put detail in the plan body or the log")
    return value


def with_note(text: str, note: str | None) -> str:
    if not note:
        return text
    one_line(note, "a task note")
    prose, suffix = split_suffix(text)
    return f"{prose} ({note}){suffix}"


def _join(lines: list[str], body: str) -> str:
    return "\n".join(lines) + ("\n" if body.endswith(("\n", "\r")) else "")


def _write_task(body: str, task: Task, marker: str, text: str) -> str:
    lines = task_md.split_lines(body)
    m = task_md.ITEM_LINE_RE.match(lines[task.line_no])
    if m is None:
        raise AgentError(f"task #{task.index} is no longer a checkbox line")
    lines[task.line_no] = f"{m.group(1)}{marker}] {text}"
    return _join(lines, body)


def set_task(body: str, task: Task, done: bool, note: str | None = None) -> str:
    return _write_task(body, task, "x" if done else " ", with_note(task.text, note))


def supersede_task(body: str, task: Task, reason: str, link: str) -> str:
    reason, link = (one_line(reason, "the reason").strip(), one_line(link, "the link").strip())
    if not reason or not link:
        raise AgentError("superseding a task needs a reason and a link (a commit, task, feature or URL)")
    return _write_task(body, task, SUPERSEDED_MARKER, with_note(task.text, f"superseded: {reason}; {link}"))


def add_task(body: str, text: str) -> str:
    return add_task_at(body, text)[0]


def add_task_at(body: str, text: str) -> tuple[str, int]:
    text = text.strip()
    if not text:
        raise AgentError("task text cannot be empty")
    one_line(text, "a task")
    lines = task_md.split_lines(body)
    before = [(t.state, t.text) for t in list_tasks(body)]
    counted = [item for item in task_md.scan(body) if item.text]
    candidates: list[list[str]] = []
    if counted:
        last = counted[-1]
        at = last.end_line
        while at > last.line_no + 1 and (not lines[at - 1].strip()):
            at -= 1
        m = task_md.ITEM_LINE_RE.match(lines[last.line_no])
        prefix = m.group(1) if m else "- ["
        if not re.match("\\s*[-*+]", prefix):
            prefix = prefix[: len(prefix) - len(prefix.lstrip())] + "- ["
        candidates.append(templates.drop_tasks_hint([*lines[:at], f"{prefix} ] {text}", *lines[at:]]))
        candidates.append(templates.drop_tasks_hint([*lines[:at], "", f"- [ ] {text}", *lines[at:]]))
    else:
        start = task_md.tasks_heading_line(body)
        end = task_md.tasks_section_end(body)
        if start is None or end is None:
            raise AgentError("plan.md has no '## Tasks' section")
        section = lines[start + 1 : end]
        section = templates.drop_tasks_hint([row for row in section if row.strip() not in ("- [ ]", "- [x]")])
        while section and (not section[-1].strip()):
            section.pop()
        for gap in ([], [""]):
            candidates.append([*lines[: start + 1], *section, *gap, f"- [ ] {text}", "", *lines[end:]])
    tail = list(lines)
    while tail and (not tail[-1].strip()):
        tail.pop()
    candidates.append([*tail, "", "## Tasks", "", f"- [ ] {text}"])
    for candidate in candidates:
        new_body = "\n".join(candidate).rstrip("\n") + "\n"
        after = list_tasks(new_body)
        if len(after) == len(before) + 1 and [(t.state, t.text) for t in after[:-1]] == before:
            added = after[-1]
            if added.text == text and added.is_open:
                return (new_body, added.index)
    raise AgentError("could not place the task where it counts; add it to the plan's ## Tasks by hand")


def normalize_slug(text: str) -> str:
    slug = re.sub("[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    if not slug or not SLUG_RE.match(slug):
        raise AgentError(f"cannot make a slug from {text!r}")
    return slug


def feature_slug(text: str) -> str:
    slug = normalize_slug(text)
    if slug.isdigit() or slug in RESERVED_SLUGS:
        raise AgentError(f"slug {slug!r} would collide with a feature reference; pick a descriptive slug")
    return slug


def features_dir(root: Path) -> Path:
    return root / FEATURES_DIR


def _dir_match(name: str) -> re.Match[str]:
    m = DIR_RE.match(name)
    if m is None:
        raise AgentError(f"{name} is not a feature directory (expected NNN-slug)")
    return m


def feature_dirs(root: Path) -> list[Path]:
    return list(feature_tree.walk(root).features)


def load_dir(path: Path) -> Feature:
    m = DIR_RE.match(path.name)
    if not m:
        raise AgentError(f"{path.name} is not a feature directory (expected NNN-slug)")
    meta, body = frontmatter.load(path / "plan.md")
    return Feature(id=int(m.group(1)), slug=m.group(2), dir=path, meta=meta, body=body)


def load_all(root: Path) -> list[Feature]:
    return [load_dir(p) for p in feature_dirs(root)]


def next_id(root: Path) -> int:
    dirs = feature_dirs(root)
    return max((int(_dir_match(p.name).group(1)) for p in dirs)) + 1 if dirs else 1


def resolve(root: Path, ref: str, session_id: str | None = None) -> Feature:
    ref = ref.strip()
    if ref == "current":
        from . import sessions

        try:
            sid = sessions.resolve_session_id(root, session_id)
        except AgentError as exc:
            raise AgentError(
                "no session id for `current`: name a feature explicitly, or pass --session-id for a session mapped with `agent feature use <n>`."
            ) from exc
        record = sessions.get(root, sid)
        if not record or not record.get("feature"):
            raise NotFound(
                "no feature mapped to this session; name a feature explicitly, or run `agent feature use <n>`"
            )
        return resolve(root, str(record["feature"]["id"]))
    dirs = feature_dirs(root)
    if ref.isdigit():
        wanted = int(ref)
        for p in dirs:
            if int(_dir_match(p.name).group(1)) == wanted:
                return load_dir(p)
        raise NotFound(f"no feature with id {wanted}")
    for p in dirs:
        if p.name == ref or _dir_match(p.name).group(2) == ref:
            return load_dir(p)
    raise NotFound(f"no feature matching {ref!r}")


def new(
    root: Path,
    slug: str,
    title: str,
    status: str = "active",
    tickets: list[str] | None = None,
    repos: list[str] | None = None,
    tags: list[str] | None = None,
    opened: date | None = None,
) -> Feature:
    slug = feature_slug(slug)
    if status not in OPEN_STATUSES:
        raise AgentError(f"new features must be one of {', '.join(OPEN_STATUSES)}; got {status!r}")
    if not title.strip():
        raise AgentError("--title is required and cannot be empty")
    repos = validate_repos(root, repos or [])
    for p in feature_dirs(root):
        if _dir_match(p.name).group(2) == slug:
            raise AgentError(f"slug {slug!r} already used by {p.name}")
    from . import notes_claims

    fid = notes_claims.claim(root, "feature", slug).id
    return write_folder(root, fid, slug, title, status, tickets, repos, tags, opened)


def write_folder(
    root: Path,
    fid: int,
    slug: str,
    title: str,
    status: str,
    tickets: list[str] | None,
    repos: list[str],
    tags: list[str] | None,
    opened: date | None,
) -> Feature:
    number = f"{fid:03d}"
    fdir = features_dir(root) / f"{number}-{slug}"
    meta = {
        "id": fid,
        "slug": slug,
        "title": title.strip(),
        "status": status,
        "opened": opened or today(),
        "closed": None,
        "tickets": validate_tickets(tickets or []),
        "repos": repos,
        "adrs": [],
        "decisions": [],
        "tags": with_type_tag(tags),
        "links": [],
    }
    fdir.mkdir(parents=True, exist_ok=False)
    frontmatter.dump(fdir / "plan.md", meta, templates.plan_body(number, title.strip()))
    (fdir / "log.md").write_text(templates.log_header(number, slug), encoding="utf-8")
    return load_dir(fdir)


def with_type_tag(tags: list[str] | None, type_tag: str = TYPE_TAG) -> list[str]:
    out = [type_tag]
    for t in tags or []:
        t = str(t).strip().lstrip("#")
        if t and t not in out:
            out.append(t)
    return out


def add_links(meta: dict[str, Any], links: list[str]) -> None:
    current = [str(x) for x in as_list(meta.get("links"))]
    for link in links:
        if link not in current:
            current.append(link)
    meta["links"] = current


def validate_repos(root: Path, names: list[str]) -> list[str]:
    if not names:
        return []
    from . import repos as repos_mod

    registry = repos_mod.load_registry(root)
    out: list[str] = []
    for n in names:
        repo = registry.find(n)
        if repo is None:
            raise AgentError(f"unknown repo {n!r}; see `agent repos list`")
        if repo.name not in out:
            out.append(repo.name)
    return out


def refuse_open_tasks(feature: Feature) -> None:
    still_open = [t.index for t in list_tasks(feature.body) if t.is_open]
    if still_open:
        raise AgentError(
            f"feature {feature.number} has {len(still_open)} open task(s): "
            + ", ".join((f"#{n}" for n in still_open))
            + ". Before closing, tick each one done (task_done) or supersede it with a reason and a link (task_supersede; a task moved to another feature links to it there)."
        )


def close(root: Path, feature: Feature, status: str = "done", closed: date | None = None) -> Feature:
    if status not in TERMINAL_STATUSES:
        raise AgentError(f"close status must be one of {', '.join(TERMINAL_STATUSES)}")
    if feature.status in TERMINAL_STATUSES:
        raise AgentError(f"feature {feature.number} is already {feature.status}")
    refuse_open_tasks(feature)
    feature.meta["status"] = status
    feature.meta["closed"] = closed or today()
    feature.save()
    from .journal import pending as journal_pending

    journal_pending.record(root, "feature-close", f"{feature.number} {feature.slug}", status)
    return feature


def set_fields(
    root: Path,
    feature: Feature,
    status: str | None = None,
    title: str | None = None,
    add_ticket: list[str] | None = None,
    remove_ticket: list[str] | None = None,
    add_repo: list[str] | None = None,
    remove_repo: list[str] | None = None,
    add_adr: list[int] | None = None,
    remove_adr: list[int] | None = None,
    add_decision: list[int] | None = None,
    remove_decision: list[int] | None = None,
    add_tag: list[str] | None = None,
    remove_tag: list[str] | None = None,
    reason: str | None = None,
) -> Feature:
    meta = feature.meta
    old_status = feature.status
    if status is not None:
        if status not in STATUSES:
            raise AgentError(f"status must be one of {', '.join(STATUSES)}")
        if status in TERMINAL_STATUSES:
            refuse_open_tasks(feature)
        meta["status"] = status
        if status in TERMINAL_STATUSES and (not meta.get("closed")):
            meta["closed"] = today()
        if status in OPEN_STATUSES:
            meta["closed"] = None
    if title is not None:
        if not title.strip():
            raise AgentError("title cannot be empty")
        meta["title"] = title.strip()
    if add_ticket or remove_ticket:
        _edit_list(meta, "tickets", validate_tickets(add_ticket or []), validate_tickets(remove_ticket or []))
    if add_repo or remove_repo:
        adds = validate_repos(root, add_repo or [])
        removes = validate_repos(root, remove_repo or [])
        _edit_list(meta, "repos", adds, removes)
    _edit_list(meta, "adrs", add_adr, remove_adr)
    _edit_list(meta, "decisions", add_decision, remove_decision)
    _edit_list(meta, "tags", add_tag, [t for t in remove_tag or [] if t != TYPE_TAG])
    meta["tags"] = with_type_tag(meta.get("tags"))
    feature.save()
    if status is not None and status != old_status and feature.log_path.exists():
        from . import log as log_mod
        from .blocked import status_entry

        kind = "blocker" if status == "blocked" else "note"
        log_mod.append(feature, kind, status_entry(old_status, status, reason))
    return feature


def _edit_list(meta: dict[str, Any], key: str, adds: list[Any] | None, removes: list[Any] | None) -> None:
    current = list(meta.get(key) or [])
    for item in adds or []:
        if item not in current:
            current.append(item)
    for item in removes or []:
        if item in current:
            current.remove(item)
    meta[key] = current
