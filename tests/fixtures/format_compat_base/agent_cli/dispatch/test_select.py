# ruff: noqa
from __future__ import annotations
import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from ..core import AgentError
from ..records.dispatch import scopes_of
from ..records.narrow import as_dict_or_none, as_list, as_list_or_none, dict_list, str_list
from ..repos import VerificationCommand, VerificationConfig

TABLE = "tests/selection.toml"
EXTRA_TIMEOUT = 300.0
POLICY = 2
STRUCTURAL = ("tests/test_select.py",)


@dataclass(frozen=True)
class Selection:
    scope: str
    reason: str
    paths: tuple[str, ...]
    tests: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    policy: int = 1
    unmapped_paths: tuple[str, ...] | None = None

    def _fields(self) -> dict[str, Any]:
        fields = asdict(self)
        del fields["unmapped_paths"]
        if fields["policy"] == 1:
            del fields["policy"]
        return fields

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self._fields(), sort_keys=True).encode()).hexdigest()

    def record(self, attempt: int) -> dict[str, Any]:
        row = {"attempt": attempt, **self._fields(), "policy": self.policy, "digest": self.digest()}
        if self.unmapped_paths is not None:
            row["unmapped_paths"] = list(self.unmapped_paths)
        return row


def refused(reason: str, paths: tuple[str, ...] = ()) -> Selection:
    return Selection("refused", reason, tuple(sorted(set(paths))), policy=POLICY)


def matches(path: str, patterns: Iterable[str]) -> bool:
    pure = PurePosixPath(path)
    return any((pure.full_match(p) for p in patterns))


def version_of(table: dict[str, Any]) -> int:
    value = table.get("version")
    return value if isinstance(value, int) else 1


def _table_at(repo: Path, rev: str) -> bytes | None:
    try:
        shown = subprocess.run(
            ["git", "show", f"{rev}:{TABLE}"], cwd=repo, capture_output=True, timeout=30, check=False
        )
    except OSError, subprocess.SubprocessError:
        return None
    return shown.stdout if shown.returncode == 0 else None


def launch_policy(repo: Path, base: str | None) -> int:
    at_base = _table_at(repo, base) if base else None
    if at_base is None and _table_at(repo, "HEAD") is None:
        return 0
    if at_base is None:
        raise AgentError(
            f"base {str(base)[:20]} has no readable {TABLE} while the repo keeps one; base the dispatch on local main"
        )
    try:
        table = tomllib.loads(at_base.decode())
    except ValueError, UnicodeDecodeError:
        raise AgentError(
            f"the {TABLE} at base {str(base)[:12]} is unreadable; base the dispatch on local main"
        ) from None
    if version_of(table) != POLICY:
        raise AgentError(
            f"base {str(base)[:12]} predates selection v2 (its {TABLE} is version {version_of(table)}); base the dispatch on local main"
        )
    why = malformed(table)
    if why is not None:
        raise AgentError(f"the {TABLE} at base {str(base)[:12]} is malformed ({why}); fix it on main first")
    return POLICY


def stamp(repo: Path, base: str | None, escalated: dict[str, Any] | None) -> int:
    return policy_of(escalated) if escalated is not None else launch_policy(repo, base)


def policy_of(d: dict[str, Any]) -> int:
    value = d.get("selection_policy")
    return value if isinstance(value, int) and value in (0, 1, 2) else 1


_KINDS = {"broad": ("reason", "paths", "tests"), "bundle": ("paths", "tests"), "fallback": ("tests",)}


def _strings(value: object) -> bool:
    items = as_list_or_none(value)
    return items is not None and all((isinstance(v, str) for v in items))


def malformed(table: dict[str, Any]) -> str | None:
    if version_of(table) != POLICY:
        return f"version {table.get('version')!r} is not {POLICY}"
    for key, rules in table.items():
        if key == "version":
            continue
        items = as_list_or_none(rules)
        if key not in _KINDS or items is None:
            return f"unknown or malformed section {key!r}"
        for item in items:
            rule = as_dict_or_none(item)
            if rule is None:
                return f"a {key} rule is not a table"
            for field in _KINDS[key]:
                value = rule.get(field)
                ok = isinstance(value, str) and bool(value) if field == "reason" else _strings(value)
                if not ok:
                    return f"a {key} rule's {field} is missing or malformed"
            if "extra" in rule and (not _strings(rule["extra"])):
                return f"a {key} rule's extra is malformed"
    if not table.get("fallback"):
        return "no fallback rule"
    return None


def extends(base: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if malformed(candidate) is not None:
        return False
    for kind, keys in (("broad", ("paths", "tests", "extra")), ("bundle", ("paths", "tests", "extra"))):
        for rule in dict_list(base.get(kind)):
            if not any(
                (
                    (kind != "broad" or c.get("reason") == rule.get("reason"))
                    and all((set(str_list(rule.get(k))) <= set(str_list(c.get(k))) for k in keys))
                    for c in dict_list(candidate.get(kind))
                )
            ):
                return False
    base_fallback = {t for r in dict_list(base.get("fallback")) for t in str_list(r.get("tests"))}
    cand_fallback = {t for r in dict_list(candidate.get("fallback")) for t in str_list(r.get("tests"))}
    return base_fallback <= cand_fallback


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "diff.external=",
            *args,
        ],
        cwd=worktree,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
        capture_output=True,
        timeout=30,
        check=False,
    )


@dataclass(frozen=True)
class Observed:
    table: dict[str, Any]
    changed: list[tuple[str, str]]
    head_tests: list[str]
    candidate: dict[str, Any] | None
    mentions: Callable[[str], list[str]]
    importers: Callable[[str], list[str]]


def observe(worktree: Path, base: str, head: str) -> Observed | str:
    try:
        shown = _git(worktree, "show", f"{base}:{TABLE}")
        if shown.returncode:
            return "no-table"
        table = tomllib.loads(shown.stdout.decode())
        diff = _git(
            worktree, "diff", "--name-status", "-z", "--no-renames", "--no-ext-diff", f"{base}...{head}"
        )
        listing = _git(worktree, "ls-tree", "-r", "-z", "--name-only", head, "--", "tests")
    except OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError:
        return "diff-failed"
    if diff.returncode or listing.returncode:
        return "diff-failed"
    fields = diff.stdout.decode(errors="surrogateescape").split("\x00")
    changed = [(fields[i][:1], fields[i + 1]) for i in range(0, len(fields) - 1, 2) if fields[i]]
    head_tests = [p for p in listing.stdout.decode(errors="surrogateescape").split("\x00") if p]
    candidate: dict[str, Any] | None = None
    if any((path == TABLE for _, path in changed)):
        try:
            candidate = tomllib.loads(_git(worktree, "show", f"{head}:{TABLE}").stdout.decode())
        except ValueError, UnicodeDecodeError, OSError, subprocess.SubprocessError:
            return "table-unreadable"
    mentions, importers = _lookups(worktree, head)
    return Observed(table, changed, head_tests, candidate, mentions, importers)


def _lookups(worktree: Path, head: str) -> tuple[Callable[[str], list[str]], Callable[[str], list[str]]]:

    def mentions(path: str) -> list[str]:
        name = PurePosixPath(path).name
        found = _git(worktree, "grep", "-l", "-F", "-e", path, "-e", name, head, "--", "tests/test_*.py")
        rows = found.stdout.decode(errors="surrogateescape").splitlines() if found.returncode == 0 else []
        return [r.split(":", 1)[1] for r in rows if ":" in r]

    def importers(path: str) -> list[str]:
        dotted = PurePosixPath(path).with_suffix("").as_posix().removeprefix("src/").replace("/", ".")
        dotted = dotted.removeprefix("tests.")
        package, _, name = dotted.rpartition(".")
        found = _git(
            worktree,
            "grep",
            "-l",
            "-E",
            "-e",
            f"{re.escape(dotted)}([^A-Za-z0-9_]|$)",
            "-e",
            f"from {re.escape(package)} import (.*[^A-Za-z0-9_])?{re.escape(name)}([^A-Za-z0-9_]|$)",
            head,
            "--",
            "tests/test_*.py",
        )
        rows = found.stdout.decode(errors="surrogateescape").splitlines() if found.returncode == 0 else []
        direct = [r.split(":", 1)[1] for r in rows if ":" in r]
        return direct + [t for t in _parenthesized(package) if t not in direct and _imports(t, package, name)]

    def _parenthesized(package: str) -> list[str]:
        found = _git(
            worktree, "grep", "-l", "-F", "-e", f"from {package} import (", head, "--", "tests/test_*.py"
        )
        rows = found.stdout.decode(errors="surrogateescape").splitlines() if found.returncode == 0 else []
        return [r.split(":", 1)[1] for r in rows if ":" in r]

    def _imports(test: str, package: str, name: str) -> bool:
        shown = _git(worktree, "show", f"{head}:{test}")
        try:
            tree = ast.parse(shown.stdout.decode())
        except SyntaxError, ValueError, UnicodeDecodeError:
            return True
        return any(
            (
                isinstance(node, ast.ImportFrom)
                and node.module == package
                and any((alias.name == name for alias in node.names))
                for node in ast.walk(tree)
            )
        )

    return (mentions, importers)


def _tests(rules: list[dict[str, Any]], available: set[str]) -> set[str]:
    return {t for rule in rules for t in available if matches(t, str_list(rule.get("tests")))}


def choose(seen: Observed, table: dict[str, Any], extension: dict[str, Any] | None = None) -> Selection:
    available = {
        t for t in seen.head_tests if t.endswith(".py") and PurePosixPath(t).name.startswith("test_")
    }
    fallback = _tests(dict_list(table.get("fallback")), available)
    selected: set[str] = set()
    extra: list[str] = []
    reasons: set[str] = set()
    unmapped: set[str] = set()
    if not seen.changed:
        selected |= fallback
        reasons.add("empty-diff")
    for status, path in seen.changed:
        if path == TABLE:
            selected |= set(STRUCTURAL) & available
            continue
        rules = extension if extension is not None and status == "A" else table
        broad = [r for r in dict_list(rules.get("broad")) if matches(path, str_list(r.get("paths")))]
        matched = broad + [
            r for r in dict_list(rules.get("bundle")) if matches(path, str_list(r.get("paths")))
        ]
        selected |= _tests(matched, available)
        extra += [str(e) for r in matched for e in as_list(r.get("extra"))]
        hit = bool(matched)
        if PurePosixPath(path).full_match("tests/test_*.py"):
            hit = True
            if status != "D" and path in available:
                selected.add(path)
        elif PurePosixPath(path).full_match("tests/*.py") and path != "tests/conftest.py":
            selected |= set(seen.importers(path)) & available
        if path.startswith("src/") and path.endswith(".py"):
            name, parent = (PurePosixPath(path).stem, PurePosixPath(path).parent.name)
            selected |= {
                t
                for t in available
                if matches(t, [f"tests/test_{name}*.py", f"tests/test_{parent}_{name}*.py"])
            }
            if not broad:
                selected |= set(seen.importers(path)) & available
        elif hit and (not path.endswith(".py")):
            selected |= set(seen.mentions(path)) & available
        if not hit or status == "D":
            selected |= fallback
            reasons.add("deleted" if status == "D" else "unmapped")
            if not hit and status != "D":
                unmapped.add(path)
    if not selected:
        selected = set(fallback)
        reasons.add("nothing-selected")
    paths = tuple(sorted((p for _, p in seen.changed)))
    if not selected:
        return refused("nothing to select and the fallback bundle is empty", paths)
    if available and selected >= available:
        return refused("the selection is the whole suite; the landing certificate runs it", paths)
    reason = "+".join(sorted(reasons)) if reasons else "selected"
    return Selection(
        "scoped",
        reason,
        paths,
        tuple(sorted(selected)),
        tuple(dict.fromkeys(extra)),
        POLICY,
        tuple(sorted(unmapped)),
    )


def select(worktree: Path, base: str | None, head: str, policy: int = POLICY) -> Selection:
    if policy != POLICY:
        return Selection("full", "no-table" if policy == 0 else "pre-v2 dispatch", ())
    if not base:
        return refused("no base commit")
    seen = observe(worktree, base, head)
    if isinstance(seen, str):
        return refused(f"selection could not be read ({seen})")
    if version_of(seen.table) != POLICY:
        return refused("base predates selection v2")
    why = malformed(seen.table)
    if why is not None:
        return refused(f"the base selection table is malformed: {why}")
    extension = seen.candidate if seen.candidate is not None and extends(seen.table, seen.candidate) else None
    return choose(seen, seen.table, extension)


def selector_for(d: dict[str, Any]) -> Any:
    from types import SimpleNamespace

    policy = policy_of(d)

    def bound(worktree: Path, base: str | None, head: str) -> Selection:
        return select(worktree, base, head, policy)

    return SimpleNamespace(select=bound)


def select_for(worktree: Path, d: dict[str, Any], head: str) -> Selection:
    return select(worktree, d.get("start_sha"), head, policy_of(d))


def is_pytest(command: str) -> bool:
    try:
        return "pytest" in [PurePosixPath(w).name for w in shlex.split(command)]
    except ValueError:
        return False


def with_extra(config: VerificationConfig, extra: Iterable[str]) -> VerificationConfig:
    added = tuple((VerificationCommand(e, EXTRA_TIMEOUT) for e in extra))
    if not added:
        return config
    return VerificationConfig(config.commands + added, config.timeout_seconds + EXTRA_TIMEOUT * len(added))


def config_for(config: VerificationConfig, selection: Selection) -> VerificationConfig:
    if selection.scope == "scoped" and (not selection.tests):
        raise ValueError("a scoped selection names no tests; a bare pytest would run the whole suite")
    if selection.scope == "refused":
        raise ValueError(f"a refused selection runs nothing: {selection.reason}")
    commands = [
        VerificationCommand(
            c.command + " " + " ".join((shlex.quote(t) for t in selection.tests)), c.timeout_seconds
        )
        if selection.scope == "scoped" and is_pytest(c.command)
        else c
        for c in config.commands
    ]
    return with_extra(VerificationConfig(tuple(commands), config.timeout_seconds), selection.extra)


def _stored(d: dict[str, Any]) -> dict[str, object] | None:
    found = [r for r in scopes_of(d) if r.get("attempt") == d.get("attempt")]
    return found[-1] if found else None


def publish(d: dict[str, Any], selection: Selection, *, head_sha: str, observed_at: str | None) -> None:
    previous = _stored(d)
    rows = [r for r in scopes_of(d) if r.get("attempt") != d["attempt"]]
    row = selection.record(d["attempt"])
    if previous and previous.get("digest") == row["digest"]:
        row = previous
    if row is not previous:
        row.update(base_sha=d.get("start_sha"), head_sha=head_sha)
        if observed_at is not None:
            row["observed_at"] = observed_at
    d["verification_scopes"] = [*rows, row]


def accepted(
    worktree: Path, d: dict[str, Any], final_sha: str, config: VerificationConfig
) -> VerificationConfig:
    stored = _stored(d)
    if stored is None:
        return config
    if policy_of(d) != POLICY:
        if stored.get("scope") == "full":
            return config
        raise ValueError("a policy-1 scoped selection cannot be re-derived; reverify the attempt")
    again = select(worktree, d.get("start_sha"), final_sha, policy_of(d))
    if again.digest() != stored.get("digest"):
        raise ValueError("the scoped test selection no longer matches the attempt's diff")
    return config_for(config, again)


def label(d: dict[str, Any]) -> str | None:
    stored = _stored(d)
    if stored is None or stored.get("scope") != "scoped":
        return None
    return f"scoped check: {len(as_list(stored.get('tests')))} test files for {len(as_list(stored.get('paths')))} changed paths; the full suite runs at landing"


def rates(records: Iterable[dict[str, Any]]) -> str:
    rows = [r for d in records for r in scopes_of(d)]
    if not rows:
        return "attempt checks: no scoped-selection data yet"
    full = [r for r in rows if r.get("scope") == "full"]
    refused_rows = [r for r in rows if r.get("scope") == "refused"]
    fallback = [r for r in rows if r.get("scope") == "scoped" and "unmapped" in str(r.get("reason"))]
    by_reason: dict[str, int] = {}
    for r in full:
        by_reason[str(r.get("reason"))] = by_reason.get(str(r.get("reason")), 0) + 1
    unknown = sorted({p for r in full if r.get("reason") == "unknown-path" for p in str_list(r.get("paths"))})
    scoped = len(rows) - len(full) - len(refused_rows)
    text = f"attempt checks: {len(rows)} attempts, {scoped} scoped, {len(full)} full ({len(full) / len(rows):.0%})"
    if refused_rows:
        text += f", {len(refused_rows)} refused"
    if fallback:
        text += f"; {len(fallback)} scoped ran the fallback for unmapped paths"
    if by_reason:
        text += "; full by reason: " + ", ".join((f"{k} {v}" for k, v in sorted(by_reason.items())))
    if unknown:
        text += "; unmapped paths: " + ", ".join(unknown[:10])
    return text
