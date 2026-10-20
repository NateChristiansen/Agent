# ruff: noqa
from __future__ import annotations
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from .. import procs as procs_mod
from .. import sessions as sessions_mod
from ..core import EXIT_NOT_FOUND, EXIT_OK, AgentError
from ..records.narrow import as_dict, dict_list
from ._shared import Op


def link(root: Path, *, target: str) -> Op:
    from urllib.parse import quote
    from ..dispatch.opener import vault_name

    path = PurePosixPath(target.replace("\\", "/"))
    if not target.strip() or path.is_absolute() or PureWindowsPath(target).drive or (".." in path.parts):
        raise AgentError("link needs a vault-relative file path without parent traversal")
    if str(path) == ".":
        raise AgentError("link needs a vault-relative file path")
    url = f"obsidian://open?vault={quote(vault_name(root), safe='')}&file={quote(str(path), safe='')}"
    return Op({"url": url, "path": str(path)}, url)


_INTERPRETER = re.compile("^\\S*/(?:Python|python3?(?:\\.\\d+)?|node)\\s+")


def _short(command: str) -> str:
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
    if r["escaped"] and (not r["orphan"]) and (not r["supervisor_of"]):
        flags.append("escaped")
    live = "live" if r["owner_live"] else "gone"
    tail = f"  [{', '.join(flags)}]" if flags else ""
    return f"{r['pid']:>7}  {r['elapsed']:>11}  {r['owner']:<22} ({live})  {_short(r['command'])[:80]}{tail}"


def procs_list(root: Path) -> Op:
    rows = procs_mod.classify(root, procs_mod.snapshot())
    if not rows:
        return Op(rows, "no harness-owned processes running")
    orphan_count = sum((1 for r in rows if r["orphan"]))
    head = f"{len(rows)} harness-owned process(es), {orphan_count} orphaned"
    return Op(rows, "\n".join([head, *(_proc_line(r) for r in rows)]))


def handoffs_migrate(
    root: Path, *, feature: str | None = None, apply: bool = False, plan_hashes: dict[str, str] | None = None
) -> Op:
    from .. import handoff_views
    from .status import status_render

    row = handoff_views.migrate(root, feature=feature, apply=apply, expect_sha256=plan_hashes)
    if apply:
        status_render(root)
    lines = [
        f"{p['path']} {p['sha256']} {('changes' if p['changed'] else 'unchanged')}" for p in row["plans"]
    ]
    verb = "applied" if apply else "previewed"
    return Op(row, "\n".join([*lines, f"{verb} {row['changed']} of {row['count']} handoff view migrations"]))


def verify_record(root: Path, *, rev: str, reuse: bool = True) -> Op:
    from .. import verify_record as records

    data = records.record(root, rev, reuse=reuse)
    verb = "reused" if data["reused"] else "recorded"
    text = f"{verb} {records.pointer(data)} for {data['sha'][:12]}"
    return Op({k: v for k, v in data.items() if k != "environment"}, text)


def verify_show(root: Path, *, rev: str) -> Op:
    from .. import verify_record as records

    data = records.show(root, rev)
    if data["applies"]:
        record = data["record"]
        text = f"{data['sha'][:12]}: {records.pointer(record)}, recorded {record['completed']}"
    else:
        text = data["why"]
    if data["kept"]:
        text += f"\n{len(data['kept'])} record(s) kept for tree {data['tree'][:12]}"
    if small := data.get("small"):
        anchor = str(as_dict(small.get("anchor")).get("sha") or "")[:12]
        text += f"\nsmall-tier link {small.get('links')} after full pass {anchor} (run {small.get('run_id')}); it certified the landing, never a push"
    return Op(data, text, code=EXIT_OK if data["applies"] else EXIT_NOT_FOUND)


def verify_full_if_due(root: Path, *, check: bool = False) -> Op:
    from .. import land_tier
    from .. import verify_record as records

    is_due, why = land_tier.due(root)
    if not is_due:
        return Op({"due": False, "why": why}, f"not due: {why}")
    if check:
        return Op({"due": True, "why": why}, f"due: {why}", code=EXIT_NOT_FOUND)
    data = records.record(root, "refs/heads/main")
    text = f"{why}; {('reused' if data['reused'] else 'recorded')} {records.pointer(data)}"
    return Op({"due": True, "why": why, **{k: v for k, v in data.items() if k != "environment"}}, text)


def tidy(root: Path, *, apply: bool = False, wait_s: float = 60.0) -> Op:
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
    with result.open("wb") as output, log.open("ab") as stream:
        proc = subprocess.Popen(
            argv, stdout=output, stderr=stream, stdin=subprocess.DEVNULL, start_new_session=True
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
    from ..tidy import format_result

    listing = json.loads(result.read_text(encoding="utf-8"))
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
    names = ", ".join((str(m.get("branch")) for m in dict_list(journal.get("members")))) or branch
    from .. import land_warning

    kind = journal.get("record_kind") or "full"
    text = f"landed {names} ({journal['strategy']}): main {journal['old'][:12]} -> {journal['cand'][:12]}; {how} {('pass' if kind == 'full' else kind)} record {journal['record']}"
    if journal.get("full_because"):
        text += f" (full suite: {journal['full_because']})"
    if kind != "notes-only":
        links = journal.get("small_links") or 0
        text += f"\n{links} small link{('' if links == 1 else 's')} since the last full pass"
    if warning := land_warning.text(journal.get("restart")):
        text += "\n" + warning
    _tell_asker(root, op, f"landing {op}: {text}")
    return Op(journal, text)


def _tell_asker(root: Path, op: str | None, message: str) -> None:
    asker = os.environ.get("AGENT_LAND_SESSION") or ""
    if not op or not asker:
        return
    import contextlib
    from .peer_delivery import deliver

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
    try:
        env["AGENT_LAND_SESSION"] = sessions_mod.resolve_session_id(root, None)
    except AgentError:
        env.pop("AGENT_LAND_SESSION", None)
    with log.open("ab") as stream:
        proc = subprocess.Popen(
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
    from ..core import is_alive

    if op.get("state") in ("done", "failed") or is_alive(op.get("pid")):
        return op
    try:
        tail = Path(str(op.get("log") or "")).read_text(encoding="utf-8").strip().splitlines()[-1:]
    except OSError:
        tail = []
    return {**op, "state": "stopped", "why": tail[0][:500] if tail else "the landing process is gone"}
