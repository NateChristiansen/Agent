"""The code-review gate on pushes (ADR-0063): every guarded ref a push sends needs a clean
`code-review` round on the commit it publishes.

Git's `pre-push` hook is the authority. It reads the exact refs and object ids from git's stdin
(`push_input`), so no shell spelling of the push changes what is checked: the harness's own hook
runs `problems` after the pass-record gate, and each registered product clone's hook, which setup
installs (`gitgate.install_review`), runs it against the harness's records. The PreToolUse
`push-guard` parses the command text with the same scope and reasons as an early warning.

A ref is guarded when a dispatch record in the pushed repo names its source or its destination
branch, and every ref pushed from a dispatch's worktree is, so a renamed branch or a tag pushed
from a worker's checkout is guarded too. A deletion publishes no commit and is never refused.
`AGENT_PUSH_UNREVIEWED=1`, exactly, lets an unclean ref through and logs a note to the feature of
the dispatch that guards it; a ref whose note cannot be written is refused.

Unreadable state refuses a guarded ref: a guarded object that is not a commit this checkout can
read, and a round file of the head that is not readable JSON. A registry file that cannot be read
leaves the pushed repo unknown: `main`, deletions and refs no dispatch record names pass, a ref a
dispatch record in any repo names is refused whatever its rounds say, and the override still lets
it through with its note. An unreadable dispatch record is skipped, as `dispatch.load_all` skips
it everywhere.

The harness is the harness whether or not the registry lists it: a push sharing the records
root's git directory is never guarded by a dispatch record naming a registered product repo.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .push_input import Update
    from .repos import Repo

OVERRIDE_ENV = "AGENT_PUSH_UNREVIEWED"
REVIEW_REASON_RE = re.compile(r"^\s*review(?=[:,]|\s+row\b)|(?:^|;\s*)rubric row review(?=[:\s]|$)", re.I)
HEADS = "refs/heads/"
REFUSED = "push refused: every guarded ref needs a clean code-review round on the commit it pushes (ADR-0063)"


def problems(
    root: Path, checkout: Path, updates: list[Update], env: Mapping[str, str] | None = None
) -> list[str]:
    """Why this push may not go, one line per refused ref; empty when every guarded ref has a
    clean round or the override holds. `root` is where the harness keeps its records, `checkout`
    the pushed one."""
    from . import config_file
    from . import dispatch as dispatch_mod
    from . import repos as repos_mod
    from .push_records import commit

    registry_file = config_file.registry_path(root)
    registry: list[Repo] = []
    unread: str | None = None
    if registry_file.exists() or registry_file.is_symlink():
        try:
            registry = repos_mod.load_registry(root).repos
        except Exception as exc:  # noqa: BLE001 — an unreadable registry leaves the pushed repo unknown
            unread = f"the registry could not be read, so the pushed repo is unknown: {exc}"
    records = dispatch_mod.load_all(root)
    held = worktree_records(records, checkout)
    repo = repo_of(root, registry, held, checkout)
    others = harness_others(root, registry, repo, checkout)
    override = (os.environ if env is None else env).get(OVERRIDE_ENV) == "1"
    out: list[str] = []
    for update in updates:
        if set(update.local_oid) == {"0"}:
            continue
        target = update.remote_ref.removeprefix(HEADS)
        mine = guarding(records, held, repo, {target, _local_name(update.local_ref, checkout)}, others)
        if not mine:
            continue
        head = commit(checkout, update.local_oid)
        if head is None:
            out.append(
                f"{target} at {update.local_oid}: not a commit this checkout can read, so its code-review "
                "round cannot be checked; refusing"
            )
            continue
        why = unread or unclean(root, head, repo.name if repo else None, mine, target, strict=True)
        if why is None:
            continue
        sent = _sent(update.local_oid, head)
        if not override:
            how = f"set {OVERRIDE_ENV}=1 in the push's environment"
            out.append(
                reason(
                    mine,
                    target,
                    head,
                    why,
                    how=how,
                    checkout=checkout,
                    repo=repo.name if repo else None,
                    sent=update.local_oid,
                )
            )
        elif failed := note_override(root, mine, target, head, why):
            out.append(f"{target} at {sent}: {OVERRIDE_ENV}=1 is honoured only with its note, and {failed}")
    return out


def _sent(oid: str, head: str) -> str:
    """The object id git sent, with the commit it peels to beside it when they differ (a tag)."""
    return oid if oid == head else f"{oid} (commit {head})"


def _local_name(local_ref: str, checkout: Path) -> str:
    """The branch a pushed ref's local side names: the checked-out one for `HEAD`."""
    if local_ref == "HEAD":
        return git_line(checkout, ["symbolic-ref", "--quiet", "--short", "HEAD"]) or ""
    return local_ref.removeprefix(HEADS)


def guarding(
    records: list[dict[str, Any]],
    held: list[dict[str, Any]],
    repo: Repo | None,
    names: set[str],
    others: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """The dispatch records that guard a ref: those naming one of `names` as their branch, in the
    pushed repo when it is known, else in none of `others`, then those whose worktree the push
    comes from."""
    names = names - {""}
    named = [d for d in records if str(d.get("branch") or "") in names]
    if repo is not None:
        # A record with no repo is not proof of another repo, so it stays.
        named = [d for d in named if str(d.get("repo") or repo.name) in (repo.name, repo.short)]
    elif others:
        named = [d for d in named if str(d.get("repo") or "") not in others]
    return named + [d for d in held if d not in named]


def harness_others(root: Path, registry: list[Repo], repo: Repo | None, where: Path) -> frozenset[str]:
    """The names of every registered repo when the push comes from the harness itself and the
    registry does not list it: a record naming one of them is another repo's. Empty otherwise."""
    if repo is not None or not registry:
        return frozenset()
    common = safe_common_dir(where)
    if common is None or common != safe_common_dir(root):
        return frozenset()
    return frozenset(name for r in registry for name in (r.name, r.short))


def worktree_records(records: list[dict[str, Any]], where: Path) -> list[dict[str, Any]]:
    """Every dispatch whose worktree holds `where`, so a push from a worker's checkout is
    guarded whatever the branch is called. Path-only: no git, no subprocess."""
    try:
        target = where.expanduser().resolve()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for d in records:
        raw = str(d.get("worktree") or "")
        if not raw:
            continue
        try:
            base = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if target == base or base in target.parents:
            out.append(d)
    return out


def repo_of(root: Path, registry: list[Repo], held: list[dict[str, Any]], where: Path) -> Repo | None:
    """The registered repo the push comes from: a dispatch worktree's repo first; else the one
    sharing `where`'s git common directory, so a linked worktree outside the checkout resolves
    too; else the registered checkout holding `where` when git cannot say (the deepest, for a
    nested clone)."""
    named = {str(d.get("repo") or "") for d in held} - {""}
    if held:
        return next((r for r in registry if named & {r.name, r.short}), None)
    try:
        target = where.expanduser().resolve()
    except OSError:
        return None
    common = safe_common_dir(target)
    if common is not None:
        return next((r for r in registry if safe_common_dir(r.abs_path(root)) == common), None)
    holding: list[tuple[int, Repo]] = []
    for r in registry:
        try:
            base = r.abs_path(root).resolve()
        except OSError:
            continue
        if target == base or base in target.parents:
            holding.append((len(base.parts), r))
    return max(holding, key=lambda pair: pair[0])[1] if holding else None


def common_dir(start: Path) -> Path | None:
    """`git_discovery.common_dir`, else git's answer (on Windows the ownership check always
    defers to git). None when neither can say."""
    from . import git_discovery

    found = git_discovery.common_dir(start)
    if found is not None:
        return found
    out = git_line(start, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    if not out:
        return None
    try:
        return Path(out).resolve()
    except OSError:
        return None


def safe_common_dir(start: Path) -> Path | None:
    try:
        return common_dir(start) if start.exists() else None
    except Exception:  # noqa: BLE001 — git that cannot say leaves the repo to the path rules
        return None


def unclean(
    root: Path, head: str, repo: str | None, mine: list[dict[str, Any]], branch: str, *, strict: bool
) -> str | None:
    """Why `head` has no clean code-review round in `repo` (any repo when the push's is unknown)
    under the pushed branch's name; None when it has one. `strict` is the pre-push hook: a round
    file of the head that is not readable JSON is a reason, and only a round under the pushed name
    counts. Otherwise (push-guard's early warning) an unreadable round lets the ref through, and
    with no round under the pushed name a clean one under a guarding dispatch's branch counts."""
    from . import code_review

    if unread := code_review.unreadable(root, head):
        return f"an unreadable round record ({', '.join(unread)})" if strict else None
    if bad := code_review.malformed(root, head):
        return f"a malformed round record ({', '.join(bad)})"
    if not strict and code_review.latest(root, branch=branch, head=head, repo=repo) is None:
        others = dict.fromkeys(
            str(d.get("branch")) for d in mine if d.get("branch") and d.get("branch") != branch
        )
        if any(code_review.clean(root, branch=name, head=head, repo=repo) for name in others):
            return None
    return code_review.unclean(root, branch=branch, head=head, repo=repo)


def reason(
    records: list[dict[str, Any]],
    ref: str,
    head: str,
    why: str,
    *,
    how: str,
    checkout: Path,
    repo: str | None,
    sent: str | None = None,
) -> str:
    """The refusal: the ref, the object id git sent (`sent`, else the commit) with the commit
    beside it when they differ, why it is not clean, the call that makes a round, and the override
    (`how` says where to set it)."""
    return (
        f"{ref} at {_sent(sent or head, head)} has no clean code-review round on that head: {why}. A push needs a "
        "complete `code-review` round with zero verified blocking findings on the pushed head "
        f"(ADR-0063). {remedy(records, ref, head, checkout=checkout, repo=repo)} To push anyway, {how} "
        "(it is logged to the feature)."
    )


def remedy(records: list[dict[str, Any]], ref: str, head: str, *, checkout: Path, repo: str | None) -> str:
    """The `code-review` call that records a round under exactly `ref` on `head`. The action
    reviews what its branch name resolves to in the clone, so a pushed name that does not resolve
    to the pushed commit there is made to first; a name other than the build dispatch's branch
    takes the call's `branch` form."""

    def is_build(d: dict[str, Any]) -> bool:
        row = d.get("row")
        if row:
            return row != "review"
        reason = str(d.get("choice_reason") or "")
        return not REVIEW_REASON_RE.search(reason)

    build = next((d for d in records if is_build(d)), records[0])
    full = ref if ref.startswith("refs/") else HEADS + ref
    first = "Run"
    if git_line(checkout, ["rev-parse", "--verify", "--quiet", f"{full}^{{commit}}"]) != head:
        first = (
            f"`{ref}` does not name {head[:12]} in the clone, so point it there first "
            f"(`git update-ref {full} {head}`). Then run"
        )
    if ref == str(build.get("branch") or "") and build.get("repo"):
        call = f'dispatch_id="{build.get("id")}"'
    else:
        base = str(build.get("start_sha") or build.get("base") or "<the commit the work started from>")
        order = str(build.get("work_order") or "")
        paths = f'["{order}"]' if order else "[the acceptance file]"
        name = repo or str(build.get("repo") or "")
        call = f'branch="{ref}", base="{base}", paths={paths}' + (f', repo="{name}"' if name else "")
    return (
        f"{first} the `dispatch` action `code-review` with {call}, and call it again after each return "
        "until the round completes."
    )


def note_override(root: Path, mine: list[dict[str, Any]], ref: str, head: str, why: str) -> str | None:
    """The override is not silent: it goes in the log of the feature of the guarding dispatch,
    where the next retro reads it, naming the head and why its round is not clean. Returns why
    the note could not be written, or None."""
    return note(
        root,
        mine,
        f"pushed {ref} at {head[:12]} with {OVERRIDE_ENV}=1 without a clean code-review round: {why}.",
    )


def note(root: Path, mine: list[dict[str, Any]], text: str) -> str | None:
    """Log `text` on the first of `mine`'s features; why it could not, or None. Never raises."""
    feature_id = next((d.get("feature") for d in mine if d.get("feature") is not None), None)
    if feature_id is None:
        return "no dispatch guarding it names a feature to log the override on"
    try:
        from . import features as features_mod
        from . import log as log_mod

        log_mod.append(features_mod.resolve(root, str(feature_id)), "note", text)
    except Exception as exc:  # noqa: BLE001 — a hook reports, it never raises
        return f"the note to feature {feature_id} could not be written: {exc}"
    return None


#: What `git rev-parse --local-env-vars` lists: a hook inherits them from the pushing checkout,
#: and any of them would point a query about another checkout back at the pushing one.
LOCAL_GIT_ENV = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_INTERNAL_SUPER_PREFIX", "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
)  # fmt: skip


def git_line(cwd: Path, args: list[str]) -> str | None:
    """One line of git's answer about the checkout at `cwd`, whatever the caller's git
    environment points at; None when git says nothing."""
    env = {k: v for k, v in os.environ.items() if k not in LOCAL_GIT_ENV}
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=2, check=False, env=env
        )
    except OSError, subprocess.SubprocessError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None
