# Add or edit one repo

Read [[harness/agent-yaml]] for the schema. Keep the shared identity, remote, default branch,
forge and dispatch settings in `agent.yaml`; keep machine-specific locations in
`agent.local.yaml`. This procedure discovers configuration, not product implementation. It
runs the same stages in order on Claude and Codex. One run onboards one repo on one machine; a
second machine reruns it and adds only its local block and its own validation.

Rules for every stage:

- Cite each detected value as `path@sha` or a command and its output. Secrets never enter the
  record.
- Write no config before the decision batch is answered. The intake record is a working
  record in the feature folder: written before the batch, updated with the answers after it.
- Never say "ready". The last word is `configured` or `blocked: <named prerequisite>`.
  Readiness comes only from the live probe stage.
- A rerun on a registered repo preserves every existing value it was not asked to change and
  never adds a second entry.

## S0 Resolve the input

Call the `repos_list` tool first. An invalid config is an error to resolve, not an empty list.
Match the input against existing entries by remote first, then name or short. A match edits
that entry; no match adds one.

| Repo input | Resolve it this way |
|---|---|
| none | List repos with effective forge, branch, resolved path and clone presence. Ask which to edit or what to add. |
| registered name or short | Select that one entry; preserve its current dispatch settings. |
| local clone path | Read Git metadata to identify its origin. Match existing registry remotes before adding an entry. Ask about multiple remotes or multiple matches. |
| remote URL | Match existing entries first. Otherwise fetch it for reading (S1); clone into an unused `../repos/<name>` directory only for a clone the owner keeps. Never overwrite an occupied destination. |
| unregistered bare name | Check `../repos/<name>` for a clone. If absent, ask the owner which organization or namespace holds it, look up the exact repo under the configured forge, then show the result and ask before cloning. |

For a bare-name lookup, use read-only `gh repo view <org>/<name>` or `glab repo view` with the
correct host/namespace. A placeholder organization, ambiguous forge or unknown self-hosted
endpoint is an owner question. Do not infer forge solely from a custom host's spelling. Report
authentication or network errors as such. Claim no match only after a successful exact lookup;
if searching a list, inspect all pages. Ask for the URL when no repo is found. Never guess
between matches.

## S1 Freshen before reading product files

Establish the fetched default-branch SHA before reading any product file, and record the SHA
and the method.

- Registered repo: call `repos_sync` with `name=<short>` and `pull=True`. A dirty or
  non-default checkout can skip the pull; preserve it. Use the fetched default-branch commit
  for detection when the working tree was not fast-forwarded, and disclose that source. Report
  a broken registered worktree as its own error.
- Unregistered repo: `git ls-remote --symref <remote> HEAD`, then fetch (or clone if absent),
  then read tracked files with `git show <sha>:<file>`. This meets the pull-before-research
  rule for a repo that is not registered yet; no entry is written before the decision batch.
- Local-only clone (an existing clone with no remote): skip `ls-remote` and fetch. Take the
  SHA from `git rev-parse <default-branch>` in the clone and record the method as local.
- When Git refuses a read for dubious ownership, repeat it with a command-local
  `git -c safe.directory=<path> ...`. Never change global config.
- If HEAD cannot be resolved, ask rather than assuming `main`. Never read a stale working tree
  to report test commands.

## S2 Detect basics

- Remote: use the clone's selected remote URL, preserving its usable transport. Never record
  an embedded password or token; ask for a credential-free URL if one is supplied. A remote
  with no host (a local path) is `unresolved`; ask for a host-bearing URL. A clone with no
  remote at all is local-only: record the remote as absent (`detected`) and S6 omits `remote`.
- Forge: `github.com` identifies `github`, `gitlab.com` identifies `gitlab`; confirm custom
  hosts. A remote with no host (a local path) is recorded in the `forge` row as having no host
  (`detected`); S6 writes no `forge` override, since only `github` or `gitlab` is valid. Set a
  per-repo override when a host-bearing remote differs from the shared default. Changing one repo's forge does not change the
  registry-wide value.
- Default branch: use the remote HEAD result, not the current feature branch or stale
  `origin/HEAD` alone. With no remote, use the clone's `init.defaultBranch` if its branch
  exists, else the checked-out branch; ask when neither is clear. Record the source.
- Name and short: match an existing entry first. For a new entry propose names from the remote
  and check all existing names and shorts for collisions. When the remote's basename and the
  repo's own name (manifest, else README) disagree, the row is `unresolved`, lists both, and
  prefills the manifest name.
- Clone location: when the supplied clone is outside `../repos/`, offer the owner a choice:
  keep it there with an absolute `agent.local.yaml` override, or create a sibling clone. Never
  move it automatically. Keep product clones outside the harness vault. The shared entry still
  needs a portable relative location for other machines; ask if that location is unclear.
- Worktree location: for a new repo propose the shared `worktrees_dir`
  `../repos/_worktrees/<short, lowercased>`, the location `harness/agent-yaml` documents as the
  dispatch default; doctor warns when it is omitted.

## S3 Inventory the repo's agent setup

Read each tracked item at the S1 SHA. Local state is not in Git history: `.git/hooks`,
`core.hooksPath` and other local git config, and ignored files. Read it from the local clone,
with the command and its output as evidence. A read must not change the clone: list ignored
files with `git ls-files --others --ignored --exclude-standard`, never `git status` (it runs
the LFS filter, which writes hooks into `core.hooksPath`). With no local clone yet, record each local-state
item `unresolved` and say why. For every area record present or absent, the evidence, and the
effect on a dispatched worker. List absent areas too.

| Area | Look for |
|---|---|
| Instructions | `AGENTS.md`, `CLAUDE.md`, `.agents/`, workflow docs; "ask first" and forbidden-action lists |
| Claude config | `.claude/settings.json` hooks (what they rewrite, block or run on Stop), skills, agents, `WorktreeCreate` |
| Codex config | `.codex/hooks.json`, `.codex/config.toml` (MCP servers, approvals) |
| MCP | `.mcp.json` servers and their prerequisites (`npm ci` and the like) |
| Git hooks | `.git/hooks`, `core.hooksPath`, pre-commit config; what each needs installed |
| Repo CLI | Its own worktree, merge, publish, release commands; which a worker must never run |
| Conventions | Branch prefix, commit format, trailers, never-rebase, files never hand-edited, size caps |
| Checks | CI config and the exact commands it runs; local gates when there is no CI |
| Per-machine state | Gitignored files and folders a fresh worktree lacks (tool paths, SDKs, local settings, nested repos) |
| Storage | Git LFS (`filter.lfs.required`), submodules |
| Concurrency | Running-app locks, fixed ports, anything that stops two workers in parallel (app-only ones go in `launch the app`, not `dispatch.max_concurrent`) |

Test command: inspect tracked CI workflows, manifests and scripts, build files and the repo's
agent instructions at the S1 SHA. Prefer the command CI actually runs, including its working
directory and package manager; with no CI, use the local gates. The commands of one CI job
together are one suite. Multiple suites or conflicting evidence require an owner choice. A language or
manifest filename alone does not establish a test command.

## S4 Write the intake record

Fill [the intake template](intake.md) at `features/<feature>/intake/<short>-<machine>.md`
before the batch; it is a working record, not config. Each field gets a value, a source and
one state: detected, inherited, owner-confirmed, unresolved or unsupported (naming the blocking feature). Preserve existing model, effort,
budgets, tools, environment rules and branch template unless the owner requested changes.
Unknown harness or setup settings stay unset with that stated; do not copy illustrative
commands from `agent.example.yaml`.

## S5 Ask one decision batch

One batch, prefilled from S2-S4, asking only `unresolved` fields, in the template's field
order. Claude asks in consecutive `AskUserQuestion` calls of at most four questions each, in
the fixed field order, with no other work between them; Codex asks one numbered list. Both ask
the same questions; the count follows the unresolved set, not the client. Show each detected value
with its evidence so the owner can correct it. After the answers, update the intake record
(`owner-confirmed`, citing this run) before S6 writes any config.

- Budget: offer `dispatch.max_budget_usd: 6` as the default, matching the `review` row; an
  existing entry's budget stays unless the owner changes it.
- Verification: a detected test command is not a measured verification budget. Configure
  `dispatch.verify` and `verify_timeout_seconds` only from source-backed commands and measured
  bounds, following [[harness/safety#Verification]]. Do not run product tests or invent a timeout.
  Propose that a dispatched worker runs the repo's check commands once and reports the commands
  and timings; the import feature sets `verify` and `verify_timeout_seconds` from
  that report before its checked probes. Until then record verification as unconfigured, put
  the known test command in repo `notes`, and report dispatch as blocked for non-exempt work.
- Do not grant new tool permissions without the owner's decision.

## S6 Write

Change `agent.yaml` through the `harness` tool's `notes_write` action, never with Edit or
`apply_patch` on `agent.yaml` itself. It is a symlink into the notes repo, a hard link on plain
Windows, and both tools write a new file and rename it over the old one, which severs a hard
link. Three calls:

1. `action="notes_write"`, `target="agent.yaml"`: copies the file to a scratch draft and returns
   its path.
2. Edit the draft with the Edit tool (Codex: `apply_patch`): add or change exactly one shared
   entry with the owner-approved fields (on a rerun, only those being changed). Keep inherited
   values and comments. Then call `notes_write` again with `from_file=<draft>`; it returns the
   diff and `before_sha256`. Show the owner that diff.
3. `notes_write` with `from_file=<draft>`, `apply=True` and `expect_sha256=<before_sha256>`. It
   refuses if `agent.yaml` changed since the preview, and its result's `link` is `linked` when
   the link held.

`agent.local.yaml` is a plain file in the clone, so edit it directly with Edit (Codex:
`apply_patch`): one block keyed by the registered short with the machine's `path` and/or
`worktrees_dir`, never both name and short as aliases. Keep other local overrides. Facts with no
key go in `notes` or `harness`, written for a worker; worker-facing facts go in `notes`, and
`harness` is the harness name only. `repos_list` then validates both files (`repos_validate`);
fix only errors this edit introduced.

## S7 Validate

1. `repos_list`: the selected entry's effective values.
2. `harness` tool with `action="notes_status"`: link health.
3. `harness` tool with `action="doctor"`: repo registry, notes link and MCP registration checks, with
   unrelated findings listed apart; `repo <short> path` when path resolution needs confirming.
4. `dispatch new dry_run=True` once per intended provider, under a feature that lists the repo
   in `repos:`.

5. Until the validator rejects strings, check that each `dispatch` boolean written in S6
   (such as `codex_network_access`) is a YAML boolean, not a quoted string.

Copy every refusal verbatim into the record. Fix only errors this change introduced.

## S8 Report

Changed keys and which file holds them, the S1 SHA, the resolved path and which values are
local, each unsupported and unresolved field, verification status, each dry-run result, and the
final word `configured` or `blocked: <named prerequisite>`. A registry that validates, or a
dry run that passes, does not mean dispatch is ready.

## S9 Live probe

Not run by this skill; the import feature runs it. Per repo, machine and provider,
a checked dispatch passes when it returns `done` with successful supervisor verification on the
recorded head, calls one tool on the repo's selected MCP server, and is denied one forbidden
action with the policy named and no side effect. Record owner active minutes, input batches and
manager repairs. Only a pass here supports calling the repo ready.
