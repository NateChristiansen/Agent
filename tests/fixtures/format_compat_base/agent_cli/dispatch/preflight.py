# ruff: noqa
from __future__ import annotations
import re
import time
from pathlib import Path
from typing import Any
from .. import permissions
from .. import repos as repos_mod
from ..core import AgentError
from ..hooks.worker_push import git_push
from ..hooks.worker_redirect import outside_redirect
from ..hooks.worker_tests import full_suite
from ..launcher import is_windows
from ..records.narrow import as_list
from . import codex_capability, readonly_policy, telemetry_events
from .codex_policy import policy_for
from .state import prompt_text


def verification_preflight(repo: repos_mod.Repo, *, no_checks: bool = False) -> dict[str, Any]:
    if type(no_checks) is not bool:
        raise AgentError("no_checks must be an explicit per-dispatch boolean")
    config = repo.verification
    if no_checks:
        reason = "explicit per-dispatch no_checks exemption; supervisor checks will not run. This does not make worker permissions read-only."
    elif config is None:
        reason = f"{repo.short}: runnable verification is not configured; configure agent.yaml dispatch.verify with nonempty command/timeout_seconds mappings and an explicit dispatch.verify_timeout_seconds before launching. An authorized exempt dispatch must explicitly set no_checks=True (--no-checks); missing configuration is not an exemption."
    else:
        reason = "runnable verification is configured but has not run for this attempt; supervisor evidence is required before verified acceptance"
    windows: dict[str, Any] = {}
    no_shell = False
    if is_windows() and config is not None and (not no_checks):
        from .verification_shell import find_bash

        try:
            windows["shell"] = str(find_bash())
        except AgentError as exc:
            windows["shell"], reason, no_shell = (None, str(exc), True)
    return {
        **windows,
        "status": "exempt" if no_checks else "unverified",
        "configuration": "configured" if config is not None else "unconfigured",
        "launch_allowed": (no_checks or config is not None) and (not no_shell),
        "required": not no_checks,
        "reason": reason,
        "exemption": "explicit dispatch --no-checks" if no_checks else None,
        "commands": [
            {"command": command.command, "timeout_seconds": command.timeout_seconds}
            for command in config.commands
        ]
        if config is not None and (not no_checks)
        else [],
        "timeout_seconds": config.timeout_seconds if config is not None and (not no_checks) else None,
    }


def check_verification_preflight(assessment: dict[str, Any]) -> None:
    if not assessment["launch_allowed"]:
        raise AgentError(assessment["reason"])


KNOWN_BINARIES = frozenset(
    {
        "git",
        "gh",
        "pnpm",
        "npm",
        "npx",
        "yarn",
        "dotnet",
        "uv",
        "pip",
        "pytest",
        "python",
        "python3",
        "node",
        "make",
        "cargo",
        "go",
    }
)
_FENCE_RE = re.compile("^(```|~~~)\\s*([A-Za-z0-9_+-]*)")
SHELL_FENCE_TAGS = frozenset({"", "sh", "bash", "zsh", "shell", "console", "shell-session"})


def extract_candidate_commands(text: str) -> list[str]:
    seen: list[str] = []

    def add(cmd: str) -> None:
        cmd = cmd.strip()
        if cmd and cmd not in seen:
            seen.append(cmd)

    in_fence = False
    fence_marker = ""
    fence_is_shell = True
    for line in text.splitlines():
        stripped = line.strip()
        fence = _FENCE_RE.match(stripped)
        if fence:
            if not in_fence:
                in_fence, fence_marker = (True, fence.group(1))
                fence_is_shell = fence.group(2).lower() in SHELL_FENCE_TAGS
            elif stripped.startswith(fence_marker):
                in_fence = False
            continue
        if in_fence:
            command = stripped[2:] if stripped.startswith("$ ") else stripped
            if command and fence_is_shell and (not command.startswith("#")):
                add(command)
            continue
        if stripped.startswith("$ "):
            command = stripped[2:]
            if not command.startswith("#"):
                add(command)
    return seen


def preflight(
    root: Path,
    d: dict[str, Any],
    repo: repos_mod.Repo,
    settings: dict[str, Any] | None = None,
    *,
    record: bool = False,
) -> list[dict[str, Any]]:
    from .rules import build_settings

    started = time.monotonic()
    perms = (settings or build_settings(root, repo, worktree=d.get("worktree")))["permissions"]
    allow, deny = (perms["allow"], perms["deny"])
    rows: list[dict[str, Any]] = []
    worktree = Path(str(d["worktree"])) if d.get("worktree") else None
    for cmd in extract_candidate_commands(prompt_text(root, d)):
        verdict, rule = _score(cmd, allow, deny, worktree)
        rows.append({"command": cmd, "verdict": verdict, "rule": rule, "source": "work-order"})
    for cmd in as_list(repo.dispatch.get("setup")):
        verdict, rule = _score(str(cmd), allow, deny, worktree)
        rows.append({"command": str(cmd), "verdict": verdict, "rule": rule, "source": "setup"})
    for cmd in as_list(repo.dispatch.get("probes")):
        verdict, rule = _score(str(cmd), allow, deny, worktree)
        rows.append({"command": str(cmd), "verdict": verdict, "rule": rule, "source": "probe"})
    rows += precedents_check(root, d, repo)
    rows += per_commit_check(root, d, rows)
    rows += codex_capability.check(
        d, repo, rows, policy=policy_for(d, repo, root=root) if d.get("provider") == "codex" else None
    )
    if record:
        telemetry_events.preflight(root, d, rows, round(time.monotonic() - started, 3), "work-order")
    return rows


def _score(
    cmd: str, allow: list[str], deny: list[str], worktree: Path | None = None
) -> tuple[str, str | None]:
    from .. import hooks as hooks_mod

    if hooks_mod.is_chained(cmd):
        return ("denied", hooks_mod.NO_CHAIN_REASON + hooks_mod.respelling_hint(cmd))
    verdict, rule = permissions.matches(cmd, allow, deny)
    if verdict == "no-rule" and (why := readonly_policy.reason(cmd)):
        verdict, rule = ("allowed", why)
    if verdict == "allowed" and (why := runtime_refusal(cmd, worktree)):
        return ("denied", why)
    return (verdict, rule)


def runtime_refusal(cmd: str, worktree: Path | None) -> str | None:
    if worktree is not None and (why := outside_redirect(cmd, worktree)):
        return why
    return git_push(cmd, worktree or Path.cwd()) or full_suite(cmd)


def answer_preflight(
    root: Path, d: dict[str, Any], repo: repos_mod.Repo, answer: str
) -> list[dict[str, Any]]:
    from .rules import build_settings

    started = time.monotonic()
    perms = build_settings(root, repo, worktree=str(d["worktree"]))["permissions"]
    rows = [
        {"command": cmd, "verdict": v, "rule": r, "source": "answer"}
        for cmd in extract_candidate_commands(answer)
        for v, r in [_score(cmd, perms["allow"], perms["deny"], Path(str(d["worktree"])))]
    ]
    rows += codex_capability.check(
        {**d, "writes": None, "mcp": None},
        repo,
        rows,
        policy=policy_for(d, repo, root=root) if d.get("provider") == "codex" else None,
    )
    seconds = round(time.monotonic() - started, 3)
    telemetry_events.preflight(root, d, rows, seconds, "answer", attempt=int(d["attempt"]) + 1)
    return rows


BUILD_ROW = "build"
PRECEDENTS_WARNING = "a `build` row work order that names no sibling path under the repo: /dispatch step 3 wants a precedents list (for each new file or mechanism, the sibling file to match), because a mechanism the manager invents is the mechanism the reviewers reject. Not a refusal: check the work order, then launch."
_PATHY_RE = re.compile("[A-Za-z0-9_.@~+-]+(?:/[A-Za-z0-9_.@~+-]+)+")


def precedents_check(root: Path, d: dict[str, Any], repo: repos_mod.Repo) -> list[dict[str, Any]]:
    if BUILD_ROW not in str(d.get("choice_reason") or "").lower():
        return []
    bases = [Path(p) for p in (d.get("worktree"), repo.abs_path(root)) if p]
    bases = [b for b in bases if b.exists()]
    if not bases:
        return []
    for token in _PATHY_RE.findall(prompt_text(root, d)):
        candidate = token.strip("/.,;:)`'\"")
        if not candidate or candidate.startswith(("http:", "https:")):
            continue
        if any(((base / candidate).exists() for base in bases)):
            return []
    return [{"verdict": "warning", "warning": PRECEDENTS_WARNING, "source": "precedents"}]


PER_COMMIT_RE = re.compile(
    "\\b(?:every|each|per)\\s+(?:commit|attempt)\\b|\\bcommit[-\\s]by[-\\s]commit\\b", re.IGNORECASE
)
HISTORY_READ_VERBS = frozenset({"show", "log", "diff", "blame", "rev-list", "range-diff", "diff-tree"})
UNVERIFIABLE_CLAIM = "the work order asks for a per-commit or per-attempt claim and names no command that can produce one. A worker cannot move HEAD: `git checkout <sha>` and `git checkout <ref> -- <path>` are denied on purpose. It CAN read any commit: `git show <sha>:<path>`, `git log`, `git diff <sha>`, `git range-diff`, and `git restore --source=<sha> -- <path>`. Name the command the claim rests on, narrow the ask to the tip, or pass --allow-unmatched with a reason in the preview."


def _git_verb(command: str) -> str:
    parts = command.split()
    if not parts or parts[0] != "git":
        return ""
    i = 1
    while i < len(parts) and parts[i].startswith("-"):
        i += 2 if parts[i] in ("-C", "-c") else 1
    return parts[i] if i < len(parts) else ""


def per_commit_check(root: Path, d: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not PER_COMMIT_RE.search(prompt_text(root, d)):
        return []
    for row in rows:
        if row.get("verdict") != "allowed" or row.get("source") != "work-order":
            continue
        if _git_verb(str(row.get("command") or "")) in HISTORY_READ_VERBS:
            return []
    return [{"verdict": "unverifiable", "warning": UNVERIFIABLE_CLAIM, "source": "per-commit-claim"}]


def check_preflight(findings: list[dict[str, Any]], *, allow_unmatched: bool) -> None:
    blocked = [f for f in findings if f["verdict"] not in ("allowed", "warning")]
    if not blocked or allow_unmatched:
        return
    lines = [
        f["warning"]
        if f.get("warning")
        else f"denied by {f['rule']}: {f['command']} ({f['source']})"
        if f["verdict"] == "denied"
        else f"no matching rule: {f['command']} ({f['source']})"
        for f in blocked
    ]
    trailer = (
        "\nfix agent.yaml dispatch.allowed_tools or BASE_ALLOW, or pass --allow-unmatched with a reason in the preview"
        if any((not f.get("warning") for f in blocked))
        else ""
    )
    raise AgentError("preflight refused this launch:\n  " + "\n  ".join(lines) + trailer)
