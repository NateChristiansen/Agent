"""`dispatch` action `plan-review`: run one blind review round of a plan, and advance it one step per
call (`plan_review.py` holds the rule and the hash).

A round is two blind, read-only reviewers, Codex and Claude, on the same work order, then one
verifier on their findings. The plan is read once per round. Those bytes are hashed, frozen as
`.agent/plan-reviews/<sha>.md` (mode 0444), and embedded in the work order, so every dispatch of
the round judges exactly the version the gate later checks, never the live file. Neither reviewer
sees the other's findings or the ledger. The launching session's client does not pick them.

Each call reads the latest round of this plan on the current sha and acts on its phase. With no
round, or a complete one, it launches a new round's reviewers: a manager who rejected a finding
with reasons gets a fresh round on the same bytes. In `reviewing`, once both reviewers returned,
their `Blocking:` and `Optional:` items go to the ledger and the verifier launches (`verifying`);
no findings at all completes the round clean. In `verifying`, once the verifier returned, its table
goes to the ledger and the round is `complete`. A reviewer or verifier that ends without a usable
return is relaunched once; a second failure leaves the round incomplete for the owner. Nothing
calls back in: the manager calls `plan-review` again after the supervisor reports a return.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, cast

from .. import dispatch as dispatch_mod
from .. import plan_review, sessions
from .. import plan_review_ledger as ledger_mod
from ..core import AgentError, atomic_write, iso, now
from ..records.narrow import as_dict_or_none, dict_list
from ._shared import Op

ROW = "review"
#: Provider, model and effort of each blind reviewer. Passed explicitly, so a repo
#: `dispatch.model` pin cannot replace them.
REVIEWERS: tuple[tuple[str, str, str], ...] = (
    ("codex", "gpt-6.1-sol", "high"),
    ("claude", "sonnet", "high"),
)
AGAIN = "call `plan-review` again on the same plan after the supervisor reports a return"


def _write_snapshot(path: Path, body: bytes) -> None:
    """Write the frozen plan read-only. One that exists already holds these exact bytes (its
    name is their hash), and a read-only file cannot be replaced on Windows, so it is kept."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.chmod(tmp, 0o444)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def work_order(sha: str, text: str) -> str:
    """The reviewer's whole input: the contract, then the frozen plan between sha-stamped marks."""
    return (
        f"Plan review of sha256 {sha}: a fresh, read-only review of a plan before the owner sees it.\n\n"
        "Review only the plan snapshot below. It is frozen: do not open the plan's live file, which may "
        "have changed since. Change no file; this dispatch is read-only.\n\n"
        "Do not stop with a certain number of findings. Deeply review the entire plan and bring back the "
        "ENTIRE list of findings, to avoid multiple revision rounds as best possible.\n\n"
        "Blocking rules:\n"
        "1. A threshold or bar that is not in the plan's own acceptance criteria is optional, never "
        "blocking.\n"
        "2. Blocking means a failure against the plan's acceptance on a path real use takes or "
        "plausibly would. A failure needing tampering, stacked faults, or input no one produces is "
        "optional unless you show it is likely.\n\n"
        f"Reply: the first line of your returned summary is exactly `{plan_review.AGREE}` or "
        f"`{plan_review.REVISE}`. Revise only for blocking findings. List each blocking finding under "
        "`Blocking:`, and label every other point `Optional:`. Each finding states its path and the "
        "read or probe behind it. Put the whole review in your returned summary.\n\n"
        f"----- BEGIN PLAN SNAPSHOT sha256 {sha} -----\n"
        f"{text.rstrip(chr(10))}\n"
        f"----- END PLAN SNAPSHOT sha256 {sha} -----\n"
    )


# ---------------------------------------------------------------- reading a reviewer's findings

_SECTION = re.compile(
    r"^(?:[-*+•]\s+|\d+[.)]\s+)?[#>*_\s]*(?P<kind>blocking|optional)(?:\s+(?:findings?|issues?|points?))?"
    r"(?:\s*\(\d+\))?[*_\s]*:[*_\s]*(?P<rest>.*)$",
    re.IGNORECASE,
)
_ITEM = re.compile(r"^(?P<indent>\s*)(?:[-*+•]|\d+[.)]|\(\d+\))\s+(?P<text>\S.*)$")
_HEADING = re.compile(r"^(?:#+\s.*|[*_]*[A-Z][\w /-]{0,40}:[*_]*)$")
_EMPTY = frozenset({"none", "none.", "n/a", "nothing", "-", "—"})


def findings_of(summary: str) -> list[str]:
    """Every item under `Blocking:` and `Optional:` in a reviewer's summary, one string each. A
    section line with text after the colon is itself one item; a list item's indented lines and
    sub-bullets belong to it. Another heading, or unindented prose after a blank line, ends the
    section. `none` is no item."""
    items: list[list[str]] = []
    current: list[str] | None = None
    in_section = False
    base: int | None = None
    blank = False
    for line in summary.splitlines():
        stripped = line.strip()
        section = _SECTION.match(stripped)
        if section is not None:
            in_section, base, blank, current = True, None, False, None
            rest = section["rest"].strip()
            item = _ITEM.match(rest)
            rest = item["text"].strip() if item else rest
            if rest.casefold() in _EMPTY:
                in_section = False
            elif rest:
                current = [rest]
                items.append(current)
            continue
        if not in_section:
            continue
        if not stripped:
            blank = True
            continue
        indent = len(line) - len(line.lstrip())
        item = _ITEM.match(line)
        if item is not None and (base is None or indent <= base):
            base = indent if base is None else base
            current = None
            if item["text"].strip().casefold() not in _EMPTY:
                current = [item["text"].strip()]
                items.append(current)
        elif _HEADING.match(stripped) or (blank and indent == 0 and item is None):
            in_section, current = False, None
        elif current is not None:
            current.append(stripped)
        elif stripped.casefold() not in _EMPTY:
            current = [stripped]
            items.append(current)
        blank = False
    return [" ".join(parts) for parts in items]


# ---------------------------------------------------------------- the round record


def _save(root: Path, rec: dict[str, Any]) -> None:
    rec["updated_at"] = iso(now())
    path = plan_review.round_path(root, str(rec["sha256"]), int(rec["round"]))
    atomic_write(path, json.dumps(rec, indent=2, sort_keys=True) + "\n")


def _label(slots: list[dict[str, Any]]) -> str:
    return " and ".join(str(s["dispatch_id"]) for s in slots)


def _next_round(root: Path, plan: str, sha: str) -> int:
    """One past every round this plan file has used, in its records and in its ledger. Each legacy
    record counts as one."""
    mine = plan_review.records_for(root, plan)
    ledger = ledger_mod.load(root, plan)
    used = [len(mine), *(plan_review.round_no(r) for r in mine)]
    used += [int(f.get("round") or 0) for f in ledger_mod.findings(ledger)]
    used += [int(k) for k in ledger["verifiers"] if str(k).isdigit()]
    n = max(used) + 1
    while plan_review.round_path(root, sha, n).exists():  # the same bytes under another plan path
        n += 1
    return n


def _launch_reviewer(root: Path, order: str, provider: str, model: str, effort: str, sid: str | None) -> str:
    from ..verify_record import harness_repo

    repo = harness_repo(root)
    result = dispatch_mod.new(
        root,
        repo=str(repo.name),
        feature="current",
        prompt=order,
        provider=provider,
        row=ROW,
        model=model,
        effort=effort,
        # The plan travels in the work order, so landed main is the right checkout.
        base=repo.default_branch,
        writes=[],
        no_checks=True,
        # The owner's standing rule launches this review without a per-launch preview.
        just_go=True,
        # The plan's own fences are data under review, not commands the reviewer runs.
        allow_unmatched=True,
        choice_reason="plan review: one of two blind reviewers, read-only",
        session_id=sid,
    )
    return str(result["id"])


def _dispatch(root: Path, dispatch_id: str) -> tuple[str, str]:
    """A dispatch's state and returned summary; `missing` when its record cannot be read."""
    from ..dispatch.state import load

    try:
        d = load(root, dispatch_id)
    except Exception:  # noqa: BLE001 — a missing or unreadable dispatch is no return
        return "missing", ""
    summary = str((as_dict_or_none(d.get("result")) or {}).get("summary") or "")
    return str(d.get("state") or ""), summary


# ---------------------------------------------------------------- the action


def dispatch_plan_review(root: Path, *, paths: list[str], session_id: str | None = None) -> Op:
    """Advance the plan at `paths[0]` one step through its review round (module docstring)."""
    if len(paths) != 1:
        raise AgentError("plan-review takes exactly one plan path in `paths`")
    from ..dispatch.guards import area_guard

    sid = sessions.resolve_session_id(root, session_id)
    plan = plan_review.resolve_plan(root, paths[0])
    try:
        raw = plan.read_bytes()  # the one read: everything below uses these bytes
    except OSError as exc:
        raise AgentError(f"cannot read the plan {plan}: {exc}") from exc
    sha = plan_review.digest(raw)
    # Round numbers are per plan file and round files per sha, so both are held: plan, then sha.
    with (
        area_guard(root, f"plan-review:{ledger_mod.plan_key(root, str(plan))}"),
        area_guard(root, f"plan-review-sha:{sha}"),
    ):
        mine = [r for r in plan_review.records(root, sha) if r.get("plan_path") == str(plan)]
        latest = mine[-1] if mine else None
        if latest is not None and not plan_review.is_round(latest):
            seen = plan_review.verdict(root, latest)
            if seen.state == "running":
                return Op(latest, f"review {seen.dispatch_id} of this exact plan is still running; {AGAIN}.")
        elif latest is not None and latest.get("phase") == "reviewing":
            return _reviewing(root, latest, sid)
        elif latest is not None and latest.get("phase") == "verifying":
            return _verifying(root, latest, sid)
        return _launch_round(root, plan, raw, sha, sid)


def _launch_round(root: Path, plan: Path, raw: bytes, sha: str, sid: str | None) -> Op:
    body = plan_review.normalise(raw)
    round_no = _next_round(root, str(plan), sha)
    snapshot = plan_review.reviews_dir(root) / f"{sha}.md"
    _write_snapshot(snapshot, body)
    order = work_order(sha, body.decode("utf-8", errors="replace"))
    reviewers: list[dict[str, Any]] = []
    for provider, model, effort in REVIEWERS:
        try:
            dispatch_id = _launch_reviewer(root, order, provider, model, effort, sid)
        except AgentError as exc:
            if not reviewers:
                raise
            launched = _label(reviewers)
            raise AgentError(
                f"plan review launched {launched} but not its {provider} reviewer: {exc}. "
                f"Cancel {launched}, then call plan-review again."
            ) from exc
        reviewers.append({"dispatch_id": dispatch_id, "provider": provider, "model": model, "effort": effort})
    rec: dict[str, Any] = {
        "sha256": sha,
        "plan_path": str(plan),
        "snapshot": str(snapshot),
        "round": round_no,
        "phase": "reviewing",
        # A label for messages that name "the review"; `reviewers` is what the round reads.
        "dispatch_id": _label(reviewers),
        "reviewers": reviewers,
        "verifier": None,
        "session_id": sid,
        "launched_at": iso(now()),
    }
    _save(root, rec)
    names = " and ".join(f"{r['dispatch_id']} ({r['provider']} {r['model']})" for r in reviewers)
    return Op(
        rec,
        f"plan review round {round_no} launched two blind reviewers, {names}, for {plan.name} "
        f"(sha256 {sha[:12]}). Once both have returned, {AGAIN}: their findings then go to one verifier.",
    )


def _relaunch(root: Path, rec: dict[str, Any], slot: dict[str, Any], sid: str | None) -> str:
    body = Path(str(rec["snapshot"])).read_bytes().decode("utf-8", errors="replace")
    order = work_order(str(rec["sha256"]), body)
    new_id = _launch_reviewer(
        root, order, str(slot["provider"]), str(slot["model"]), str(slot["effort"]), sid
    )
    slot["relaunch_of"] = slot["dispatch_id"]
    slot["dispatch_id"] = new_id
    return new_id


def _stalled(root: Path, rec: dict[str, Any], why: str) -> Op:
    rec["stalled"] = why
    _save(root, rec)
    return Op(
        rec,
        f"plan review round {rec['round']} cannot complete: {why}. It stays incomplete; bring it to "
        "the owner, who decides how to go on.",
    )


def _reviewing(root: Path, rec: dict[str, Any], sid: str | None) -> Op:
    sha = str(rec["sha256"])
    slots = dict_list(rec.get("reviewers"))
    running: list[str] = []
    failed: list[tuple[dict[str, Any], str]] = []
    texts: dict[str, list[str]] = {}
    for slot in slots:
        seen = plan_review.reviewer_verdict(root, str(slot.get("dispatch_id") or ""), sha)
        if seen.state == "running":
            running.append(seen.dispatch_id)
            continue
        if seen.state in ("agree", "revise"):
            found = findings_of(_dispatch(root, seen.dispatch_id)[1])
            if seen.state == "agree" or found:
                texts[seen.dispatch_id] = found
                continue
            failed.append((slot, "it asked for revision but listed no finding"))
            continue
        failed.append((slot, seen.detail))
    twice = [(s, why) for s, why in failed if s.get("relaunch_of")]
    if twice:
        why = "; ".join(
            f"reviewer {s['relaunch_of']} and its relaunch {s['dispatch_id']}: {w}" for s, w in twice
        )
        return _stalled(root, rec, f"{why}, so no usable return")
    if running:
        return Op(
            rec,
            f"plan review round {rec['round']}: reviewer {' and '.join(running)} is still running; "
            f"nothing launched. {AGAIN[0].upper()}{AGAIN[1:]}.",
        )
    relaunched: list[str] = []
    for slot, _ in failed:
        relaunched.append(_relaunch(root, rec, slot, sid))
        # Saved per relaunch, so a later launch failure cannot lead a retry to relaunch this slot again.
        rec["dispatch_id"] = _label(slots)
        rec["reviewers"] = slots
        _save(root, rec)
    if relaunched:
        why = "; ".join(f"{s['relaunch_of']}: {w}" for s, w in failed)
        return Op(
            rec,
            f"plan review round {rec['round']}: relaunched {', '.join(relaunched)} once ({why}). "
            f"When it returns, {AGAIN}.",
        )
    return _to_verifier(root, rec, slots, texts, sid)


def _to_verifier(
    root: Path, rec: dict[str, Any], slots: list[dict[str, Any]], texts: dict[str, list[str]], sid: str | None
) -> Op:
    plan, round_no = str(rec["plan_path"]), int(rec["round"])
    if "findings" not in rec:
        ids: list[str] = []
        for slot in slots:
            found = texts.get(str(slot["dispatch_id"])) or []
            if found:
                added = ledger_mod.add_findings(
                    root,
                    plan,
                    plan_sha=str(rec["sha256"]),
                    round_no=round_no,
                    dispatch_id=str(slot["dispatch_id"]),
                    client=str(slot["provider"]),
                    texts=found,
                )
                ids += [str(e["id"]) for e in added]
        rec["findings"] = ids
        _save(root, rec)
    if not rec["findings"]:
        rec["phase"], rec["blocking"] = "complete", []
        _save(root, rec)
        return Op(
            rec,
            f"plan review round {round_no} is complete and clean: neither reviewer raised a finding. "
            "ExitPlanMode passes on these bytes.",
        )
    # A call that failed after launching the verifier left it on the ledger: adopt that one.
    held = as_dict_or_none(ledger_mod.load(root, plan)["verifiers"].get(str(round_no)))
    if held is None:
        held = {
            "dispatch_id": ledger_mod.launch_verifier(
                root, plan_path=plan, round_no=round_no, session_id=sid
            )["id"]
        }
    rec["verifier"] = {"dispatch_id": str(held["dispatch_id"])}
    rec["phase"] = "verifying"
    _save(root, rec)
    return Op(
        rec,
        f"plan review round {round_no}: {len(rec['findings'])} finding(s) in the ledger "
        f"({', '.join(rec['findings'])}); verifier {held['dispatch_id']} launched. When it returns, {AGAIN}.",
    )


def _verifying(root: Path, rec: dict[str, Any], sid: str | None) -> Op:
    plan, round_no = str(rec["plan_path"]), int(rec["round"])
    verifier = as_dict_or_none(rec.get("verifier")) or {}
    vid = str(verifier.get("dispatch_id") or "")
    state, summary = _dispatch(root, vid)
    if state in plan_review.RUNNING_STATES:
        return Op(
            rec,
            f"plan review round {round_no}: verifier {vid} is still running; nothing launched. "
            f"{AGAIN[0].upper()}{AGAIN[1:]}.",
        )
    if state == "done":
        missing = ledger_mod.record_verifier(root, plan, round_no=round_no, summary=summary, dispatch_id=vid)
    else:
        missing = [str(f) for f in cast("list[object]", rec.get("findings") or [])]
    ledger = ledger_mod.load(root, plan)
    disputes = ledger_mod.disputes(ledger)
    if not missing:
        kept = [
            str(e["id"]) for e in ledger_mod.verified_blocking(ledger_mod.round_findings(ledger, round_no))
        ]
        rec["phase"], rec["blocking"] = "complete", kept
        rec.pop("stalled", None)
        _save(root, rec)
        if kept:
            tail = (
                f"verified blocking: {', '.join(kept)}. ExitPlanMode stays shut on these bytes. Revise the plan "
                "and record each disposition (`plan-ledger`), or reject a finding with reasons; then call "
                "`plan-review` again for a fresh round."
            )
        else:
            tail = "no finding is verified blocking. ExitPlanMode passes on these bytes."
        return Op(
            {**rec, "disputes": disputes},
            f"plan review round {round_no} is complete: {tail} Disputes for the owner: "
            f"{_disputes_text(disputes)}.",
        )
    why = f"verifier {vid} ended {state or 'unknown'} leaving {', '.join(missing)} without a result"
    if verifier.get("relaunch_of"):
        return _stalled(root, rec, f"the relaunched {why}; disputes: {_disputes_text(disputes)}")
    new = ledger_mod.launch_verifier(root, plan_path=plan, round_no=round_no, session_id=sid, replaces=vid)
    rec["verifier"] = {"dispatch_id": str(new["id"]), "relaunch_of": vid}
    _save(root, rec)
    return Op(
        rec,
        f"plan review round {round_no}: {why}; relaunched the verifier once as {new['id']}. When it "
        f"returns, {AGAIN}.",
    )


def _disputes_text(disputes: list[dict[str, Any]]) -> str:
    return ", ".join(f"{d['finding']['id']} ({d['kind']})" for d in disputes) or "none"
