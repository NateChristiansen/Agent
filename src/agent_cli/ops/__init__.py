"""Every CLI action as a keyword-only function returning an Op. No argparse, no stdout.

cli/ (argv) and mcp/ (typed tools) are two thin front ends over this package.
So `--json` output and a tool's structured content are the same dict. Anything a caller
needs to hear about goes on stderr or in the Op. Nothing here writes to sys.stdout.

The package is split by domain: `features` (feature/task/log/raw), `adrs`,
`status` (status render/check/line, obsidian), `dispatch` (dispatch new/resume/.../
ledger), `repos` (repos list/status/sync), `sessions` (session/lease/scratch/
capture), `infra` (doctor/setup/journal), `_shared` (the `Op` type and `render_views`).
Everything a caller outside this package reach on `ops.<name>` is re-exported below,
so `from . import ops` keeps working unchanged. A test that MONKEYPATCHES a name must patch
the submodule that owns it: `ops.status.render`, `ops.dispatch.dispatch_mod`. A re-export here is a
separate binding the owning module never reads.

`status` and `dispatch` are each both a submodule and a public function name (`status_render`
lives in the `status` submodule; the dispatch actions live in the `dispatch` submodule). The
submodules are reachable as `ops.status` / `ops.dispatch`, same as `agent_cli.dispatch.notify`
is reachable through the dispatch package.
"""

from __future__ import annotations

from . import adrs, dispatch, features, infra, notes, reconcile, repos, sessions, status
from ._shared import Op, render_views
from .adrs import adr_new
from .code_review import dispatch_code_ledger, dispatch_code_review
from .dispatch import (
    DISPATCH_WATCH_LINES,
    dispatch_abandon,
    dispatch_ack,
    dispatch_cancel,
    dispatch_cleanup,
    dispatch_collect,
    dispatch_collect_files,
    dispatch_consume,
    dispatch_discard_files,
    dispatch_ledger,
    dispatch_line,
    dispatch_list,
    dispatch_new,
    dispatch_open,
    dispatch_preview_text,
    dispatch_read_show,
    dispatch_reconcile,
    dispatch_restore,
    dispatch_reverify,
    dispatch_say,
    dispatch_show,
    dispatch_watch,
)
from .dispatch_resume import dispatch_resume
from .dispatch_spans import dispatch_spans
from .feature_close import feature_close
from .features import (
    feature_current,
    feature_list,
    feature_new,
    feature_set,
    feature_show,
    feature_use,
    log,
    raw_preserve,
    raw_redact,
    task_add,
    task_check,
    task_done,
    task_list,
    task_supersede,
    task_undo,
)
from .harness import (
    land,
    procs_list,
    verify_record,
    verify_show,
)
from .infra import (
    doctor,
    journal_collect,
    journal_missing,
    journal_write,
    setup,
)
from .notes import (
    mcp_servers_install,
    notes_document,
    notes_init,
    notes_link,
    notes_publish,
    notes_root,
    notes_status,
    notes_write,
)
from .peers import peer_inbox, peer_list, peer_send
from .plan_ledger import dispatch_plan_ledger
from .plan_review import dispatch_plan_review
from .repos import (
    PULLED_KEY,
    repos_list,
    repos_status,
    repos_sync,
    repos_validate,
)
from .sessions import (
    capture,
    lease_clear,
    lease_list,
    lease_release,
    lease_take,
    lease_wait,
    owner_question,
    scratch_path,
    session_export,
    session_get,
    session_list,
    session_prune,
    session_rm,
    session_set,
)
from .status import (
    obsidian_daily,
    obsidian_open,
    obsidian_search,
    obsidian_setup,
    status_check,
    status_line,
    status_render,
)
from .worktree import (
    worktree_link,
    worktree_list,
    worktree_new,
    worktree_reconcile,
    worktree_remove,
    worktree_restore,
)

__all__ = [
    # submodules, reachable as ops.<name> (a monkeypatch must target the owning submodule)
    "adrs",
    "dispatch",
    "features",
    "infra",
    "notes",
    "reconcile",
    "repos",
    "sessions",
    "status",
    # shared
    "Op",
    "render_views",
    # features
    "feature_close",
    "feature_current",
    "feature_list",
    "feature_new",
    "feature_set",
    "feature_show",
    "feature_use",
    "log",
    "raw_preserve",
    "raw_redact",
    "task_add",
    "task_check",
    "task_done",
    "task_list",
    "task_supersede",
    "task_undo",
    # adrs
    "adr_new",
    # status
    "obsidian_daily",
    "obsidian_open",
    "obsidian_search",
    "obsidian_setup",
    "status_check",
    "status_line",
    "status_render",
    # repos
    "PULLED_KEY",
    "repos_list",
    "repos_status",
    "repos_sync",
    "repos_validate",
    # sessions / leases / scratch / capture
    "capture",
    "lease_clear",
    "lease_list",
    "lease_release",
    "lease_take",
    "lease_wait",
    "worktree_link",
    "worktree_list",
    "worktree_new",
    "worktree_reconcile",
    "worktree_remove",
    "worktree_restore",
    "peer_inbox",
    "peer_list",
    "peer_send",
    "scratch_path",
    "session_export",
    "session_get",
    "owner_question",
    "session_list",
    "session_prune",
    "session_rm",
    "session_set",
    # harness: procs, pass records
    "procs_list",
    "verify_record",
    "verify_show",
    "land",
    "dispatch_spans",
    # infra: journal, doctor, setup
    "doctor",
    "journal_collect",
    "journal_missing",
    "journal_write",
    "setup",
    # notes: the private set (see ADR-0012)
    "mcp_servers_install",
    "notes_document",
    "notes_init",
    "notes_link",
    "notes_root",
    "notes_status",
    "notes_publish",
    "notes_write",
    # dispatch
    "DISPATCH_WATCH_LINES",
    "dispatch_abandon",
    "dispatch_ack",
    "dispatch_cancel",
    "dispatch_cleanup",
    "dispatch_code_ledger",
    "dispatch_code_review",
    "dispatch_collect",
    "dispatch_collect_files",
    "dispatch_consume",
    "dispatch_discard_files",
    "dispatch_reconcile",
    "dispatch_restore",
    "dispatch_ledger",
    "dispatch_line",
    "dispatch_reverify",
    "dispatch_list",
    "dispatch_new",
    "dispatch_open",
    "dispatch_plan_ledger",
    "dispatch_plan_review",
    "dispatch_preview_text",
    "dispatch_read_show",
    "dispatch_resume",
    "dispatch_say",
    "dispatch_show",
    "dispatch_watch",
]
