# ruff: noqa
from __future__ import annotations
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from . import links
from .codex_profile import CODEX_CONFIG_FILE as CODEX_CONFIG_FILE
from .codex_profile import configure_codex_notes_root as configure_codex_notes_root
from .core import AgentError, agent_dir_name, agent_path, atomic_write
from .records.narrow import as_dict

Row = dict[str, Any]
DEFAULT_NOTES_DIRNAME = "Agent-notes"


def default_notes_root(root: Path) -> Path:
    return root.parent / DEFAULT_NOTES_DIRNAME


@dataclass(frozen=True)
class Entry:
    path: str
    kind: str
    example: str | None
    generated: bool


PRIVATE_SET: tuple[Entry, ...] = (
    Entry("features", "dir", None, False),
    Entry("decisions", "dir", None, False),
    Entry("journal", "dir", None, False),
    Entry("usage", "dir", None, False),
    Entry("notes", "dir", None, False),
    Entry("harness/owner", "dir", "harness/owner.example", False),
    Entry("harness/voice.md", "file", "harness/voice.example.md", False),
    Entry("agent.yaml", "file", "agent.example.yaml", False),
    Entry(".mcp.json", "file", ".mcp.example.json", False),
    Entry("bases", "dir", None, True),
)


def entries(notes_root: Path | None) -> tuple[Entry, ...]:
    return PRIVATE_SET


LOCAL_GENERATED_FILES: tuple[str, ...] = ("STATUS.md", "Dashboard.md", "Index.md")


def unlink_local_generated(root: Path) -> list[Row]:
    rows: list[Row] = []
    for rel in LOCAL_GENERATED_FILES:
        path = root / rel
        if not path.is_symlink():
            continue
        target = _symlink_target(path)
        path.unlink()
        rows.append(
            {"path": rel, "state": "done", "detail": f"unlinked from {target}; render writes it here"}
        )
    return rows


NOTES_ROOT_FILE = agent_path(Path(), "notes-root")
README_MARKER = "<!-- agent notes repo (ADR-0012) -->"
README_TEMPLATE = f"{README_MARKER}\n# Notes\n\nThe private half of an agent harness. The harness itself — the CLI, hooks, skills, rules,\nharness ADRs and the Obsidian config — is a separate public repo. This repo holds the work:\nfeatures, work decisions, the journal, personal notes, the repo registry and the MCP config.\n\nLink it into a harness clone:\n\n```\ncd <the harness clone>\n./setup.sh --notes {{path}}\n```\n\nSetup symlinks every path below into the harness clone, so Obsidian and Claude Code see one\nvault while git sees two repos. The harness gitignores all of them.\n\n{{paths}}\n\n`bases/` is generated: `agent status render` writes it here through the link. `STATUS.md`,\n`Dashboard.md` and `Index.md` are generated too, but as plain files in the harness clone, never\nhere: Obsidian does not watch a symlinked file at the vault root.\n"
GITIGNORE = f"{agent_dir_name()}/\n.venv/\n.obsidian/\n__pycache__/\nbases/*.base\ndecisions/README.md\n"
GITATTRIBUTES = "features/*/log.md merge=union\n"


def _row(entry: Entry, state: str, detail: str = "") -> Row:
    return {"path": entry.path, "state": state, "detail": detail}


def harness_root() -> Path:
    return Path(__file__).resolve().parents[2]


def read_notes_root(root: Path) -> Path | None:
    try:
        text = (root / NOTES_ROOT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else root / path


def write_notes_root(root: Path, notes_root: Path | str) -> None:
    atomic_write(root / NOTES_ROOT_FILE, f"{notes_root}\n")


def linked_copies(path: Path) -> list[Path]:
    path = Path(os.path.abspath(path))
    for root in path.parents:
        notes_root = read_notes_root(root)
        if notes_root is None:
            continue
        rel = path.relative_to(root)
        seen = {os.path.normcase(path)}
        found: list[Path] = []
        for clone in (notes_root, *_sibling_clones(root)):
            copy = Path(os.path.normpath(clone / rel))
            if os.path.normcase(copy) in seen:
                continue
            seen.add(os.path.normcase(copy))
            if links.is_hard_linked(path, copy):
                found.append(copy)
        if found:
            return found
    return []


def _sibling_clones(root: Path) -> list[Path]:
    from .core import ExternalFailure
    from .git_discovery import common_dir

    try:
        common = common_dir(root)
        if common is None:
            out = _git_out(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
            common = Path(out.strip())
    except ExternalFailure, OSError, subprocess.SubprocessError:
        return []
    clones = [common.parent]
    try:
        admin = sorted((common / "worktrees").iterdir())
    except OSError:
        return clones
    for entry in admin:
        try:
            dotgit = Path((entry / "gitdir").read_text(encoding="utf-8").strip())
        except OSError:
            continue
        if not dotgit.is_absolute():
            dotgit = entry / dotgit
        clones.append(Path(os.path.normpath(dotgit)).parent)
    return clones


def _make(entry: Entry, dest: Path, examples_root: Path) -> tuple[str, str]:
    if entry.generated and entry.kind == "file":
        return ("skipped", "generated; `agent status render` writes it")
    example = examples_root / entry.example if entry.example else None
    if example is not None and (not example.exists()):
        return ("skipped", f"no example at {entry.example}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if entry.kind == "dir":
        if example is not None:
            shutil.copytree(example, dest)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".gitkeep").touch()
    elif example is not None:
        shutil.copy2(example, dest)
    else:
        dest.touch()
    return ("done", f"created from {entry.example}" if example is not None else "created")


def _symlink_target(path: Path) -> Path:
    return links.link_target(path)


def _resolve_notes(root: Path, notes_root: Path | str) -> Path:
    notes = Path(notes_root).expanduser()
    if not notes.is_absolute():
        notes = root / notes
    return Path(os.path.normpath(notes))


def link(root: Path, notes_root: Path | str) -> list[Row]:
    notes = _resolve_notes(root, notes_root)
    if not notes.is_dir():
        raise AgentError(f"notes repo {notes} is not a directory; `agent notes init {notes}` creates one")
    record = linked_record(root)
    rows: list[Row] = []
    for entry in entries(notes):
        path, target = (root / entry.path, Path(os.path.normpath(notes / entry.path)))
        was_linked = record.get(entry.path) == str(target)
        row = _link_one(entry, path, target, root, was_linked, notes)
        if entry.kind == "file" and row["state"] in ("done", "already") and links.points_at(path, target):
            record[entry.path] = str(target)
        rows.append(row)
    write_linked_record(root, record)
    return rows


LINKED_RECORD_FILE = agent_path(Path(), "notes-linked.json")


def linked_record(root: Path) -> dict[str, str]:
    try:
        data = json.loads((root / LINKED_RECORD_FILE).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return {str(k): str(v) for k, v in as_dict(data).items()}


def write_linked_record(root: Path, record: dict[str, str]) -> None:
    if record or (root / LINKED_RECORD_FILE).exists():
        atomic_write(root / LINKED_RECORD_FILE, json.dumps(record, indent=2, sort_keys=True))


COMMITTED_CHECK_S = 5.0


def is_committed_copy(notes_root: Path, rel: str, data: bytes) -> bool:
    try:
        if (notes_root / rel).read_bytes() == data:
            return True
        blob = _git_out(notes_root, ["hash-object", f"--path={rel}", "--stdin"], data).strip()
        log = [
            "log",
            "--all",
            "--reflog",
            "-m",
            "--format=",
            "--raw",
            "--no-abbrev",
            "--no-renames",
            "--",
            rel,
        ]
        history = _git_out(notes_root, log)
    except OSError, subprocess.SubprocessError:
        return False
    return any((blob in line.split()[2:4] for line in history.splitlines() if line.startswith(":")))


def diverged_edit(path: Path, target: Path) -> str:
    return f"{path} no longer shares {target} and holds an edit the notes repo has not committed, so it was not relinked. Both copies are kept: merge the edit into {target}, then make {path} match it (or remove it) and run `agent notes link`"


def _git_out(root: Path, args: list[str], data: bytes | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        input=data,
        capture_output=True,
        check=True,
        timeout=COMMITTED_CHECK_S,
    )
    return proc.stdout.decode("utf-8", "replace")


def _link_one(
    entry: Entry,
    link_path: Path,
    target: Path,
    examples_root: Path,
    was_linked: bool = False,
    notes_root: Path | None = None,
) -> Row:
    target = Path(os.path.normpath(target))
    if links.is_link(link_path):
        points_at = _symlink_target(link_path)
        if points_at == target:
            return _row(entry, "already", str(target))
        return _row(entry, "refused", f"already a link to {points_at}, not {target}")
    if links.is_hard_linked(link_path, target):
        return _row(entry, "already", f"{target} (hard link)")
    if link_path.exists():
        if entry.kind == "file" and link_path.is_file() and target.is_file() and was_linked:
            try:
                data = link_path.read_bytes()
                if notes_root is None or not is_committed_copy(notes_root, entry.path, data):
                    return _row(entry, "refused", diverged_edit(link_path, target))
                parked = links.recover(examples_root, link_path, target, expected=data)
            except (AgentError, OSError) as exc:
                return _row(entry, "refused", f"diverged from {target} and could not relink: {exc}")
            return _row(entry, "done", f"relinked to {target}; your previous copy is at {parked}")
        what = "directory" if link_path.is_dir() else "file"
        return _row(entry, "refused", f"a real {what} is there; move it to {target} yourself, then re-run")
    detail = f"-> {target}"
    if not target.exists():
        state, made = _make(entry, target, examples_root)
        if state == "skipped" and (not entry.generated):
            return _row(entry, "skipped", made)
        detail = f"{detail} ({made})"
    try:
        kind = links.make(link_path, target, is_dir=entry.kind == "dir")
    except AgentError as exc:
        return _row(entry, "refused", exc.message)
    return _row(entry, "done", detail if kind == "symlink" else f"{detail} ({kind})")


def is_skeleton(path: Path) -> bool:
    readme = path / "README.md"
    try:
        return README_MARKER in readme.read_text(encoding="utf-8")
    except OSError:
        return False


def occupied(path: Path) -> bool:
    return any((child.name != ".git" for child in path.iterdir()))


def init(path: Path | str, *, root: Path | None = None) -> list[Row]:
    dest = Path(path).expanduser()
    examples_root = Path(root) if root is not None else harness_root()
    if dest.exists() and (not dest.is_dir()):
        raise AgentError(f"{dest} is a file, not a directory")
    if dest.is_dir() and occupied(dest) and (not is_skeleton(dest)):
        raise AgentError(
            f"{dest} is not empty and is not a notes repo; pick an empty directory, or move your notes into it first and link them with `agent setup --notes <path>`"
        )
    dest.mkdir(parents=True, exist_ok=True)
    rows: list[Row] = []
    for entry in PRIVATE_SET:
        target = dest / entry.path
        if target.exists():
            rows.append(_row(entry, "already", str(target)))
            continue
        state, detail = _make(entry, target, examples_root)
        rows.append(_row(entry, state, detail))
    rows.append(_write_if_absent(dest / "README.md", _readme(dest), "README.md"))
    rows.append(_write_if_absent(dest / ".gitignore", GITIGNORE, ".gitignore"))
    rows.append(_write_if_absent(dest / ".gitattributes", GITATTRIBUTES, ".gitattributes"))
    rows.append(_git_init(dest))
    return rows


def _readme(dest: Path) -> str:
    paths = "\n".join((f"- `{e.path}`" for e in PRIVATE_SET))
    return README_TEMPLATE.format(path=dest, paths=paths)


def ensure_hygiene(dest: Path) -> list[str]:
    changed: list[str] = []
    for name, wanted in ((".gitignore", GITIGNORE), (".gitattributes", GITATTRIBUTES)):
        path = dest / name
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        have = {line.strip() for line in current.splitlines()}
        missing = [line for line in wanted.splitlines() if line.strip() and line.strip() not in have]
        if missing:
            joined = current + ("" if not current or current.endswith("\n") else "\n")
            atomic_write(path, joined + "\n".join(missing) + "\n")
            changed.append(name)
    return changed


def _write_if_absent(target: Path, text: str, name: str) -> Row:
    if target.exists():
        return {"path": name, "state": "already", "detail": str(target)}
    atomic_write(target, text)
    return {"path": name, "state": "done", "detail": str(target)}


def _git_init(dest: Path) -> Row:
    if (dest / ".git").exists():
        return {"path": "git", "state": "already", "detail": "already a git repo"}
    try:
        proc = subprocess.run(["git", "init", "-q", str(dest)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"path": "git", "state": "skipped", "detail": f"git init failed: {exc}"}
    if proc.returncode != 0:
        return {"path": "git", "state": "skipped", "detail": (proc.stderr or "git init failed").strip()}
    return {"path": "git", "state": "done", "detail": "git init (no commit; the first one is yours)"}


def status(root: Path) -> list[Row]:
    notes = read_notes_root(root)
    record = linked_record(root)
    rows: list[Row] = []
    for entry in entries(notes):
        p = root / entry.path
        copy = notes / entry.path if notes is not None else None
        linked_to = record.get(entry.path)
        if links.is_link(p):
            target = _symlink_target(p)
            state = "linked" if target.exists() else "broken"
            rows.append({"path": entry.path, "state": state, "target": str(target)})
        elif copy is not None and links.is_hard_linked(p, copy):
            rows.append({"path": entry.path, "state": "linked", "target": str(copy)})
        elif linked_to and entry.kind == "file" and p.is_file() and Path(linked_to).is_file():
            rows.append({"path": entry.path, "state": "diverged", "target": linked_to})
        elif p.exists():
            rows.append({"path": entry.path, "state": "plain", "target": str(p)})
        else:
            rows.append({"path": entry.path, "state": "missing", "target": ""})
    return rows


def write_file(
    root: Path,
    *,
    target: str,
    source: Path | None = None,
    apply: bool = False,
    expect_sha256: str | None = None,
) -> dict[str, Any]:
    files = {e.path for e in PRIVATE_SET if e.kind == "file"}
    if target not in files:
        raise AgentError(f"target must be one of {', '.join(sorted(files))}")
    path = root / target
    if not path.is_file():
        raise AgentError(f"{target} does not exist here; `agent setup` links it")
    before = path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    result: dict[str, Any] = {
        "target": target,
        "resolved_target": str(path.resolve()),
        "before_sha256": digest,
    }
    if source is None:
        from . import scratch

        drafts = scratch.ensure(root) / "notes-files"
        drafts.mkdir(exist_ok=True)
        draft = drafts / f"{uuid.uuid4().hex[:8]}-{path.name}"
        draft.write_bytes(before)
        return {**result, "draft": str(draft)}
    if apply and (not expect_sha256):
        raise AgentError("apply needs expect_sha256 from a fresh preview")
    replacement = source.read_bytes()
    try:
        text = replacement.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentError(f"{source} is not UTF-8") from exc
    if expect_sha256 is not None and expect_sha256 != digest:
        raise AgentError(f"{target} changed since the preview; preview again")
    diff = "".join(
        difflib.unified_diff(
            before.decode("utf-8", "replace").splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=target,
            tofile=target,
        )
    )
    changed = before != replacement
    if apply and changed:
        atomic_write(path, text, newline="")
    link = next((r["state"] for r in status(root) if r["path"] == target), "missing")
    return {
        **result,
        "applied": apply and changed,
        "changed": changed,
        "after_sha256": hashlib.sha256(replacement).hexdigest(),
        "diff": diff,
        "link": link,
    }
