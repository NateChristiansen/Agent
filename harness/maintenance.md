---
tags: [doc]
---

# Maintaining this harness

Read before editing the CLI, hooks, skills, settings, instructions, or setup. Product code
still goes through `/dispatch`. This repository's CLI and tests are manager-owned.

## Instructions and clients

AGENTS.md is the shared always-loaded instruction file. Do not add CLAUDE.md or an import
shim. Claude Code reads AGENTS.md natively unless a competing Claude instruction file at or
above the root suppresses it. Codex does not follow `@path` imports. Verify changed client
behavior before changing this arrangement; see `doctor/checks_instructions.py` and
[[harness/provider-gaps]].

Keep shared canon here. Claude settings live in `.claude/`; Codex hooks live in `.codex/`.
Read both configs and the handler before calling a rule enforced. Claude's native permissions
do not apply to Codex. Verify generated-file protection live in both clients after a wiring
change; neither a hook name nor a unit test proves live parity.

Claude selects the caveman-lite output style; no repo hook injects or enforces the voice on
either client, so the shared style floor in AGENTS.md remains necessary. `vendor/caveman` is a
submodule that supplies the `/caveman` skill, never hand-edited; update it with
`git submodule update --remote vendor/caveman`, then rerun setup.

## Private and generated paths

`src/agent_cli/notes.py:PRIVATE_SET` is the source of truth for private paths. Currently these
include features, decisions, journal, usage, the owner's notes and harness docs, voice samples,
agent.yaml, .mcp.json, and generated Bases. `agent setup --notes <path>` links them
from a private notes repo; without one, setup uses tracked examples. Do not copy private data
into public files.

With `notes_sync: on` in `agent.local.yaml`, private paths commit and push themselves at each
turn's end (ADR-0034). With the switch off, call the `harness` tool with `action="notes_root"`
and use `git -C <notes-root> add <relative-path>` there. Public source, ADRs, and shared
harness and config files commit here; see [[harness/safety#Publication]] for the push boundary.

Dashboard.md and Index.md are ignored plain files, never links. Obsidian does not
watch a symlinked file at the vault root. Generated Bases live in the private directory.
Use `status render` to regenerate; edit templates or sources instead of outputs.
`harness/dispatch-rubric.md` is a hand-kept table; `tests/test_rubric.py` checks it against
`src/agent_cli/dispatch/rubric.py`, so change both together.
Obsidian view scripts format data; domain rules stay in Python.

## Tests and records

The CLI uses Python 3.14 and uv. Before a CLI commit, run ruff and pyright on the touched
files and the selected tests: `uv run ruff check`, `uv run ruff format --check`,
`uv run pyright`, `uv run pytest tests/test_x.py`. The full suite runs once, at landing
(ADR-0045). Run each as its own command. Setup scripts also require tests. Read [[harness/testing]]
for changes whose correctness depends on ordering. The pre-commit gate is not a replacement
for the full checks. Tool calls run current source; reconnect MCP only after a tool schema changes.
Comments and docstrings under `src/` carry no history tag (a feature, task, lesson or audit
id); only `tests/test_prose_rules.py` states that rule. Run it with the selected tests before
a small change lands.

A dispatch attempt runs only the tests its diff selects (`tests/selection.toml`, read from the
attempt's base). A change that adds a source module adds its table entry in the same commit:
a bundle naming the tests that consume it, or a broad rule with a reason and the tests that cover
it. `tests/test_select.py` fails while a module maps to nothing, and the reviewer checks that the
entry names the real consumers, not only the module's own test file. An unmapped path adds the
fallback tests; the full suite runs only at landing (ADR-0045).

### Test admission

`tests/budget.toml` caps the suite, and three guards hold it (feature 084). Each failure names the
guard and the budget entry that would allow it.

- **Suite budget.** Landing refuses a full pass record over `[suite]` `tests_max` or
  `spawns_max`. Both are load-independent counts; wall time is advisory.
- **Per-test spawn ceiling.** `tests/suite_cost.py` fails a test that starts more than 100
  processes in its call phase, unless `[slow]` names it with its own limit. It runs in
  dispatch-selected runs too. `suite_cost.uncounted()` is reserved for the shared record cache in
  `tests/land_helpers.py`; the hygiene scan refuses it anywhere else.
- **Hygiene scan.** `tests/test_suite_hygiene.py` refuses trivial asserts, duplicate test bodies,
  tests with neither an assert nor `pytest.raises`, or only mock-call asserts (unless in
  `[weak]`), a file's `time.sleep`
  count above its `[sleep]` value, and a skip or xfail without a reason. An xfail, an
  unconditional skip or a non-platform `skipif` also needs an unexpired `[[skip]]` entry.
  Grandfather entries that are no longer needed fail too, so the lists only shrink.

Tightening the file needs no raise, though the record must still fit the tighter limits.
Loosening it, by raising a number, adding a grandfather entry or extending an expiry, lands only
with a new `[[raise]]` entry (`date`, `from`, `to`, `reason`, `approved_by`) and the owner's
matching `budget_raise` on `land`. `repos_sync` needs the same `budget_raise` to accept a pushed
tip that loosens the budget, or that exceeds it when this machine holds the tip's pass record;
without a local record it compares only the budget files. Pass `budget_raise`
only on the owner's say-so; prefer cutting tests or spawns over a raise.

When a test gives a result that looks impossible on its platform, list the autouse fixtures and
monkeypatches it runs under before forming a platform theory. A Windows test took three CI
rounds to chase a live process that vanished from the process inventory between two calls. The
cause was the autouse conftest fixture `_no_real_reaping`, which replaces `procs.snapshot` with
an empty list; one native test never restored the real snapshot. The reviewer had theorised
about psutil and Windows process reads without reading the fixtures.

## Live checks

Some behaviour shows only in the client's UI: a permission prompt, a tool approval, a reconnect.
Neither a successful tool result nor a passing unit test proves what the owner saw. A tool call
that returned without a prompt may have matched an allow rule, or the owner may have approved it.
When a change depends on prompt behaviour, ask the owner what appeared and record their answer
in the feature log as their observation, with the client and the call.

Before asking the owner to reconnect the MCP server, check that no long MCP operation is still
running, such as `verify_record`, which runs the full suite inside the call. A reconnect during
one cuts it off and leaves its outcome unknown. Wait for it to finish, then ask. A `land` returns
at once and runs detached, so its log, not the tool call, says when it is done.

A change to the dispatch entry path (anything that runs between `dispatch new` and the worker's
start) is accepted only after one real dispatch from each client, right after it lands. A change
once tested only with provider doubles fetched the notes remote at launch, and the first real
Codex dispatch refused with "notes remote unreachable" and blocked another feature's launch.

## The CLI surface

Models reach the CLI only through the MCP (ADR-0033); the `cli-route` hook refuses a model's
shell call to `bin/agent`. A CLI command a model needs therefore needs an MCP tool or a `harness`
action in `mcp/specs_harness.py`; nothing checks that every command has one. List it in
`src/agent_cli/mcp/coverage.py` (`CLI_TO_MCP`, or `PERSON_ONLY` with the reason) so the
`cli-route` refusal names the tool. Messages an op returns may name CLI commands; `mcp_wording`
turns the mapped ones into tool calls for the model.

Hook rules. A hook never raises and always exits 0; it prints only when it has something to say
(session-start context, a PreToolUse deny). A PreToolUse hook that times out fails open, so it
must finish well inside its timeout on the real session store: keep hooks to file reads and
small writes, and never scan the process table in one. The plan gate did, took 6.5 s against a
5 s timeout, and let an unreviewed plan through. When a hook edit spans files, write the
defining module before the file that imports it; the other order broke every hook for a minute.
Every hook has pytest coverage under `tests/`, and a new hook is wired on both clients where
both have the event (`.claude/settings.json`, `.codex/hooks.json`).

## Hook groups

Each client event and matcher starts one hook process. Before, a Bash call
started nine PreToolUse and two PostToolUse processes, each a Node loader and a Python
interpreter. `src/agent_cli/hooks/groups.py` lists the groups: Claude `start`, `prompt-submit`,
`stop`, `pre-bash`, `pre-mcp`, `pre-ask`, `post-tool`; Codex `codex-start`,
`codex-prompt-submit`, `codex-pre-bash`, `codex-stop`. An event and tool with one hook still
wires that hook by name. `notes-sync` stays its own command at Stop and UserPromptSubmit,
since its network git would hold up every other member, and so do the SessionEnd hooks,
because Codex gives SessionEnd a 1 s default timeout.

A group runs its members in their old order, each exactly as `agent hook <member>` runs it
alone: its owner switch, its failure log, the switch note on a deny, the denial count. Each
member gets its own copy of the payload, and members run one after another in one thread; one
that raises fails open alone. A Claude group's wired timeout is the sum of its members' old
timeouts plus 5 s; a Codex group keeps Codex's 600 s default, which its members had. A hung
member holds up the rest of its group until that timeout, so keep members fast. Outputs
combine in member order: the first PreToolUse deny wins and ends the group, then the first
`ask`; a UserPromptSubmit block wins and ends the group; Stop blocks join their reasons; every
`additionalContext` joins in order. A deny, an ask or a lone output is passed on unchanged.

To add a hook to an event that already has a group, add a `Member` with its budget (and its
own matcher if it is narrower than the group's), then set the group's `timeout` in the config.
`tests/test_hook_groups.py` pins the members per event and tool against the pre-group wiring,
one process per tool call, and each group's timeout. Changing `.codex/hooks.json` makes Codex
skip the changed hooks until the owner re-trusts them in `/hooks`; the old member names stay
valid commands, but nothing runs them in the meantime, so re-trust right after updating.

Prefer a hook or CLI validation for mechanically decidable rules, a skill for occasional
procedures, and a product repo's own harness for its conventions. Use `/lesson` for mistakes:
keep findings tracked until they have a binding mechanism or justified prose loaded where a
decision is made. A warning alone is not enforcement. Protect required behavior with tests;
a hook or permission decision can be pinned as a row in `tests/policy_corpus/`. Search files for
reference information instead of growing always-loaded instructions. Architectural decisions
get `/adr`. No harness changelog is needed; the commit explains its diff.
