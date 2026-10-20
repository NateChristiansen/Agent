# Repo intake record

One record per repo and machine, at `features/<feature>/intake/<short>-<machine>.md`. Every
field has a value, a source and one state. A second machine copies the shared rows and adds
only its local rows and its own validation. Secrets never enter the record.

## States

| State | Meaning | Example row |
|---|---|---|
| detected | Tracked item read at the recorded SHA, or local state read from the local clone; evidence cited | `default_branch` = `main`; `git ls-remote --symref origin HEAD` -> `ref: refs/heads/main`; detected. Local: `core.hooksPath` = `.githooks`; `git config core.hooksPath` -> `.githooks`; detected |
| inherited | Harness default or an existing entry's value | `dispatch.permission_mode` = `acceptEdits`; `harness/agent-yaml` default; inherited |
| owner-confirmed | The owner answered it, in this run or a recorded earlier one (cite where) | `dispatch.max_budget_usd` = `6`; offered default, owner accepted in this run's batch; owner-confirmed |
| unresolved | Needs the owner, or local state with no local clone yet (say why); or a repo value the harness must adapt, with the adapted value prefilled; goes into the decision batch | `test command` = two CI suites (`ci.yml` lines 20 and 41); which one gates; unresolved. `dispatch.branch_template` = `feat/{number}-{slug}{suffix}`; `AGENTS.md` says `feat/<id>-<slug>`, the harness adds `{suffix}`; unresolved |
| unsupported | The harness cannot do it on this machine yet; names the blocking feature | `dispatch.verify` = `npm test`, timeout unmeasured; blocked until the import feature measures it; unsupported |

A value the repo states as-is is `detected`. A value the harness must adapt (a branch convention
that needs the dispatch suffix, prose turned into a shell command, a pattern turned into a
regex) is `unresolved`, prefilled. A row with any adapted part is `unresolved`. A blocked
prerequisite is recorded as `unsupported` on the field it blocks and gets no row of its own;
one that blocks no field (the write path) appears only in the header outcome.

A limit the repo states for workers (ask first, never rebase, ask before merge or publish) is
`detected`; "ask first" means "not without the owner". A grant to workers is always
`unresolved`, prefilled from the evidence, because only the owner grants. The grant rows are
`dispatch.allowed_tools`, `dispatch.env_allow`, `dispatch.codex_network_access`, both
`worker providers` rows and `dispatch.repo_mcp`. Restrictions (`launch the app`,
`commit and publication limits`, `forbidden repo commands` when classified) stay `detected`.

## Header

- Repo, short, machine, date.
- Fetched SHA and method (`git show <sha>:<file>` on a fetch, or `repos_sync pull=True`).
- Procedure outcome: `configured` or `blocked: <named prerequisite>`. Never "ready".

## Fields, in asking order

One row each: `field | value | source (path@sha or command and output) | state`. The record has
exactly the rows below, and each question has one owning row. A `dispatch:` key gets a row only
on its evidence trigger; every other key, and every documented default, gets none (an existing
entry's value is preserved, not re-listed). Do not restate defaults.

1. Always a row: `name`, `short`, `remote`, `forge`, `default_branch`, `path`, `worktrees_dir`,
   `harness`, `notes`.
   - `remote`: a remote with no host (a local path) is `unresolved`; the batch asks for a
     host-bearing URL.
   - `forge`: a remote with no host (a local path) is recorded as having no host, `detected`.
     `forge` accepts only `github` or `gitlab`; no override is written for it.
   - `name`, `short`: when the remote's basename and the repo's own name (manifest, else README)
     disagree, `unresolved`, both candidates listed, the manifest name prefilled.
   - `worktrees_dir`: for a new repo, always `unresolved`, prefilled
     `../repos/_worktrees/<short, lowercased>`.
   - `harness`: the name of the repo's own agent harness, or `none`; as `harness/agent-yaml`
     defines it. Never worker-facing facts.
   - `notes`: worker-facing facts the repo's instructions state (ask-first list, never-change
     files and paths, ports and locks, app limits), the known test command and the
     verification status. Summarising facts the repo states is `detected`, not adaptation.
2. Always a row: `launch the app` (runtime actions a worker may take). Also
   `dispatch.repo_mcp`, only when `.mcp.json` lists servers; it selects servers only, and
   its `mcp__<server>__*` or exact `mcp__<server>__<tool>` grants go in
   `dispatch.allowed_tools` (item 5). The
   `dispatch.repo_mcp` row is a grant: `unresolved`, prefilled. This sentence does not cover
   `launch the app`, which is a restriction and stays `detected`.
3. Always a row: `commit and publication limits`. Also `dispatch.branch_template`, only when the
   repo states a branch convention. When the convention's `<id>` could map to `{number}` or
   `{ticket}`, the prefill lists both and the question asks which.
4. Always a row: `forbidden repo commands`; `unresolved` while any repo CLI command is not
   classified by the instructions. No `dispatch.diff_rules` row: never-change files and
   ask-first paths go in `notes`.
5. `dispatch.setup`, only when the repo documents setup for a fresh worktree or an MCP server
   or check needs an install step. `dispatch.allowed_tools`, only when setup or check commands
   fall outside the standard set, or `dispatch.repo_mcp` has a row; it owns tool grants,
   including an `mcp__<server>__*` or exact `mcp__<server>__<tool>` grant per selected server;
   a bare `mcp__<server>` or any other pattern naming a selected server refuses the launch,
   alone or beside a valid grant. A grant: `unresolved`, prefilled.
6. `dispatch.env_allow`, only when the repo's setup, docs or MCP config name env variables
   (names only). A grant: `unresolved`, prefilled with the names.
7. Always a row: `dispatch.verify` with `verify_timeout_seconds`, and whether it is measured.
   Unmeasured is `unsupported`, blocked until the import feature measures it.
8. Always a row: `dispatch.max_budget_usd`; offer `6`.
9. Always two rows: `worker providers: claude` and `worker providers: codex`. Both rows are
   grants: `unresolved`, prefilled from the evidence. When the repo has its own
   `.codex/hooks.json`, the Codex question says a Codex dispatch refuses any of its hooks that
   Codex reports untrusted or disabled, so the owner reviews, trusts and enables the current
   definitions in Codex first.
10. `dispatch.codex_network_access`, only when setup or checks need the network; a package
    install in setup or checks (for example `npm ci`) counts. Grant: `unresolved`, prefilled
    `false`; the question names the install step that would need network.
    `dispatch.max_concurrent`, only when the instructions say two workers cannot run at once
    or setup or checks use a shared lock or port. A lock or port that only the running app
    takes does not trigger it; it goes in the `launch the app` row and the Concurrency
    inventory row.

## Inventory rows

One row per area from the procedure's inventory stage: area, present or absent, evidence, and
the effect on a dispatched worker. Absent areas are listed too.

## Validation

Each `repos_list`, `notes_status`, doctor and dry-run result. Every refusal is copied verbatim.
