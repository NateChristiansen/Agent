"""The manager's findings ledger for one plan file, and the verifier that judges each round.

Every finding a plan reviewer raises, across rounds, goes in one ledger per plan file at
`.agent/plan-reviews/ledgers/<plan-key>.json`, where the key is the sha256 of the resolved plan
path. It sits in a subdirectory because `plan_review.all_records` reads every `*.json` directly in
`.agent/plan-reviews/` as a review record. Reviewers never see the ledger: showing a reviewer earlier
findings anchors it toward agreement.

Per round one verifier (Codex, read-only) keeps or refutes each finding, and labels each kept one
blocking or optional. The manager then records a disposition per finding: `fixed` (with the
changed section) or `rejected` (with a reason). `disputes` lists what the owner should see: a
Claude reviewer's finding the verifier refuted or called optional, and a finding the manager
rejected that a later round raised again.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

from . import plan_review
from .core import AgentError, atomic_write, iso, now
from .records.narrow import as_dict_or_none, dict_list

LEDGER_DIR = "ledgers"
VERIFIER_PROVIDER = "codex"
VERIFIER_MODEL = "gpt-6.1-sol"
VERIFIER_EFFORT = "high"
VERIFIER_ROW = "review"
RESULTS = ("kept", "refuted")
LABELS = ("blocking", "optional")
DISPOSITIONS = ("fixed", "rejected")
VERIFIER_INSTRUCTION = (
    "For each finding, decide kept or refuted. Refute only with a concrete reason backed by a read or "
    "an executed probe; when unsure, keep it. Then label each kept finding blocking or optional by these "
    "rules:\n"
    "1. A threshold or bar that is not in the plan's own acceptance criteria is optional, never blocking.\n"
    "2. Blocking means a failure against the plan's acceptance on a path real use takes or plausibly "
    "would. A failure needing tampering, stacked faults, or input no one produces is optional unless you "
    "show it is likely.\n"
    "Return one table row per finding id: id | kept or refuted | blocking or optional | reason. Put the "
    "whole table in your returned summary."
)


def ledgers_dir(root: Path) -> Path:
    return plan_review.reviews_dir(root) / LEDGER_DIR


def plan_key(root: Path, plan_path: str) -> str:
    return hashlib.sha256(str(plan_review.resolve_plan(root, plan_path)).encode()).hexdigest()


def ledger_path(root: Path, plan_path: str) -> Path:
    return ledgers_dir(root) / f"{plan_key(root, plan_path)}.json"


def _empty(root: Path, plan_path: str) -> dict[str, Any]:
    return {"plan_path": str(plan_review.resolve_plan(root, plan_path)), "findings": [], "verifiers": {}}


def load(root: Path, plan_path: str) -> dict[str, Any]:
    """The plan's ledger; a fresh one when none exists. A file that cannot be read or does not
    hold a ledger raises: an empty view would hide every earlier finding and disposition."""
    path = ledger_path(root, plan_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty(root, plan_path)
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentError(f"cannot read the plan-review ledger {path}: {exc}") from exc
    try:
        data = as_dict_or_none(json.loads(text))
    except ValueError as exc:
        raise AgentError(f"the plan-review ledger {path} is not valid JSON: {exc}") from exc
    if data is None or not _well_formed(data):
        raise AgentError(f"the plan-review ledger {path} is malformed; fix or move it aside")
    data.setdefault("verifiers", {})
    return data


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _entry_ok(entry: object) -> bool:
    f = as_dict_or_none(entry)
    if f is None or not all(_is_str(f.get(k)) for k in ("id", "plan_sha", "text")):
        return False
    round_no = f.get("round")
    if not isinstance(round_no, int) or isinstance(round_no, bool):
        return False
    reviewer = as_dict_or_none(f.get("reviewer"))
    if reviewer is None or not all(_is_str(reviewer.get(k)) for k in ("dispatch_id", "client")):
        return False
    verdict = f.get("verifier")
    if verdict is not None:
        v = as_dict_or_none(verdict)
        if (
            v is None
            or v.get("result") not in RESULTS
            or v.get("label") not in (*LABELS, None)
            or not _is_str(v.get("reason"))
            or not _is_str(v.get("dispatch_id"))
        ):
            return False
    disposition = f.get("disposition")
    if disposition is not None:
        d = as_dict_or_none(disposition)
        if d is None or d.get("state") not in DISPOSITIONS:
            return False
    return f.get("reraise_of") is None or _is_str(f.get("reraise_of"))


def _well_formed(data: dict[str, Any]) -> bool:
    raw: object = data.get("findings")
    verifiers: object = data.get("verifiers", {})
    if not isinstance(raw, list) or not isinstance(verifiers, dict):
        return False
    items = cast("list[object]", raw)
    held = cast("dict[str, object]", verifiers)
    if not all(_entry_ok(e) for e in items):
        return False
    return all(
        (rec := as_dict_or_none(v)) is not None and _is_str(rec.get("dispatch_id")) for v in held.values()
    )


def _guard(root: Path, plan_path: str):
    from .dispatch.guards import area_guard

    return area_guard(root, f"plan-ledger:{plan_key(root, plan_path)}")


def _save(root: Path, plan_path: str, ledger: dict[str, Any]) -> None:
    atomic_write(ledger_path(root, plan_path), json.dumps(ledger, indent=2, sort_keys=True) + "\n")


def findings(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    return dict_list(ledger.get("findings"))


def _find(ledger: dict[str, Any], finding_id: str) -> dict[str, Any]:
    for entry in findings(ledger):
        if entry.get("id") == finding_id:
            return entry
    raise AgentError(f"no finding {finding_id!r} in the ledger for {ledger.get('plan_path')}")


def add_findings(
    root: Path,
    plan_path: str,
    *,
    plan_sha: str,
    round_no: int,
    dispatch_id: str,
    client: str,
    texts: list[str],
) -> list[dict[str, Any]]:
    """Record one reviewer's findings for a round; each gets the next `F<n>` id."""
    with _guard(root, plan_path):
        ledger = load(root, plan_path)
        entries = findings(ledger)
        added: list[dict[str, Any]] = []
        for text in texts:
            entry: dict[str, Any] = {
                "id": f"F{len(entries) + 1}",
                "plan_sha": plan_sha,
                "round": round_no,
                "reviewer": {"dispatch_id": dispatch_id, "client": client},
                "text": text.strip(),
                "verifier": None,
                "disposition": None,
                "reraise_of": None,
                "recorded_at": iso(now()),
            }
            entries.append(entry)
            added.append(entry)
        ledger["findings"] = entries
        _save(root, plan_path, ledger)
    return added


def dispose(root: Path, plan_path: str, *, finding_id: str, disposition: str, note: str) -> dict[str, Any]:
    """The manager's disposition: `fixed` with the changed section, or `rejected` with a reason."""
    if disposition not in DISPOSITIONS:
        raise AgentError(f"disposition must be one of {', '.join(DISPOSITIONS)}; got {disposition!r}")
    if not note.strip():
        what = "the changed section" if disposition == "fixed" else "the reason"
        raise AgentError(f"a `{disposition}` disposition needs {what}")
    field = "section" if disposition == "fixed" else "reason"
    with _guard(root, plan_path):
        ledger = load(root, plan_path)
        entry = _find(ledger, finding_id)
        entry["disposition"] = {"state": disposition, field: note.strip(), "recorded_at": iso(now())}
        _save(root, plan_path, ledger)
    return entry


def link_reraise(root: Path, plan_path: str, *, finding_id: str, earlier_id: str) -> dict[str, Any]:
    """Mark `finding_id` as a later round's re-raise of `earlier_id`."""
    if finding_id == earlier_id:
        raise AgentError("a finding cannot re-raise itself")
    with _guard(root, plan_path):
        ledger = load(root, plan_path)
        earlier = _find(ledger, earlier_id)
        entry = _find(ledger, finding_id)
        if int(entry.get("round") or 0) <= int(earlier.get("round") or 0):
            raise AgentError(f"{finding_id} is not from a later round than {earlier_id}")
        entry["reraise_of"] = earlier_id
        _save(root, plan_path, ledger)
    return entry


# ---------------------------------------------------------------- the verifier


def round_findings(ledger: dict[str, Any], round_no: int) -> list[dict[str, Any]]:
    return [f for f in findings(ledger) if f.get("round") == round_no]


def verifier_work_order(sha: str, plan_text: str, entries: list[dict[str, Any]]) -> str:
    """The verifier's whole input. Findings carry their ids and text only, never who raised
    them, so the verdict cannot lean on the reviewer's client."""
    listed = "\n".join(f"- {e['id']}: {' '.join(str(e.get('text') or '').split())}" for e in entries)
    return (
        f"Plan-review verifier for sha256 {sha}: judge each finding below against the frozen plan "
        "snapshot. Change no file; this dispatch is read-only. Read the harness repo to check a "
        "finding, and prefer an executed read-only probe to an opinion. Do not open the plan's live "
        "file, which may have changed since.\n\n"
        f"{VERIFIER_INSTRUCTION}\n\n"
        f"Findings:\n{listed}\n\n"
        f"----- BEGIN PLAN SNAPSHOT sha256 {sha} -----\n"
        f"{plan_text.rstrip(chr(10))}\n"
        f"----- END PLAN SNAPSHOT sha256 {sha} -----\n"
    )


def launch_verifier(
    root: Path, *, plan_path: str, round_no: int, session_id: str | None = None
) -> dict[str, Any]:
    """Launch the one verifier for a round's findings, on the frozen snapshot those findings
    judged, and record its dispatch id on the ledger. Returns the dispatch's `new` result."""
    with _guard(root, plan_path):
        return _launch_locked(root, plan_path, round_no, session_id)


def _launch_locked(root: Path, plan_path: str, round_no: int, session_id: str | None) -> dict[str, Any]:
    from . import dispatch as dispatch_mod
    from . import sessions
    from .verify_record import harness_repo

    ledger = load(root, plan_path)
    held = as_dict_or_none(ledger["verifiers"].get(str(round_no)))
    if held is not None:
        raise AgentError(
            f"round {round_no} of {plan_path} already has verifier {held['dispatch_id']}; "
            "one verifier per round, so nothing was launched"
        )
    entries = round_findings(ledger, round_no)
    if not entries:
        raise AgentError(f"round {round_no} of {plan_path} has no findings to verify")
    shas = {str(e.get("plan_sha") or "") for e in entries}
    if len(shas) != 1:
        raise AgentError(f"round {round_no} of {plan_path} mixes plan versions: {sorted(shas)}")
    sha = shas.pop()
    snapshot = plan_review.reviews_dir(root) / f"{sha}.md"
    try:
        plan_text = snapshot.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        raise AgentError(f"cannot read the frozen plan snapshot {snapshot}: {exc}") from exc
    sid = sessions.resolve_session_id(root, session_id)
    repo = harness_repo(root)
    result = dispatch_mod.new(
        root,
        repo=str(repo.name),
        feature="current",
        prompt=verifier_work_order(sha, plan_text, entries),
        provider=VERIFIER_PROVIDER,
        model=VERIFIER_MODEL,
        effort=VERIFIER_EFFORT,
        row=VERIFIER_ROW,
        base=repo.default_branch,
        writes=[],
        no_checks=True,
        just_go=True,
        allow_unmatched=True,
        choice_reason="plan-review verifier: keeps or refutes each finding of a round, read-only",
        session_id=sid,
    )
    ledger["verifiers"][str(round_no)] = {"dispatch_id": result["id"], "plan_sha": sha}
    _save(root, plan_path, ledger)
    return result


_ROW = re.compile(r"^\|?\s*(?P<id>F\d+)\s*\|(?P<rest>.*)$")


def parse_table(summary: str, ids: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Read the verifier's `id | kept or refuted | blocking or optional | reason` rows. Returns
    the parsed results by id and the ids in `ids` that got none: missing from the table, or on a
    row that does not parse. A refuted finding may leave the label empty or `-`; a kept one must
    carry one."""
    wanted = set(ids)
    parsed: dict[str, dict[str, Any]] = {}
    bad: set[str] = set()
    for line in summary.splitlines():
        match = _ROW.match(line.strip().replace("`", ""))
        if match is None or match["id"] not in wanted:
            continue
        cells = [c.strip() for c in match["rest"].rstrip().removesuffix("|").split("|")]
        fid = match["id"]
        result = cells[0].lower() if cells else ""
        label = cells[1].lower() if len(cells) > 1 else ""
        reason = " | ".join(cells[2:]).strip()
        if (
            result not in RESULTS
            or not reason
            or (label not in LABELS and (result == "kept" or label not in ("", "-")))
        ):
            bad.add(fid)
            continue
        if fid in parsed:
            bad.add(fid)
            continue
        parsed[fid] = {"result": result, "label": label if label in LABELS else None, "reason": reason}
    for fid in bad:
        parsed.pop(fid, None)
    return parsed, [i for i in ids if i not in parsed]


def record_verifier(
    root: Path, plan_path: str, *, round_no: int, summary: str, dispatch_id: str
) -> list[str]:
    """Parse a round's verifier summary into the ledger. Returns the ids left without a result,
    which the caller must surface: a missing result is not a kept finding or a refuted one."""
    with _guard(root, plan_path):
        ledger = load(root, plan_path)
        entries = round_findings(ledger, round_no)
        parsed, missing = parse_table(summary, [str(e["id"]) for e in entries])
        for entry in entries:
            if entry["id"] in parsed:
                entry["verifier"] = {**parsed[entry["id"]], "dispatch_id": dispatch_id}
            else:
                entry["verifier"] = None
        _save(root, plan_path, ledger)
    return missing


# ---------------------------------------------------------------- disputes


def _norm(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def disputes(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """What the owner should see: each Claude reviewer's finding the verifier refuted or labelled
    optional, and each later finding that re-raises one the manager rejected (linked through
    `plan-ledger`, or with the same text)."""
    entries = findings(ledger)
    by_id = {str(e.get("id")): e for e in entries}
    out: list[dict[str, Any]] = []
    for entry in entries:
        reviewer = as_dict_or_none(entry.get("reviewer")) or {}
        verdict = as_dict_or_none(entry.get("verifier")) or {}
        if reviewer.get("client") == "claude":
            if verdict.get("result") == "refuted":
                out.append({"kind": "claude-refuted", "finding": entry})
            elif verdict.get("result") == "kept" and verdict.get("label") == "optional":
                out.append({"kind": "claude-optional", "finding": entry})
        earlier = by_id.get(str(entry.get("reraise_of") or ""))
        if earlier is None:
            earlier = next(
                (
                    e
                    for e in entries
                    if int(e.get("round") or 0) < int(entry.get("round") or 0)
                    and _rejected(e)
                    and _norm(e.get("text")) == _norm(entry.get("text"))
                ),
                None,
            )
        if earlier is not None and _rejected(earlier):
            out.append({"kind": "reraised-rejected", "finding": entry, "rejected": earlier})
    return out


def _rejected(entry: dict[str, Any]) -> bool:
    return (as_dict_or_none(entry.get("disposition")) or {}).get("state") == "rejected"
