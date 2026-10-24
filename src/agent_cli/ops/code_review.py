"""`dispatch` action `code-review`: run one blind review round of a branch head, and advance it one
step per call; `code-ledger` reads or disposes of the branch's findings (`code_review.py` holds the
records and the clean check).

The round is plan review's (`ops/plan_review.py`, `Flow`): two blind, read-only reviewers on one
work order, then one verifier on their findings, relaunched once on an unusable return. What
differs is the subject. A worker branch comes from a build `dispatch_id` (its repo, branch, start
sha and work order); a manager branch from `branch`, `base` and `paths` = [an acceptance file].
The acceptance is frozen read-only, and every dispatch of a round is pinned to the head sha read
at launch, with the base as its comparison commit.

Each call reads the branch head. A round on that head that is reviewing or verifying advances one
step; a complete one is reported, never rerun. With none, a new round launches, unless the branch
already has `ROUND_CAP` complete rounds: then the owner decides, with the ledger shown. A round on
an older head is left as it stands; it never reads as clean for the new head.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import code_review, plan_review, sessions
from .. import plan_review_ledger as ledger_mod
from ..core import AgentError, iso, now
from ..records.narrow import dict_list
from . import plan_review as engine
from ._shared import Op
from .plan_ledger import ledger_op

AGAIN = "call `code-review` again on the same branch after the supervisor reports a return"
BAR = ACCEPTANCE = "the acceptance"


def work_order(base: str, head: str, acceptance: str) -> str:
    """The reviewer's whole input: the contract, then the frozen acceptance."""
    return (
        f"Review the change from `{base}` to `{head}`. First run `git rev-parse HEAD` and confirm it "
        f"starts with `{head}`; if not, return a question. Read the diff with `git diff {base} HEAD`. "
        "Change no file; restore any probe edit. Do not read other branches, later commits, or any "
        "handoff under `features/`: this review is blind.\n\n"
        "The change's acceptance is below.\n\n"
        f"{plan_review.exhaustive('change')}\n\n"
        "Blocking rules:\n"
        f"{plan_review.blocking_rules(BAR, ACCEPTANCE)}\n\n"
        "Run probes where they settle a finding.\n\n"
        f"Return: {plan_review.VERDICT_FIELD} Revise only for blocking findings. List each blocking "
        "finding under `Blocking:`, and label every other point `Optional:`. Each finding states its file "
        "and line and the read or probe behind it. Put the whole review in your returned summary.\n\n"
        f"{_acceptance_block(acceptance)}"
    )


def verifier_work_order(base: str, head: str, acceptance: str, entries: list[dict[str, Any]]) -> str:
    """The verifier's whole input: the same pinned change and acceptance, and the findings by id
    only (`plan_review_ledger.finding_list`)."""
    return (
        f"Code-review verifier for `{head}`: judge each finding below against the change from `{base}` "
        f"to `{head}` and the acceptance below. First run `git rev-parse HEAD` and confirm it starts with "
        f"`{head}`; if not, return a question. Read the diff with `git diff {base} HEAD`. Change no file; "
        "restore any probe edit. Do not read other branches, later commits, or any handoff under "
        "`features/`. Prefer an executed read-only probe to an opinion.\n\n"
        f"{ledger_mod.verifier_instruction(BAR, ACCEPTANCE)}\n\n"
        f"Findings:\n{ledger_mod.finding_list(entries)}\n\n"
        f"{_acceptance_block(acceptance)}"
    )


def _acceptance_block(text: str) -> str:
    return f"----- BEGIN ACCEPTANCE -----\n{text.rstrip(chr(10))}\n----- END ACCEPTANCE -----\n"


def _acceptance(rec: dict[str, Any]) -> str:
    path = Path(str(rec["acceptance"]))
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        raise AgentError(f"cannot read the frozen acceptance {path}: {exc}") from exc


def _placement(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": str(rec["repo"]),
        "feature": str(rec["feature"]),
        "target": str(rec["head"]),
        "compare": str(rec["base"]),
    }


def _no_verifier(root: Path, sha: str, entries: list[dict[str, Any]]) -> str:
    raise AgentError("a code-review verifier launches from its round; call `code-review` on the branch")


def book(root: Path, repo: str, branch: str, rec: dict[str, Any] | None = None) -> ledger_mod.Book:
    """The branch's ledger; with a round record, its verifier judges that round's head."""
    key = code_review.branch_key(repo, branch)
    order = _no_verifier
    if rec is not None:
        held = rec

        def order(root: Path, sha: str, entries: list[dict[str, Any]]) -> str:
            return verifier_work_order(str(held["base"]), sha, _acceptance(held), entries)

    return ledger_mod.Book(
        path=code_review.ledger_path(root, repo, branch),
        guard=f"code-ledger:{key}",
        identity={"branch": branch, "repo": repo},
        label=branch,
        kind="code-review",
        sha_field="head",
        verifier_order=order,
        placement=_placement(rec) if rec is not None else {},
        verifier_reason="code-review verifier: keeps or refutes each finding of a round, read-only",
    )


CODE = engine.Flow(
    noun="code review",
    action="code-review",
    again=AGAIN,
    version="head",
    path=lambda root, rec: code_review.round_path(root, str(rec["head"]), int(rec["round"])),
    book=lambda root, rec: book(root, str(rec["repo"]), str(rec["branch"]), rec),
    order=lambda rec: work_order(str(rec["base"]), str(rec["head"]), _acceptance(rec)),
    placement=_placement,
    reason="code review: one of two blind reviewers, read-only",
    passes="This head is clean.",
    shut=(
        "This head is not clean. Fix the branch and record each disposition (`code-ledger`); a new head "
        "starts a fresh round. A finding you reject goes to the owner."
    ),
)


# ---------------------------------------------------------------- what is reviewed


@dataclass(frozen=True)
class Subject:
    repo: str
    checkout: Path
    branch: str
    base: str
    acceptance: bytes
    feature: str
    source: dict[str, str]


def _checkout(root: Path, repo: str | None) -> tuple[str, Path]:
    """A registered repo's name and main checkout; the harness's when `repo` is None."""
    from .. import repos as repos_mod
    from ..verify_record import harness_repo

    rp = harness_repo(root) if repo is None else repos_mod.load_registry(root).require(repo)
    return str(rp.name), Path(rp.abs_path(root))


def _commit(checkout: Path, ref: str, what: str) -> str:
    from .. import repos as repos_mod

    sha = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], checkout)
    if not sha:
        raise AgentError(f"{what} {ref!r} is not a commit in {checkout}")
    return sha.strip()


def _branch_of(root: Path, dispatch_id: str) -> dict[str, Any]:
    from ..dispatch.state import load

    d = load(root, dispatch_id)
    if not d.get("branch") or not d.get("repo"):
        raise AgentError(f"{dispatch_id} names no branch or repo to review")
    return d


def subject(
    root: Path,
    *,
    dispatch_id: str | None,
    branch: str | None,
    base: str | None,
    paths: list[str] | None,
    repo: str | None,
) -> Subject:
    """What a `code-review` call reviews: a build dispatch's branch, or a named branch and base
    judged by an acceptance file."""
    if dispatch_id is not None:
        if branch or base or paths or repo:
            raise AgentError(
                "code-review takes a build `dispatch_id`, or `branch`, `base` and `paths` = [the "
                "acceptance file], not both"
            )
        d = _branch_of(root, dispatch_id)
        name, checkout = _checkout(root, str(d["repo"]))
        thread = dict_list(d.get("thread"))
        text = str(thread[0].get("text") or "") if thread else ""
        start = str(d.get("start_sha") or "") or _commit(checkout, str(d.get("base") or ""), "base")
        return Subject(
            repo=name,
            checkout=checkout,
            branch=str(d["branch"]),
            base=_commit(checkout, start, "start sha"),
            acceptance=_nonempty(text.encode(), f"{dispatch_id}'s work order"),
            feature=str(d.get("feature") or "current"),
            source={"dispatch_id": dispatch_id},
        )
    if not branch or not base or not paths or len(paths) != 1:
        raise AgentError(
            "code-review needs a build `dispatch_id`, or `branch`, `base` and `paths` = [the acceptance file]"
        )
    file = plan_review.resolve_plan(root, paths[0])
    try:
        raw = file.read_bytes()
    except OSError as exc:
        raise AgentError(f"cannot read the acceptance {file}: {exc}") from exc
    name, checkout = _checkout(root, repo)
    return Subject(
        repo=name,
        checkout=checkout,
        branch=branch,
        base=_commit(checkout, base, "base"),
        acceptance=_nonempty(raw, str(file)),
        feature="current",
        source={"acceptance_path": str(file)},
    )


def _nonempty(raw: bytes, what: str) -> bytes:
    if not raw.strip():
        raise AgentError(f"the acceptance ({what}) is empty; a review needs something to judge against")
    return raw


# ---------------------------------------------------------------- the actions


def dispatch_code_review(
    root: Path,
    *,
    dispatch_id: str | None = None,
    branch: str | None = None,
    base: str | None = None,
    paths: list[str] | None = None,
    repo: str | None = None,
    session_id: str | None = None,
) -> Op:
    """Advance the review of a branch head one step (module docstring)."""
    from ..dispatch.guards import area_guard

    sid = sessions.resolve_session_id(root, session_id)
    s = subject(root, dispatch_id=dispatch_id, branch=branch, base=base, paths=paths, repo=repo)
    head = _commit(s.checkout, s.branch, "branch")
    # Round numbers are per branch and round files per head, so both are held: branch, then head.
    with (
        area_guard(root, f"code-review:{code_review.branch_key(s.repo, s.branch)}"),
        area_guard(root, f"code-review-head:{head}"),
    ):
        rec = code_review.latest(root, branch=s.branch, head=head, repo=s.repo)
        if rec is not None and rec.get("phase") in ("reviewing", "verifying"):
            return engine.advance(root, CODE, rec, sid)
        if rec is not None and code_review.complete(rec):
            return _report(root, rec)
        used = code_review.rounds_used(root, branch=s.branch, repo=s.repo)
        if used >= code_review.ROUND_CAP:
            return _capped(root, s, head, used)
        return _launch_round(root, s, head, sid)


def _next_round(root: Path, s: Subject, head: str) -> int:
    """One past every round this branch has used, in its records and in its ledger."""
    mine = code_review.records(root, branch=s.branch, repo=s.repo)
    ledger = ledger_mod.load(root, book(root, s.repo, s.branch))
    used = [0, *(plan_review.round_no(r) for r in mine)]
    used += [int(f.get("round") or 0) for f in ledger_mod.findings(ledger)]
    used += [int(k) for k in ledger["verifiers"] if str(k).isdigit()]
    n = max(used) + 1
    while code_review.round_path(root, head, n).exists():  # the same head under another branch
        n += 1
    return n


def _launch_round(root: Path, s: Subject, head: str, sid: str | None) -> Op:
    body = plan_review.normalise(s.acceptance)
    digest = plan_review.digest(body)
    frozen = code_review.acceptance_path(root, digest)
    engine.write_snapshot(frozen, body)
    left = [
        r
        for r in code_review.records(root, branch=s.branch, repo=s.repo)
        if r.get("head") != head and r.get("phase") in ("reviewing", "verifying")
    ]
    rec: dict[str, Any] = {
        "repo": s.repo,
        "branch": s.branch,
        "head": head,
        "base": s.base,
        "feature": s.feature,
        "source": s.source,
        "acceptance": str(frozen),
        "acceptance_sha256": digest,
        "round": _next_round(root, s, head),
        "phase": "reviewing",
    }
    reviewers = engine.launch_reviewers(root, CODE, CODE.order(rec), _placement(rec), sid)
    rec.update(
        {
            # A label for messages that name "the review"; `reviewers` is what the round reads.
            "dispatch_id": " and ".join(str(r["dispatch_id"]) for r in reviewers),
            "reviewers": reviewers,
            "verifier": None,
            "session_id": sid,
            "launched_at": iso(now()),
        }
    )
    engine.save(root, CODE, rec)
    names = " and ".join(f"{r['dispatch_id']} ({r['provider']} {r['model']})" for r in reviewers)
    older = "".join(
        f" Round {r.get('round')} on older head {str(r.get('head'))[:12]} stays incomplete and never "
        "reads as clean; cancel its dispatches if they still run."
        for r in left
    )
    return Op(
        rec,
        f"code review round {rec['round']} launched two blind reviewers, {names}, for {s.branch} at "
        f"{head[:12]} against {s.base[:12]}. Once both have returned, {AGAIN}: their findings then go to "
        f"one verifier.{older}",
    )


def _report(root: Path, rec: dict[str, Any]) -> Op:
    ledger = ledger_mod.load(root, CODE.book(root, rec))
    disputes = ledger_mod.disputes(ledger)
    kept = plan_review.blocking(rec)
    tail = f"verified blocking: {', '.join(kept)}. {CODE.shut}" if kept else CODE.passes
    return Op(
        {**rec, "disputes": disputes},
        f"code review round {rec['round']} on {str(rec['head'])[:12]} is already complete: {tail} "
        f"Disputes for the owner: {engine.disputes_text(disputes)}.",
    )


def _capped(root: Path, s: Subject, head: str, used: int) -> Op:
    ledger = ledger_mod.load(root, book(root, s.repo, s.branch))
    lines = ledger_mod.owner_lines(ledger)
    shown = "; ".join(lines) or "no verified blocking finding and no dispute"
    return Op(
        {
            "repo": s.repo,
            "branch": s.branch,
            "head": head,
            "rounds_used": used,
            "round_cap": code_review.ROUND_CAP,
            "ledger": ledger,
            "disputes": ledger_mod.disputes(ledger),
            "owner_lines": lines,
        },
        f"code review of {s.branch}: {used} complete rounds reached the cap of {code_review.ROUND_CAP}, "
        f"so no round launched on {head[:12]}. The owner decides, with the ledger: {shown}.",
    )


def dispatch_code_ledger(
    root: Path,
    *,
    dispatch_id: str | None = None,
    branch: str | None = None,
    repo: str | None = None,
    finding_id: str | None = None,
    disposition: str | None = None,
    note: str | None = None,
    reraise_of: str | None = None,
) -> Op:
    """`plan-ledger` for a branch: named by a build `dispatch_id`, or by `branch` (and `repo`,
    the harness by default)."""
    if dispatch_id is not None:
        if branch or repo:
            raise AgentError("code-ledger takes a build `dispatch_id` or a `branch`, not both")
        d = _branch_of(root, dispatch_id)
        name, branch = str(d["repo"]), str(d["branch"])
    elif not branch:
        raise AgentError("code-ledger needs a build `dispatch_id` or a `branch`")
    else:
        name = _checkout(root, repo)[0]
    return ledger_op(
        root,
        book(root, name, branch),
        action="code-ledger",
        finding_id=finding_id,
        disposition=disposition,
        note=note,
        reraise_of=reraise_of,
    )
