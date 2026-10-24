---
tags: [doc]
---

# Provider gaps

What one client can do that the other cannot, what the harness does about each gap, and when to
look again.

The harness aims at one mechanism with two thin wrappers: `bin/agent hook <name>` holds the
behaviour, and `.claude/settings.json` and `.codex/hooks.json` only wire it. This file is the list
of places that aim does not reach, so a gap is a tracked decision rather than a silent difference.

Each measurement is on the client versions and date its section or row names. The earliest were
taken on **Claude Code 2.1.280** and **codex-cli 0.155.1** on 2026-09-23; later ones name newer
versions. Re-measure when either moves; the version is part of the claim.

## What is the same

Worth stating first, because it is most of it. Both clients run command hooks, pass a JSON payload
on stdin with `session_id`, `cwd`, `hook_event_name`, `tool_name` and `tool_input`, and read a JSON
object back on stdout. Both honour `hookSpecificOutput.additionalContext` on `SessionStart` and
`UserPromptSubmit`, `decision: "block"` on `UserPromptSubmit`, and `permissionDecision` with
`permissionDecisionReason` on `PreToolUse`. Both skip a subagent's events for us the same way,
through `agent_id` / `agent_type` on the payload.

A shell call arrives in the same shape on both: `tool_name` is `Bash` and the command line is at
`tool_input.command`. Measured on codex-cli 0.155.1 by the Codex manager in an isolated project:
two shell commands produced two distinct `PreToolUse` events, each with its own `tool_use_id`, and
a `deny` stopped the first before it ran while the model reported the reason and went on to the
second. A `UserPromptSubmit` `block` stopped a turn before any tool call.

`peer send` queues the message, then requests a turn through `codex queue --thread` or Claude's
per-session messaging socket. The recipient's `UserPromptSubmit` hook drains the queue when a
turn starts. A successful queue command or socket write is not a delivery receipt. A live
Claude socket write on 2026-09-24 left the recipient idle with two messages pending, so automatic
wake is not yet verified. The synthetic socket test proves the harness wiring only.

So `hooks/session.py:session_start` — the reorientation block, the feature mapping, the nags, the
peer inbox — runs on both with no provider branch at all. `dispatch-inbox` also drains peer
messages on `UserPromptSubmit`, so a running manager reads them at its next turn.

Codex's payloads are richer: `SessionStart` requires `model`, `permission_mode` and
`transcript_path`, none of which Claude supplies.

## The gaps

| Capability | Claude Code | Codex | What the harness does | Revisit when |
|---|---|---|---|---|
| Hook-driven session rename | `hookSpecificOutput.sessionTitle` | none, and its output schemas are `additionalProperties: false`, so sending it risks the whole output being rejected | `session_start` emits `sessionTitle` only for Claude. Codex's Obsidian tab uses `hooks/activity.py`; its saved chat name for `codex resume` is left to the owner (the app-server `thread/name/set` writer was removed: it spawned an app-server on every prompt and Stop, and no label depends on it) | Codex adds a rename field |
| Resume startup | `SessionStart` runs on resume | `codex resume` omitted `SessionStart` before a prompt on 0.155.1 and 0.156.1 | The first `UserPromptSubmit` from a new process injects the reorientation block, clears the closed tab's saved label and claims the active Terminal tab. If a later version emits `SessionStart source=resume`, the same tab claim runs there | Codex runs `SessionStart` before the first resume prompt |
| Clear thread replacement | Claude `/clear` not measured | On macOS 0.160.0, `SessionStart(source=clear)` identifies a new thread but supplies no predecessor or stable TUI connection identity; the old end event can arrive later | No automatic mapping or tab inheritance. Manual `feature_use` restores feature routing but leaves the old tab label and glyph. See [[#Clear thread replacement]] | Codex exposes a predecessor ID, stable TUI connection ID, or a thread-replacement event that positively links both threads |
| Session cost in dollars | transcript tokens, without a dollar total in Stop | rollout tokens, without a dollar total in Stop | Both clients price manager sessions and their children on `Stop`, recording a local `price-estimate`; guardian tokens use the parent model rate | Either client supplies a complete price on Stop |
| `SessionEnd` hook output | honoured | **input only** — Codex defines no `session-end.command.output` | nothing reads output there anyway; `capture`, `codex-activity` and `session-reap` all act by side effect | — |
| `Stop` nudge output | `hookSpecificOutput.additionalContext` continues without an error notice | that field is invalid; `decision: "block"` with `reason` starts a continuation turn | No Stop hook nudges the model. The informational `journal-pending` nudge runs on the next `UserPromptSubmit`, where both clients accept `additionalContext` | Codex accepts non-error model-visible Stop feedback |
| Output style | `outputStyle` in settings, a project style under `.claude/output-styles/`, sent with every request | none | the `caveman-lite` style is Claude only (ADR-0025). A Codex session gets the voice from `AGENTS.md` alone. Nothing enforces it on either client | Codex adds output styles or equivalent persistent instructions |
| `PostToolUseFailure`, `PermissionDenied` | both exist and carry `thrash-guard` | neither exists | thrash detection on Codex sees successes only, so it under-counts | Codex adds failure events |
| `PreCompact`, `PostCompact`, `SubagentStart` | all three (hooks docs, fetched 2026-10-12); `PostCompact` is a side effect only, with no decision control and no context injection (fetched 2026-10-13) | all three | managers use none of them; a Claude worker's settings wire `precompact` (`dispatch/settings.py` `build_settings`). The reorientation block stays on `SessionStart` `source: "compact"`, because Claude's `PostCompact` cannot inject context | Claude's `PostCompact` accepts model-visible context |
| Per-call tool permissions | allow/deny rule strings over named tools (`Bash(git status:*)`), plus an SDK `can_use_tool` callback | sandbox mode (`read-only` / `workspace-write` / `danger-full-access`) plus `.codex/rules` execpolicy prefix rules | Workers: one rule set (`dispatch/rules.py`) decides both, through `can_use_tool` on Claude and a worker PreToolUse hook plus the sandbox policy on Codex (`codex_permissions.py`, `codex_policy.py`). The manager's `.codex/rules` file is hand-kept and nothing in `src/` reads it ([[harness/worker-permissions#What a Codex manager may run without asking]]) | Codex adds a per-call permission callback |
| Worker SDK transport | `claude-agent-sdk` streams events and exposes `can_use_tool` to the supervisor | The Python [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) is `openai-codex`. It provides streaming, steer/interrupt, per-turn model and effort, thread resume/fork, and `TurnResult.usage`; the 0.156.1 SDK has no per-call permission callback | Codex workers run over the app-server protocol (`dispatch/codex_transport.py`), not the SDK. Access is the sandbox policy plus the worker PreToolUse hook, since no callback exists; `dispatch new --dry-run` scores the work order's commands against it before launch | the SDK or app-server adds a per-call permission callback |
| Project instruction file | `CLAUDE.md`, or `AGENTS.md` natively from 2.1.277 | `AGENTS.md`, walked root to cwd at launch | one `AGENTS.md`, no `@path` imports — Codex does not expand them and renders the line as literal text (ADR-0021) | — |
| Path-scoped instructions | a subdirectory's `AGENTS.md` loads when a file there is Read | walked once at launch; nothing below the start directory loads | neither is a home for rules. Skills carry what is procedural (ADR-0021) | either client changes its trigger |
| `.claude/rules/` | loaded | not read | not a home; its content belongs in `AGENTS.md` or a skill | — |
| `PreToolUse` `ask` | prompts the owner | **parsed and unsupported** — the hook fails and the call proceeds | no guard may return `ask` on Codex; each one needs an explicit `allow` or `deny` there. The owner's ask-before-edit rule for `bin/agent`, hooks and owner files cannot be enforced by `apply_patch` until a guard can recognize a separately recorded authorization; the Bash CLI route guard does not cover file edits | Codex implements `ask` or the harness adds a recorded-authorization mechanism |
| Hook trust | wired in settings, live immediately | **trust by exact definition hash.** A new or changed hook is skipped until the owner reviews it with `/hooks` | every change to `.codex/hooks.json` needs the owner to re-trust before it takes effect. Say so when handing one over. Managed hooks can be trusted by policy; `--dangerously-bypass-hook-trust` covers one automation invocation and is not a setup step | Codex allows a trusted-by-config path |
| Hook command paths | `$CLAUDE_PROJECT_DIR` expands | no equivalent, and **hook commands run from the session cwd** | a relative `bin/agent` silently gets no hooks whenever Codex starts in a subdirectory, so commands resolve from the git root instead | Codex adds a project-dir variable |
| Plan approval | `ExitPlanMode`, a tool a PreToolUse hook can deny | none. Questions have an interactive route only in Plan mode (`request_user_input`). In Default mode, `request_user_input_async` returns `{"accepted":true}` but its questions never reached the owner in the VS Code / ribbon frontend (none answered in testing, 2026-10-09 to 11, codex-cli 0.162). Default-mode questions go in visible text plus `owner_question`. Neither is an approval tool, so in Default mode approval is still asked in chat | One fresh `plan-review` dispatch on the other client per plan version ([[harness/sessions#Plan review]]). Claude gets the hard `plan-gate` on `ExitPlanMode`; Codex follows the same steps by instruction and has no plan hook. Claude's plan mode asks before tools that write, so `plan-mode-allow` exempts `peer`, the mapping tools and the `plan-review` dispatch action; Codex needs no such hook | Codex adds a plan-approval tool |
| Peer wake delivery | one JSON frame on the session's inbox socket; a frame that lands mid-turn is handled in that turn and never runs as the wake prompt | `codex queue --thread` holds the wake prompt until the turn ends | Claude acknowledges the wake reservation on any inbox read and hands over unread messages at Stop (`peer-stop`); Codex acknowledges only on the queued wake prompt | a Codex wake is seen lost, or Claude gains a durable queue |
| Notes repo sync from hooks (ADR-0034) | `Stop` may add context; a cross-session message runs no `UserPromptSubmit` | `Stop` cannot add context; `SessionEnd` defaults to a 1 s timeout; a resumed thread under the shared daemon has no provable pid | Commit and push run synchronously at `Stop` on both, never at `SessionEnd`. Failures are kept and reported at the next prompt or session start on both. A prompt pulls once per client process; when the process is unproven, it pulls unless a pull succeeded in the last 120 s, and always when a message names a notes commit the checkout lacks. A trusted Codex hook child reached GitHub over SSH on macOS and over HTTPS through Git Credential Manager on Windows (codex-cli 0.160.0, 2026-10-02 and 10-03); the hooks set `GCM_INTERACTIVE=never` | Codex accepts `Stop` context or exposes a resume generation |
| claude.ai connectors (Notion, Slack, Gmail, Google Drive/Calendar) | Account-level and deferred: absent from `.mcp.json`; local permissions and doctor cannot see them. An authenticate-only connector is not connected. | None | Use as ordinary Claude tools. Do not add them to repo config or assume another session, Codex peer, or person has them. | — |

## Clear thread replacement

Measured on codex-cli 0.160.0, macOS and Windows ribbon, 2026-10-01 to 02. After `/clear` the
new thread's `session_meta`, `thread/read` and `thread/loaded/list` carry no predecessor,
parent, client or connection key, and `UserMessage.client_id` changes between prompts. Title,
cwd, timing or "the only live TUI" would be guesses, not identity. The old thread's
`SessionEnd(reason=other)` arrives about a minute later, and an ordinary quit showed the same
delay. `hooks/activity.py` maps that end to `ended`, which `obsidian_tabs.py:label_for` paints
with the at-rest green check, so an ended tab looks idle.

In force: manual `feature_use` restores feature routing only; the tab's title and glyph stay
stale. Candidate fix, not built: on end, clear exactly one tab matching the ended session's
unique display (none untouched, several refused) back to the plain terminal title. It needs the
owner's approval and a live same-ID resume check, because a late end can collide with a resume.
Revisit when Codex exposes a predecessor ID, a stable TUI connection ID, or a thread-replacement
event.

## Config inventories read hidden and ignored files

`rg --files` lists only tracked, unignored files, and `.codex/config.toml` and
`.claude/settings.local.json` are gitignored. List the directory itself (`ls -a`, or
`rg --files --hidden --no-ignore`) before saying a client config file is absent.

## Guards under code mode

A Codex rollout shows almost every call as `exec`, a JavaScript code-mode tool that runs many
shell commands through `tools.exec_command`. Hooks sit below that wrapper: each nested command
surfaces as its own `PreToolUse` with `tool_name` `Bash` and the command at
`tool_input.command`, and a `deny` stops that one command. Measured on the CLI and, on
2026-09-30, in the app runtime, where `cli-route` refused nested `bin/agent` calls before they
ran ([OpenAI's hook documentation](https://learn.chatgpt.com/docs/hooks)).

The `Bash` matcher does not see `apply_patch`, which Codex reports as
`tool_name: "apply_patch"`, so a file-edit boundary needs its own `apply_patch` matcher;
`.codex/hooks.json` has one for `protect-generated`. A live code-mode edit is not yet measured,
so do not claim full file-edit enforcement on Codex. Whether `no-chaining` should apply to code mode, which batches by design,
is the owner's call; today it is wired on both clients.
