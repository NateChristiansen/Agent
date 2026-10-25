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
- A session may map to a request record instead of a feature: the same mapping, shown as
  `Request NNN` with its ask and next action at session start. `request` action `new` maps the
  calling manager by default; `capture` leaves the mapping alone; `use` switches it under the same
  rules as `feature_use`. The session-start digest lists open requests under `## Requests`, apart
  from the open features. A mapped request's line says how it dispatches (no task numbers;
  research may omit `repo`), or, while it is a proposed capture, that nothing dispatches yet.
  A dispatch's worker session is not the native Codex identity: terminal retention queues a
  Codex worker by its `codex_thread_id` and a Claude worker by its transcript path, whether or
  not a manager Stop or a closure follows.

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
    shared contract. A review the owner asks of the other client is a dispatch, not a peer
    message (see Plan review).
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

A plan reaches the owner after a complete blind review round on its exact bytes keeps no
finding as blocking. Small plans are exempt.

A round is two fresh, read-only reviewers on one work order, Codex `gpt-6.1-sol` at `high` and
Claude `sonnet` at `high`, then one Codex `gpt-6.1-sol` verifier at `high`. That pairing is the
owner's measured review policy, passed explicitly at launch; it is not the rubric's `review` row
default, and a repo's `dispatch.model` pin does not replace it. Each reviewer is told to bring
back every finding, not a sample, in full in its structured return's summary, which is what the
round reads; no length guidance caps a review's findings. A reviewer sees neither the other's findings nor the manager's
ledger: earlier findings anchor a reviewer toward agreement. Every finding either reviewer lists
goes to the verifier, by id and text only. The verifier refutes a finding only with a concrete
reason, keeps it when unsure, and labels each kept one blocking or optional.

1. Write the plan. A small one (a few steps, nothing hard to reverse) may say
   `review: small — <reason>` in its first lines and skip review; a user with only one client
   says `review: none — <reason>`.
2. Otherwise call the `dispatch` tool with `action="plan-review"` and `paths=[<plan file>]`. The
   harness reads the plan once, freezes the bytes as `.agent/plan-reviews/<sha>.md`, embeds them
   in the work order, and launches the round's two reviewers (phase `reviewing`).
3. Call `plan-review` again on the same plan each time the supervisor reports a return. Nothing
   calls back in; each call moves the round one step. Once both reviewers returned, their
   `Blocking:` and `Optional:` items go to the plan's ledger and the verifier launches
   (`verifying`); no finding at all completes the round clean. Once the verifier returned, its
   table goes to the ledger and the round is `complete`. A reviewer or verifier without a usable
   return is relaunched once; a second failure leaves the round incomplete for the owner.
4. A complete round with verified blocking findings keeps the gate shut on those bytes. Weigh
   each: does it reproduce, does real use take that path, and is it worth building around? Fix
   the real ones and record `fixed` with the changed section through `plan-ledger`, or record
   `rejected` with the reason. Then call `plan-review` again: an edited plan gets a new round, and
   so do unchanged bytes after a complete round. A round covers only the bytes it read.
5. When presenting a reviewed plan, name the clean round, its reviewer dispatch ids and the
   frozen copy, `.agent/plan-reviews/<sha>.md`, so the owner can open the exact bytes reviewed,
   and relay the round's disputes.

The ledger, one per plan file, holds every finding across rounds with its reviewer, round,
verifier result and the manager's disposition. Never show it to a reviewer. `plan-ledger` reads
it and lists the disputes the owner should see: a Claude reviewer's finding the verifier refuted
or called optional, and a later finding that re-raises one the manager rejected (linked with
`reraise_of`, or the same text).

Claude's `ExitPlanMode` is held by `plan-gate` until the latest complete round on the presented
sha kept no finding as blocking, or the plan is exempt. After three complete rounds of one plan
file (each record from before rounds counts as one), the gate stops denying and adds a note with
the ledger's verified blocking findings and disputes; the owner's own approval prompt decides.
`plan-review` itself has no cap and keeps launching rounds. Codex has no approval tool and no
plan hook: a Codex manager follows the same steps before it asks for approval in chat.
`dispatch new` adds a warning, never a refusal, when a `build`, `design` or `max` launch's
feature has no clean plan-review round on record.

A branch head gets the same round through `action="code-review"` (a build `dispatch_id`, or
`branch`, `base` and `paths=[<acceptance file>]`): both reviewers and the verifier are pinned to
the head sha read at launch, with the base as comparison, and `code-ledger` is the branch's
ledger. A complete round on the same head is reported, not rerun; a new commit starts a new
round, and an older head's round never reads as clean for it. After three complete rounds of a
branch, `code-review` launches no more and the owner decides with the ledger shown. `land` and
the push gate want a clean round on exactly the head (ADR-0063).

An owner's ask for a review on a named client ("have Claude review your plan", "get Codex to
look at this branch") is a dispatch, never a `peer` message to another manager: `plan-review`
for a plan (its round always has a Claude and a Codex reviewer), `code-review` for a branch
head, or a `review`-row worker launched with that `provider` for anything else.
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
