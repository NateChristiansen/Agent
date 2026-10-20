# ruff: noqa
from __future__ import annotations
import os
import re
import shlex
from pathlib import Path
from typing import Any
from .. import repos as repos_mod
from ..records.narrow import str_list
from . import readonly_policy
from .codex_policy import CodexPolicy, policy_for

PROTECTED_DIRS = (".agents", ".codex", ".git")
METADATA_VERBS = {
    "merge",
    "rebase",
    "reset",
    "switch",
    "cherry-pick",
    "revert",
    "pull",
    "tag",
    "am",
    "apply",
    "stash",
}
NETWORK_HEADS = {"gh", "glab", "curl", "wget", "ssh", "scp"}
NETWORK_GIT = {"fetch", "pull", "push", "clone", "ls-remote"}
NETWORK_TOOLS = {
    ("uv", "sync"),
    ("uv", "add"),
    ("uv", "lock"),
    ("uv", "pip"),
    ("pip", "install"),
    ("pip3", "install"),
    ("npm", "install"),
    ("npm", "ci"),
    ("pnpm", "install"),
    ("yarn", "install"),
}
ROUTE = "route it to a Claude worker (provider claude) or do it on the manager side"


def check(
    d: dict[str, Any], repo: repos_mod.Repo, rows: list[dict[str, Any]], *, policy: CodexPolicy | None = None
) -> list[dict[str, Any]]:
    if d.get("provider") != "codex":
        return []
    out: list[dict[str, Any]] = []
    for path in str_list(d.get("writes") or []):
        top = str(path).strip("/").split("/", 1)[0]
        if top in PROTECTED_DIRS:
            out.append(
                _row(
                    str(path),
                    f"a Codex worker cannot edit `{path}`: its sandbox protects {', '.join(PROTECTED_DIRS)} and the host declines the patch approval; {ROUTE}",
                )
            )
    policy = policy or policy_for(d, repo)
    for row in rows:
        command = str(row.get("command") or "")
        if not command or row.get("verdict") != "allowed" or row.get("source") == "setup":
            continue
        row["rule_verdict"] = row["verdict"]
        if why := _refusal(command, network=policy.network_access):
            row.update(verdict="unsupported", capability_verdict="unsupported", warning=why)
        elif why := _filesystem_refusal(command, d, policy):
            row.update(verdict="unsupported", capability_verdict="unsupported", warning=why)
        elif why := _opaque_reason(command):
            row.update(verdict="unverified", capability_verdict="unverified", warning=why)
        else:
            row["capability_verdict"] = "supported"
    if d.get("mcp") and (not policy.network_access):
        out.append(
            {
                "verdict": "warning",
                "command": ", ".join(str_list(d["mcp"])),
                "warning": "the Codex shell sandbox has no network; MCP servers execute outside that shell boundary and need separate trusted backend controls",
                "source": "codex-capability",
            }
        )
    return out


def _refusal(command: str, *, network: bool) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        return ""
    if not words:
        return ""
    if words[1:] in (["--version"], ["-V"]):
        return ""
    head = words[0].rsplit("/", 1)[-1]
    verb = _git_verb(words) if head == "git" else ""
    remote_args = words[words.index(verb) + 1 :] if verb == "remote" else []
    remote_write = bool(remote_args) and remote_args[0] not in {"-v", "--verbose", "get-url", "show"}
    if (
        verb in METADATA_VERBS
        or _unapproved_cleanup(words, verb)
        or _branch_write(words, verb)
        or remote_write
    ):
        return f"`git {verb}` writes Git metadata, which a Codex worker's sandbox refuses and the host approves only for add, commit and worktree restores; {ROUTE}"
    remote_network = remote_args[:1] == ["show"] and "-n" not in remote_args
    if not network and (
        head in NETWORK_HEADS or verb in NETWORK_GIT or tuple(words[:2]) in NETWORK_TOOLS or remote_network
    ):
        return f"`{' '.join(words[:2])}` needs the network, and Codex workers run without it (dispatch.codex_network_access is off); {ROUTE}"
    if head == "uv" and words[1:3] == ["run", "python"]:
        return "`uv run python` crashes inside a Codex worker's sandbox on macOS (uv's proxy lookup); use `.venv/bin/python` instead (`.venv\\Scripts\\python.exe` on Windows)"
    return ""


_RUNNERS = {"pytest", "ruff", "pyright", "mypy", "black"}
_LOCAL_TOOLS = {"ls", "diff", "which", "echo", "mkdir", "touch", "cp", "mv", "rm", "chmod"}
_PYTHON = re.compile("python(?:\\d+(?:\\.\\d+)*)?(?:\\.exe)?$")
_SUPPORTED_GIT = {
    "status",
    "diff",
    "log",
    "show",
    "blame",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "cat-file",
    "grep",
    "merge-base",
    "rev-list",
    "range-diff",
    "diff-tree",
    "describe",
    "shortlog",
    "name-rev",
    "remote",
    "add",
    "commit",
    "rm",
    "mv",
    "restore",
    "checkout",
    "branch",
    *NETWORK_GIT,
}


def _filesystem_refusal(command: str, d: dict[str, Any], policy: CodexPolicy) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        return ""
    if not words or not d.get("worktree"):
        return ""
    head, args = (words[0].rsplit("/", 1)[-1], words[1:])
    targets = _write_targets(head, args)
    if targets is None:
        return ""
    cwd = Path(str(d["worktree"])).resolve()
    grants = [(Path(path).resolve(), "write") for path in policy.writable_roots]
    grants += [(Path(path).resolve(), access) for path, access in policy.filesystem_permissions]
    if policy.permissions_profile is None:
        grants += [(Path("/tmp").resolve(), "write")]
        if tmpdir := os.environ.get("TMPDIR"):
            grants.append((Path(tmpdir).resolve(), "write"))
        grants += [(cwd / name, "read") for name in PROTECTED_DIRS]
    if head in {"cp", "mv"} and (cwd / args[-1]).is_dir():
        targets += [str(Path(args[-1]) / Path(source).name) for source in args[:-1]]
    for target in targets:
        if "$" in target or "~" in target or any((c in target for c in "*?[")):
            return ""
        path = (cwd / target).resolve()
        matches = [
            (len(root.parts), index, access)
            for index, (root, access) in enumerate(grants)
            if path.is_relative_to(root)
        ]
        access = max(matches, default=(0, 0, "read"))[2]
        if access != "write":
            return f"`{command}` writes `{path}`, outside the rendered Codex write permissions or inside a read-only restriction; no runtime escalation is granted by preflight"
    return ""


def _write_targets(head: str, args: list[str]) -> list[str] | None:
    if head in {"mkdir", "touch", "rm"}:
        flags: set[str] = {"-p"} if head == "mkdir" else {"-c", "-a", "-m"} if head == "touch" else set()
        return (
            [a for a in args if a != "--" and a not in flags]
            if all((not a.startswith("-") or a == "--" or a in flags for a in args))
            else None
        )
    if head in {"cp", "mv"} and len(args) >= 2 and (not any((a.startswith("-") for a in args))):
        return args[-1:] if head == "cp" else args
    if head == "chmod" and len(args) >= 2 and (not any((a.startswith("-") for a in args))):
        return args[1:]
    return None


def _opaque_reason(command: str) -> str:
    unknown = f"`{command}` has opaque executable effects that preflight cannot establish; capability is unverified under the rendered Codex sandbox. Name a supported command, or use allow_unmatched only for an explicitly approved diagnostic attempt"
    from ..hooks import is_chained

    if is_chained(command):
        return unknown
    try:
        words = shlex.split(command)
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        shell_words = list(lexer)
    except ValueError:
        return unknown
    if not words:
        return unknown
    head, args = (words[0].rsplit("/", 1)[-1], words[1:])
    if args in (["--version"], ["-V"]):
        return ""
    if readonly_policy.reason(command):
        return ""
    if any((re.fullmatch("[|<>;&()]+", word) for word in shell_words)):
        return unknown
    if head == "git":
        if (
            _git_verb(words) not in _SUPPORTED_GIT
            or "-c" in words
            or any(
                (w in {"--ext-diff", "--textconv", "--output"} or w.startswith("--output=") for w in words)
            )
        ):
            return unknown
        return ""
    if head in _LOCAL_TOOLS:
        if head not in {"ls", "diff", "which", "echo"}:
            targets = _write_targets(head, args)
            if targets is None or any(
                ("$" in a or "~" in a or any((c in a for c in "*?[")) for a in targets)
            ):
                return unknown
        return ""
    if head in _RUNNERS:
        return ""
    if _PYTHON.fullmatch(head):
        return "" if len(args) >= 2 and args[0] == "-m" and (args[1] in _RUNNERS) else unknown
    if head == "uv":
        if args and args[0] in {"sync", "add", "lock", "pip"}:
            return ""
        return "" if len(args) >= 2 and args[0] == "run" and (args[1] in _RUNNERS) else unknown
    if head in NETWORK_HEADS or tuple(words[:2]) in NETWORK_TOOLS:
        return ""
    return unknown


def _unapproved_cleanup(words: list[str], verb: str) -> bool:
    if verb not in ("restore", "checkout"):
        return False
    args = words[words.index(verb) + 1 :]
    if "--" not in args:
        return True
    allowed = {"--worktree", "-W"} if verb == "restore" else set[str]()
    return bool(set(args[: args.index("--")]) - allowed)


_BRANCH_READS = {
    "-a",
    "-r",
    "-v",
    "-vv",
    "-l",
    "-i",
    "--list",
    "--all",
    "--remotes",
    "--verbose",
    "--show-current",
    "--ignore-case",
    "--column",
    "--no-column",
    "--color",
    "--no-color",
}
_BRANCH_READ_VALUED = (
    "--contains",
    "--no-contains",
    "--merged",
    "--no-merged",
    "--points-at",
    "--sort",
    "--format",
    "--abbrev",
)


def _branch_write(words: list[str], verb: str) -> bool:
    if verb != "branch":
        return False
    args = words[words.index("branch") + 1 :]
    listing, i = (False, 0)
    while i < len(args):
        a = args[i]
        if a in _BRANCH_READS:
            listing = listing or a in ("-l", "--list", "-a", "-r", "--all", "--remotes")
        elif a.split("=", 1)[0] in _BRANCH_READ_VALUED:
            listing = True
            if "=" not in a:
                i += 1
        elif a.startswith("-") or not listing:
            return True
        i += 1
    return False


def _git_verb(words: list[str]) -> str:
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in ("-C", "-c") else 1
    return words[i] if i < len(words) else ""


def _row(command: str, sentence: str) -> dict[str, Any]:
    return {"verdict": "unsupported", "command": command, "warning": sentence, "source": "codex-capability"}
