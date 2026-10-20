---
tags: [doc]
---

# Rules and what holds them

A rule written only as prose holds until a session forgets it. An audit listed twelve
prose rules that a check, hook or generated text could hold. Each row says what holds it now, or
why it stays prose. A row that says "prose only" is a known gap, not a settled choice.

| # | Rule (where it is written) | Held by | Status |
|---|---|---|---|
| 1 | Never use `request_user_input_async` (AGENTS.md, Start and route) | `ask-guard`, only when the reply's final text ends in a question it detects: an ask through that tool registers no text, so it does not cover that question and the Stop is blocked once (`src/agent_cli/hooks/ask_guard.py`) | partial: nothing refuses the call itself, and a turn whose final text holds no detected question stops normally |
| 2 | Ask the owner about unchecked tasks before closing (`/close`) | `feature_close` refuses while a task is open (`src/agent_cli/ops/feature_close.py`) | check |
| 3 | A new `.agent/` subtree goes in the registry that feeds both deny lists (`harness/sessions.md`, `hooks/paths.py`) | `core.AGENT_SUBTREES` feeds both. Three layers, each enforcing only what it says. (1) Runtime: `core.agent_path`, `agent_parts` and `agent_root(...) / x` raise for a subtree in neither `AGENT_SUBTREES` nor `AGENT_UNREGISTERED`, and for a component that leaves it (`..`, a separator, a drive). (2) Static, `tests/test_prose_rules.py`: outside `core.py`, no `.agent` literal (string, f-string part, bytes), no `AGENT_DIR`, and no `agent_dir_name()` in a path expression; prose may mention `.agent/...` only in a docstring or as a direct argument of `help=`/`description=`, an exception or a tool description. It does not see a name built by concatenation or `chr()`. (3) Dynamic, `tests/conftest.py`: at the end of each test session, every directory directly under any `.agent/` in pytest's basetemp must be registered, however it was spelled; it covers only code the tests run, under basetemp | check, in layers; code no test runs, spelling the name in a way the static ban cannot see, is not covered. Whether to protect the unregistered ones is open |
| 4 | Never write the owner's `notes/` (AGENTS.md) | nothing | prose only: a deny needs its own `protect-generated` rule and message (notes are not generated), a permission change left to the harness |
| 5 | No Obsidian moves (AGENTS.md, Standing rules) | `tests/test_docs_refs.py`: every `[[wikilink]]` and `#heading` in tracked Markdown resolves | check for this repo; links inside the notes repo are not checked |
| 6 | The removable and protected hook lists (`harness/standing-rules.md`, `harness/agent-yaml.md`) | `tests/test_prose_rules.py` against `toggles.TOGGLEABLE` and `run.HANDLERS` | check |
| 7 | Hand copies of code lists: Obsidian views (`harness/obsidian.md`), journal properties (`harness/journal.md`) | `tests/test_prose_rules.py` against `harness/obsidian/views/*.js` and `journal.block.meta_for` | check |
| 7 | The same, written as sentences: the doctor rows (README), `PRIVATE_SET` (`harness/maintenance.md`) | nothing | prose only: they summarise in English rather than list; a generated section is the fix |
| 8 | A new module adds its `tests/selection.toml` entry (AGENTS.md) | `tests/test_select.py` fails on an unmapped module | check |
| 9 | Small changes (about 50 non-test lines) skip dispatch (AGENTS.md) | nothing | prose only: the route is a judgment; a land-time report of non-test lines is a harness feature |
| 10 | A task added after plan approval needs the owner's sign-off (AGENTS.md) | nothing | prose only: `task_add` cannot see the plan's approval state yet; a harness feature |
| 11 | Each SHA, id and command in backticks (AGENTS.md, `/dispatch`) | nothing | prose only: a warning on bare ids needs a noise measurement first; a harness feature |
| 12 | Every action is described in its tool's description (`mcp/specs_*.py`) | `tests/test_docs_refs.py` | check |

`tests/test_docs_refs.py` also holds the wider rule behind rows 5 and 12: every `agent` command
and flag, MCP tool and action, repo path and hook name a doc cites must exist in the code. A tool
name is checked when it is written as code (`` the `dispatch` MCP tool ``, a list of them, bold
or linked) or in snake_case before "tool"; a bare English word before "tool" cannot be told
from prose and is not checked, so write tool names in backticks.
