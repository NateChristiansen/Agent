"""What `agent land` sees of the main checkout, observed without running any configured program.

`git hash-object --path`, `git status` and `git diff` run clean filters, which can write files and
map changed content to the original blob. Land never calls them on the main checkout. Worktree
state comes from Python (`lstat`, reads without following links, `readlink`) and raw blob hashing;
git is asked only plumbing questions that never run a filter (`ls-tree`, `ls-files -s`,
`check-attr`, `cat-file blob`). Every git call blanks the configured filter drivers and skips
hooks and fsmonitor.

Explicit `text eol=crlf` paths with LF-only blobs are supported by Python-side normalisation
when the working tree, old tree and candidate tree agree. No filter runs: the attributes fix
the conversion to replacing CRLF with LF. A lone CR or any blob mismatch reads as `other`.

`core.fileMode=false` and `core.symlinks=false` (Git for Windows writes both) are read the way
git reads them: the executable bit, or a link's kind, is taken from the tree, and only content is
observed. An affected path whose old and candidate entries differ only in what the setting hides
is refused by name, since its working file cannot show which one it holds.

Other configurations whose checked-out bytes are not the blob bytes are refused up front:
a filter, `ident`, `working-tree-encoding` or unsupported `eol=crlf` on an affected path, in the
working tree's attributes or in the old or candidate tree's; `core.autocrlf` on an affected text
path with no `eol` attribute; `core.eol=crlf`, `core.attributesFile` or `core.splitIndex`;
skip-worktree or assume-unchanged index entries; and a submodule pointer change.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .core import AgentError

AT_OLD, AT_CAND, OTHER = "at-old", "at-cand", "other"
ABSENT = ("absent", "")
SHOWN_BY = ("core.filemode", "core.symlinks")
FILTER_KEY = re.compile(r"^filter\..*\.(clean|smudge|process|required)$")
FULL_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# What Git for Windows' installer writes to the system config that land does not read.
WINDOWS_SYSTEM_AUTOCRLF = "true"
ON_WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class Entry:
    mode: str
    oid: str


class Settings:
    """One `git config --list` read, answering `--get` and `--type=bool --get` the way git does
    for the same config: the last value wins, a bare key reads as empty or true."""

    def __init__(self, git: Git, listing: str):
        self.git = git
        self.values: dict[str, str | None] = {}
        self.keys: list[str] = []
        for item in listing.split("\0"):
            if item:
                key, sep, value = item.partition("\n")
                self.keys.append(key)
                self.values[key] = value if sep else None

    def get(self, key: str) -> str:
        return (self.values.get(key) or "").strip()

    def flag(self, key: str) -> str:
        """`git config --type=bool --get key`: "true", "false", or "" when unset."""
        if key not in self.values:
            return ""
        value = self.values[key]
        if value is None or value.lower() in ("true", "yes", "on"):
            return "true"
        if value.lower() in ("", "false", "no", "off"):
            return "false"
        return self.git.run("config", "--type=bool", "--get", key, ok=(0, 1)).strip()

    def shows(self) -> tuple[bool, bool]:
        return self.flag(SHOWN_BY[0]) != "false", self.flag(SHOWN_BY[1]) != "false"


class Git:
    """Hardened plumbing in one checkout: no system config, no hooks, no fsmonitor, no filters.
    On Windows `core.autocrlf` comes from the repo's own config, or else is Git for Windows'
    system default; a global setting never decides it.

    One instance serves one operation. It remembers only what cannot change under it: the tree of
    a full object id and the repository's object format."""

    def __init__(self, checkout: Path):
        self.checkout = checkout
        self.trees: dict[str, dict[str, Entry]] = {}
        self.format = ""
        self.args = [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "diff.external=",
        ]
        self.env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        self.initial = self.settings()
        for key in self.initial.keys:
            if FILTER_KEY.search(key):
                self.args += ["-c", key + ("=false" if key.endswith(".required") else "=")]
        if ON_WINDOWS and not self.repo_sets("core.autocrlf"):
            self.args += ["-c", f"core.autocrlf={WINDOWS_SYSTEM_AUTOCRLF}"]

    def repo_sets(self, key: str) -> bool:
        """Whether the repo's own config (local or worktree) decides `key`, whatever its value."""
        out = self.run("config", "--show-scope", "--get", key, ok=(0, 1))
        return out.split("\t", 1)[0] in ("local", "worktree")

    def run(
        self,
        *args: str,
        ok: tuple[int, ...] = (0,),
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> str:
        proc = subprocess.run(
            [*self.args, *args],
            cwd=self.checkout,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            input=stdin,
            check=False,
        )
        if proc.returncode not in ok:
            raise AgentError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def settings(self) -> Settings:
        """The config as it is now, in one call."""
        return Settings(self, self.run("config", "-z", "--list", ok=(0, 1)))

    def config(self, key: str) -> str:
        return self.settings().get(key)

    def shows(self) -> tuple[bool, bool]:
        """Whether the working tree shows the executable bit and symlinks (`core.fileMode`,
        `core.symlinks`; unset means it does)."""
        return self.settings().shows()

    def blob(self, oid: str) -> bytes:
        """Raw blob bytes, without filters or subprocess text-mode newline conversion."""
        proc = subprocess.run(
            [*self.args, "cat-file", "blob", oid],
            cwd=self.checkout,
            env=self.env,
            capture_output=True,
            check=False,
        )
        if proc.returncode:
            raise AgentError(f"git cat-file blob failed: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout

    def with_index(self, index: Path) -> Git:
        """The same hardened git, reading and writing `index` instead of `.git/index`."""
        other = copy.copy(self)
        other.env = {**self.env, "GIT_INDEX_FILE": str(index)}
        return other


def tree_entries(git: Git, rev: str) -> dict[str, Entry]:
    if rev in git.trees:
        return dict(git.trees[rev])
    out = git.run("ls-tree", "-r", "-z", rev)
    entries: dict[str, Entry] = {}
    for row in out.split("\0"):
        if row:
            meta, path = row.split("\t", 1)
            mode, _kind, oid = meta.split()
            entries[path] = Entry(mode, oid)
    if FULL_OID.fullmatch(rev):
        git.trees[rev] = dict(entries)
    return entries


def index_entries(git: Git, index: Path | None = None) -> dict[str, Entry]:
    """The index, read without a lock; an unmerged entry (stage > 0) refuses."""
    env = {"GIT_INDEX_FILE": str(index)} if index is not None else None
    out = git.run("ls-files", "-s", "-z", env=env)
    entries: dict[str, Entry] = {}
    for row in out.split("\0"):
        if row:
            meta, path = row.split("\t", 1)
            mode, oid, stage = meta.split()
            if stage != "0":
                raise AgentError(f"the index has an unmerged entry at {path}")
            entries[path] = Entry(mode, oid)
    return entries


def changed_paths(git: Git, old: str, cand: str) -> list[str]:
    out = git.run("diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", old, cand)
    return [p for p in out.split("\0") if p]


def unsupported(
    git: Git,
    paths: list[str],
    old: dict[str, Entry],
    cand: dict[str, Entry],
    revs: tuple[str, ...] = (),
    config: Settings | None = None,
) -> tuple[list[str], set[str]]:
    """Refusal reasons and paths safe to normalise from CRLF. Attributes are read from the
    working tree and from each of `revs` (the old main and the candidate), so a candidate that
    brings its own `.gitattributes` is judged by it. `config` is a read the caller just made."""
    why: list[str] = []
    config = config or git.settings()
    exec_bit, links = config.shows()
    for path in paths:
        was, now = old.get(path), cand.get(path)
        if (
            was
            and now
            and was != now
            and as_checked_out(was, exec_bit, links) == as_checked_out(now, exec_bit, links)
        ):
            setting = (
                "core.fileMode=false"
                if {was.mode, now.mode} == {"100644", "100755"}
                else "core.symlinks=false"
            )
            why.append(
                f"{path} changes only from {was.mode} to {now.mode}, which {setting} checks out the same"
            )
    if config.get("core.attributesfile"):
        why.append("core.attributesFile adds attributes land cannot account for")
    if config.get("core.splitindex").lower() == "true":
        why.append("core.splitIndex keeps the index in two files land cannot swap as one")
    if config.get("core.eol").lower() == "crlf":
        why.append("core.eol=crlf rewrites line endings of text files on checkout")
    for path in paths:
        if old.get(path, Entry("", "")).mode == "160000" or cand.get(path, Entry("", "")).mode == "160000":
            why.append(f"{path} is a submodule pointer")
    sources: list[tuple[str, dict[str, dict[str, str]]]] = []
    for source in (None, *revs) if paths else ():
        where = f" as of {source[:12]}" if source else ""
        flag = ["--source", source] if source else []
        attrs = git.run(
            "check-attr", "-z", *flag, "filter", "ident", "working-tree-encoding", "text", "eol", "--", *paths
        )
        fields = attrs.split("\0")
        by_path: dict[str, dict[str, str]] = {}
        for i in range(0, len(fields) - 2, 3):
            path, name, value = fields[i], fields[i + 1], fields[i + 2]
            by_path.setdefault(path, {})[name] = value
        sources.append((where, by_path))
    autocrlf = config.get("core.autocrlf").lower()
    if autocrlf in ("true", "input"):
        for where, attrs in sources:
            for path, values in attrs.items():
                if values["eol"] in ("unspecified", "unset") and values["text"] != "unset":
                    note = f"{path} has no `eol` attribute{where}, so core.autocrlf={autocrlf} rewrites its line endings"
                    if note not in why:
                        why.append(note)
    crlf: set[str] = set()
    for path in paths:
        if len(revs) >= 2 and all(
            attrs[path]["text"] == "set"
            and attrs[path]["eol"] == "crlf"
            and all(
                attrs[path][name] in ("unspecified", "unset")
                for name in ("filter", "ident", "working-tree-encoding")
            )
            for _, attrs in sources
        ):
            if all(b"\r" not in git.blob(tree[path].oid) for tree in (old, cand) if path in tree):
                crlf.add(path)
    for where, attrs in sources:
        for path, values in attrs.items():
            for name, value in values.items():
                if value in ("unspecified", "unset"):
                    continue
                if name in ("filter", "ident", "working-tree-encoding") or (
                    name == "eol" and value == "crlf" and path not in crlf
                ):
                    note = f"{path} has `{name}: {value}`{where}, so its checked-out bytes are not its blob"
                    if note not in why:
                        why.append(note)
    return why, crlf


def hidden_entries(git: Git) -> list[str]:
    """Index entries marked skip-worktree or assume-unchanged: git ignores their working files, so
    a landing could neither write nor observe them the way it promises."""
    rows = git.run("ls-files", "-v", "-z").split("\0")
    return [row[2:] for row in rows if row and (row[0].islower() or row[0] == "S")]


def blob_id(data: bytes, object_format: str) -> str:
    h = hashlib.sha256() if object_format == "sha256" else hashlib.sha1()  # noqa: S324 -- git's object ids
    h.update(b"blob %d\0" % len(data) + data)
    return h.hexdigest()


def observed(checkout: Path, path: str, object_format: str, *, crlf: bool = False) -> Entry | None:
    """The worktree entry, normalised only for validated CRLF text; `ABSENT`; or None for
    unexpected file types or a lone CR in CRLF text."""
    full = checkout / path
    try:
        st = full.lstat()
    except FileNotFoundError:
        return Entry(*ABSENT)
    if stat.S_ISLNK(st.st_mode):
        return Entry("120000", blob_id(os.fsencode(os.readlink(full)), object_format))
    if not stat.S_ISREG(st.st_mode):
        return None
    fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as fh:
        data = fh.read()
    if crlf:
        data = data.replace(b"\r\n", b"\n")
        if b"\r" in data:
            return None
    mode = "100755" if st.st_mode & 0o111 else "100644"
    return Entry(mode, blob_id(data, object_format))


def as_checked_out(entry: Entry, exec_bit: bool, links: bool) -> Entry:
    """`entry` as a working tree that cannot show the executable bit, or symlinks, holds it."""
    if (entry.mode == "100755" and not exec_bit) or (entry.mode == "120000" and not links):
        return Entry("100644", entry.oid)
    return entry


def classify(
    checkout: Path,
    paths: list[str],
    old: dict[str, Entry],
    cand: dict[str, Entry],
    object_format: str,
    *,
    crlf: set[str] | frozenset[str] = frozenset(),
    config: Settings | None = None,
) -> dict[str, str]:
    """Each path as `at-old`, `at-cand` or `other`, with only validated CRLF text normalised and
    the modes the checkout cannot show read from the tree. `config` is a read of the checkout's
    config the caller just made."""
    if not paths:
        exec_bit, links = True, True
    else:
        exec_bit, links = (config or Git(checkout).initial).shows()
    out: dict[str, str] = {}
    for path in paths:
        seen = observed(checkout, path, object_format, crlf=path in crlf)
        if seen is not None and not exec_bit and seen.mode == "100755":
            seen = Entry("100644", seen.oid)
        want_old = as_checked_out(old.get(path, Entry(*ABSENT)), exec_bit, links)
        want_cand = as_checked_out(cand.get(path, Entry(*ABSENT)), exec_bit, links)
        if seen == want_old:
            out[path] = AT_OLD
        elif seen == want_cand:
            out[path] = AT_CAND
        else:
            out[path] = OTHER
    return out
