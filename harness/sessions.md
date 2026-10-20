# Sessions and leases

Several Claude Code and Codex sessions run against this vault at once: a manager per feature,
a background job, a worker per dispatch in its own worktree. This file says what a session is
to the harness, how two of them share the vault, and how they talk.

## What a session is

- A session record at `.agent/sessions/<id>.json`: the session id, the feature it is mapped
  to, its cwd, its name, its pid, when it started. The session-start hook writes it and maps
  the pid. `bin/agent session list` prints every record as LIVE or DEAD (a pid that is
  gone). A worker's session is a dispatch record instead, with the supervisor's and the
  worker's pids.
- Claude Code's own view: `ListAgents` in a session lists the peers on this machine by
  name, with busy or idle and an age. A running worker is in that list under its dispatch id
  (`<id>`), because the SDK passes the id as the session name. The harness adds what
  Claude Code cannot know: which feature each session holds and which areas.

## Names

A manager session mapped to a feature is titled `mgr-NNN-slug`. The
`session-start` hook and the `session-name` hook (UserPromptSubmit) return `sessionTitle`,
which has the same effect as `/rename`. So a session mapped mid-turn is renamed on its next
prompt, and a remap renames it again. The name is what `ListAgents`, `@name` and the
"Other live sessions" block show. It also goes on the session record's `name`, so lease
messages use it.

A name the owner set is never overwritten. The hook reads `nameSource` from Claude Code's
`~/.claude/sessions/<pid>.json` and renames only a session that is unnamed, auto-named, or
still carries the harness's own name. A worker keeps `--name <dispatch-id>`.

## Leases

A lease says "this session is working in this area". An area is a path:

| Area | Covers |
|---|---|
| `vault:src/agent_cli/dispatch` | that package, and nothing beside it |
| `vault:harness/safety.md` | that one file |
| `vault:src` | all of `src/`, `tests/`, `bin/` |
| `vault:harness` | all of `harness/`, `.claude/`, `.agents/`, `AGENTS.md` |
| `repo-slot:<short>/<k>` | a dispatch's slot in that registered repo |
| `worktree:<short>/<name>` | a dispatch's worktree |

Two areas block each other only when their paths overlap, so two sessions in sibling packages
do not wait for each other. A whole-subtree area still covers everything inside it, both
directions: a holder of `vault:src` blocks `vault:src/agent_cli/cli`, and a holder of
`vault:src/agent_cli/cli` blocks `vault:src`. Ask for the narrowest area you are really
working in. `area_for_path` picks it for you on an edit, capped per top-level by `AREA_DEPTH`
because the directories are shaped differently — `src/` is packages, `tests/` and `harness/`
are mostly flat. `features/` is in no area at all.

`.agent/leases/<area>.json` holds the holder's session id, pid, feature, when it was taken
and when it was last touched — with `/` written as `~` in the filename. The rules:

- The first edit under an area takes the lease for the editing session. Every later edit
  touches it. `dispatch new` takes a `repo-slot:<short>/<k>` and a `worktree:` lease and releases
  them when the dispatch reaches a terminal state. A session ending releases everything it holds.
- A lease whose holder's pid is dead is free. A lease with a known pid not touched for thirty
  minutes is free: the holder walked away. A lease whose pid is unknown is `unverified` after
  thirty minutes and still blocks; only its release, its holder's turn ending, or `lease clear`
  frees it. Taking over a free lease names the previous holder.
- A lease whose holder's turn ended after its last touch is `resting`: the holder is at a
  prompt, not mid-edit. The next session that edits the area takes it over and is told whose
  it was.
- A second live session that edits under a live lease is denied by the hook with one line:
  call the `lease` tool with `action="wait"`, `area=<area>`, `timeout=120`. The denial, and `lease wait`, put a request on
  the lease. `wait` with `ask` also messages the holder, who may release it sooner at a safe
  point; otherwise the lease goes `resting` when the holder's turn ends (the `lease-yield` Stop
  hook stamps it). The `wait` action polls until the lease frees and returns. Call it with its own timeout,
  then retry the edit. The hook itself never waits.
- The `lease` tool with `action="list"` shows every lease as live, resting, idle or dead.
  Its `take` and `release` actions cover the cases the hooks do not cover. `clear` frees every
  lease a named holder has, and only when the owner asks. It never frees a lease a
  verification run protects, even with `confirm=True`.

Leases are advisory and cooperative, on one machine. A take holds a file lock on its whole
subtree (`leases.py` `lease_guarded`), so of two sessions racing for the same area, one wins.
Nothing stops an editor or process that never asks for a lease.

## Awareness

The session-start block, after the feature line, names every other live session with its
feature and leases, or says there are none. It warns when `src/` or `tests/` carry
uncommitted changes.

## Talking

- Manager to manager, Claude or Codex: call the `peer` tool with `action="send"`, `to=<name>`,
  `message=<text>`. It appends to the
  peer's inbox under `.agent/peers/` and wakes it (a socket frame for Claude, `codex queue` for
  Codex); the peer's prompt hook delivers the message in its next turn. The `peer` tool's `list`
  action names the sessions a message can reach; `inbox` reads your own. Claude Code's
  `SendMessage` cannot reach a Codex session and is refused for any harness manager by the
  `peer-route` hook.
- Naming a peer: `to` takes an exact name or session id, the short name on the peer's Obsidian
  tab (`cl 834e`, `cx 8ddb`, `cx8ddb`, or the bare four characters), or a session id prefix.
  `peer list` shows each tab short name. A name prefix is not matched: a reply to a manager
  that had just ended once went by prefix to a different live one. A miss names the nearest
  live sessions instead, and more than one match is refused.
- Another machine: once the owner has paired the two devices (`peer-net pair`, person-only;
  ADR-0035), `peer` reaches a session there by its device-qualified name, `<device>/<name>`, for
  example `win1/mgr-NNN-slug` or `laptop/cx 1a2b`. The other device resolves `<name>` exactly,
  never by prefix. `peer list` shows the paired devices' sessions with that prefix, or one
  `<device>/*` row reading `unknown` when the device does not answer. A message from another
  device arrives labelled `remote device <device>`, and replying to its `from` sends it back.
  - An accepted result means the other device recorded the message, not that it was read.
    Results can also be refused, unconfirmed, abandoned or acceptance-unknown. An unconfirmed
    send stays in the local outbox. Later sends to that device retry a bounded batch while
    messages remain within their 10-minute expiry and the original route remains valid.
  - Delivery, measured live: an idle recipient of either client is woken within about 10 s. A
    busy Claude gets the message at its next tool step. A busy Codex gets it only at its next
    prompt or its own `inbox` call.
  - Without pairing, a Claude manager can still reach another machine through Remote Control:
    `ListAgents`, then `SendMessage`. `peer-route` does not refuse that, because the session has
    no record here. Check before telling the owner there is no route.
- Work that spans machines:
  - **One lead per outcome.** One manager leads each feature or cross-platform bug, plans it,
    and dispatches and lands where it runs. Another machine's manager supplies bounded probes
    against a named commit and does not take over or renegotiate the plan.
  - **Landing delegation, until a native Windows land passes** (Windows landing is enabled but
    unproven). A lead on a Windows machine picks a paired non-Windows machine to land for it
    and sends it one message per branch: `land request: <branch> at <commit>`, with the
    verification evidence's notes path. That machine lands it and replies once with the landed sha. Nothing else goes back and forth about it.
  - **Evidence by file.** A probe's output goes in a file under the feature
    (`features/<id>/evidence/<host>-<name>.md`), never pasted into a message. Run the `harness`
    tool's `notes_publish` action, then send the path with `notes-commit: <sha>`. If it says
    not published, send nothing yet: the file stays and is pushed at the next Stop. The reader's
    prompt hook pulls until it has that commit, so it never reads a missing or older copy.
- Message rules (owner-approved):
  - Send no acknowledgement, receipt or status-only message. Status goes in the feature log.
  - Send one message per decision, with findings batched.
  - Reply only when a message asks for something.
  - Message a peer manager only for a cross-feature conflict, such as overlapping files or a
    shared contract.
  - The `peer` tool enforces the ringing half: a message that is only an acknowledgement
    ("ack", "thanks", "got it") is stored but never wakes its recipient, and a question,
    decision or request wakes it at once. With `peers.defer_nonurgent_s` set in agent.yaml,
    other informational messages wait unrung for the recipient's next turn, or until the
    sender's turn ends after that many seconds.
- Worker or subagent: `SendMessage` to it by name. A message to a worker lands inside its
  turn as an instruction. An answer sent that way continues a worker holding on a `question`.
  Nothing sent this way is on the record. A steer that should be is `dispatch say` (it lands
  on the dispatch thread and in the handoff as a mid-turn steer). An answer
  that should be is `dispatch resume`.
- Durable: the feature's log and plan. A session that finishes a piece of work leaves a
  `note` or a `blocker`. The next session mapped to the feature reads it in the session-start
  block and the plan's open tasks.

## Plan review

A plan reaches the owner after one fresh reviewer on the other client agrees to its exact
bytes. The owner wants Claude and Codex to review each other's plans; small plans are exempt.

1. Write the plan. A small one (a few steps, nothing hard to reverse) may say
   `review: small — <reason>` in its first lines and skip review; a user with only one client
   says `review: none — <reason>`.
2. Otherwise launch the `dispatch` tool with `action="plan-review"` and `paths=[<plan file>]`.
   The harness freezes the plan's bytes, embeds them in a read-only review on the other client,
   and records the sha. The reviewer answers `VERDICT: agree` or `VERDICT: revise`, with blocking
   findings separate from optional ones.
3. On `revise`, weigh each blocking finding: does it reproduce, does real use take that path,
   and is it worth building around? Fix the real ones. Answer a theoretical one with reasons
   in the revised plan; the next reviewer judges that answer. Launch again. Any edit needs a
   new review; a verdict covers only the bytes it read. After three reviews of one plan file
   the owner decides.
4. When presenting a reviewed plan, name the agreeing review's dispatch id and its frozen copy,
   `.agent/plan-reviews/<sha>.md`, so the owner can open the exact bytes that were reviewed.

Claude's `ExitPlanMode` is held by `plan-gate` until one of those holds. Codex has no approval
tool and no plan hook: a Codex manager follows the same steps before it asks for approval in chat.
`dispatch new` adds a warning, never a refusal, when a `build`, `design` or `max` launch's feature
has no agreed plan review on record.
On Claude, `plan-mode-allow` lets `peer`, `feature_use`, `session_get` and the
`plan-review` dispatch action through without a prompt; every other dispatch action and agent
tool still asks in plan mode.

For evidence work, such as live runs or observations from another machine, log each observation
in the feature log as it arrives, and write the reference doc in one reviewed change once the
planned runs are done; a doc rewritten per observation needs a review per commit and goes stale
between them.

The gate checks the plan's sha against the plan-review records, never the process table, so it
stays well inside its fail-open timeout.

## Scratch

Plan-mode drafts, dispatch work orders and journal drafts live under `.agent/scratch/`
(gitignored, hidden from Obsidian). It is the one corner of `.agent/` an agent writes to
with the Edit and Write tools. The native deny list and the hook name the CLI-owned subtrees
(`dispatches`, `sessions`, `leases`, `journal`, `peers`, `peer-net`, `plan-reviews`, `raw`,
`default-feature`, the json files)
instead of `.agent/**`. Both derive from `core.AGENT_SUBTREES`; a new subtree goes there, or in
`core.AGENT_UNREGISTERED` with its reason; `core.agent_path` refuses an unregistered one, and
[[harness/rules-enforcement]] row 3 says what else checks it and where that stops. `notes/`
is the human's Obsidian new-note folder. Nothing of the harness's lands there.
