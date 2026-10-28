---
tags: [doc]
---

# Safety

What this harness protects, which mechanism protects it, and how to check. Each section names
the controls and the gaps they leave. Review this page whenever a new tool surface (an MCP
server, a skill, a hook, an outbound path) is added.

A **manager** session runs in the vault, records work and never writes product code. It
dispatches **workers**: headless sessions, one per unit of work, each in its own git worktree
inside a product repo, with a work order, a narrow permission set and the harness MCP server in
its worker profile. A worker returns a handoff. Only the manager speaks on the forge, and only
when the owner asks. Two facts drive the model: a worker is autonomous inside its permission
set, and it reads content that neither it nor the manager wrote.

## Injection

A worker reads untrusted content by design: forge review comments and CI output (read-only),
responses from MCP servers a repo declares, a memory store a later worker reads, web content
through `WebFetch` and `WebSearch` (both in the base allow list), repo files a previous agent
wrote, and subagent output. A work order that quotes upstream text is a channel too.

**Controls.** The permission layer decides last. Deny rules are checked first; a Claude
worker's fall-through callback is deny-only, so a call matching no rule is refused with a
reason. A Codex worker runs in Codex's sandbox, and a PreToolUse hook applies the same rule set
(`src/agent_cli/dispatch/rules.py`, `codex_permissions.py`). Injected instructions therefore
cannot reach a capability that was not already granted. A repo's own `PreToolUse` deny hook
outranks the harness allow list, so a repo can narrow a worker but never widen it. The client
neutralizes instruction-shaped text in subagent returns.

**Gaps.** No outbound domain allowlist: `WebFetch` reaches anything. A Claude worker has no OS
sandbox and runs as the owner on the host. Memory contents are trusted on read. An injection
attempt is not detected as an event; it shows up only as one denial among many.

**Check.** `dispatch new --dry-run` scores every command the work order marks executable as
allowed, denied or no rule. `tests/policy_corpus/` pins hook and permission decisions row by row
(`tests/test_policy_corpus.py`).

## Model output and commands

Workers are denied recursive and forced removal, `find -delete`, hard reset, `clean`, forced
branch delete, worktree manipulation, any force flag and nested agent invocation. Push is
denied in three spellings. Every forge write verb is denied, including the argument spellings
that turn a read API call into a write. Editing anywhere inside the vault is denied, so a worker
cannot rewrite the harness, its own permission settings, the records or the hooks. A launch is
refused if any permission rule cannot match anything, so a dead rule fails at launch instead of
silently denying work mid-run.

Model-written product code reaches a default branch only through a pull request the owner
reviews. This harness's own main moves only through `agent land` ([[#Landing]]).

**Gaps.** A permitted command with a hostile argument is permitted: shell is constrained by the
allow list, not by output validation. Human review is the control, and the weakest strong one.

## Supply chain

Third-party skills are a pinned submodule (`vendor/caveman`) or a vendored copy
(`obsidian-markdown`, `obsidian-bases`), never a floating dependency. The manager's MCP servers
install from a pinned manifest (`mcp-servers install`); a floating version in a hand-edited MCP
config or worker profile is not reported. Obsidian vendor plugins download at pinned versions.
Worker MCP servers are explicit grants; see [[harness/worker-permissions]].

**Gaps.** No SBOM, no signature check on MCP packages beyond the registry's, and no container
isolation for a server that runs as a local process. A pinned submodule is still unreviewed code
that runs every session. Model provenance is the client's concern.

## Credentials

The harness holds no secrets: no keychain access, no credential read in the CLI, no token in
any worker MCP config. Forge auth stays in that CLI's own credential store, and the
token-printing and secret-management commands are denied. The Claude launch adapter passes an
explicit environment allowlist plus the repo's `dispatch.env` and `env_allow` names at both
version probe and main process creation, so inherited secret, notification and base-URL values
stay out of the worker and its descendants
([[adrs/0019-explicit-claude-child-environment|ADR-0019]]). It fails closed on an unsupported
SDK version, and a cleanup error prevents a successful acceptance. Auto-memory is off for
workers. Work orders carry paths and ids, never file bodies.

**Gaps.** The Python SDK host keeps the supervisor's environment: this is a CLI inheritance
boundary, not isolation from the supervisor or from credentials on disk. Anything a worker can
read, it can send through `WebFetch`. A repo whose tooling puts a credential on a command line
exposes it to anything that can list processes.

## Disclosure

The harness persists prompts, replies, work orders, handoffs, dispatch records and, at feature
close, complete session transcripts with every tool result the session saw.

**Controls.** Private paths (`PRIVATE_SET` in `src/agent_cli/notes.py`) live in the notes repo
and are links here, never public files; `agent export` refuses a tree that tracks one
([[#Export]]). Credential-shaped records are stripped when a transcript is
preserved. Records and generated files are write-protected against the manager and its workers
(`protect-generated` on both clients, plus Claude's deny list).

**Gaps.** Retention is unbounded. A coding agent's transcript cannot be free of confidential
content, because the content is the work: that is a retention problem, not a redaction one, and
any requirement otherwise needs an exception rather than a fix.

## Agency

A worker cannot push, open or change a pull request, comment or review, touch issues, releases,
workflows or secrets, or invoke another agent. Its scope is one worktree on one branch, and the
vault is read-only to it. Every run has a dollar budget (a soft cap on Codex), a wall-clock
timeout, and a turn cap (Claude) or tool-item cap (Codex). A `done` that skipped required checks
comes back `blocked`. A fourth attempt needs an explicit `again` or an escalation. Every
dispatch leaves a record: work order, thread, return, cost. The manager routes and records, the
worker implements, and the owner approves publishing and pushing. Before handing the owner a
command that a harness check will judge, read that check's code and predict its result.

Nothing a session or dispatch launches outlives it unless the owner keeps it
([[adrs/0017-process-ownership-by-env-tag|ADR-0017]]). Every process inherits an ownership tag
(the session id, and a dispatch id in a worker's tree) that survives leaving the process group
and reparenting. A dispatch reaps its tagged processes when it ends, a manager session reaps what
escaped it, and a harness command that times out kills its whole process group. `doctor` reports
tagged, detached processes whose owner is gone.

**Gaps.** Scope inside a worktree is instruction, not enforcement: nothing stops a worker
editing more files than its work order named. A process that clears its environment loses its
tag and escapes the reapers. The owner's approval is load-bearing throughout, so approval fatigue
is a real failure mode.

## Verification

A worker's `done` is not evidence. The supervisor runs the repo's own `dispatch.verify` commands
against the commit the worker left, and that run is what `done` rests on
([[adrs/0018-supervisor-verification-evidence|ADR-0018]]). A dispatch with no checks configured
cannot launch until someone configures them or passes `--no-checks` with a reason.
The old `dispatch.required_checks` key, which matched regexes against the commands a worker said it
ran, is retired: the config refuses to load while it is present. Rewrite each regex by hand as a
`dispatch.verify` command with its timeout.

`dispatch new --dry-run` shows each verify command, its timeout, the total and whether the
prerequisite is met. Configured checks read `unverified` there; an exemption reads `exempt`,
never verified, and exempts the checks only, not the permissions. `dispatch list`, `dispatch
show` and every handoff carry the recorded assessment: `verified`, `exempt`, `unverified`,
`failed` or `incomplete`. It is read back from the record, not re-checked; a missing or
malformed record reads `unverified`, and a worker's summary cannot produce one.

Before running a command the supervisor publishes its binding: attempt, SHA, config, run.
Completion and orphan collection (`dispatch collect`) pass the same terminal gate, which wants
matching durable evidence, a freshly clean tree, confirmed cleanup and repository ownership.
Collection reads evidence and never reruns the checks. A cancellation recorded before the
terminal commit wins. Cleanup that was never confirmed keeps the repo lease, even after the
supervisor dies. Repo locks and record revision checks serialize the lease, resume, cleanup and
terminal writers; locks are POSIX file locks, or a byte-range lock on Windows.

**Gaps.** The runner's output log is capped but not redacted. Sparse, hidden-index and submodule
layouts are unsupported. Atomic, descriptor-relative writes stop accidents; they do not
authenticate evidence against a same-user attacker. Handoff, log and notification side effects
are not a crash-safe exactly-once outbox.

### Scoped attempt checks

An attempt never runs the full suite; the landing record for the exact candidate is the one full
run ([[adrs/0045-full-suite-only-at-landing|ADR-0045]]). The verifier narrows only the pytest
command; ruff, the format check and pyright run whole. It reads `tests/selection.toml` from the
attempt's base, never the candidate, and diffs base against head. Launch stamps
`selection_policy` 2 when the base table is version 2; a repo that keeps a table refuses a base
whose table is missing, unreadable or older. A repo with no table, or a record from before the
stamp, runs its verify commands as configured. An escalation keeps its parent's policy.

The selection is the union of changed test files, tests that a `[[broad]]` rule or bundle maps a
changed path to, tests named after or importing a changed module (not for a broad path), tests
importing a changed test helper, and tests that mention a changed non-Python file. An unmapped or
deleted path adds the `[[fallback]]` set, as does an empty diff that no exemption or pass record
covers. A candidate table may add mappings for the files it adds; one that removes or narrows a
mapping is ignored. A runtime change reruns the real process-inventory tests once more.

The selection is refused, never widened, when there is no base, the base table is not version
2, git or the table cannot be read, nothing can be selected and the fallback is empty, or the
selection covers every test. A refusal is `incomplete` with its reason. Acceptance derives the
selection again and refuses evidence that no longer matches. A scoped pass reads `verified`
with a `scoped check` reason and is never a pass record, so land ready branches together to
share one full run.

### Pass records

A pass record says every `dispatch.verify` command passed against one commit's tree, in an
isolated detached checkout with hooks, fsmonitor and filters off and the tracked example config
in place of the owner's ([[adrs/0041-tree-keyed-pass-records|ADR-0041]]). `agent verify record
<rev>` reuses or makes one; `agent verify show <rev>` says which applies, or why none does
(the `harness` tool's `verify_record` and `verify_show`). A run holds one of the repo's
verification lanes (`dispatch.max_concurrent_verify`), shared with supervisor verification.
Requests for the same tree, environment and policy share one run. Locks go in one order: the
land lock, a record's key lock, then a lane.

A record applies to any commit with the identical tree under the same uv, git and Python, OS,
git config, verify configuration and verifier environment. The environment is keyed by value:
`dispatch.env`, the variables `env_allow` passes, locale and time zone. Shell plumbing (`PATH`,
`HOME`, `TERM`) and per-session values are left out unless `dispatch.env` sets them. The codex
version is recorded but not keyed. Scoped, exempt, no-diff, filtered and failed runs are never
records.

A clean harness head that already has a record is `verified` by it; one with no record and a
full selection makes one, and `agent land` reuses it. A no-diff exemption is the supervisor's
own observation that HEAD is still the start and the tree is clean; an explicit `--no-checks`
binds HEAD only. Neither is a pass record.

**Gap.** Records live in the main checkout's `.agent/verify/`, outside a Codex worker's sandbox,
and workers are refused commands that name the store or run `agent verify`. A Claude worker's
`cp`, `mv` and `python` grants still reach any path, so a record proves an honest run, not one
nobody could fake, until worker containment lands.

Every landing, prose included, certifies its candidate with a full pass record
([[adrs/0062-every-landing-runs-the-full-suite|ADR-0062]]). Small and notes-only records that
older landings left under `.agent/verify/` are never read and certify nothing.

## Landing

Main moves only through `agent land <branch>` (the `harness` tool's `land` action), or through
`repos_sync` with `harness=true`. That one takes the land lock, fetches main from the URL pinned as
`harness.sync_url` in this machine's `agent.local.yaml` into a fresh private ref, and
fast-forwards main to that sha by compare-and-swap when main has nothing it lacks and is checked
out in at most one place. It only ever undoes its own write: any other change to main during the
sync is left as is and reported. A machine that lands through another uses it to reach the
pushed tip, which passed the full-pass gate where it was landed. No git spelling (`pull`,
`merge`) may move main, and the guard resolves symbolic refs (an alias of main or of HEAD) before
deciding: origin, refspecs and tags are mutable. `ff` moves
main to the branch tip; `squash` and `merge` build one commit against current main with `git
merge-tree`, outside the checkout. The default is `ff` for one commit on main, `squash`
otherwise. The candidate tree gets a pass record first.

A landing is one journaled transaction (`.agent/land/<id>.json`). It checks main, the index and
every file it will change, takes `.git/index.lock`, writes files with `read-tree` on a copy of
the index, moves main by compare-and-swap under the lock, renames the index into place, and
reports `done` only when every changed file matches the candidate. Hooks, fsmonitor and filter
drivers are off for its git calls. A file edited during the landing is never overwritten: the
landing stays unfinished and names it. `agent land --recover <id>` finishes, undoes or stops
with nothing changed, and runs before every landing; `--abort <id>` puts the files back. It never
removes a lock it did not write.

One landing runs at a time; another queues up to `--wait` seconds (default 3600). `--detach`,
and the MCP action always, run it in the background. `agent land --status` lists recent
landings. `land A --with B` (the `batch` parameter) lands up to four branches in order as one
tree, one record and one move of main; a failed batch lands nothing.

It refuses, before writing, where checked-out bytes are not blob bytes: a `filter`, `ident` or
`working-tree-encoding` attribute on a changed file (from the working tree, the old tree or the
candidate), `core.autocrlf` on a changed text file with no `eol` attribute, `core.eol=crlf`,
`core.attributesFile`, `core.splitIndex`, a skip-worktree or assume-unchanged entry, or a
submodule pointer change, and checks again under the lock. An `eol=crlf` file lands when all
three attribute sources give it explicit `text eol=crlf` and neither its old nor its new blob
holds a CR: its working file is read with CRLF turned into LF, and a lone CR reads as an edit.
Any other `eol=crlf` file is refused. This repo's `*.cmd` and `*.ps1` launchers are pinned
`text eol=crlf`, so they land like any other file. Its git reads no system config. On Windows
`core.autocrlf` comes from the repo's own config, whatever the value, or else is `true`, Git for
Windows' system default; a global setting never decides it. `core.fileMode=false` and
`core.symlinks=false`, which Git for Windows always writes, are read as git reads them: the
mode comes from the tree and only content is observed, and a changed file whose old and new
entries differ only in that hidden mode is refused by name. To bring main up to origin, land it:
`agent land origin/main --strategy ff`.

**Guarantee.** Every main `agent land` produces has a record, and every pushed tip has one. The
ordinary git spellings that move main are refused to agents. Any other move of main, including
the owner's own commit, is reported by doctor and session start as "main moved outside agent
land"; so is an unfinished landing. A worker set on bypassing this is not stopped until worker
containment lands. Windows lands natively: the land tests pass on a Git for Windows checkout.
Until the full suite passes there too, a lead on a Windows machine lands through a paired machine
where it does.

## Publication

Setup installs this repo's git hooks ([[adrs/0026-git-hooks-gate-commits-and-pushes|ADR-0026]];
`agent git hooks` shows their state). Pre-commit checks staged Python with ruff and the
always-loaded instruction budget. Pre-push reads git's ref updates once and refuses unless, in
order: the checkout is not export-only, no update deletes main ("refusing to delete main"),
every pushed tip (branches, `HEAD:main`, refspecs, tags peeled to their commit) has a full pass
record that applies now. Intermediate commits are not record-checked, only tips. An
unreadable object, a non-commit tip or an unreadable `agent.yaml` refuses. The hook never runs
the suite; a refusal names `agent verify record <sha>`. Records are read from the main
checkout, so a push from a linked worktree is checked the same way. A clone with no `agent.yaml`,
or whose registry does not list the harness itself, is not record-gated. A person's `git push
--no-verify` skips all of it and is outside the guarantee.

For product branches, `push-guard` denies a `git push` of a branch a dispatch record names unless
a `review`-row dispatch finished after its last commit. It checks review state, not the owner's
authorization.

A force-push does not remove a commit from GitHub: the commits API still returns a replaced
commit by sha, and the same holds for a deleted branch. Before proposing a force-push or a branch delete as cleanup, say that the replaced commits stay
fetchable by sha. Removal means deleting and recreating the repo. Check with
`gh api repos/<owner>/<repo>/commits/<sha>` before calling the content gone.

A pre-publish audit reads every added line. A search for known private terms misses content
that a reader going through the diff line by line finds. An audit names its commit range and its
reader reads every added line. A term search supports that read; it does not replace it.

A PR review comment the manager drafts for the owner is one to three sentences: the problem,
the evidence in one clause, and a question. Full reasoning stays in the feature log.

## Export

`agent export <dir> --remote <url> -m <message>` archives `HEAD` into an empty directory outside
this checkout and commits it as one squashed commit. The commit is authored and committed as
`export.identity` from the shared `agent.yaml` ([[harness/agent-yaml]]), never the checkout's git
config; without that key export refuses before building. It refuses a remote that is this
checkout's own origin, flags any path of the private set in the tree, and stops before pushing:
the owner reads the result and runs the printed push command.

An older standalone pre-push hook, marked on its second line as the retired push hook, runs a
subcommand that no longer exists. Setup's hook install replaces that hook with the gate's own
pre-push; a hook without the marker is left alone and reported.
