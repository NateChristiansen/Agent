"""Procs, verify and land ops: the CLI-only commands a model runs, moved behind ops so the
`harness` MCP tool and the CLI share one implementation (see ADR-0005)."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .. import procs as procs_mod
from .. import sessions as sessions_mod
from ..core import EXIT_NOT_FOUND, EXIT_OK, AgentError
from ..records.narrow import dict_list
from ._shared import Op


def link(root: Path, *, target: str) -> Op:
    """An owner-facing vault URI. Accept a vault-relative path, including Windows separators;
    encode every reserved character so a filename cannot become a query or fragment."""
    from urllib.parse import quote

    from ..dispatch.opener import vault_name

    path = PurePosixPath(target.replace("\\", "/"))
    if not target.strip() or path.is_absolute() or PureWindowsPath(target).drive or ".." in path.parts:
        raise AgentError("link needs a vault-relative file path without parent traversal")
    if str(path) == ".":
        raise AgentError("link needs a vault-relative file path")
    url = f"obsidian://open?vault={quote(vault_name(root), safe='')}&file={quote(str(path), safe='')}"
    return Op({"url": url, "path": str(path)}, url)


_INTERPRETER = re.compile(r"^\S*/(?:Python|python3?(?:\.\d+)?|node)\s+")


def _short(command: str) -> str:
    """Drop a leading interpreter path: `.../Python.app/.../Python` before
    `/x/bin/agent dispatch run 001-01` tells you nothing until the part after the interpreter."""
    return _INTERPRETER.sub("", command, count=1)


def _proc_line(r: dict[str, Any]) -> str:
    flags: list[str] = []
    if r["kept"]:
        flags.append("KEPT")
    if r["orphan"]:
        flags.append("ORPHAN")
    if r["stuck_supervisor"]:
        flags.append("STUCK SUPERVISOR")
    if r.get("supervisor_exiting"):
        flags.append("supervisor exiting")
    # A supervisor is detached from its manager on purpose, so its being reparented says nothing.
    if r["escaped"] and not r["orphan"] and not r["supervisor_of"]:
        flags.append("escaped")
    live = "live" if r["owner_live"] else "gone"
    tail = f"  [{', '.join(flags)}]" if flags else ""
    return f"{r['pid']:>7}  {r['elapsed']:>11}  {r['owner']:<22} ({live})  {_short(r['command'])[:80]}{tail}"


def procs_list(root: Path) -> Op:
    """Every harness-owned process, by owner, with orphans marked."""
    rows = procs_mod.classify(root, procs_mod.snapshot())
    if not rows:
        return Op(rows, "no harness-owned processes running")
    orphan_count = sum(1 for r in rows if r["orphan"])
    head = f"{len(rows)} harness-owned process(es), {orphan_count} orphaned"
    return Op(rows, "\n".join([head, *(_proc_line(r) for r in rows)]))


def verify_record(root: Path, *, rev: str, reuse: bool = True) -> Op:
    """Reuse the full pass record for `rev`'s tree, or run every verify command in an isolated
    checkout and write one (see ADR-0041). A run takes as long as the full suite."""
    from .. import verify_record as records

    data = records.record(root, rev, reuse=reuse)
    verb = "reused" if data["reused"] else "recorded"
    text = f"{verb} {records.pointer(data)} for {data['sha'][:12]}"
    return Op({k: v for k, v in data.items() if k != "environment"}, text)


def verify_show(root: Path, *, rev: str) -> Op:
    """The record that applies to `rev`'s tree now, or why none does; exits non-zero when none."""
    from .. import verify_record as records

    data = records.show(root, rev)
    if data["applies"]:
        record = data["record"]
        text = f"{data['sha'][:12]}: {records.pointer(record)}, recorded {record['completed']}"
    else:
        text = data["why"]
    if data["kept"]:
        text += f"\n{len(data['kept'])} record(s) kept for tree {data['tree'][:12]}"
    return Op(data, text, code=EXIT_OK if data["applies"] else EXIT_NOT_FOUND)


def tidy(root: Path, *, apply: bool = False, wait_s: float = 60.0, feature: int | None = None) -> Op:
    """Run cleanup in a detached process; return its log if it outlasts the MCP wait. The
    returned lists are bounded; the result file holds them all."""
    import json
    import subprocess
    import sys
    import uuid

    from .. import scratch
    from ..core import EXIT_EXTERNAL

    directory = scratch.ensure(root) / "harness"
    directory.mkdir(parents=True, exist_ok=True)
    ident = uuid.uuid4().hex[:12]
    log = directory / f"tidy-{ident}.log"
    result = directory / f"tidy-{ident}.json"
    argv = [sys.executable, "-u", "-c", "from agent_cli.entry import main; raise SystemExit(main())"]
    argv += ["--root", str(root), "--json", "tidy"]
    if apply:
        argv.append("--apply")
    if feature is not None:
        argv += ["--feature", str(feature)]
    with result.open("wb") as output, log.open("ab") as stream:
        proc = subprocess.Popen(
            argv,
            stdout=output,
            stderr=stream,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    data = {"pid": proc.pid, "log": str(log), "result": str(result), "dry_run": not apply}
    try:
        code = proc.wait(timeout=max(0.0, min(wait_s, 60.0)))
    except subprocess.TimeoutExpired:
        return Op(
            {**data, "detached": True}, f"tidy continues in the background; log: {log}; result: {result}"
        )
    if code:
        return Op({**data, "detached": False}, f"tidy failed; log: {log}", code=EXIT_EXTERNAL)
    from ..tidy import EXAMPLES, TIDY_LISTS, bounded, format_result

    listing = json.loads(result.read_text(encoding="utf-8"))
    long = any(len(listing.get(key) or []) > EXAMPLES for key in TIDY_LISTS)
    listing = bounded(listing, TIDY_LISTS, result if long else None)
    return Op({**listing, **data, "detached": False}, format_result(listing))


def land(
    root: Path,
    *,
    branch: str | None = None,
    strategy: str | None = None,
    message: str | None = None,
    recover: str | None = None,
    abort: str | None = None,
    wait_s: float = 3600.0,
    detach: bool = False,
    status: bool = False,
    op: str | None = None,
    extra: list[str] | None = None,
    origins: list[str] | None = None,
) -> Op:
    """Move local main to `branch` through a full pass record (see ADR-0041), finish or undo an
    interrupted landing (`recover`), or put a stalled one's files back (`abort`). A landing
    queues for the land lock up to `wait_s`. `detach` runs it in the background and returns its
    log at once (a landing outlasts an MCP call);
    `status` lists recent landings. `extra` lands more branches in the same batch (land.py).
    `origins` carries '<branch> <tip-sha>' entries for the original branches of copied work."""
    from .. import land as land_mod
    from .. import land_recover

    if status:
        return _land_status(root)
    if detach and branch:
        return _land_detached(root, branch, strategy, message, wait_s, extra or [], origins or [])
    if recover:
        journal = land_recover.recover(root, recover)
        return Op(journal, f"land {recover}: {journal['state']}")
    if abort:
        journal = land_recover.abort(root, abort)
        return Op(journal, f"land {abort}: {journal['state']} ({journal.get('why', '')})")
    if not branch:
        raise AgentError("name the branch to land, or pass a landing id to recover or abort")
    from .. import land_journal as jn

    main = land_mod.main_of(root)
    try:
        journal = land_mod.land(
            root,
            branch,
            strategy=strategy,
            message=message,
            wait_s=wait_s,
            op=op,
            extra=extra,
            origins=origins,
        )
    except Exception as exc:
        jn.op_update(main, op, state="failed", why=str(exc)[:500])
        _tell_asker(root, op, f"landing {op} of {branch} failed: {str(exc)[:300]}")
        raise
    jn.op_update(main, op, state="done", journal=journal["id"], new_main=journal["cand"])
    how = "reused" if journal["record_reused"] else "ran"
    names = ", ".join(str(m.get("branch")) for m in dict_list(journal.get("members"))) or branch
    from .. import land_warning

    kind = journal.get("record_kind") or "full"
    text = (
        f"landed {names} ({journal['strategy']}): main {journal['old'][:12]} -> {journal['cand'][:12]}; "
        f"{how} {'pass' if kind == 'full' else kind} record {journal['record']}"
    )
    if warning := land_warning.text(journal.get("restart")):
        text += "\n" + warning
    _tell_asker(root, op, f"landing {op}: {text}")
    return Op(journal, text)


def _tell_asker(root: Path, op: str | None, message: str) -> None:
    """Wake the requester with a detached landing outcome. Use agent-land as sender so it rings rather
    than queueing as a self-note. Never raise: notification failure cannot undo a landing.
    """
    asker = os.environ.get("AGENT_LAND_SESSION") or ""
    if not op or not asker:
        return
    import contextlib

    from .peer_delivery import deliver

    # A failed notice must not fail or undo the landing; `land status` still has the outcome.
    with contextlib.suppress(Exception):
        deliver(root, target=asker, frm="agent-land", message=message)


def _land_detached(
    root: Path,
    branch: str,
    strategy: str | None,
    message: str | None,
    wait_s: float,
    extra: list[str],
    origins: list[str],
) -> Op:
    """Start `agent land` in its own process and return its log path and pid."""
    import subprocess
    import sys

    from .. import land as land_mod
    from .. import land_journal as jn

    main = land_mod.main_of(root)
    op_id = jn.new_id()
    logs = jn.land_dir(main)
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"detached-{op_id}.log"
    jn.op_update(main, op_id, state="queued", branch=branch, log=str(log), ts=jn.now())
    argv = [sys.executable, "-c", "from agent_cli.entry import main; raise SystemExit(main())"]
    argv += ["--root", str(root), "land", branch, "--wait", f"{wait_s:g}", "--op", op_id]
    argv += ["--strategy", strategy] if strategy else []
    argv += ["--message", message] if message else []
    argv += [arg for other in extra for arg in ("--with", other)]
    argv += [arg for origin in origins for arg in ("--origins", origin)]
    env = dict(os.environ)
    try:  # who asked for the landing: its task notice to that session is queued, not rung
        env["AGENT_LAND_SESSION"] = sessions_mod.resolve_session_id(root, None)
    except AgentError:
        env.pop("AGENT_LAND_SESSION", None)
    with log.open("ab") as stream:
        proc = subprocess.Popen(  # noqa: S603 — our own CLI, argv built above
            argv,
            stdout=stream,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    jn.op_update(main, op_id, pid=proc.pid)
    data = {"detached": True, "op": op_id, "branch": branch, "pid": proc.pid, "log": str(log)}
    return Op(data, f"landing {branch} in the background as {op_id} (pid {proc.pid}); check with land status")


def _land_status(root: Path) -> Op:
    """The most recent landings, newest first, and any still unfinished."""
    from .. import land as land_mod
    from .. import land_journal as jn

    main = land_mod.main_of(root)
    journals = jn.every(main)[-5:][::-1]
    rows = [
        {k: j.get(k) for k in ("id", "state", "branch", "strategy", "record", "record_reused", "ts")}
        for j in journals
    ]
    ops = [_settled(o) for o in jn.ops_recent(main)]
    lines = [
        f"op {o.get('id')} {o.get('state')} {o.get('branch')}" + (f": {o['why']}" if o.get("why") else "")
        for o in ops
    ]
    lines += [f"{r['id']} {r['state']} {r['branch']}" for r in rows]
    return Op({"operations": ops, "landings": rows}, "\n".join(lines) or "no landings")


def _settled(op: dict[str, Any]) -> dict[str, Any]:
    """An unfinished operation whose process is gone did not reach its own failure note (it died
    before `land` started, say on a bad root): report it stopped, with its log's last line."""
    from ..core import is_alive

    if op.get("state") in ("done", "failed") or is_alive(op.get("pid")):
        return op
    try:
        tail = Path(str(op.get("log") or "")).read_text(encoding="utf-8").strip().splitlines()[-1:]
    except OSError:
        tail = []
    return {**op, "state": "stopped", "why": tail[0][:500] if tail else "the landing process is gone"}
