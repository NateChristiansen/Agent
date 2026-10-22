"""Expose harness operations through one action-routed MCP tool. Call the same ops as the CLI (see
ADR-0005); model shell CLI access is guarded. Large results return a short summary plus a scratch
path to bound context.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from .. import features as features_mod
from .. import ops
from .. import scratch as scratch_mod
from ..core import EXIT_OK, AgentError, json_default
from ..ops import harness as harness_ops
from ..records.narrow import as_dict, as_dict_or_none
from .tools import Ctx, Op, ToolSpec

HarnessAction = Literal[
    "link",
    "doctor",
    "tidy",
    "notes_root",
    "notes_status",
    "notes_publish",
    "notes_document",
    "notes_write",
    "journal_collect",
    "journal_missing",
    "journal_write",
    "scratch_path",
    "worktree_new",
    "worktree_list",
    "worktree_remove",
    "worktree_reconcile",
    "worktree_restore",
    "procs_list",
    "verify_record",
    "verify_show",
    "land",
    "telemetry_summary",
]
LandStrategy = Literal["squash", "merge", "ff"]

# Longer summaries go to a scratch file; the tool returns the head and the path.
SUMMARY_LIMIT = 4000
SUMMARY_HEAD_LINES = 20


def _harness_require(value: Any, name: str, action: str) -> Any:
    if value is None:
        raise AgentError(f"harness action={action!r} needs `{name}`")
    return value


def _spill(ctx: Ctx, action: str, op: Op) -> Op:
    """Keep the result short. Past SUMMARY_LIMIT characters of text or of JSON data, write both
    in full to one scratch file and return the head, `ok`, and the path. Structured content goes
    to the model too, so trimming only the text would still flood it (review, 052)."""
    data_json = json.dumps(op.data, default=json_default)
    if len(op.text) <= SUMMARY_LIMIT and len(data_json) <= SUMMARY_LIMIT:
        return op
    out_dir = scratch_mod.ensure(ctx.root) / "harness"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{action}-{stamp}-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps({"text": op.text, "data": json.loads(data_json)}, indent=1), encoding="utf-8")
    head = "\n".join(op.text.splitlines()[:SUMMARY_HEAD_LINES])[: SUMMARY_LIMIT // 2]
    ok = as_dict(op.data).get("ok")
    data = {"ok": ok, "truncated": True, "full_output": str(path)}
    return Op(data, f"{head}\n… full output ({len(op.text)} chars of text) in {path}", code=op.code)


def _mark(op: Op) -> Op:
    """Carry the exit code into the data, so a doctor failure is visible without
    making the call an error (the model still needs the findings)."""
    data = as_dict_or_none(op.data)
    if data is not None:
        return Op({**data, "ok": op.code == EXIT_OK}, op.text, code=op.code)
    return Op({"items": op.data, "ok": op.code == EXIT_OK}, op.text, code=op.code)


def harness(
    ctx: Ctx,
    action: HarnessAction,
    ref: str | None = None,
    feature: str | None = None,
    target: str | None = None,
    from_file: str | None = None,
    apply: bool = False,
    expect_sha256: str | None = None,
    create: bool = False,
    expect_absent: bool = False,
    date: str | None = None,
    write: bool = False,
    file: str | None = None,
    name: str | None = None,
    base: str = "main",
    sync: bool = True,
    force: bool = False,
    no_reuse: bool = False,
    strategy: LandStrategy | None = None,
    message: str | None = None,
    recover: str | None = None,
    abort: str | None = None,
    wait: float = 3600.0,
    status: bool = False,
    batch: list[str] | None = None,
    origins: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
) -> Op:
    root = ctx.root
    if action == "telemetry_summary":
        from ..telemetry.summary import summary

        data = summary(root, day=date, since=since, until=until)
        return Op(data, json.dumps(data, default=json_default))
    if action == "link":
        op = harness_ops.link(root, target=_harness_require(target, "target", action))
    elif action == "doctor":
        op = ops.doctor(root)
    elif action == "tidy":
        number = features_mod.resolve(root, feature).id if feature else None
        op = harness_ops.tidy(root, apply=apply, wait_s=min(wait, 60.0), feature=number)
    elif action == "notes_root":
        op = ops.notes_root(root)
    elif action == "notes_status":
        op = ops.notes_status(root)
    elif action == "notes_publish":
        op = ops.notes_publish(root)
    elif action == "notes_document":
        op = ops.notes_document(
            root,
            target=_harness_require(target, "target", action),
            from_file=_harness_require(from_file, "from_file", action),
            apply=apply,
            expect_sha256=expect_sha256,
            create=create,
            expect_absent=expect_absent,
        )
    elif action == "notes_write":
        op = ops.notes_write(
            root,
            target=_harness_require(target, "target", action),
            from_file=from_file,
            apply=apply,
            expect_sha256=expect_sha256,
        )
    elif action == "journal_collect":
        op = ops.journal_collect(root, date=date, write=write, as_json=True)
    elif action == "journal_missing":
        op = ops.journal_missing(root)
    elif action == "journal_write":
        op = ops.journal_write(
            root, date=_harness_require(date, "date", action), file=_harness_require(file, "file", action)
        )
    elif action == "scratch_path":
        op = ops.scratch_path(root)
    elif action == "worktree_new":
        op = ops.worktree_new(
            root,
            name=_harness_require(name, "name", action),
            base=base,
            sync=sync,
            feature=feature,
            session_id=ctx.session_id() if feature else None,
        )
    elif action == "worktree_list":
        op = ops.worktree_list(root)
    elif action == "worktree_remove":
        op = ops.worktree_remove(root, name=_harness_require(name, "name", action), force=force)
    elif action == "worktree_reconcile":
        op = ops.worktree_reconcile(root, path=_harness_require(target, "target", action), apply=apply)
    elif action == "worktree_restore":
        op = ops.worktree_restore(
            root,
            name=_harness_require(name, "name", action),
            head=_harness_require(ref, "ref", action),
            apply=apply,
            sync=sync,
        )
    elif action == "procs_list":
        op = ops.procs_list(root)
    elif action == "verify_record":
        op = ops.verify_record(root, rev=_harness_require(ref, "ref", action), reuse=not no_reuse)
    elif action == "verify_show":
        op = ops.verify_show(root, rev=_harness_require(ref, "ref", action))
    elif action == "land":
        # A landing outlasts the MCP call (a full run is ~13 min), so it always runs detached here.
        op = ops.land(
            root,
            branch=ref,
            strategy=strategy,
            message=message,
            recover=recover,
            abort=abort,
            wait_s=wait,
            detach=True,
            status=status,
            extra=batch,
            origins=origins,
        )
    else:
        raise AgentError(f"unknown harness action {action!r}")  # unreachable: the schema enum refuses it
    return _spill(ctx, action, _mark(op))


HARNESS_TOOLS: dict[str, ToolSpec] = {
    "harness": ToolSpec(
        harness,
        "Run a harness check or maintenance action",
        "The CLI-only harness commands, routed by `action`. Never run `bin/agent` in a shell; use "
        "this or the typed tool for the action. "
        "`telemetry_summary` (date or since/until) returns fixed counts, spans and health. "
        "`link` (target = vault-relative file path) returns an encoded obsidian://open URL with "
        "the vault name from this machine's config; put it in inline code in the final reply. "
        "For repo files use `path:line`. "
        "`doctor` validates the harness. "
        "`tidy` lists closed-feature leftovers and terminal dispatch trees of open features, or only "
        "one `feature`'s; apply=True removes only safe trees and resolved branches; missing paths are "
        "reported, not removed; lists hold up to 10 examples beside `counts`, the rest in the result file. "
        "It returns a log and result path after at most 60 seconds (`wait` can shorten that); "
        "cleanup continues detached. "
        "`notes_root` names the repo to `git add` a private path in; `notes_status`. `notes_publish` "
        "commits and pushes the notes repo now and returns the commit: run it before handing another "
        "machine an evidence file, then send the path with `notes-commit: <sha>`. "
        "`notes_document` (target, from_file; preview first, then apply=True with expect_sha256 from "
        "the preview; create=True with expect_absent=True for a new file; unsupported on Windows, "
        "where it refuses). "
        "`notes_write` (target = agent.yaml, .mcp.json or harness/voice.md) changes a linked private "
        "file without severing its link, which Edit and apply_patch do: no from_file copies it to a "
        "draft to edit; from_file previews the diff; apply=True with expect_sha256 writes it. "
        "`journal_collect` (date, write), `journal_missing`, `journal_write` (date, file). "
        "`scratch_path`. `worktree_new` (name, base, sync, feature; an existing agent/<name> is checked out as "
        "it is and `branch_differs` says when it is not at base; a base other than main refuses; feature "
        "binds the worktree to that open feature, also on a re-run, and moving it to another open feature "
        "is refused), "
        "`worktree_list`, `worktree_remove` (name, force). `worktree_reconcile` (target = a registered "
        "path, apply) drops one proven-missing path's Git registration once its HEAD and index are "
        "preserved, never a global prune. `worktree_restore` (name, ref = the head agent/<name> must "
        "still be at, apply, sync) recreates a lost manager worktree with its state links; both preview "
        "unless apply=True. "
        "`procs_list`. "
        "`verify_show` (ref) says whether a full pass record covers that commit's tree; "
        "`verify_record` (ref, no_reuse) reuses one or runs the full verify list in an isolated "
        "checkout, which takes as long as the suite. `land` (ref = the branch, strategy, message, wait, "
        "batch = more branches to land in the same run; origins = ['<branch> <tip-sha>'] for copied "
        "peer branches, fetched first) moves local main to it through a full pass record "
        "(ADR-0062), the only way main moves. It runs in the background and "
        "returns its log at once, queueing up to `wait` seconds for another landing; `land` with "
        "status=true lists recent landings and their state. `land` with recover=<id> finishes "
        "or undoes an interrupted landing, and abort=<id> puts a stalled one's files back, only on the "
        "owner's say-so. "
        "`ok` is false when the command found a problem (a doctor failure). Long output "
        "comes back as a head plus a scratch file path. Setup, export, hooks and process reaping are "
        "person-only: give the owner the command instead.",
    ),
}
