---
name: model-integration
description: "Integrate a new Claude or Codex model into this harness manually: verify availability, pricing, client effort defaults and dispatch routing, then check the selected model and cost evidence. Use when a new model arrives, an alias moves, or dispatch reports missing pricing."
allowed-tools: mcp__agent__*, Read, Edit, Write, Glob, Grep, Bash, AskUserQuestion
---

# Model integration

Make the requested model usable with accountable cost and deliberate routing. This is a manual
workflow. Comparison, scheduled discovery and automatic updates need their own scope.
Use the existing feature/intake and harness maintenance workflow for records, review and landing.

## Verify the model before editing

Inventory the installed client version and its available models without an inference turn.
For Codex, prefer the account's paginated live app-server `model/list`; `codex debug models`
is a bundled fallback, not proof of account access. Record which source was available. For
Claude, check the installed CLI and its native aliases against the provider-specific model
configuration. An alias can select different models on first-party and third-party providers.
Keep native Claude aliases; use explicit Codex model IDs until the owner changes that policy.
An accepted thread configuration that echoes an alias does not prove what model inference uses.
For Claude's observed concrete ID, use the newest finished dispatch record's `resolved_model`
(a field of the dispatch record; the preview does not show it). That is historical evidence, not a
fresh alias lookup. This Python/subscription path has no free preview lookup of the current
resolved Claude ID; if there is no observed run, record that gap rather than starting inference.

Read current primary vendor sources. Check client defaults separately from API defaults: they
can differ. Record URLs, checked date, exact IDs, supported effort levels, default effort,
input/output/cache prices, context thresholds and any service-tier assumptions. Useful sources:

- [OpenAI pricing](https://developers.openai.com/api/docs/pricing) and the target model's page.
- [Codex models](https://learn.chatgpt.com/docs/models) and live `model/list` metadata.
- [Claude model configuration](https://code.claude.com/docs/en/model-config),
  [effort](https://platform.claude.com/docs/en/build-with-claude/effort), and
  [pricing](https://platform.claude.com/docs/en/about-claude/pricing).

Get the owner's row/model and effort choices where not already authorized. A cheaper or newer
model does not automatically replace every row. Distinguish a vendor default from an owner row
policy. Codex `review` and `build` currently pin `medium`; recheck that choice when replacing
their model. Explicit and inherited parent effort win over row policy.
Escalation preserves omission as well as an explicit level.

## Update the actual integration surfaces

Search the current code and consumers rather than treating this inventory as exhaustive:

| Surface | Contract to preserve |
|---|---|
| `src/agent_cli/prices.py` | The one price table. Codex: exact IDs, Standard short/long rates, source/date; cached input is a subset of total input. Claude: running estimates; ordinary input, cache writes and cache reads are disjoint, and all input categories count toward tier boundaries. Unknown future IDs stay unknown. |
| `src/agent_cli/dispatch/usage.py` | The running worker count that applies the Claude rates. SDK final costs remain authoritative; 5-minute cache-write estimates are not a bill. |
| `src/agent_cli/dispatch/rubric.py`, `options.py` | Rows, owner effort policies, budgets, selection precedence, effort provenance and model ladders. Preserve old explicit pins and historical escalation. Do not hard-code vendor defaults. |
| `src/agent_cli/dispatch/lifecycle.py`, `codex_transport.py` | Codex requires a priced target before allocation and inference, including resume. Keep the mid-run unknown-price stop. Claude unknown local estimates do not prevent launch. |
| `src/agent_cli/hooks/repo_guards.py`, `.agents/skills/dispatch/SKILL.md` | Recommendations must name the current rows and effort policy. Search other callers too. |
| `harness/dispatch-rubric.md` | The rubric table and effort guidance. Edit its table with `rubric.py`; `tests/test_rubric.py` checks they agree. |

Retain historical prices and dated model records. Never price an unknown model by prefix or
copy an older model's rates without verification. A missing Codex price must be added and
checked before launch; changing the row alone leaves the worker unable to enforce its soft cap.
If verified prices or access are unavailable, record the specific gap and keep the integration
pending. Do not run a paid discovery probe to fill it without authorization.

## Check and hand off

Run scoped tests and the maintenance checks on the touched consumers. Exercise both context
tiers and their boundary, cache-heavy input, an unknown model, and an old pinned model.
Probe preview and actual attempt setup for unpriced and omitted Codex targets: neither may
allocate a worker or start an inference turn. Keep a priced control and a Claude unknown-estimate
control. Check row policy, explicit precedence and inherited effort through preview, record
and worker options, and that the rubric table and `rubric.py` agree.

Validate this skill if changed. Canonical skills live under `.agents/skills`; Claude discovers
the matching `.claude/skills` symlink. Verify both clients' discovery without an inference turn;
the current session's loaded catalog may need a refresh or a new session.

Give the sole reviewer the artifact and these behavior contracts, then land through the harness.
Recheck the originally blocked selection on landed source: a successful preview proves setup,
not a live dispatch. Use an authorized real run's resolved model, token usage and labeled cost
evidence when available; preserve unknowns honestly. Record sources, choices, checks and any
remaining access or cost gap in the feature. Public evidence must omit private feature links.
