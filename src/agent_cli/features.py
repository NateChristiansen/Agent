"""Feature lifecycle: features/NNN-slug/{plan.md, log.md}, .agent/raw/NNN-slug/. A feature may
also sit inside an initiative at any depth; `feature_tree.walk` finds both."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import feature_tree, frontmatter, lessons, task_md, templates
from .core import AgentError, NotFound, agent_path, today
from .records.narrow import as_list
from .tickets import validate_tickets

FEATURES_DIR = feature_tree.FEATURES_DIR
STATUSES = ("proposed", "active", "blocked", "done", "abandoned")
OPEN_STATUSES = ("proposed", "active", "blocked")
TERMINAL_STATUSES = ("done", "abandoned")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TYPE_TAG = "feature"
DIR_RE = feature_tree.FEATURE_DIR_RE
# Read top-level CommonMark tasks with `task_md`. Match Dataview: x/X is done, - is superseded, and
# every other marker is open. Snapshot and Dashboard must count alike.
DONE_MARKERS = ("x", "X")
SUPERSEDED_MARKER = "-"
# Keep `{writes=… after=…} ^tN` last on task lines; notes precede it (`with_note`).
TASK_SUFFIX_RE = re.compile(r"\s+(?:\{[^{}\n]*\}\s+)?\^t\d+$")
# Reject every separator that `str.splitlines` recognises so one task cannot split and renumber
# later tasks.
LINE_SEPARATORS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85  "
RESERVED_SLUGS = {"current", "all", "new", "list", "default"}
# A request shares this store and its numbering; its plan.md says so in `record_kind`. A plan
# without the field is a feature, so every older record keeps its meaning.
KIND_FIELD = "record_kind"
REQUEST_KIND = "request"
REQUEST_TAG = "request"


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
        """`.agent/raw/<NNN-slug>/`: hook-captured prompts, replies and preserved transcripts.
        Lives under `.agent/` (gitignored, CLI-owned), not `features/`, so it never needs its
        own gitignore entry and never shows up in the Obsidian graph. Always at the
        vault root, however deep in an initiative the feature sits."""
        return agent_path(feature_tree.vault_root(self.dir), "raw", self.dir.name)

    @property
    def status(self) -> str:
        return str(self.meta.get("status", ""))

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_request(self) -> bool:
        return self.meta.get(KIND_FIELD) == REQUEST_KIND

    @property
    def label(self) -> str:
        """`Request NNN` or `Feature NNN`, for output that names the record."""
        return f"{'Request' if self.is_request else 'Feature'} {self.number}"

    @property
    def type_tag(self) -> str:
        return REQUEST_TAG if self.is_request else TYPE_TAG

    def rel_dir(self, root: Path) -> str:
        return self.dir.relative_to(root).as_posix()

    def wikilink(self, root: Path) -> str:
        """Obsidian link to this feature's plan. Every feature's file is plan.md, so the
        link must carry the full vault path. The alias keeps it readable."""
        return f"[[{self.rel_dir(root)}/plan|{self.number} {self.slug}]]"

    def tasks(self) -> dict[str, Any]:
        return parse_tasks(self.body)

    def save(self) -> None:
        frontmatter.dump(self.plan_path, self.meta, self.body)


@dataclass
class Task:
    index: int  # 1-based position among all checkbox lines under ## Tasks
    done: bool
    text: str
    line_no: int  # 0-based line index in the body
    superseded: bool = False  # `- [-]`: neither open nor done; keeps its position number

    @property
    def is_open(self) -> bool:
        return not self.done and not self.superseded

    @property
    def state(self) -> str:
        return "done" if self.done else "superseded" if self.superseded else "open"


def list_tasks(body: str) -> list[Task]:
    """Every task (`task_md.scan`), in order. Blank `- [ ]` placeholders are skipped. A
    superseded line keeps its place, so the `#n` numbers never shift."""
    tasks: list[Task] = []
    for item in task_md.scan(body):
        if item.text:
            done, superseded = item.marker in DONE_MARKERS, item.marker == SUPERSEDED_MARKER
            tasks.append(Task(len(tasks) + 1, done, item.text, item.line_no, superseded))
    return tasks


def parse_tasks(body: str) -> dict[str, Any]:
    """Counts plus the first open task; what the snapshot and the dashboard show."""
    tasks = list_tasks(body)
    open_tasks = [t for t in tasks if t.is_open]
    return {
        "open": len(open_tasks),
        "done": sum(1 for t in tasks if t.done),
        "superseded": sum(1 for t in tasks if t.superseded),
        "next": open_tasks[0].text if open_tasks else None,
        "open_items": [t.text for t in open_tasks],
    }


def resolve_task(body: str, ref: str) -> Task:
    """`ref` is a 1-based index or a case-insensitive substring unique among tasks."""
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
            f"{ref!r} matches {len(hits)} tasks; use the number: " + ", ".join(f"#{t.index}" for t in hits)
        )
    return hits[0]


def split_suffix(text: str) -> tuple[str, str]:
    """A task's text and its trailing structured suffix (`TASK_SUFFIX_RE`), which may be ''."""
    m = TASK_SUFFIX_RE.search(text)
    return (text[: m.start()], text[m.start() :]) if m else (text, "")


def one_line(value: str, what: str) -> str:
    """`value`, refused if it holds any line separator (`LINE_SEPARATORS`)."""
    if any(ch in LINE_SEPARATORS for ch in value):
        raise AgentError(f"{what} must be one line; put detail in the plan body or the log")
    return value


def with_note(text: str, note: str | None) -> str:
    """Insert the note before any structured `{writes=… after=…} ^tN` suffix so task parsing remains
    stable.
    """
    if not note:
        return text
    one_line(note, "a task note")
    prose, suffix = split_suffix(text)
    return f"{prose} ({note}){suffix}"


def _join(lines: list[str], body: str) -> str:
    return "\n".join(lines) + ("\n" if body.endswith(("\n", "\r")) else "")


def _write_task(body: str, task: Task, marker: str, text: str) -> str:
    """Rewrite `task`'s line with a new box and text, keeping its indent and list marker."""
    lines = task_md.split_lines(body)
    m = task_md.ITEM_LINE_RE.match(lines[task.line_no])
    if m is None:  # `list_tasks` found it with the same pattern; the body changed under us
        raise AgentError(f"task #{task.index} is no longer a checkbox line")
    lines[task.line_no] = f"{m.group(1)}{marker}] {text}"
    return _join(lines, body)


def set_task(body: str, task: Task, done: bool, note: str | None = None) -> str:
    return _write_task(body, task, "x" if done else " ", with_note(task.text, note))


def supersede_task(body: str, task: Task, reason: str, link: str) -> str:
    """Mark `task` superseded (`- [-]`) with why and where the work went. The only writer of
    `[-]`: a task moved to another feature is superseded with a link to it."""
    reason, link = one_line(reason, "the reason").strip(), one_line(link, "the link").strip()
    if not reason or not link:
        raise AgentError("superseding a task needs a reason and a link (a commit, task, feature or URL)")
    return _write_task(body, task, SUPERSEDED_MARKER, with_note(task.text, f"superseded: {reason}; {link}"))


def add_task_at(body: str, text: str) -> tuple[str, int]:
    """Add a task so that it is a counted task, the last one, and no existing number shifts;
    return the new body and the new task's number as a re-parse of that body reads it.

    Candidate placements, in order, each kept only if `list_tasks` on the result reads the old
    tasks unchanged followed by this one:
    - right after the last counted task's list item (markdown-it's `map`: its sub-steps and
      notes included), with that task's indent and bullet; then the same after a blank line,
      which ends an HTML block or a lazy paragraph;
    - with no task yet, at the end of the first Tasks section, replacing the template's blank
      placeholder and hint; then the same after a blank line;
    - last, a new `## Tasks` heading with the task at the end of the plan: the one placement no
      nested heading or open block can swallow.
    Nothing is written when none counts."""
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
        while at > last.line_no + 1 and not lines[at - 1].strip():
            at -= 1
        m = task_md.ITEM_LINE_RE.match(lines[last.line_no])
        prefix = m.group(1) if m else "- ["
        if not re.match(r"\s*[-*+]", prefix):  # an ordered item: continue as a bullet
            prefix = prefix[: len(prefix) - len(prefix.lstrip())] + "- ["
        # The template's graph hint goes once real tasks exist, as it always has.
        candidates.append(templates.drop_tasks_hint([*lines[:at], f"{prefix} ] {text}", *lines[at:]]))
        candidates.append(templates.drop_tasks_hint([*lines[:at], "", f"- [ ] {text}", *lines[at:]]))
    else:
        start = task_md.tasks_heading_line(body)
        end = task_md.tasks_section_end(body)
        if start is None or end is None:
            raise AgentError("plan.md has no '## Tasks' section")
        section = lines[start + 1 : end]
        # drop the empty placeholder and graph hint the template ships with
        section = templates.drop_tasks_hint([row for row in section if row.strip() not in ("- [ ]", "- [x]")])
        while section and not section[-1].strip():
            section.pop()
        for gap in ([], [""]):
            candidates.append([*lines[: start + 1], *section, *gap, f"- [ ] {text}", "", *lines[end:]])
    tail = list(lines)
    while tail and not tail[-1].strip():
        tail.pop()
    candidates.append([*tail, "", "## Tasks", "", f"- [ ] {text}"])
    for candidate in candidates:
        new_body = "\n".join(candidate).rstrip("\n") + "\n"
        after = list_tasks(new_body)
        if len(after) == len(before) + 1 and [(t.state, t.text) for t in after[:-1]] == before:
            added = after[-1]
            if added.text == text and added.is_open:
                return new_body, added.index
    raise AgentError("could not place the task where it counts; add it to the plan's ## Tasks by hand")


def normalize_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    if not slug or not SLUG_RE.match(slug):
        raise AgentError(f"cannot make a slug from {text!r}")
    return slug


def feature_slug(text: str) -> str:
    """A feature slug additionally must not look like a feature reference."""
    slug = normalize_slug(text)
    if slug.isdigit() or slug in RESERVED_SLUGS:
        raise AgentError(f"slug {slug!r} would collide with a feature reference; pick a descriptive slug")
    return slug


def features_dir(root: Path) -> Path:
    """The standalone features folder, where `new` creates a feature. Discovery uses
    `feature_dirs`, which also walks initiatives."""
    return root / FEATURES_DIR


def _dir_match(name: str) -> re.Match[str]:
    """`DIR_RE.match` for a name already known to match (filtered by `feature_dirs` or checked
    just above). Narrows the Optional for the type checker. A name that fails to match here
    would mean `feature_dirs`'s own filter is broken, not something these callers need to
    handle."""
    m = DIR_RE.match(name)
    if m is None:
        raise AgentError(f"{name} is not a feature directory (expected NNN-slug)")
    return m


def feature_dirs(root: Path) -> list[Path]:
    """Every feature dir in the vault, standalone and nested, sorted by id. Raises AgentError
    on a duplicate id or a containment cycle (`feature_tree.walk`)."""
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
    """The next feature id this vault has not used. `new` claims its id on the notes remote
    instead (`notes_claims.claim`), which also counts the other devices'."""
    dirs = feature_dirs(root)
    return (max(int(_dir_match(p.name).group(1)) for p in dirs) + 1) if dirs else 1


def resolve(root: Path, ref: str, session_id: str | None = None) -> Feature:
    """Accept 1, 001, 001-slug, slug, or 'current' (via the session mapping)."""
    ref = ref.strip()
    if ref == "current":
        from . import sessions

        try:
            sid = sessions.resolve_session_id(root, session_id)
        except AgentError as exc:
            raise AgentError(
                "no session id for `current`: name a feature explicitly, or pass --session-id for a session mapped "
                "with `agent feature use <n>`."
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
    *,
    request: dict[str, Any] | None = None,
) -> Feature:
    """`request` makes a request record (`requests.new`): its frontmatter fields and `ask`, which
    the compact template puts in Context. The id comes from the same claim either way."""
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
    return write_folder(root, fid, slug, title, status, tickets, repos, tags, opened, request=request)


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
    *,
    request: dict[str, Any] | None = None,
) -> Feature:
    number = f"{fid:03d}"
    fdir = features_dir(root) / f"{number}-{slug}"
    meta: dict[str, Any] = {
        "id": fid,
        "slug": slug,
        "title": title.strip(),
        "status": status,
        "opened": (opened or today()),
        "closed": None,
        "tickets": validate_tickets(tickets or []),
        "repos": repos,
        "adrs": [],
        "decisions": [],
        "tags": with_type_tag(tags, REQUEST_TAG if request is not None else TYPE_TAG),
        "links": [],
    }
    if request is None:
        body = templates.plan_body(number, title.strip())
    else:
        meta = {KIND_FIELD: REQUEST_KIND, **meta, "category": request["category"]}
        body = templates.request_body(number, title.strip(), request["category"], request["ask"])
    fdir.mkdir(parents=True, exist_ok=False)
    frontmatter.dump(fdir / "plan.md", meta, body)
    (fdir / "log.md").write_text(templates.log_header(number, slug), encoding="utf-8")
    return load_dir(fdir)


def with_type_tag(tags: list[str] | None, type_tag: str = TYPE_TAG) -> list[str]:
    """The artifact-type tag always comes first. User tags follow, deduplicated."""
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
    """Map repo names/shorts to canonical registry names. Error on unknowns."""
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
    """Refuse terminal status while counted tasks remain open. Both `close` and `set_fields` check
    before writing, so every CLI and MCP route enforces the same rule.
    """
    still_open = [t.index for t in list_tasks(feature.body) if t.is_open]
    if still_open:
        raise AgentError(
            f"{feature.label.lower()} has {len(still_open)} open task(s): "
            + ", ".join(f"#{n}" for n in still_open)
            + ". Before closing, tick each one done (task_done) or supersede it with a reason and a link "
            "(task_supersede; a task moved to another feature links to it there)."
        )


def refuse_terminal(root: Path, feature: Feature, status: str) -> None:
    """Every route to a terminal status checks here before it writes. A feature needs its tasks
    settled and room in the lesson queue; a request needs its tasks settled and no unfinished
    dispatch work, and `done` needs its delivered result (`requests.refuse_unfinished`)."""
    refuse_open_tasks(feature)
    if feature.is_request:
        from . import requests

        requests.refuse_unfinished(root, feature, status)
    else:
        lessons.refuse_full_queue(root)


def close(root: Path, feature: Feature, status: str = "done", closed: date | None = None) -> Feature:
    if status not in TERMINAL_STATUSES:
        raise AgentError(f"close status must be one of {', '.join(TERMINAL_STATUSES)}")
    if feature.status in TERMINAL_STATUSES:
        raise AgentError(f"{feature.label.lower()} is already {feature.status}")
    refuse_terminal(root, feature, status)
    feature.meta["status"] = status
    feature.meta["closed"] = closed or today()
    feature.save()
    if not feature.is_request:
        # A request's close is journal evidence through its record, not a pending milestone.
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
    waits_on: str | None = None,
    resume_when: str | None = None,
) -> Feature:
    meta = feature.meta
    old_status = feature.status
    if status == "blocked":
        reason = one_line(reason or "", "reason").strip()
        resume_when = one_line(resume_when or "", "resume_when").strip()
        if not reason or len(reason) > 120:
            raise AgentError("blocked needs a non-empty reason of at most 120 characters")
        if not resume_when:
            raise AgentError("blocked needs resume_when")
        if waits_on not in ("owner", "external", "manager"):
            if waits_on and re.fullmatch(r"feature:\d{3}", waits_on):
                dependency = resolve(root, waits_on.partition(":")[2])
                if not dependency.is_open:
                    raise AgentError("waits_on must name an open feature")
            elif not waits_on or not re.fullmatch(r"dispatch:[^\s]+", waits_on):
                raise AgentError("waits_on must be owner, external, manager, feature:NNN or dispatch:<id>")
    if status == "active" and old_status in ("proposed", "blocked") and not (reason or "").strip():
        raise AgentError("activating a proposed or blocked feature needs a reason")
    if status is not None:
        if status not in STATUSES:
            raise AgentError(f"status must be one of {', '.join(STATUSES)}")
        # Whatever the current status: done -> abandoned with a task reopened after the close
        # went through too.
        if status in TERMINAL_STATUSES:
            refuse_terminal(root, feature, status)
        meta["status"] = status
        if status in TERMINAL_STATUSES and not meta.get("closed"):
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
    _edit_list(meta, "tags", add_tag, [t for t in (remove_tag or []) if t != feature.type_tag])
    meta["tags"] = with_type_tag(meta.get("tags"), feature.type_tag)
    feature.save()
    if status is not None and (status != old_status or status == "blocked"):
        # Record each status change with its reason so later work can detect a stale blocked
        # status.
        from . import log as log_mod
        from .blocked import status_entry

        kind = "blocker" if status == "blocked" else "note"
        text = status_entry(old_status, status, reason)
        if status == "blocked":
            text += f"\nnext: {waits_on}\nresume: {resume_when}"
        log_mod.append(feature, kind, text)
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
