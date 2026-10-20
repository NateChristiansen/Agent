"""Claude Code hooks as CLI subcommands: `agent hook <name>` reads the hook JSON on stdin.

Contract for every hook: never raise, never block by accident, exit 0. Output only when the
hook has something to say (session-start context, or a PreToolUse decision). A hook that
fails open is better than one that wedges a session.

Threat model: the hooks are a floor for the
honest-agent reflex, plus a hard stop for the obvious footguns. They are not a sandbox.
Anything they cannot classify falls through to Claude Code's own permission prompt. That is
why `allow` is only ever returned for a command this code fully understands.

The module is a package, split the way `dispatch/` and `cli/` were: one module per concern.
`paths` (the protected-path regex, deny lists, and the Edit/Write and Bash halves of
`protect-generated`'s path checks, plus a few small shared helpers with no dependency on the
rest of the package), `guards` (no-chaining, protect-generated), `repo_guards` (push-guard,
pull-warn), `stop` (lease-yield, journal-pending, thrash-guard, and the hook-denial counter `run()`
calls), `ask_guard` (a reply ending in an unregistered question to the owner),
`worker` (worker-stop, precompact — the two hooks that run inside a dispatched worker), `session`
(session-start, capture, dispatch-inbox, render-after-plan-edit), `groups` (the grouped commands
the client configs wire, one process per event and matcher, and how their outputs combine), and
`run` (the `NAMES` tuple, `HANDLERS` table, and `run()` itself, `bin/agent hook <name>`'s entry
point, which also runs a group). The names a
caller outside this package reaches on `hooks.<name>` are re-exported below and listed in
`__all__`, so `from agent_cli import hooks` and `from . import hooks as hooks_mod` keep
resolving (`hooks.respelling_hint`, `hooks.NO_CHAIN_REASON`, `hooks.THRASH_DENIALS`, ...). A
re-export is a separate binding, so anything that ever needs to MONKEYPATCH one of these names
has to patch the submodule that owns it, not this package.
"""

from __future__ import annotations

from .activity import activity
from .guards import (
    NO_CHAIN_REASON,
    SHELL_WRAPPER_HEADS,
    is_chained,
    mcp_stale,
    no_chaining,
    protect_generated,
    respelling_hint,
)
from .naming import desired_title, session_name, title_for
from .notes_sync import notes_sync_hook
from .paths import (
    DELETE_FLAG_RE,
    HEREDOC_RE,
    IN_PLACE_FLAG_RE,
    NATIVE_GENERATED_DENY,
    OPERATE_VERBS,
    PACKAGE_HEADS,
    PRODUCT_CODE_REASON,
    PROTECTED_DIR_RE,
    PROTECTED_DIRS,
    PROTECTED_PATH_RE,
    READ_ONLY_HEADS,
    SHELL_WRITE_REASON,
    STREAM_EDITORS,
    VAULT_GENERATED_DENY,
)
from .repo_guards import (
    PULL_WARN_TOOLS,
    PULL_WARNED_KEY,
    PULLED_KEY,
    PUSH_GUARD_REASON,
    PUSH_OVERRIDE_ENV,
    REVIEW_ROW,
    pull_warn,
    push_guard,
)
from .run import ALIASES, HANDLERS, NAMES, run
from .session import (
    capture_hook,
    dispatch_inbox,
    render_after_plan_edit,
    session_start,
)
from .session_blocks import (
    UNASSIGNED_HEAVY_RECORDS,
    journal_nag,
)
from .stop import (
    THRASH_DENIALS,
    THRASH_EDIT_EXEMPT,
    THRASH_EDIT_EXEMPT_DIRS,
    THRASH_ERRORS,
    THRASH_HISTORY,
    THRASH_REPEATS,
    THRASH_REVERTS,
    THRASH_SAME_SPOT,
    journal_pending,
    lease_yield,
    thrash_guard,
)
from .worker import precompact, worker_stop

__all__ = [
    # run
    "ALIASES",
    "HANDLERS",
    "NAMES",
    "run",
    # paths
    "DELETE_FLAG_RE",
    "HEREDOC_RE",
    "IN_PLACE_FLAG_RE",
    "NATIVE_GENERATED_DENY",
    "OPERATE_VERBS",
    "PACKAGE_HEADS",
    "PRODUCT_CODE_REASON",
    "PROTECTED_DIR_RE",
    "PROTECTED_DIRS",
    "PROTECTED_PATH_RE",
    "READ_ONLY_HEADS",
    "SHELL_WRITE_REASON",
    "STREAM_EDITORS",
    "VAULT_GENERATED_DENY",
    # guards
    "NO_CHAIN_REASON",
    "SHELL_WRAPPER_HEADS",
    "is_chained",
    "mcp_stale",
    "no_chaining",
    "protect_generated",
    "respelling_hint",
    # repo_guards
    "PULL_WARN_TOOLS",
    "PULL_WARNED_KEY",
    "PULLED_KEY",
    "PUSH_GUARD_REASON",
    "PUSH_OVERRIDE_ENV",
    "REVIEW_ROW",
    "pull_warn",
    "push_guard",
    # stop
    "THRASH_DENIALS",
    "THRASH_EDIT_EXEMPT",
    "THRASH_EDIT_EXEMPT_DIRS",
    "THRASH_ERRORS",
    "THRASH_HISTORY",
    "THRASH_REPEATS",
    "THRASH_REVERTS",
    "THRASH_SAME_SPOT",
    "journal_pending",
    "lease_yield",
    "thrash_guard",
    # activity
    "activity",
    # naming
    "desired_title",
    "session_name",
    "title_for",
    # notes_sync
    "notes_sync_hook",
    # worker
    "precompact",
    "worker_stop",
    # session
    "capture_hook",
    "dispatch_inbox",
    "render_after_plan_edit",
    "session_start",
    # session_blocks
    "UNASSIGNED_HEAVY_RECORDS",
    "journal_nag",
]
