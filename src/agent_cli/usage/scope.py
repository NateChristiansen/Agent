"""Which cwds are harness usage, and which dispatch a session or thread belongs to.

The owner's rule: the vault root, a harness worktree, a registered product clone or
one of its worktrees, or a harness temp dir. Anything else is out, even when it works on Agent.
The cwd is read from the transcript or rollout itself; a Claude project folder name is lossy
(every non-alphanumeric becomes `-`) and is never the deciding evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..records.narrow import as_dict, as_str

Kind = Literal["vault", "eval", "harness"]

EVAL_PREFIXES = ("agent-eval-manager-", "agent-dogfood-")
TEMP_PREFIXES = ("agent-codex-review-",)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


@dataclass
class Scope:
    vault: Path
    trees: tuple[Path, ...] = ()
    problems: tuple[str, ...] = ()
    _cache: dict[str, Kind | None] = field(default_factory=dict[str, "Kind | None"])

    @classmethod
    def for_root(cls, root: Path) -> Scope:
        from .. import repos as repos_mod
        from .. import worktree as worktree_mod
        from ..core import AgentError

        vault = _resolved(root)
        trees = [worktree_mod.worktrees_dir(vault)]
        problems: list[str] = []
        try:
            registry = repos_mod.load_registry(vault)
        except AgentError as exc:
            problems.append(f"product clones not scoped: {exc}")
        else:
            for repo in registry.repos:
                clone = repo.abs_path(vault)
                trees.append(clone)
                trees.append(
                    repo.abs_worktrees(vault)
                    or clone.parent / repos_mod.WORKTREES_PARENT / repo.short.lower()
                )
        return cls(vault, tuple(_resolved(t) for t in trees), tuple(problems))

    def kind(self, cwd: object) -> Kind | None:
        """`vault` for the vault root, `eval` for an eval or dogfood temp vault, `harness` for any
        other in-scope cwd, None for a cwd outside the rule."""
        if not isinstance(cwd, str) or not cwd:
            return None
        if cwd not in self._cache:
            self._cache[cwd] = self._kind(Path(cwd))
        return self._cache[cwd]

    def _kind(self, cwd: Path) -> Kind | None:
        path = _resolved(cwd)
        if path == self.vault or self.vault in path.parents:
            return "vault"
        if any(name.startswith(EVAL_PREFIXES) for name in path.parts):
            return "eval"
        if any(name.startswith(TEMP_PREFIXES) for name in path.parts):
            return "harness"
        if any(path == tree or tree in path.parents for tree in self.trees):
            return "harness"
        if path.parent == self.vault.parent and path.name.startswith(f"{self.vault.name}-wt-"):
            return "harness"
        return None


def dispatch_class(d: Mapping[str, object]) -> str:
    """`probe` wins over `worker`; a `review` row is a reviewer."""
    if d.get("kind") == "probe":
        return "probe"
    return "reviewer" if d.get("row") == "review" else "worker"


def dispatch_index(records: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """Claude session id or Codex thread id to the class of the dispatch that ran it. Every
    attempt's session is included, so a resumed attempt in a fresh session is still a worker's."""
    index: dict[str, str] = {}
    for d in records:
        cls = dispatch_class(d)
        ids = [d.get("session_id"), d.get("codex_thread_id")]
        ids += [as_dict(attempt).get("session_id") for attempt in as_dict(d.get("cost_attempts")).values()]
        for key in ids:
            if text := as_str(key):
                index[text] = cls
    return index
