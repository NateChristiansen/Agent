# Dispatch rubric

Which model for which task shape. Read before every dispatch: name the row and give the reason.
A native subagent needs no row. `dispatch new --row <name>` derives the model and
budget from it. The launch reads the Claude, Codex, effort-policy and budget columns from
`src/agent_cli/dispatch/rubric.py`; `tests/test_rubric.py` fails when this table and that code
disagree, so change both together.

Effort normally stays omitted, so the model runs at its vendor's default. Owner policy pins
Codex `review` and `build` to `medium`; recheck it with the owner when changing a row's model.
Plan and code review rounds pin their own models and effort (Review rounds below).
Leave the default only with `--effort` and a choice reason that says why (see Effort below). A
repo that pins its own model (`dispatch.model`) wins over the row, and its `dispatch.effort`
over the default. Escalation keeps the parent's recorded effort, including omission, unless
`--effort` is passed explicitly.

## Rows

| Row | Task shape | Claude | Codex | Effort policy | Budget | Why |
|---|---|---|---|---|---|---|
| probe | smoke test of the harness, a one-command check, a 'does this flag work' | haiku | gpt-6-luna | vendor default | $0-1 | the answer is a fact, not a judgment; the cheapest model finds it |
| review | read a PR or a branch and report; verify a claim; measure something in a repo | sonnet | gpt-6.1-sol | codex: medium (owner) | $3-6 | read-only work with a clear question; a cheap review can still find a real defect. Pass --no-checks: a review reads code, it does not build it |
| mechanical | scaffold from a spec, rename, move, add tests to existing code, apply named review comments | sonnet | gpt-6-luna | vendor default | $6-12 | the shape is known, so a bigger model buys little |
| build | a new package or feature from a plan that leaves design choices open; a change across modules | opus | gpt-6.1-sol | codex: medium (owner) | $10-20 | judgment calls inside the work |
| design | a decision document, options with trade-offs, an ADR draft, an unfamiliar repo | opus | gpt-6-astra | vendor default | $10-20 | the output is reasoning, such as a design document |
| max | only when the owner says so | fable | gpt-6-astra | vendor default | the owner sets it | owner-only model choice; Codex names Astra and still uses its default effort unless --effort is explicit |

A row's budget band raises a lower repo default to its top and never lowers a higher one.
`max` is owner-only and cannot be passed as `--row`. Escalation (`--escalate-from`) steps one
model rung (haiku → sonnet → opus; gpt-6-luna → gpt-6.1-sol → gpt-6-astra); at the top the
model stays unchanged.

## Effort

Claude Code runs Opus 5.5, Sonnet 5.5 and Haiku 5.5 at `medium` when effort is omitted. Each
step needs a reason in `choice_reason`:

- `high` on Opus 5.5 or Sonnet 5.5: reasoning burden (hard concurrency, protocol or
  cross-module design work) or a measured failure at the default.
- `xhigh` on those two: a measured failure at `high`, or an explicit owner choice.
- `low`: short, scoped, latency-sensitive work that is not intelligence-sensitive.
- Codex `high`: hard reasoning, complex debugging or high-value review; `xhigh` only after a
  measured gain over `high`.

Duration is never an effort reason: a long job gets a longer `timeout_minutes`, and higher
effort makes a run slower. A level the model does not support is refused by its provider when
the worker starts.

## Review rounds

The `dispatch` actions `plan-review` and `code-review` do not use these defaults. Their round is
the owner's measured review policy, passed explicitly on every launch: two blind reviewers,
Codex `gpt-6.1-sol` at `high` and Claude `sonnet` at `high`, then one Codex `gpt-6.1-sol`
verifier at `high`, all on the `review` row's budget and with no preview. A repo's
`dispatch.model` pin does not replace them, and the row's `codex: medium` policy and the vendor
defaults above do not apply. The reviewers are told to return every finding, in full in the
structured return with no cap, and every finding goes to the verifier. The process is in [[harness/sessions#Plan review]]. A
`review`-row worker launched with `new` (a claim to check, a measurement) still follows the row
and the Effort rules.

Calibration rows live in the owner's private `harness/owner/calibration.md` (ADR-0012); `/retro`
appends them for a finished feature's dispatches and fills in each row's note.
