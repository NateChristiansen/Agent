---
name: dispatch
description: "Send a piece of a feature's work to a headless worker inside a product repo: write a self-contained work order, preview it with the `dispatch` MCP tool, get the owner's yes, launch it, and later read the handoff. Use when a task in the plan needs code changes in a registered repo."
user-invocable: true
allowed-tools: mcp__agent__*, Read, Write, AskUserQuestion
---

# /dispatch

Input: $ARGUMENTS (a repo, task numbers, or a description of what to send), or the
conversation so far. Output: a running worker and a `dispatch` entry in the log.
Read `references/lessons.md` before writing the work order or choosing a rubric row.

1. Confirm the feature (`feature_show`) and that the work exists as tasks
   (`task_list`). If it does not, add the tasks first (`task_add`), one verifiable outcome
   each. The feature's first preview of a session carries `task_check` (also run by
   `feature_use`): before launching a flagged task, tick it (`task_done`), supersede it
   (`task_supersede`, with a reason and a link) or keep it, and say which.
2. Pick the repo. It must be in the feature's `repos:`; if not, `feature_set` with
   `add_repo` after checking with the owner. Pick the task numbers the worker owns.
3. Write the work order to `.agent/scratch/dispatch/<NNN>-<short>.md` (gitignored) in the owner's voice:
   the goal, what done looks like, the files or areas involved, the build and test commands
   to run, what not to touch. Self-contained: the worker gets the plan's Context and this
   text, nothing else from this conversation.

   Three rules keep it short and keep the manager out of the worker's job:

   - **Do not restate the contract.** Every worker already has
     `harness/worker-contract.md`: never push, never open a PR, never speak on github,
     commit as you go in the repo's convention, one command per shell call, return the JSON
     with the nonce, ask rather than guess. Repeating any of it is tokens in every dispatch
     and a second copy to keep in sync. Say only what is specific to this job.
   - **Point at artifacts, do not paste them.** A PR review, a ticket, a failing log, a
     config file: name it and name the command that reads it. The worker has `gh` and is in
     the repo. Pasting means reading it yourself first, which is the manager doing the
     worker's thinking with worse context, and it puts the same text through two models.
   - **Scope and constraints, not solutions.** Say what done looks like and what must not
     change. Where you already know a trap, name the trap, not the fix: "do not change how
     the existing package installs, and you cannot run the release build to check" beats
     prescribing the edit.
   - **Precedents, not mechanisms.** A build work order carries a "Precedents" list: for each
     new file or mechanism, the sibling file in the repo to match, path and line, found on a
     clone pulled this session. Anything you would have written as "do X via Y" becomes the
     constraint it serves ("the page paints complete; how it drains matches a sibling") with
     the sibling as the how. The return's `summary` carries a precedents table: each new
     file, the sibling it mirrors, or `none` with the reason; read that table before the
     push.
     Ask what the mirror gets wrong. A sibling is a precedent, not a proof, and a faithful
     copy of a defective one satisfies every gate the precedents rule creates. The table's
     verdict column says `copied`, `deviated` or `filed` — copying is still the default, and
     a worker rewriting siblings it was told to match is the failure the precedents rule exists to stop, so
     a deviation needs its reason and a defect left alone gets recorded rather than fixed.
   - **A convention doc decides; it is not an address.** Where a repo has written the rule
     down, cite the file for the question it answers and say that the file, not the work
     order, decides: "whether a unit test belongs here at all is `<project>/AGENTS.md`'s
     call; read it before you write one". Never name it as a place to put something. "Put it
     where `<project>/AGENTS.md` says" reads as permission plus a location, and a worker that
     gets it will follow the placement and never open the file for the decision.
     The composer now appends the governing AGENTS.md/CLAUDE.md for every path the work
     order names, saying they outrank it and any mirrored sibling. A convention that must
     hold goes in the repo's `dispatch.diff_rules`, where the return is checked against the
     diff; prose in the work order is not a substitute.
   - **Your reasoning is not a comment.** Say why in the work order, never in words the
     worker can paste into code: a rationale phrase in the order tends to come back as a doc
     line. No comment needs a row in the return; the commit check refuses ids and three-file
     repeats, and the handoff lists every added comment and docstring. When you dispatch a review, give the reviewer that list from the handoff with
     one question: "Which lines merely restate the code or its history?" A cosmetic finding
     alone is not another acceptance round.
   - **A bot finding is a question before it is an instruction.** Before you pass on an
     automated reviewer's warning, ask whether the case can happen. Guarding a case that
     cannot happen adds code a reader has to work out is dead.
   - **A requirement the repo cannot express is a question.** Before requiring a property,
     ask whether the repo's shared helpers can state it. Where they cannot, say so in the
     work order and name the two ways out: change the helper, or return `question`. Never
     leave the worker to satisfy the requirement by inventing a mechanism beside an existing
     one.
4. Choose the row from `harness/dispatch-rubric.md` and say in one line why this job is that
   shape. That line is `choice_reason`. `row` sets the model and budget; effort normally stays
   at the model's vendor default. Owner policy sets Codex `review` and `build` to `medium`.
   Explicit and inherited parent effort take precedence. Leave the default only with `effort`
   and a reason from the rubric's Effort section (`high` for reasoning burden such as
   concurrency, protocol or cross-module design work). Duration is never an effort reason: a
   job expected to run long gets `timeout_minutes`, and the preview shows the applied limit and
   its source. On Claude, a repo that pins its own model (`dispatch.model`) wins over the row,
   and the preview says so. Codex ignores the pin.
5. Preview with the `dispatch` tool: `action="new"`, `repo=<short>`,
   `feature="current"`, `tasks=[<n>]`, `prompt=<work-order text>`, `row=<row>`,
   `choice_reason=<why this shape>`, `dry_run=True`. The preview prints the whole composed work order between rule lines — every
   line the worker is sent, not a head — plus the model and effort, the budget, and the
   **preflight table**: every command the work order marks as executable plus the repo's
   `setup`, each marked allowed / denied / no rule. Executable means a shell-tagged fence
   (`sh`, `bash`, `zsh`, `shell`, `console`, `shell-session`), an untagged command fence, or
   an explicit `$ ` line outside other fences. Inline backticks, non-shell fences and shell
   comment lines are prose or data and are not scanned, so a command the worker must run goes
   in one of those forms, and a mere mention is not an instruction. Each scanned command is
   checked with the shared chaining guard. This only previews the runtime permission rules;
   it does not change them. For an exact-version review, tell the worker to hash the named
   artifact with this portable command, substituting the artifact for `spec.md`. That form is
   for a Claude worker; Codex preflight refuses `uv run python`, so a Codex reviewer runs the
   same `-c` script with the venv's python (`.venv/bin/python`, or `.venv\Scripts\python.exe`
   on Windows):

   ```sh
   uv run python -c "import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())" spec.md
   ```

   Keep the script in double quotes with none inside: `cmd.exe` runs setup lines on Windows
   and does not treat single quotes as quoting.

   A `no rule` or `denied` row is a dead permission, the failure that cost the most here;
   fix the rule in `agent.yaml` (`dispatch.allowed_tools`) or `BASE_ALLOW` and preview again.
   `allow_unmatched=True` exists for a command the worker is meant to discover cannot run; say
   why in the preview. A job that needs an extra MCP server (a browser for a QA pass) sets
   `mcp=[<profile>]` (an agent.yaml `worker_mcp:` profile): the worker gets that server and
   its allow rules, and the preview shows `mcp: [<profile>]`. The launch refuses while the
   profile's required file (a saved login) is missing; ask the owner to run `bin/agent worker-mcp list` to see which
   are ready. Show the owner the preview verbatim and wait for a yes, unless the owner said
   "just go" for this dispatch or this feature, then pass `just_go=True`.

   The preview writes `features/<NNN>/orders/<preview-id>.md` and returns `preview_id`
   and `work_order_url`. Hand documents over as AGENTS.md says: a vault file as its `harness link`
   URL, anything outside the vault as a copy-only absolute path. Every launch question shows the order text or preview plus that URL
   in inline code, before the ask. If unseen linked, preview instead of using `just_go`.
   Standing "just go" authorization stays valid; relay its launch link.
   Pass the order text inline as `prompt`, never a `.agent/scratch` path.
   Launch with the returned `preview_id`; changed bytes, nonce or choices need a new preview.
6. Call `dispatch` with the same fields and `dry_run=False`. The tool writes the `dispatch` log entry; do not log it again.
   Launch the whole ready wave, not one dispatch at a time: every task whose prerequisites have
   landed and whose writable paths overlap no running task starts now, up to the repo's slots.
   When you serialise ready tasks, record why (a `decision` log entry naming the tasks).
   Pass each dispatch's writable paths as `writes`: a launch overlapping running work is
   refused, and a running dispatch that declares none holds back every other writer in its repo.
   When a dispatch took long, `dispatch` `action="spans"` shows where the time went, attempt by
   attempt.
   **Work-order checklist**, before `new` or `resume` (most refused calls and extra attempts
   come from the order, not the worker):
   - `writes` names every file the worker must change, including each test that asserts on the
     surface being changed (a tool count, a line budget, an "unbuilt" check). A shared file goes in
     one concurrent dispatch's `writes` only. A review declares `writes=[]` (read-only: it blocks
     nothing and must restore its probe edits); omitting `writes` blocks the whole repo. Name
     files, not directories: a directory claim blocks every other feature's writer under it, and
     the preview warns about one.
   - A numeric limit in the order (lines, seconds, counts) matches the approved plan.
   - Commands go in `sh` fences, anything else in `text` fences; inline backticks are prose.
   - Full repo-relative paths; each SHA, id and command in backticks with spaces around it.
   - Inline the excerpt a worker needs; never point it at another worktree, `.agent/verify` or
     runtime logs, which it cannot read.
   - `budget_reason` whenever `budget_usd` is above the default.
   - For Codex: no `.agents/`, `.codex/` or `.git` edits, no `git merge`/`rebase`/branch
     changes, no network, and no `uv run python` (preflight refuses these and names the route:
     send them to Claude). Fix a Codex worker's finding with a fresh dispatch on its branch when
     a resume would carry a very long thread.
   - Ask the worker to return a question, not to log it and wait.
   - A dry run (`dry_run=True`) shows the preflight table before anything is allocated.

   **Waiting.** Rely on the supervisor's wake, which tells this session when a dispatch leaves
   `running`. Arm a **Monitor** only as a liveness backstop, for the one dispatch you are blocked
   on, from this session, not a subagent:

   ```
   while true; do s=$(jq -r '.state' .agent/dispatches/<id>.json 2>/dev/null || echo unknown); case "$s" in queued|running) ;; *) echo "<id> terminal: $s"; break;; esac; sleep 60; done
   ```

   with `timeout_ms` 900000 (15 minutes). It emits one line when the dispatch leaves
   `queued`/`running` (done, question, blocked, failed, cancelled alike) and exits. On that line,
   run the `dispatch_read` tool with `action="show"`, `dispatch_id=<id>`, and read the handoff. Every
   time it expires without one, run `dispatch_read` `action="watch"` instead: a bounded view that names the
   worker's last activity and what the dispatch waits on, and calls out a worker silent for ten
   minutes.
   A silent worker is not stopped for you: check `dispatch` `action="spans"`, then `say` or `cancel`. Re-arm
   only after reading `show`. The Monitor tool's script is exempt from the no-chaining hook, which
   is why this loop is legal here and nowhere else. A Codex manager has no Monitor: poll
   `dispatch_read` `watch` (bounded: compact status, last activity and the last 40 transcript
   lines) at the same interval; `dispatch_read` asks no approval and changes nothing. Never wrap a
   side-effecting MCP call (`new`, `resume`) in a JS `store()`, whose error can hide a launch that succeeded.

   If `dispatch_read` is missing (a session started before it existed), use `dispatch show`
   and `dispatch watch`. If `consume` is rejected, the `show` alias already consumed.

   Do not spawn a "runner" subagent to wait. A runner's background watch can be cut off
   before a long worker finishes, a foreground wait wakes the session over and over, and a
   parked subagent costs tokens.

   The owner's live view is `bin/agent dispatch watch <id> --follow`, run with a `!` prefix
   or in their own terminal; give them the id and that command in the same message.
7. When the wake or the Monitor reports (or the session-start / prompt block says a worker returned):
   call `dispatch_read` with `action="show"`, `dispatch_id=<id>` (`detail=True` for the full record),
   read the handoff file it names, then mark it read with the `dispatch` tool, `action="consume"`.
   A hook's notice is delivery, not a read: until `consume`, the result counts against this tab
   (`✅ +N`) and returns to the owner if this session ends. Then either report to
   the owner, put a `question` to them and answer it with `action="resume"`,
   `dispatch_id=<id>`, `answer=<text>`, or propose abandoning. The tool writes the `handoff` entry. A `blocked` on a budget
   hit is resumed with `budget_usd=N` and `budget_reason=<reason>`; the harness never raises a budget itself.

Never edit inside the worker's worktree. Workers never publish. When reviewed work is ready,
ask once through `AskUserQuestion` (or `owner_question` on Codex): push and merge `<branch>` now?
On yes, run it yourself. Hand over a command only if the client refuses, and say why. Put each
complete command in its own fenced block in the final reply, with resolved operands and no
placeholders. Read [[harness/voice.md]] for command and file references.

Manager handoff and restart notes follow [[harness/continuity]] and end with `First action:`.

## Before the push

A branch that will become a PR gets one review dispatch before anyone outside sees it: one
fresh probe reviewer, with no second reviewer. It gets the artifact, the contract and a
requirement to run executed probes against behaviour, because executed probes find real defects
that reading misses. No peer-manager review round runs on top of it. Write the
work order at launch time against the pinned base, never as an earlier draft.

The reviewer judges against the acceptance the build work order stated. It lists blocking
findings (the contract is not met, or a real defect) apart from optional ones, and only blocking
findings go back to the implementer. A blocking finding needs a path real use takes or plausibly
would; one needing tampering, stacked faults or input no one produces is optional unless the
reviewer shows it is likely. Each finding states its path. A threshold the work order did not set, or a wording
preference, is optional and does not start another round.

The review dispatch uses the `review` row (sonnet at vendor default, or gpt-6.1-sol on Codex
at the owner's `medium` row policy, $3-6), same repo, its own branch
cut from the artifact's resolved SHA, passed explicitly as `base`. `target=<artifact branch>`
also defaults a review's checkout base to that branch's pinned tip. Keep the comparison base
separate from the checkout SHA. Before artifact checks, the reviewer runs `git rev-parse HEAD`
and confirms it equals the artifact SHA in the order. It gets
a work order that names: the build work order's precedents list, the live interfaces the
change depends on, the repo's testing skill, and the lenses the human reviewers
use there: correctness, conventions against the named siblings, hand-written types against
their source of truth, tests that can fail, config entries that resolve. The lenses run in that order for a
reason, and each one asks whether the thing should be there at all before it asks whether the
thing is well formed. A lens phrased as "does X follow `<doc>`" gets answered on style; the
lens that catches a reviewer's real objection is "does `<doc>` say X belongs here". A
lens on a new test or assertion asks whether it states itself the way the suite around it
does, not only whether it is strong enough. Where
the work order named a mirror, one lens reads the mirror itself and asks what it gets wrong,
because "matches the sibling" is the answer to a different question than "is this right".
Paste the build handoff's "Comments added" list into the review work order as a lens:
for each comment, what a reader of the code would lose without it. The return is a findings list with file
and line; nothing is fixed on that branch. Findings go to the build worker with
`dispatch resume`, and the manager checks the numeric bars on the resumed branch
before pushing. Anything the review misses gets found after the PR is up, where each finding
costs a public round.

The `push-guard` hook holds this: a `git push` of a branch some dispatch record names, or any
push from a dispatch's worktree, is denied unless a `review`-row dispatch on that branch
finished after its last commit, or a `done` review whose `start_sha` equals the pushed head
(branches with no dispatch record, including the manager's own, are not guarded unless the
push is from a dispatch's worktree). The override is
`AGENT_PUSH_UNREVIEWED=1 git push ...`, and it writes a `note` to the feature saying the branch
went out unreviewed.

## Sending review feedback back

A PR review, a red check, or the owner's own comments on a worker's branch are a dispatch,
not manager work. Read only enough to route: which repo, which branch, which PR number, and
whether anything in there is the owner's to decide rather than the worker's. Do not work through
the findings and do not form opinions on them; the worker reads the thread itself.

The work order names the PR number and says to read it first, and it is explicit that the
worker owns the judgment:

- `gh pr view <n> --comments` for the review bodies and the summary comment, `gh api
  repos/<owner>/<repo>/pulls/<n>/comments` for the inline comments (`gh pr view` omits
  those), `gh pr checks <n>` for the red ones.
- Weigh every comment: fix it, or decline it with a one-line reason in the return. A comment phrased as a question ("why did we add this?") is usually
  a request to remove it or to justify it in one line, so decide and say which in the return.
- Where a comment asks for a change the work order did not anticipate and it is a real
  design choice, return `status: question` rather than guessing.
- Reply to nothing on GitHub. The return lists what changed per comment; the owner answers
  the threads, or asks you to.

When the owner asks you to resolve the threads that are done: check each change is on the pushed
head first, not on the worker's summary, then resolve them all in one `gh api graphql` call
with an alias per thread. One call per thread is one permission prompt per thread. Thread ids
come from a `reviewThreads` query, which also tells you which are already resolved.

Send it back to the worker that wrote the code, with the `dispatch` tool's `resume` action and
`answer=<the work order>`, not as a new dispatch: the agent asked "why did we add this?" is the one
that added it, and the follow-up commits land on the open PR with no branch juggling. See the
facts below for what resume actually does.

When the ask is a matter of degree ("remove unnecessary comments", "make it smaller",
"simplify"), the work order states a number the worker can check itself: a count, a length, a
command whose output must read a certain way. Before pushing, run that check on the branch
yourself; a worker's summary saying "swept" is not the check. A bar the worker cannot measure,
such as "keep a one-line why when it is non-obvious", leaves the result to taste. A third
round on the same question is a fresh dispatch with a stricter bar, and a stronger model
when the owner asks for one.

Start a new dispatch instead when the follow-up is genuinely different work, when the original
session is gone, when the cost per turn has climbed past a few rounds, or when that worker's
judgment is what went wrong. Say which, and why, when you preview it.

## Facts worth not rediscovering

These were each learned by reading `src/agent_cli/dispatch/` mid-task. They are true; do
not re-derive them.

- Claude workers run through the Agent SDK (ADR-0009): one open connection per attempt, the
  `claude` on PATH as the binary. `dry_run=True` prints an options block; `cli_path` there must
  be the PATH claude, never the SDK's bundled one. Codex workers run over the app-server
  protocol in Codex's sandbox (see [[harness/provider-gaps]]).
- A running worker is a peer session named by its dispatch id: `ListAgents` shows it
  (`<id> · interactive · busy`) within seconds of launch, and `SendMessage` to that id lands
  inside its turn as an instruction, an answer included. Use
  that to look and to nudge; a steer that should be on the record goes through `dispatch
  say`, because the thread on the record only sees the harness's own channels.
- `dispatch say <id> "<text>"` works on every repo and lands inside the worker's running
  turn, not at the next boundary. After a `question` the worker is held open about ten
  minutes for a `say`; an answer in that window continues the same session. After that the
  dispatch finalizes as `question` and `resume` is the way back.
- `dispatch resume <id>` works on any non-running state, `done` included. It continues the
  worker's own claude session (the SDK's `resume`) and reuses that dispatch's worktree and
  branch, recreating the worktree if it was cleaned up, and refusing if a later dispatch has
  moved that worktree to another branch. `--answer` text becomes the first message of the
  resumed session, appended to the thread.
- Every attempt regenerates the worker's settings, mcp config, and schema, so a resume runs
  with any harness fix made since it last stopped. Widening a permission and resuming is a
  legitimate move, not a restart.
- `dispatch watch <id> --follow` blocks until the dispatch is terminal and streams the
  transcript. Backgrounded it is a wait; in the owner's terminal it is a live view.
- The harness never auto-resumes a `question` and never raises a budget (ADR-0008). The one
  automatic retry is a timeout, once, at the same budget. A budget hit is `blocked` with the
  resume command in its error.
- A `done` that skipped required repo checks comes back `blocked`. A fourth attempt on one
  dispatch is refused without `again=True`; prefer a new dispatch with `escalate_from=<id>`. The
  `dispatch` tool's `action="ledger"` (by PR) reports the work's cost.
- `cost_usd` on the record is cumulative across attempts, so a fifth attempt on a long
  session re-sends everything before it and costs more per turn than the first did. Past
  three or four rounds, a fresh dispatch reading the current branch is usually cheaper than
  another resume.
- One worktree serves a feature and repo pair. A new dispatch reuses it when the last
  dispatch on it is terminal, not waiting on the owner, and clean, and its branch holds no
  unlanded work, so `node_modules` survives; otherwise it gets its own path with a `-<tag>-SS`
  suffix. A builder's worktree stays with its branch until that branch lands or the dispatch
  is abandoned: a review gets its own checkout, and its findings go back to the builder with
  `resume` (or to a dispatch onto the builder's branch, which continues in that worktree).
- A dispatch into this harness starts from locally landed main, pinned to its SHA; a product
  repo starts from `origin/<default branch>`. An explicit `base` that does not resolve is
  refused, and so is a base whose `tests/selection.toml` predates version 2.
- A dispatch's verification runs only its scoped selection, never the full suite; a refused
  selection reads `incomplete` with its reason (ADR-0045). Every landing runs the
  full suite, reusing a full pass for the same tree (ADR-0062); a pushed tip needs a full pass.
  Land ready branches together: `land` with `batch` (up to four) shares one certificate.
  When landing a copy of another machine's branch, read its tip before copying, fetch the peer,
  and pass `origins=["<original-branch> <tip-sha>"]` to `land` (CLI: repeat `--origins`).
- Workers have `git merge`, not `git rebase`. A stacked branch is brought forward by merging
  its base into it. When the base PR squash-merges, the stacked branch's merge of main
  conflicts on every line the stack touched, because git does not see the squash commit as
  containing the branch's commits. The worker's move is to check
  `git diff <base tip> origin/main -- <paths>` is empty, then `git merge -X ours origin/main`
  and regenerate the lockfile; the manager's move is to say so in the resume, not "expect a
  clean merge".
- Rules that widen what a worker may run go in `BASE_ALLOW` in `src/agent_cli/dispatch/settings.py`,
  not in the work order. After changing any permission list, preview a dispatch with
  `dry_run=True`: building the worker's settings refuses a rule that cannot match, which is the
  failure that has cost the most here, because a dead rule reads exactly like a deliberate denial.
