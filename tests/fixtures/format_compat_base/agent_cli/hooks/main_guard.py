# ruff: noqa
from __future__ import annotations
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from fnmatch import fnmatchcase
from pathlib import Path
from .paths import deny

MAIN = "refs/heads/main"
_MOVERS = {"commit", "merge", "rebase", "reset", "pull", "cherry-pick", "revert", "am"}
_ALWAYS = {"filter-branch", "filter-repo", "replace"}
_RELEVANT = (
    _MOVERS
    | _ALWAYS
    | {"update-ref", "symbolic-ref", "branch", "checkout", "switch", "worktree", "fetch", "push"}
)
REASON = "main moves only through `agent land` (ADR-0041): {what} would move main. Land a branch with the harness tool's `land` action. To bring main up to the tip another machine landed and pushed, use the harness tool's `repos_sync` with harness=true, a checked fast-forward from the pinned `harness.sync_url` (ADR-0041 amendment)."


def _git(at: Path, options: list[str], *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *options, *args],
            cwd=at,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return (1, "")
    return (proc.returncode, proc.stdout.strip())


def _common(at: Path, options: list[str]) -> Path | None:
    code, out = _git(at, options, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(out).resolve() if code == 0 and out else None


def _is_main(name: str, head_is_main: bool) -> bool:
    name = name.lstrip("+")
    return name in ("main", "heads/main", MAIN) or (head_is_main and name in ("HEAD", "@"))


def _positionals(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _expand_alias(at: Path, options: list[str], verb: str, args: list[str]) -> tuple[str, list[str], str]:
    code, value = _git(at, options, "config", "--get", f"alias.{verb}")
    if code or not value:
        return (verb, args, "")
    if value.startswith("!"):
        words = re.findall("[a-z-]+", value)
        hit = next((w for w in words if w in _RELEVANT), "")
        return (verb, args, f"the shell alias `git {verb}` (it runs `{hit}`)" if hit else "")
    try:
        parts = shlex.split(value)
    except ValueError:
        return (verb, args, f"the unreadable alias `git {verb}`")
    return (parts[0], parts[1:] + args, "") if parts else (verb, args, "")


def moves_main(root: Path, at: Path, verb: str, args: list[str], options: list[str]) -> str:
    if any((o.startswith("alias.") for o in options)) or any(
        (options[i] == "-c" and options[i + 1].startswith("alias.") for i in range(len(options) - 1))
    ):
        if _harness(root, at, options):
            return "an inline `-c alias.*`"
        return ""
    if verb not in _RELEVANT:
        verb, args, why = _expand_alias(at, options, verb, args)
        if why:
            return why if _harness(root, at, options) else ""
        if verb not in _RELEVANT:
            return ""
    if not _harness(root, at, options):
        return ""
    from ..harness_role import consumer

    if consumer(root):
        return _consumer(at, options, verb, args)
    head_is_main = _git(at, options, "symbolic-ref", "-q", "HEAD")[1] == MAIN
    pos = _positionals(args)

    def reaches(name: str) -> bool:
        return _reaches_main(at, options, name, head_is_main)

    if verb in _ALWAYS:
        return f"`git {verb}`"
    if verb in _MOVERS:
        if not head_is_main or set(args) & {"--abort", "--quit"}:
            return ""
        if verb == "reset" and (not _reset_moves(at, options, args)):
            return ""
        return f"`git {verb}` with main checked out"
    if verb == "update-ref":
        if "--stdin" in args:
            return "`git update-ref --stdin`"
        refs = _positionals(_without_values(args, "m", ("--message",)))
        if not refs:
            return ""
        if "--no-deref" in args:
            return "`git update-ref` of main" if _is_main(refs[0], False) else ""
        return "`git update-ref` of main" if reaches(refs[0]) else ""
    if verb == "symbolic-ref":
        if _is_main(_symref_written(args), False):
            return "`git symbolic-ref` of main"
        refs = _positionals(_without_values(args, "m", ()))
        aliases = len(refs) == 2 and refs[0] not in ("HEAD", "@") and reaches(refs[1])
        return "`git symbolic-ref` aliasing main" if aliases else ""
    if verb == "branch":
        return "`git branch` moving main" if any((reaches(p) for p in _branch_moves(args, pos))) else ""
    if verb in ("checkout", "switch", "worktree"):
        shorts, longs = {"checkout": ("B", ()), "switch": ("C", ("--force-create",)), "worktree": ("B", ())}[
            verb
        ]
        if any((reaches(v) for v in _option_values(args, shorts, longs))):
            return f"`git {verb}` forcing main"
        if _second_checkout(verb, args, pos, reaches):
            return f"`git {verb}` making a second checkout of main"
        return ""
    if verb in ("fetch", "push"):
        if verb == "push" and (not {".", "./"} & set(pos)):
            return ""
        dsts = [s.split(":", 1)[1] for s in pos if ":" in s]
        return f"`git {verb}` into main" if any((_names_main(d) or reaches(d) for d in dsts)) else ""
    return ""


def _reaches_main(at: Path, options: list[str], name: str, head_is_main: bool) -> bool:
    name = name.lstrip("+")
    if _is_main(name, head_is_main):
        return True
    if not name or "*" in name or name.startswith("-"):
        return False
    full = name if name.startswith("refs/") or name in ("HEAD", "@") else f"refs/heads/{name}"
    for candidate in dict.fromkeys((name, full)):
        code, target = _git(at, options, "symbolic-ref", "-q", "--end-of-options", candidate)
        if code == 0 and target == MAIN:
            return True
    return False


def _second_checkout(verb: str, args: list[str], pos: list[str], reaches: Callable[[str], bool]) -> bool:
    letters, longs = _flags(args)
    if verb == "worktree":
        forced = "f" in letters or "--force" in longs
        return bool(pos) and pos[0] == "add" and forced and any((reaches(p) for p in pos[2:]))
    return "--ignore-other-worktrees" in longs and any((reaches(p) for p in pos))


def _consumer(at: Path, options: list[str], verb: str, args: list[str]) -> str:
    if verb == "pull" and "--ff-only" in args:
        return ""
    if verb == "merge" and "--ff-only" in args and any((a.startswith("origin/") for a in _positionals(args))):
        return ""
    letters, longs = _flags(args)
    if (
        verb in _MOVERS | _ALWAYS | {"update-ref", "push"}
        or (verb == "symbolic-ref" and _symref_written(args))
        or (
            verb in ("branch", "checkout", "switch", "worktree")
            and (letters & set("fBCMm") or longs & {"--force", "--force-create", "--move"})
        )
    ):
        return f"`git {verb}` in a consumer install, which only fast-forwards to its upstream"
    return ""


def _harness(root: Path, at: Path, options: list[str]) -> bool:
    mine, theirs = (_common(root, []), _common(at, options))
    return mine is not None and mine == theirs


def _branch_moves(args: list[str], pos: list[str]) -> list[str]:
    letters, longs = _flags(args)
    if letters & set("dD") or "--delete" in longs:
        return pos
    if letters & set("mM") or "--move" in longs:
        return pos if len(pos) == 2 else [*pos, "HEAD"]
    if letters & set("cC") or "--copy" in longs:
        return pos[-1:]
    if "f" in letters or "--force" in longs:
        return pos[:1]
    return []


def _symref_written(args: list[str]) -> str:
    refs = _positionals(_without_values(args, "m", ()))
    writes = len(refs) == 2 or bool({"-d", "--delete"} & set(args))
    return refs[0] if refs and writes else ""


def _names_main(dst: str) -> bool:
    dst = dst.lstrip("+")
    return _is_main(dst, False) or ("*" in dst and (fnmatchcase(MAIN, dst) or fnmatchcase("main", dst)))


def _flags(args: list[str]) -> tuple[set[str], set[str]]:
    letters: set[str] = set()
    longs: set[str] = set()
    for a in args:
        if a == "--":
            break
        if a.startswith("--"):
            longs.add(a.split("=", 1)[0])
        elif a.startswith("-") and len(a) > 1:
            letters |= set(a[1:])
    return (letters, longs)


def _option_values(args: list[str], shorts: str, longs: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if name in longs:
                if eq:
                    out.append(value)
                elif i + 1 < len(args):
                    out.append(args[i + 1])
                    i += 1
        elif a.startswith("-") and len(a) > 1:
            for j, ch in enumerate(a[1:], start=1):
                if ch in shorts:
                    if a[j + 1 :]:
                        out.append(a[j + 1 :])
                    elif i + 1 < len(args):
                        out.append(args[i + 1])
                        i += 1
                    break
        i += 1
    return out


def _without_values(args: list[str], shorts: str, longs: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("--") and a.split("=", 1)[0] in longs:
            skip = "=" not in a
            continue
        if a.startswith("-") and (not a.startswith("--")) and (len(a) > 1) and (a[1] in shorts):
            skip = len(a) == 2
            continue
        out.append(a)
    return out


def _reset_moves(at: Path, options: list[str], args: list[str]) -> bool:
    if "--" in args:
        cut = args.index("--")
        if args[cut + 1 :]:
            return False
        args = args[:cut]
    if any((a.startswith("--pathspec-from-file") for a in args)):
        return False
    pos = _positionals(args)
    if len(pos) != 1:
        return False
    code, target = _git(at, options, "rev-parse", "--verify", "--quiet", f"{pos[0]}^{{commit}}")
    _, head = _git(at, options, "rev-parse", "--verify", "--quiet", "HEAD")
    return code == 0 and target != head


def main_guard(root: Path, at: Path, verb: str, args: list[str], options: list[str]) -> str:
    what = moves_main(root, at, verb, args, options)
    return deny(REASON.format(what=what)) if what else ""
