# ruff: noqa
from __future__ import annotations
import importlib
import importlib.util
from typing import TYPE_CHECKING, Any
from . import evaluate as evaluate_mod
from . import handoffs, ledger, prompt, settings, state, worktrees
from . import notify as notify_mod
from . import watch as watch_mod
from .closing import abandon, ack
from .evaluate import BUDGET_REASONS, MAX_TURNS_REASONS, evaluate
from .handoffs import parse_handoff, plan_link_line, render_handoff, transcript_path, write_handoff
from .ledger import LEDGER_BY, PR_STALE_SECONDS, apply_pr_inheritance, ledger_rows, refresh_pr, render_ledger
from .notify import NOTIFY_ENV, default_channels, inbox_block, notifications_enabled, notify, pending_block
from .opener import open_, open_command, open_uri
from .preflight import (
    KNOWN_BINARIES,
    SHELL_FENCE_TAGS,
    extract_candidate_commands,
    per_commit_check,
    precedents_check,
    preflight,
)
from .prompt import check_schema, compose_followup, compose_prompt, plan_section, verify_return
from .return_schema import RETURN_SCHEMA
from .settings import BASE_ALLOW, BASE_DENY, GIT_READ_VERBS, GIT_VERBS, build_settings
from .state import (
    ATTENTION_STATES,
    CANCEL_GRACE_SECONDS,
    CLOSED_STATES,
    DEFAULT_BUDGET_USD,
    DEFAULT_NOTIFY,
    DISPATCH_KINDS,
    DISPATCHES_DIR,
    DISPLAY_SUMMARY_CHARS,
    HANDOFF_FILE_RE,
    HANDOFFS_DIR,
    ID_RE,
    MAX_ATTEMPTS,
    POLL_SECONDS,
    QUESTION_IDLE_SECONDS,
    RETURN_STATUSES,
    STATES,
    TERMINAL_STATES,
    TYPE_TAG,
    WATCHDOG_SECONDS,
    WORKER_CONTRACT,
    WORKTREE_SETUP_TIMEOUT_S,
    allocate_seq,
    branch_name,
    closed_feature_ids,
    denied_permission_events,
    dispatches_dir,
    display_summary,
    file,
    handoff_path,
    handoffs_dir,
    is_actioned,
    is_live,
    is_probe,
    list_,
    load,
    load_all,
    make_id,
    make_nonce,
    permission_denials,
    permission_events,
    save,
    show,
    summarize,
    worktree_path,
)
from .watch import DIGEST_ARG_CHARS, DIGEST_TEXT_CHARS, attach_hint, watch, watch_command
from .worktrees import cleanup, collect_files, ensure_worktree, reuse_worktree, run_worktree_setup

if TYPE_CHECKING:
    from . import lifecycle, rules, supervisor
    from .lifecycle import new, resume
    from .rules import (
        DENY_RULE_REASON,
        GIT_READ_REASON,
        NO_RULE_REASON,
        build_mcp_config,
        build_options,
        claude_on_path,
        decide,
        next_rubric_tier,
        options_preview,
        repo_mcp_servers,
        sdk_hooks,
        worker_env,
        write_worker_files,
    )
    from .supervisor import cancel, collect, inbox_path, run, say, worker_stop
_LAZY_MODULES = ("lifecycle", "rules", "supervisor")


def __getattr__(name: str) -> Any:
    if name in _LAZY_MODULES:
        return importlib.import_module(f"{__name__}.{name}")
    if not name.startswith("__") and importlib.util.find_spec(f"{__name__}.{name}") is None:
        for module_name in _LAZY_MODULES:
            module = importlib.import_module(f"{__name__}.{module_name}")
            if name in vars(module):
                return vars(module)[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "evaluate_mod",
    "handoffs",
    "ledger",
    "lifecycle",
    "notify_mod",
    "prompt",
    "rules",
    "settings",
    "state",
    "supervisor",
    "watch_mod",
    "worktrees",
    "ATTENTION_STATES",
    "CANCEL_GRACE_SECONDS",
    "CLOSED_STATES",
    "DEFAULT_BUDGET_USD",
    "DEFAULT_NOTIFY",
    "DISPATCH_KINDS",
    "DISPATCHES_DIR",
    "DISPLAY_SUMMARY_CHARS",
    "HANDOFFS_DIR",
    "HANDOFF_FILE_RE",
    "ID_RE",
    "MAX_ATTEMPTS",
    "POLL_SECONDS",
    "QUESTION_IDLE_SECONDS",
    "RETURN_STATUSES",
    "STATES",
    "TERMINAL_STATES",
    "TYPE_TAG",
    "WATCHDOG_SECONDS",
    "WORKER_CONTRACT",
    "WORKTREE_SETUP_TIMEOUT_S",
    "allocate_seq",
    "branch_name",
    "denied_permission_events",
    "dispatches_dir",
    "display_summary",
    "file",
    "handoff_path",
    "handoffs_dir",
    "is_actioned",
    "is_live",
    "is_probe",
    "list_",
    "closed_feature_ids",
    "load",
    "load_all",
    "make_id",
    "make_nonce",
    "permission_denials",
    "permission_events",
    "save",
    "show",
    "summarize",
    "worktree_path",
    "BASE_ALLOW",
    "BASE_DENY",
    "DENY_RULE_REASON",
    "GIT_READ_REASON",
    "GIT_READ_VERBS",
    "GIT_VERBS",
    "KNOWN_BINARIES",
    "NO_RULE_REASON",
    "RETURN_SCHEMA",
    "SHELL_FENCE_TAGS",
    "build_mcp_config",
    "build_options",
    "build_settings",
    "claude_on_path",
    "decide",
    "extract_candidate_commands",
    "next_rubric_tier",
    "options_preview",
    "per_commit_check",
    "precedents_check",
    "preflight",
    "repo_mcp_servers",
    "sdk_hooks",
    "worker_env",
    "write_worker_files",
    "check_schema",
    "compose_followup",
    "compose_prompt",
    "plan_section",
    "verify_return",
    "cleanup",
    "collect_files",
    "ensure_worktree",
    "reuse_worktree",
    "run_worktree_setup",
    "parse_handoff",
    "plan_link_line",
    "render_handoff",
    "transcript_path",
    "write_handoff",
    "NOTIFY_ENV",
    "default_channels",
    "inbox_block",
    "notifications_enabled",
    "notify",
    "pending_block",
    "BUDGET_REASONS",
    "MAX_TURNS_REASONS",
    "evaluate",
    "cancel",
    "collect",
    "inbox_path",
    "run",
    "say",
    "worker_stop",
    "abandon",
    "ack",
    "open_",
    "open_command",
    "open_uri",
    "new",
    "resume",
    "LEDGER_BY",
    "PR_STALE_SECONDS",
    "apply_pr_inheritance",
    "ledger_rows",
    "refresh_pr",
    "render_ledger",
    "DIGEST_ARG_CHARS",
    "DIGEST_TEXT_CHARS",
    "attach_hint",
    "watch",
    "watch_command",
]
