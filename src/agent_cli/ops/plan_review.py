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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

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


def write_snapshot(path: Path, body: bytes) -> None:
    """Write frozen review input (a plan, an acceptance) read-only. One that exists already holds these exact bytes (its
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
        f"{plan_review.exhaustive('plan')}\n\n"
        "Blocking rules:\n"
        f"{plan_review.blocking_rules(plan_review.BAR, plan_review.ACCEPTANCE)}\n\n"
        f"Reply: {plan_review.VERDICT_FIELD} Revise only for blocking findings. List each blocking "
        "finding under `Blocking:`, and label every other point `Optional:`. Each finding states its "
        "path and the read or probe behind it. Put the whole review in your returned summary.\n\n"
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
# A bullet or number is dropped from the item's text; a label such as `B1 —` or `P2:` is kept.
_ITEM = re.compile(
    r"^(?P<indent>\s*)(?:(?:[-*+•]|\d+[.)]|\(\d+\))\s+|(?=[*_]*[A-Z]{1,2}-?\d{1,3}[*_]*(?:[:.)]|\s+[—–-])\s))"
    r"(?P<text>\S.*)$"
)
_HEADING = re.compile(r"^(?:(?P<hashes>#+)\s.*|[*_]*(?P<label>[A-Z][\w /-]{0,40}):[*_]*)$")
# Labels a reviewer puts inside one finding; unindented, they still do not end the section.
_FIELD = frozenset(
    {
        "details",
        "evidence",
        "file",
        "fix",
        "impact",
        "location",
        "path",
        "probe",
        "read",
        "reason",
        "risk",
        "severity",
        "suggestion",
        "why",
    }
)
_EMPTY = re.compile(
    r"^(?:(?:none|nothing|nil|n/?a|-+|—|–)"
    r"(?:\s+(?:identified|found|noted|raised|seen|spotted|blocking|optional|to report|to flag|so far))*"
    r"|no\s+(?:(?:other|further|additional|new|remaining)\s+)?(?:(?:blocking|optional)\s+)?"
    r"(?:findings?|issues?|points?|problems?|concerns?|blockers?)"
    r"(?:\s+(?:identified|found|noted|raised|seen|spotted|to report|so far))*)"
    r"(?:\s*[.;:!—–-]+(?:\s(?P<reason>.*))?)?$"
)
_CITES = re.compile(r"[/\\`]|\w\.\w|:\d|\b(?:probe|path|file)s?\b")


def _empty(text: str) -> bool:
    """`text` says the section has nothing in it: `none`, `None found.`, `(none)`, `No blocking
    findings.`, or one of those with a short reason that cites no file, path or probe."""
    match = _EMPTY.match(text.strip().strip("*_()[]` ").casefold())
    if match is None:
        return False
    reason = match["reason"] or ""
    return len(reason.split()) <= 10 and _CITES.search(reason) is None


def _heading_level(text: str) -> int:
    match = re.match(r"#+(?=\s)", text)
    return len(match[0]) if match else 0


def findings_of(summary: str) -> list[str]:
    """Every item under `Blocking:` and `Optional:` in a reviewer's summary, one string each. A
    section line with text after the colon is itself one item, and so is the section's first
    paragraph or a line opening with a label such as `B1 —`; a list item's indented lines,
    sub-bullets, labels and subheadings belong to it. Other unindented prose after a blank line, or
    after an empty item, is held: it joins the item before it once a later real item shows it
    sat between items, and is dropped otherwise, so a closing remark is never a finding. Only
    an unindented heading at the section's own level ends the section. `None found.` and the
    like are no item."""
    items: list[list[str]] = []
    current: list[str] | None = None
    held: list[str] | None = None
    in_section = opening = blank = False
    base: int | None = None
    level = 0
    for line in summary.splitlines():
        stripped = line.strip()
        section = _SECTION.match(stripped)
        if section is not None:
            in_section, opening, blank, base, current, held = True, True, False, None, None, None
            level = _heading_level(stripped)
            rest = section["rest"].strip()
            item = _ITEM.match(rest)
            rest = item["text"].strip() if item else rest
            if _empty(rest):
                in_section = False
            elif rest:
                current, opening = [rest], False
                items.append(current)
            continue
        if not in_section:
            continue
        if not stripped:
            blank = True
            continue
        indent = len(line) - len(line.lstrip())
        item = _ITEM.match(line)
        heading = _HEADING.match(stripped)
        field = heading is not None and (heading["label"] or "").strip().casefold() in _FIELD
        if item is not None and (base is None or indent <= base):
            base = indent if base is None else base
            if not _empty(item["text"]):
                if held is not None and current is not None:
                    current.extend(held)
                elif held is not None:
                    items.append(held)
                current = [item["text"].strip()]
                items.append(current)
            else:
                current = None
            held = None
        elif heading is not None and indent == 0 and _ends_section(heading, level, current):
            in_section, current, held = False, None, None
        elif held is not None:
            held.append(stripped)
        elif current is not None and not (blank and indent == 0 and not field):
            current.append(stripped)
        elif _empty(stripped):
            current = None
        elif opening:
            current = [stripped]
            items.append(current)
        else:
            held = [stripped]
        opening = blank = False
    return [" ".join(parts) for parts in items]


def _ends_section(heading: re.Match[str], level: int, current: list[str] | None) -> bool:
    if heading["hashes"]:
        return current is None or not level or len(heading["hashes"]) <= level
    return current is None or heading["label"].strip().casefold() not in _FIELD


# ---------------------------------------------------------------- the round engine


@dataclass(frozen=True)
class Flow:
    """What the round engine needs from one review action: its names, the record key holding
    the reviewed version, where a round record lives, the round's ledger, the reviewers' work
    order and placement (`launch_readonly` keywords), and what a finished round tells the
    manager: `passes` when nothing blocks, `shut` after the verified blocking ids."""

    noun: str
    action: str
    again: str
    version: str
    path: Callable[[Path, dict[str, Any]], Path]
    book: Callable[[Path, dict[str, Any]], ledger_mod.Book]
    order: Callable[[dict[str, Any]], str]
    placement: Callable[[dict[str, Any]], dict[str, Any]]
    reason: str
    passes: str
    shut: str

    @property
    def again_line(self) -> str:
        return f"{self.again[0].upper()}{self.again[1:]}"


def _plan_order(rec: dict[str, Any]) -> str:
    body = Path(str(rec["snapshot"])).read_bytes().decode("utf-8", errors="replace")
    return work_order(str(rec["sha256"]), body)


PLAN = Flow(
    noun="plan review",
    action="plan-review",
    again=AGAIN,
    version="sha256",
    path=lambda root, rec: plan_review.round_path(root, str(rec["sha256"]), int(rec["round"])),
    book=lambda root, rec: ledger_mod.plan_book(root, str(rec["plan_path"])),
    order=_plan_order,
    # The plan travels in the work order, so landed main is the right checkout.
    placement=lambda rec: {},
    reason="plan review: one of two blind reviewers, read-only",
    passes="ExitPlanMode passes on these bytes.",
    shut=(
        "ExitPlanMode stays shut on these bytes. Revise the plan and record each disposition "
        "(`plan-ledger`), or reject a finding with reasons; then call `plan-review` again for a fresh round."
    ),
)


def save(root: Path, flow: Flow, rec: dict[str, Any]) -> None:
    rec["updated_at"] = iso(now())
    atomic_write(flow.path(root, rec), json.dumps(rec, indent=2, sort_keys=True) + "\n")


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


def _launch_reviewer(
    root: Path,
    flow: Flow,
    order: str,
    placement: dict[str, Any],
    provider: str,
    model: str,
    effort: str,
    sid: str | None,
) -> str:
    result = ledger_mod.launch_readonly(
        root,
        prompt=order,
        provider=provider,
        model=model,
        effort=effort,
        row=ROW,
        choice_reason=flow.reason,
        session_id=sid,
        **placement,
    )
    return str(result["id"])


def launch_reviewers(
    root: Path, flow: Flow, order: str, placement: dict[str, Any], sid: str | None
) -> list[dict[str, Any]]:
    """Launch a round's blind reviewers on one work order; their slots, in `REVIEWERS` order."""
    reviewers: list[dict[str, Any]] = []
    for provider, model, effort in REVIEWERS:
        try:
            dispatch_id = _launch_reviewer(root, flow, order, placement, provider, model, effort, sid)
        except AgentError as exc:
            if not reviewers:
                raise
            launched = _label(reviewers)
            raise AgentError(
                f"{flow.noun} launched {launched} but not its {provider} reviewer: {exc}. "
                f"Cancel {launched}, then call {flow.action} again."
            ) from exc
        reviewers.append({"dispatch_id": dispatch_id, "provider": provider, "model": model, "effort": effort})
    return reviewers


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
        elif latest is not None and latest.get("phase") in ("reviewing", "verifying"):
            return advance(root, PLAN, latest, sid)
        return _launch_round(root, plan, raw, sha, sid)


def _launch_round(root: Path, plan: Path, raw: bytes, sha: str, sid: str | None) -> Op:
    body = plan_review.normalise(raw)
    round_no = _next_round(root, str(plan), sha)
    snapshot = plan_review.reviews_dir(root) / f"{sha}.md"
    write_snapshot(snapshot, body)
    order = work_order(sha, body.decode("utf-8", errors="replace"))
    reviewers = launch_reviewers(root, PLAN, order, {}, sid)
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
    save(root, PLAN, rec)
    names = " and ".join(f"{r['dispatch_id']} ({r['provider']} {r['model']})" for r in reviewers)
    return Op(
        rec,
        f"plan review round {round_no} launched two blind reviewers, {names}, for {plan.name} "
        f"(sha256 {sha[:12]}). Once both have returned, {AGAIN}: their findings then go to one verifier.",
    )


def advance(root: Path, flow: Flow, rec: dict[str, Any], sid: str | None) -> Op:
    """Move a `reviewing` or `verifying` round one step (module docstring)."""
    if rec.get("phase") == "verifying":
        return _verifying(root, flow, rec, sid)
    return _reviewing(root, flow, rec, sid)


def _relaunch(root: Path, flow: Flow, rec: dict[str, Any], slot: dict[str, Any], sid: str | None) -> str:
    new_id = _launch_reviewer(
        root,
        flow,
        flow.order(rec),
        flow.placement(rec),
        str(slot["provider"]),
        str(slot["model"]),
        str(slot["effort"]),
        sid,
    )
    slot["relaunch_of"] = slot["dispatch_id"]
    slot["dispatch_id"] = new_id
    return new_id


def _stalled(root: Path, flow: Flow, rec: dict[str, Any], why: str) -> Op:
    rec["stalled"] = why
    save(root, flow, rec)
    return Op(
        rec,
        f"{flow.noun} round {rec['round']} cannot complete: {why}. It stays incomplete; bring it to "
        "the owner, who decides how to go on.",
    )


def _reviewing(root: Path, flow: Flow, rec: dict[str, Any], sid: str | None) -> Op:
    sha = str(rec[flow.version])
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
        return _stalled(root, flow, rec, f"{why}, so no usable return")
    if running:
        return Op(
            rec,
            f"{flow.noun} round {rec['round']}: reviewer {' and '.join(running)} is still running; "
            f"nothing launched. {flow.again_line}.",
        )
    relaunched: list[str] = []
    for slot, _ in failed:
        relaunched.append(_relaunch(root, flow, rec, slot, sid))
        # Saved per relaunch, so a later launch failure cannot lead a retry to relaunch this slot again.
        rec["dispatch_id"] = _label(slots)
        rec["reviewers"] = slots
        save(root, flow, rec)
    if relaunched:
        why = "; ".join(f"{s['relaunch_of']}: {w}" for s, w in failed)
        return Op(
            rec,
            f"{flow.noun} round {rec['round']}: relaunched {', '.join(relaunched)} once ({why}). "
            f"When it returns, {flow.again}.",
        )
    return _to_verifier(root, flow, rec, slots, texts, sid)


def _to_verifier(
    root: Path,
    flow: Flow,
    rec: dict[str, Any],
    slots: list[dict[str, Any]],
    texts: dict[str, list[str]],
    sid: str | None,
) -> Op:
    book, round_no = flow.book(root, rec), int(rec["round"])
    if "findings" not in rec:
        ids: list[str] = []
        for slot in slots:
            found = texts.get(str(slot["dispatch_id"])) or []
            if found:
                added = ledger_mod.add_findings(
                    root,
                    book,
                    plan_sha=str(rec[flow.version]),
                    round_no=round_no,
                    dispatch_id=str(slot["dispatch_id"]),
                    client=str(slot["provider"]),
                    texts=found,
                )
                ids += [str(e["id"]) for e in added]
        rec["findings"] = ids
        save(root, flow, rec)
    if not rec["findings"]:
        rec["phase"], rec["blocking"] = "complete", []
        save(root, flow, rec)
        return Op(
            rec,
            f"{flow.noun} round {round_no} is complete and clean: neither reviewer raised a finding. "
            f"{flow.passes}",
        )
    # A call that failed after launching the verifier left it on the ledger: adopt that one.
    held = as_dict_or_none(ledger_mod.load(root, book)["verifiers"].get(str(round_no)))
    if held is None:
        held = {
            "dispatch_id": ledger_mod.launch_verifier(
                root, plan_path=book, round_no=round_no, session_id=sid
            )["id"]
        }
    rec["verifier"] = {"dispatch_id": str(held["dispatch_id"])}
    rec["phase"] = "verifying"
    save(root, flow, rec)
    return Op(
        rec,
        f"{flow.noun} round {round_no}: {len(rec['findings'])} finding(s) in the ledger "
        f"({', '.join(rec['findings'])}); verifier {held['dispatch_id']} launched. When it returns, {flow.again}.",
    )


def _verifying(root: Path, flow: Flow, rec: dict[str, Any], sid: str | None) -> Op:
    book, round_no = flow.book(root, rec), int(rec["round"])
    verifier = as_dict_or_none(rec.get("verifier")) or {}
    vid = str(verifier.get("dispatch_id") or "")
    state, summary = _dispatch(root, vid)
    if state in plan_review.RUNNING_STATES:
        return Op(
            rec,
            f"{flow.noun} round {round_no}: verifier {vid} is still running; nothing launched. {flow.again_line}.",
        )
    if state == "done":
        missing = ledger_mod.record_verifier(root, book, round_no=round_no, summary=summary, dispatch_id=vid)
    else:
        missing = [str(f) for f in cast("list[object]", rec.get("findings") or [])]
    ledger = ledger_mod.load(root, book)
    disputes = ledger_mod.disputes(ledger)
    if not missing:
        kept = [
            str(e["id"]) for e in ledger_mod.verified_blocking(ledger_mod.round_findings(ledger, round_no))
        ]
        rec["phase"], rec["blocking"] = "complete", kept
        rec.pop("stalled", None)
        save(root, flow, rec)
        if kept:
            tail = f"verified blocking: {', '.join(kept)}. {flow.shut}"
        else:
            tail = f"no finding is verified blocking. {flow.passes}"
        return Op(
            {**rec, "disputes": disputes},
            f"{flow.noun} round {round_no} is complete: {tail} Disputes for the owner: "
            f"{disputes_text(disputes)}.",
        )
    why = f"verifier {vid} ended {state or 'unknown'} leaving {', '.join(missing)} without a result"
    if verifier.get("relaunch_of"):
        return _stalled(root, flow, rec, f"the relaunched {why}; disputes: {disputes_text(disputes)}")
    new = ledger_mod.launch_verifier(root, plan_path=book, round_no=round_no, session_id=sid, replaces=vid)
    rec["verifier"] = {"dispatch_id": str(new["id"]), "relaunch_of": vid}
    save(root, flow, rec)
    return Op(
        rec,
        f"{flow.noun} round {round_no}: {why}; relaunched the verifier once as {new['id']}. When it "
        f"returns, {flow.again}.",
    )


def disputes_text(disputes: list[dict[str, Any]]) -> str:
    return ", ".join(f"{d['finding']['id']} ({d['kind']})" for d in disputes) or "none"
