# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Any
from .. import features as features_mod
from .. import repos as repos_mod
from ..core import iso, now
from ..records.dispatch import result_of
from ..records.narrow import as_dict, dict_list
from . import conventions as conventions_mod
from . import spend, write_scope
from .prompt import verify_return
from .return_schema import LEGACY_RETURN_FIELDS
from .state import envelope_return, file, one_line, save, store_result
from .telemetry_events import note_cost_evidence
from .worktrees import git_commits, git_files

BUDGET_REASONS = ("budget_exhausted", "error_max_budget_usd", "max_budget")
MAX_TURNS_REASONS = ("error_max_turns", "max_turns", "max_tool_items")


def _hit_budget(out: dict[str, Any]) -> bool:
    reason = str(out.get("terminal_reason") or "")
    return reason in BUDGET_REASONS


def _hit_max_turns(out: dict[str, Any]) -> bool:
    reason = str(out.get("terminal_reason") or "")
    return reason in MAX_TURNS_REASONS


def evaluate(root: Path, d: dict[str, Any], rp: repos_mod.Repo | None = None) -> dict[str, Any]:
    out = _decide(root, d, rp)
    if out["state"] in ("done", "question", "blocked") and (broken := write_scope.broken_read_only(d)):
        out["state"] = "failed"
        out["auto_resume"] = None
        out["error"] = broken
    return out


def _decide(root: Path, d: dict[str, Any], rp: repos_mod.Repo | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "state": "failed",
        "result": None,
        "error": None,
        "cost_usd": 0.0,
        "num_turns": 0,
        "auto_resume": None,
    }
    code = d.get("exit_code")
    timed_out = bool(d.get("timed_out"))
    envelope = d.get("last_result") or None
    if envelope is None and float(d.get("estimated_cost_usd") or 0.0) > 0:
        out["cost_usd"] = float(d["estimated_cost_usd"])
        out["cost_source"] = "local-estimate"
        out["cost_evidence"] = "estimate"
    ret: Any = None
    problems: list[str] = []
    if envelope is not None:
        out["cost_usd"] = float(envelope.get("total_cost_usd") or 0.0)
        if envelope.get("cost_source") in ("local-estimate", "unknown"):
            out["cost_source"] = envelope["cost_source"]
        if envelope.get("cost_source") == "local-estimate":
            out["cost_evidence"] = "estimate"
        elif envelope.get("cost_source") is None and envelope.get("total_cost_usd") is not None:
            out["cost_evidence"] = "provider"
            out["cost_session_id"] = envelope.get("session_id") or d.get("session_id")
        out["num_turns"] = int(envelope.get("num_turns") or 0)
        out["terminal_reason"] = envelope.get("terminal_reason") or envelope.get("subtype")
        denials = envelope.get("permission_denials")
        if denials:
            out["permission_denials"] = denials
        env_sid = envelope.get("session_id")
        if env_sid and env_sid != d["session_id"]:
            problems.append(f"result session_id {env_sid} != {d['session_id']}")
        ret = envelope_return(d, envelope)
        if ret is None:
            problems.append(
                f"the worker's last result ({envelope.get('subtype')}) carried no structured output"
            )
    else:
        problems.append("the worker produced no result")
        err_tail = _tail(file(root, d["id"], ".err"), 600, since=d.get("err_offset"))
        out["error"] = f"no result; stderr: {err_tail}" if err_tail else "no result from the worker"
    if ret is not None:
        legacy_denied = [
            str(row.get("cmd") or "(unnamed command)")
            for row in dict_list(ret.get("commands"))
            if "DENIED" in f"{row.get('exit', '')} {row.get('status', '')}".upper()
        ]
        if legacy_denied:
            out["legacy_denials"] = [*(d.get("legacy_denials") or []), *legacy_denied]
        ret = {k: v for k, v in ret.items() if k not in LEGACY_RETURN_FIELDS}
        problems += verify_return(d, ret)
    valid = ret is not None and (not problems)
    if valid and ret is not None:
        out["result"] = dict(ret)
        out["returned"] = True
    if (facts := git_facts(d)) is not None:
        out["git"] = facts
    if not valid and (not out["error"]):
        out["error"] = "; ".join(problems) or "invalid return"
    if ret is not None and any(("nonce mismatch" in p for p in problems)):
        out["state"] = "failed"
        return out
    if d["state"] == "cancelled":
        out["state"] = "cancelled"
        return out
    if d.get("stop_reason") == "cleanup_failed":
        out["state"] = "failed"
        out["error"] = "worker cleanup failed; process exit is unconfirmed (see dispatch stderr)"
        return out
    if out.get("terminal_reason") == "degenerate_output":
        out["auto_resume"] = None
        out["state"] = "blocked"
        out["result"] = out["result"] or _synthesize(
            d,
            "the worker streamed degenerate output and was stopped",
            next_step="launch the task fresh, or use the other provider",
        )
        out["error"] = (
            f"Codex streamed degenerate output (whitespace flood or oversized text) and was interrupted; see the dispatch stderr, then relaunch {d['id']}'s task fresh, or on the other provider"
        )
        return out
    if timed_out:
        out["auto_resume"] = "timeout"
        out["state"] = "blocked"
        out["result"] = out["result"] or _synthesize(d, "the time limit was hit before a return")
        if valid:
            out["error"] = None
            _apply_conventions(d, out, rp)
        else:
            out["error"] = f"timed out after {d['timeout_minutes']} minutes"
        return out
    if _hit_budget(out):
        if valid and ret is not None and (ret["status"] == "done"):
            out["state"] = "done"
            _apply_conventions(d, out, rp)
            return out
        out["auto_resume"] = None
        out["state"] = "blocked"
        out["result"] = out["result"] or _synthesize(d, "the budget was exhausted before a return")
        out["error"] = (
            f"budget ${float(d['budget_usd']):.2f} reached; resume {d['id']} with the dispatch tool, action resume, a higher `budget_usd` and a `budget_reason`"
        )
        if valid:
            _apply_conventions(d, out, rp)
        return out
    if _hit_max_turns(out):
        cap = int(d.get("max_turns") or 0)
        if valid and ret is not None and (ret["status"] == "done"):
            out["state"] = "done"
            _apply_conventions(d, out, rp)
            return out
        out["auto_resume"] = None
        out["state"] = "blocked"
        if out.get("terminal_reason") == "max_tool_items":
            items = as_dict(d.get("options")).get("max_tool_items") or "max_tool_items"
            out["result"] = out["result"] or _synthesize(d, "the tool-item cap was hit before a return")
            out["error"] = (
                f"Codex tool-item cap {items} reached; raise `dispatch.max_tool_items` in agent.yaml or narrow the task, then resume {d['id']} with the dispatch tool, action resume, and an `answer`"
            )
        else:
            out["result"] = out["result"] or _synthesize(d, "the turn cap was hit before a return")
            out["error"] = (
                f"turn cap {cap or 'max_turns'} reached; raise `dispatch.max_turns` in agent.yaml or narrow the task, then resume {d['id']} with the dispatch tool, action resume, and an `answer`"
            )
        if valid:
            _apply_conventions(d, out, rp)
        return out
    if code == 0:
        if not valid or ret is None:
            out["state"] = "failed"
            return out
        out["state"] = ret["status"]
        if ret["status"] == "question" and out.get("permission_denials"):
            out["state"] = "blocked"
            out["error"] = (
                f"stopped on {len(out['permission_denials'])} denied command(s). Read each denial first: a work order that names a path or command outside the worker's grants is fixed by correcting the work order and answering with the corrected step; only a command the work needs and the grants miss calls for widening the repo's dispatch.allowed_tools in agent.yaml. Then resume."
            )
        _apply_conventions(d, out, rp)
        return out
    out["state"] = "failed"
    if valid:
        out["error"] = (
            f"{_worker(d)} exited {code} despite a structurally valid return; failed on the non-zero exit"
        )
    else:
        out["error"] = f"{_worker(d)} exited {code}: {out['error']}"
    return out


def _apply_conventions(d: dict[str, Any], out: dict[str, Any], rp: repos_mod.Repo | None) -> None:
    try:
        gated = conventions_mod.gate(d, rp, out.get("result"))
    except ValueError as exc:
        out["conventions"] = {"error": str(exc)}
        return
    if gated is None:
        return
    out["conventions"] = gated
    lines = conventions_mod.block_lines(gated)
    if out["state"] == "done" and lines:
        out["state"] = "blocked"
        result = dict(out.get("result") or {})
        result["next_steps"] = lines + list(result.get("next_steps") or [])
        out["result"] = result
        out["error"] = f"convention gate: {len(lines)} finding(s); see next_steps"


def _tail(path: Path, n: int, *, since: Any = None) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        stream.seek(since if isinstance(since, int) and since >= 0 else 0)
        text = stream.read().decode("utf-8", errors="replace").strip()
    return one_line(text[-n:])


def _worker(d: dict[str, Any]) -> str:
    return "the Codex worker" if d.get("provider") == "codex" else "claude"


def git_facts(d: dict[str, Any]) -> dict[str, Any] | None:
    wt = Path(str(d.get("worktree") or ""))
    if not d.get("worktree") or not wt.exists():
        return None
    return {"commits": git_commits(wt, d["base"]), "files": git_files(wt, d["base"])}


def _synthesize(
    d: dict[str, Any], reason: str, *, next_step: str = "resume with a larger budget, or narrow the task"
) -> dict[str, Any]:
    wt = Path(d["worktree"])
    since = str(d.get("start_sha") or d["base"])
    commits = git_commits(wt, since) if wt.exists() else []
    return {
        "nonce": d["nonce"],
        "status": "blocked",
        "summary": f"synthesized by the supervisor: {reason}. {len(commits)} commit(s) on {d['branch']}.",
        "open_questions": [],
        "next_steps": [next_step],
        "notes": reason,
    }


def queue_auto_resume(
    root: Path, d: dict[str, Any], feat: features_mod.Feature, outcome: dict[str, Any], reason: str
) -> dict[str, Any]:
    budget = float(d["budget_usd"])
    text = f"the {d['timeout_minutes']:g} minute time limit was hit; same budget, same time again. commit what works, then return."
    ts = iso(now())
    result = result_of(outcome)
    if d.get("cost_accounted_attempt") != d["attempt"]:
        spend.add_attempt_cost(d, outcome, root)
        note_cost_evidence(d, outcome)
        d["cost_accounted_attempt"] = d["attempt"]
    d["thread"].append(
        {
            "ts": ts,
            "from": "worker",
            "text": one_line(result.get("summary") or outcome.get("error") or "(no return)"),
            "status": outcome["state"],
            "attempt": d["attempt"],
            "cost_usd": spend.attempt_cost(d, d["attempt"], outcome.get("cost_usd")),
        }
    )
    d["thread"].append({"ts": ts, "from": "manager", "text": text, "auto": True})
    d["auto_resumes"].append(
        {"attempt": d["attempt"], "reason": reason, "from_budget": budget, "to_budget": budget}
    )
    d["num_turns"] = int(outcome.get("num_turns") or 0)
    _close_attempt(root, {**d, "state": outcome["state"]}, reason)
    d["verification"] = None
    d["terminal_committed"] = False
    d["attempt"] += 1
    d["state"] = "queued"
    store_result(d, dict(result) or None, returned=bool(outcome.get("returned")))
    if "git" in outcome:
        d["git"] = outcome["git"]
    d["error"] = outcome.get("error")
    return save(root, d)


def _close_attempt(root: Path, ended: dict[str, Any], reason: str) -> None:
    from . import telemetry_events

    telemetry_events.terminal(root, ended)
    telemetry_events.transition(root, ended, "ended", reason=reason, auto_resume=True)
