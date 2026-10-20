"""Map CLI commands to MCP tools for route refusals and returned command guidance (see ADR-0033).
Unmapped commands receive generic guidance; person-only commands retain their stated reason.
"""

from __future__ import annotations

import re
import shlex

# (command, subcommand) -> the manager MCP tool that covers it. An `action`-routed tool
# (`dispatch`, `dispatch_read`, `harness`, `lease`, `lesson`, `peer`) covers several subcommands.
# The read-only dispatch subcommands route to `dispatch_read`, which writes nothing;
# `dispatch` keeps them as aliases for one release.
CLI_TO_MCP: dict[tuple[str, str | None], str] = {
    ("telemetry", "summary"): "harness",
    ("feature", "new"): "feature_new",
    ("feature", "list"): "feature_list",
    ("feature", "show"): "feature_show",
    ("feature", "use"): "feature_use",
    ("feature", "current"): "feature_show",
    ("feature", "set"): "feature_set",
    ("feature", "close"): "feature_close",
    ("task", "list"): "task_list",
    ("task", "done"): "task_done",
    ("task", "supersede"): "task_supersede",
    ("task", "check"): "task_check",
    ("task", "undo"): "task_undo",
    ("task", "add"): "task_add",
    ("log", None): "log",
    ("lesson", "add"): "lesson",
    ("lesson", "list"): "lesson",
    ("lesson", "resolve"): "lesson",
    ("lesson", "draft-check"): "lesson",
    ("adr", "new"): "adr_new",
    ("status", "render"): "status_render",
    ("raw", "preserve"): "raw_preserve",
    ("repos", "list"): "repos_list",
    ("repos", "validate"): "repos_validate",
    ("repos", "status"): "repos_status",
    ("repos", "sync"): "repos_sync",
    ("session", "get"): "session_get",
    ("session", "question"): "owner_question",
    ("lease", "take"): "lease",
    ("lease", "release"): "lease",
    ("lease", "list"): "lease",
    ("lease", "wait"): "lease",
    ("lease", "clear"): "lease",
    ("peer", "send"): "peer",
    ("peer", "inbox"): "peer",
    ("peer", "list"): "peer",
    ("dispatch", "new"): "dispatch",
    ("dispatch", "list"): "dispatch_read",
    ("dispatch", "show"): "dispatch_read",
    ("dispatch", "spans"): "dispatch",
    ("dispatch", "watch"): "dispatch_read",
    ("dispatch", "resume"): "dispatch",
    ("dispatch", "say"): "dispatch",
    ("dispatch", "cancel"): "dispatch",
    ("dispatch", "abandon"): "dispatch",
    ("dispatch", "ack"): "dispatch",
    ("dispatch", "collect"): "dispatch",
    ("dispatch", "collect-files"): "dispatch",
    ("dispatch", "reverify"): "dispatch",
    ("dispatch", "cleanup"): "dispatch",
    ("dispatch", "ledger"): "dispatch",
    ("doctor", None): "harness",
    ("tidy", None): "harness",
    ("notes", "root"): "harness",
    ("notes", "status"): "harness",
    ("notes", "document"): "harness",
    ("journal", "collect"): "harness",
    ("journal", "missing"): "harness",
    ("journal", "write"): "harness",
    ("scratch", "path"): "harness",
    ("worktree", "new"): "harness",
    ("worktree", "list"): "harness",
    ("worktree", "remove"): "harness",
    ("procs", "list"): "harness",
    ("verify", "record"): "harness",
    ("verify", "show"): "harness",
    ("land", None): "harness",
}

# The `action` a routed tool takes for each command, where it is not the subcommand itself.
# `cli-route` puts it in its refusal so the model can make the call without guessing.
ACTION: dict[tuple[str, str | None], str] = {
    key: f"{key[0]}_{(key[1] or '').replace('-', '_')}".rstrip("_")
    for key, tool in CLI_TO_MCP.items()
    if tool == "harness"
}
ROUTED_ACTION: dict[tuple[str, str | None], str] = {("lesson", "draft-check"): "draft_check"}

# A whole command group, or one (command, subcommand), that only a person, a client hook, git,
# or the supervisor runs, with why. A skill that needs one tells the owner the exact command.
PERSON_ONLY: dict[str | tuple[str, str | None], str] = {
    "hook": "hook entry points; clients call them, never a model",
    "capture": "hook-driven capture of payloads",
    "mcp": "serves this server",
    "git": "git hook entry points (ADR-0026)",
    "export": "the squashed public export; owner-run before a push",
    "setup": "machine setup, run by a person",
    "mcp-servers": "installs the manager's MCP servers",
    "worker-mcp": "worker profile inspection and launch",
    "obsidian": "Obsidian setup and its own CLI",
    ("notes-sync", "clear"): "lifts a sync halt after the owner resolved the notes repo by hand (ADR-0034)",
    ("notes", "init"): "notes repo creation, run by a person",
    ("notes", "link"): "setup-managed links, run by a person; a config save keeps them without it",
    ("procs", "keep"): "process keep-alive, run by a person",
    ("procs", "unkeep"): "process keep-alive, run by a person",
    ("procs", "reap"): "kills processes; run by a person",
    ("worktree", "link"): "manager worktree setup, run by a person",
    ("session", "set"): "the hooks write the mapping; models use feature_use",
    ("session", "prune"): "housekeeping, run by a person",
    ("session", "rm"): "housekeeping, run by a person",
    ("session", "export"): "housekeeping, run by a person before a clone is deleted",
    ("session", "list"): "debugging; peer list covers live sessions",
    ("raw", "redact"): "capture redaction, run by a person",
    ("status", "check"): "CI and doctor call it",
    ("status", "line"): "Claude Code's status line command",
    ("dispatch", "open"): "opens a worker in a terminal for a person",
    ("dispatch", "run"): "the supervisor's own entry point",
    ("peer-net", "receive"): (
        "infrastructure: run by the sshd forced command, never by a model or interactively by the owner"
    ),
    "peer-net": "cross-device enrollment writes SSH keys and authorized_keys; the owner runs it",
}


def tool_for(cmd: str | None, sub: str | None) -> tuple[str, str | None] | None:
    """The MCP tool (and `action`, for a routed tool) that performs `bin/agent cmd sub`, or None."""
    if cmd is None:
        return None
    for key in ((cmd, sub), (cmd, None)):
        if key in CLI_TO_MCP:
            tool = CLI_TO_MCP[key]
            if tool == "harness":
                return tool, ACTION[key]
            if tool in {"dispatch", "dispatch_read", "lease", "lesson", "peer"}:
                return tool, ROUTED_ACTION.get(key, sub)
            return tool, None
    return None


def person_only(cmd: str | None, sub: str | None) -> bool:
    return cmd is not None and ((cmd, sub) in PERSON_ONLY or cmd in PERSON_ONLY)


# CLI option -> the MCP parameters that may carry it, where the names differ. The first is the
# spelling `mcp_wording` uses.
FLAG_PARAMS: dict[str, tuple[str, ...]] = {
    "id": ("dispatch_id", "entry_id"),
    "budget": ("budget_usd",),
    "reason": ("budget_reason",),
    "prompt_file": ("prompt",),
    "all": ("include_all", "include_closed"),
    "repo": ("repos",),
    "tag": ("tags",),
    "ticket": ("tickets",),
    "task": ("tasks",),
    "yes": ("confirm",),
    "open": ("open_only",),
    "no_sync": ("sync",),
    "update": ("updates",),
    "input_file": ("push_input",),
    "timeout": ("timeout_minutes",),
    "watch": ("wait",),
    "note": ("message",),
    "add_repo": ("add_repo",),
    "rev": ("ref",),
    "branch": ("ref",),
    "extra": ("batch",),
    "read_only": ("writes",),  # `--read-only` is `writes=[]` over MCP (`_mcp_args`)
}
# A flag that stands for a fixed parameter value rather than `True`.
FLAG_VALUES = {"read_only": "[]"}


def _mcp_args(rest: str) -> str:
    """`001-01 --budget N --reason '...'` -> `001-01, budget_usd=N, budget_reason=...`, so a
    rewritten reference names the tool's parameters, not the CLI's flags (review, 052)."""
    try:
        words = shlex.split(rest)
    except ValueError:
        words = rest.split()
    out: list[str] = []
    while words:
        word = words.pop(0)
        if word.startswith("--"):
            key = word[2:].split("=", 1)
            flag = key[0].replace("-", "_")
            param = FLAG_PARAMS.get(flag, (flag,))[0]
            if len(key) == 2:
                out.append(f"{param}={key[1]}")
            elif words and not words[0].startswith("-"):
                out.append(f"{param}={words.pop(0)}")
            else:
                out.append(f"{param}={FLAG_VALUES.get(flag, 'True')}")
        else:
            out.append(word)
    return ", ".join(out)


_CLI_REF = re.compile(r"`(?:\./)?(?:bin/)?agent(?: --json)? ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?([^`]*)`")


def mcp_wording(text: str) -> str:
    """Rewrite each quoted CLI reference (`agent repos list`, `bin/agent dispatch show 12`) in a
    message a model reads over MCP into the tool that does it. Ops and errors are shared with
    the CLI and name commands; over MCP the model must never be pointed at the shell.
    A person-only command stays as written, marked for the owner to run."""

    def swap(m: re.Match[str]) -> str:
        cmd, sub, rest = m.group(1), m.group(2), m.group(3)
        found = tool_for(cmd, sub)
        if found is None and sub is not None:
            found = tool_for(cmd, None)
            if found is not None:
                rest = f" {sub}{rest}"
        if found is not None:
            tool, action = found
            args = _mcp_args(rest.strip())
            call = f"the `{tool}` tool" + (f" with action `{action}`" if action else "")
            return call + (f" ({args})" if args else "")
        if person_only(cmd, sub):
            return f"{m.group(0)} (the owner runs this)"
        return m.group(0)

    return _CLI_REF.sub(swap, text)
