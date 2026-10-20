"""Map stable dispatch rows to provider models, effort policy, budget bands and escalation ladders.
`harness/dispatch-rubric.md` describes task fit; `tests/test_rubric.py` checks agreement.

Omit effort unless selected: explicit effort wins, then escalation-parent effort, then repository
effort or row policy. Providers reject unsupported effort
levels. Stable row names are persisted on dispatch records and used by the subagent guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import repos as repos_mod
from ..core import AgentError
from ..records.narrow import as_str
from .state import DEFAULT_BUDGET_USD

CLAUDE = "claude"
CODEX = "codex"


@dataclass(frozen=True)
class Row:
    """One task shape: its model per provider, budget band and the owner's effort policies."""

    name: str
    models: dict[str, str]
    budget: tuple[float, float] | None
    efforts: dict[str, str] = field(default_factory=dict[str, str])
    owner_only: bool = False

    def model(self, provider: str = CLAUDE) -> str:
        return self.models[provider]


# Budgets are a stop and cost control, separate from effort. The Codex column is the owner's
# task-shape choice from OpenAI's model-selection guide, not a measured equivalent.
ROWS: dict[str, Row] = {
    r.name: r
    for r in (
        Row("probe", {CLAUDE: "haiku", CODEX: "gpt-6-luna"}, (0.0, 1.0)),
        Row("review", {CLAUDE: "sonnet", CODEX: "gpt-6.1-sol"}, (3.0, 6.0), {CODEX: "medium"}),
        Row("mechanical", {CLAUDE: "sonnet", CODEX: "gpt-6-luna"}, (6.0, 12.0)),
        Row("build", {CLAUDE: "opus", CODEX: "gpt-6.1-sol"}, (10.0, 20.0), {CODEX: "medium"}),
        Row("design", {CLAUDE: "opus", CODEX: "gpt-6-astra"}, (10.0, 20.0)),
        Row("max", {CLAUDE: "fable", CODEX: "gpt-6-astra"}, None, owner_only=True),
    )
}

#: Escalation steps one model rung and keeps the parent's effort, including omission.
#: At the top model both stay unchanged. `fable` is owner-only and off the ladder.
MODEL_LADDERS: dict[str, tuple[str, ...]] = {
    CLAUDE: ("haiku", "sonnet", "opus"),
    CODEX: ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"),
}


def pick(provider: str, name: str) -> Row:
    """The row, or an error naming what exists. Never a default: the rubric's point is that the
    choice is stated, and a silent fallback is the unstated choice wearing a name."""
    row = ROWS.get(name)
    if row is None or provider not in row.models:
        raise AgentError(f"no {provider} rubric row {name!r}; rows: {', '.join(sorted(ROWS))}")
    return row


def select_row(provider: str, row: str | None) -> Row | None:
    """Validate the provider and row before anything is allocated; the owner-only row is refused."""
    if provider not in (CLAUDE, CODEX):
        raise AgentError(f"provider must be claude or codex; got {provider!r}")
    row_obj = pick(provider, row) if row else None
    if row_obj is not None and row_obj.owner_only:
        raise AgentError(
            f"rubric row {row_obj.name!r} is the owner's to choose; pass --model/--effort explicitly "
            "and say in --choice-reason that they asked for it"
        )
    return row_obj


# ------------------------------------------------------------------ budget


def resolve_budget(budget: float | None, reason: str | None, default: float) -> float:
    if budget is None:
        return default
    value = float(budget)
    if value <= 0:
        raise AgentError("budget_usd must be positive")
    if value > default and not (reason or "").strip():
        raise AgentError(
            f"budget_usd {value:.2f} is above the repo default {default:.2f}; say why in "
            "`budget_reason` (`--reason` on the CLI)"
        )
    return value


def repo_budget(rp: repos_mod.Repo, default: float) -> float:
    """An unreadable value refuses rather than falling back, because the default can be higher
    than the cap the owner meant."""
    raw = rp.dispatch.get("max_budget_usd")
    if not raw:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise AgentError(f"{rp.short}: dispatch.max_budget_usd must be a positive number; got {raw!r}")
    try:
        return float(raw)
    except OverflowError:
        raise AgentError(f"{rp.short}: dispatch.max_budget_usd is too large; got {raw!r}") from None


def launch_budget(
    rp: repos_mod.Repo, row: Row | None, budget: float | None, reason: str | None
) -> tuple[float, float, str]:
    """The repo default, the effective budget, and where the effective one came from:
    `explicit`, `row`, `repo` (dispatch.max_budget_usd) or `default` (the harness default).

    A row's band raises a small repo default and never lowers one: a repo whose runs are a tree
    of delegate sessions sets a budget far above any band, and a small band could replace such a
    budget and stop the worker early."""
    default = repo_budget(rp, DEFAULT_BUDGET_USD)
    asked = budget
    if budget is None and row is not None and row.budget and row.budget[1] > default:
        asked = row.budget[1]
    if budget is not None:
        source = "explicit"
    elif asked is not None:
        source = "row"
    else:
        source = "repo" if rp.dispatch.get("max_budget_usd") else "default"
    return default, resolve_budget(asked, reason, default), source


# ------------------------------------------------------------------ model and effort


def escalate(
    old: dict[str, Any], rp: repos_mod.Repo, model: str | None, effort: str | None
) -> tuple[str | None, str | None]:
    """Step up the old provider's model ladder and keep its recorded effort."""
    from .options import next_rubric_tier

    provider = str(old.get("provider") or CLAUDE)
    # A repo's pinned model names a Claude model; Codex uses the parent's recorded model.
    repo = rp.dispatch if provider == CLAUDE else {}
    old_model = old.get("model") or as_str(repo.get("model")) or None
    old_effort = old.get("effort") if effort is None else effort
    if old_effort is not None and old_effort not in repos_mod.EFFORT_LEVELS:
        raise AgentError(f"parent dispatch has invalid effort {old_effort!r}; pass --effort")
    next_model, next_effort = next_rubric_tier(old_model, old_effort, provider)
    return model or next_model, effort if effort is not None else next_effort


def resolve(
    rp: repos_mod.Repo,
    provider: str,
    row: Row | None,
    model: str | None,
    effort: str | None,
    choice_reason: str | None,
    parent_id: str | None = None,
) -> tuple[str | None, str | None, str | None, str]:
    """`(model, effort, choice_reason, effort_source)` for a new dispatch.

    Model: explicit, else the repo's pinned `dispatch.model` (Claude only), else the row. Effort:
    explicit or the parent's, else the repo's `dispatch.effort` or the row's owner policy,
    else nothing. Every source is in the choice reason."""
    if rp.dispatch.get("model_from"):
        raise AgentError(
            f"{rp.short}: dispatch.model_from is no longer read; remove it from agent.yaml, "
            "and pin a model with dispatch.model if the repo needs one"
        )
    reasons = [choice_reason] if choice_reason else []
    source = "parent" if parent_id else "passed" if effort is not None else "default"
    if parent_id:
        label = f"recorded effort {effort}" if effort is not None else "omitted effort"
        reasons.append(f"parent dispatch {parent_id} {label}")
    if row is not None:
        reasons.append(f"rubric row {row.name} (harness/dispatch-rubric.md)")
    if provider != CLAUDE:
        # A repo's model and effort name Claude models; Codex takes its row or an explicit model.
        model = model or (row.model(provider) if row is not None else None)
        if effort is None and not parent_id and row is not None and provider in row.efforts:
            effort, source = row.efforts[provider], "row-policy"
            reasons.append(f"owner policy: {provider} row {row.name} effort {effort}")
        return model, effort, "; ".join(reasons) or None, source
    if model is None:
        pinned = as_str(rp.dispatch.get("model")) or None
        if pinned:
            reasons.append(f"{rp.short} pins {pinned} (agent.yaml dispatch.model)")
        model = pinned or (row.model(provider) if row is not None else None)
    if effort is None and not parent_id and (repo_effort := as_str(rp.dispatch.get("effort"))):
        effort, source = repo_effort, "repo"
        reasons.append(f"{rp.short} sets effort {effort} (agent.yaml dispatch.effort)")
    return model, effort, "; ".join(reasons) or None, source
