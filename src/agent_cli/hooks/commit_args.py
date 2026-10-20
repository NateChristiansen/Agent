"""How a `git commit` command line picks what it commits: from the index, or from the working
tree (`-a`, `--include`, `--only` or a pathspec). Used by the worker comment check, which needs
the paths a commit would take."""

from __future__ import annotations

#: `git commit` options that take the next word as their value.
_COMMIT_VALUE_OPTIONS = {
    "-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c", "--reedit-message",
    "--author", "--date", "-t", "--template", "--fixup", "--squash", "--trailer", "--cleanup",
    "--pathspec-from-file",
}  # fmt: skip
_COMMIT_VALUE_SHORT = set("mFCct")
#: Short options whose value, if any, is attached: `-uno`, `-Skeyid`.
_COMMIT_ATTACHED_SHORT = set("uS")


def takes_worktree(args: list[str]) -> bool:
    """True when the commit takes tracked files from the working tree rather than the index:
    `-a`, `--all`, `--include`, `--only` or a pathspec."""
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in _COMMIT_VALUE_OPTIONS:
            skip = True
            continue
        if a == "--":
            return True
        if a.startswith("--"):
            if a in ("--all", "--include", "--only"):
                return True
            continue
        if a.startswith("-") and len(a) > 1:
            for i, ch in enumerate(a[1:], start=1):
                if ch in "aio":
                    return True
                if ch in _COMMIT_VALUE_SHORT:
                    skip = i == len(a) - 1
                    break
                if ch in _COMMIT_ATTACHED_SHORT:
                    break
            continue
        return True
    return False
