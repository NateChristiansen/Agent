---
tags: [harness]
---

# agent.yaml

`agent.yaml` is the harness config ([[adrs/0030-agent-yaml-config|ADR-0030]]): the repos the
harness manages, who it works for, which code host each repo is on, and the extra MCP servers a
worker can be given.

There are two files:

| File | Where | Shared? | Holds |
|---|---|---|---|
| `agent.yaml` | your notes repo, linked into each clone | yes, versioned with your notes | everything that is the same on every machine |
| `agent.local.yaml` | the clone root, gitignored | no, one per machine | what differs per machine: repo paths, the notes sync switch and the machine label |

`agent.example.yaml` is the tracked template. Setup copies it into a new notes repo. Never put
real repos or names in the example: this repo is public.

The easy way to edit either file is the `agent-config` skill: `/agent-config` in Claude Code, or
`$agent-config` in Codex (Codex spells every skill with `$`). Run it with no argument to see
what is set and what is missing, or `/agent-config repo <name, path or URL>` to add a repo.
Call the `repos_list` tool after a hand edit: it loads and validates the whole file and names the
key that is wrong.
Use `repos_validate` (`agent repos validate` on the CLI) to check unknown keys too. Unknown
shared, repo and dispatch keys are errors there, with a closest-key hint when one matches.
Doctor warns about those keys; ordinary registry loading keeps accepting them for compatibility.

## How paths resolve

A relative path is resolved from the clone root, the folder holding `bin/agent`. It is never
resolved from the notes repo or from the directory you ran a command in. `../repos/web` means a
`repos/` folder beside the harness clone. Clones live there, never inside the harness, because
Claude Code loads every `CLAUDE.md` up the directory tree.

`agent.yaml` accepts only relative paths, because the notes repo can be shared by machines whose
folders differ. A machine that keeps a repo somewhere else sets an absolute path in its own
`agent.local.yaml`. The `harness` tool with `action="doctor"` prints each repo's resolved path and says when it came
from `agent.local.yaml`.

## Top level

| Key | Type | Default | What it does |
|---|---|---|---|
| `version` | integer | `1` | The file format. Only `1` exists. |
| `forge` | `github` or `gitlab` | `github` | The default code host. It decides which CLI a worker may read with (`gh` or `glab`); every write through it is denied. A repo can override it. |
| `owner` | mapping | none | Its `name` is who the harness works for. The session-start block names them, and everything else says "the owner". Setup asks you to set it. |
| `worker_mcp` | mapping | none | Extra MCP servers a single dispatch can hand its worker (`dispatch new --mcp <profile>`). See below. |
| `repos` | list | required | The repos the harness manages. See below. |
| `peers` | mapping | `defer_nonurgent_s: 0` | Peer wakes. Questions, decisions and requests ring at once and acknowledgements never ring. A positive `defer_nonurgent_s` lets other informational messages wait unrung for the recipient's next turn, or that many seconds before the sender's next turn end rings them. |
| `journal` | mapping | none | Its `machine` is the one machine label (1-8 lowercase letters and digits, starting with a letter) allowed to write journal notes. Any other machine is refused on `journal write` and `journal collect --write`, and its journal nudges stay quiet. Unset, every machine writes, as before. A non-mapping, an extra key or a bad label is a config error. See [[harness/journal]]. |
| `hooks` | mapping | all on | Switches a removable hook off on every machine: `hooks: {thrash-guard: false}`. `agent.local.yaml` can set it too, and wins. Only thrash-guard, peer-route, plan-gate, mcp-stale, pull-warn, journal-pending, ask-guard and record-guard can be switched. A retired hook's name is ignored; any other name, or a non-boolean value, is a config error in the file that has it. |
| `export` | mapping | none | Its `identity` (exactly `name` and `email`, both non-empty strings with no `<`, `>` or line break; the email holds an `@`) authors and commits the squash `agent export` builds. Without it export refuses; it never falls back to git config. Anything else under `export`, or a malformed identity, is a config error. |
| `notes_sync` | — | — | Not read here. It belongs in `agent.local.yaml`, because the sync itself would carry a shared value to every machine. See below. |
| `machine` | — | — | Not read here. Each machine's dispatch label belongs in `agent.local.yaml`. See below. |

## `repos:` entries

| Key | Type | Default | What it does |
|---|---|---|---|
| `name` | string | required | The repo's name. |
| `short` | string | `name` | The handle used everywhere else: `repos sync <short>`, worktree folders, dispatch ids. Names and shorts must be unique. |
| `path` | relative path | required | Where the clone is. |
| `remote` | git URL | none | Where the clone comes from. Doctor warns when the clone's `origin` does not match. Leave it out for a local-only repo: sync never clones, fetches or pulls it, doctor reports it as local-only, and a dispatch into a clone with no `origin` starts from the local `default_branch`. |
| `default_branch` | string | `main` | The branch workers start from. |
| `worktrees_dir` | relative path | none; doctor warns | Where dispatch worktrees go. When it is omitted, dispatch puts them in `_worktrees/<short, lowercased>` inside the clone's parent folder, and `repos sync` and the path guards skip worktree handling. Set it. |
| `forge` | `github` or `gitlab` | top-level `forge` | This repo's code host, when it differs from the default. |
| `harness` | string | none | The name of the repo's own agent harness, told to its workers. `none` means it has none. |
| `notes` | string | none | What a worker needs to know first: the package manager, the test command, the convention reviewers reject. It is shown to every worker. |
| `dispatch` | mapping | `{}` | How workers run in this repo. See below. |

## `dispatch:` keys

| Key | Type | Default | What it does |
|---|---|---|---|
| `model` | string or null | rubric row, then client default | Pins a Claude worker model; an explicit launch model wins. Codex ignores this repo key and uses its explicit model or rubric row. |
| `model_from` | — | — | Retired: remove it from your config. A config that sets it fails validation, and a dispatch to that repo is refused. Pin `model` if the repo needs one. |
| `effort` | `low`, `medium`, `high`, `xhigh`, `max` | vendor default | Claude only: applies after explicit or parent effort, before the vendor default. Codex ignores this repo key and uses explicit/parent effort or the rubric row's effort policy, then the vendor default. |
| `permission_mode` | `default`, `acceptEdits`, `plan`, `bypassPermissions`, `dontAsk`, `auto` | `acceptEdits` | The worker's Claude Code permission mode. |
| `max_budget_usd` | positive number | `2.0`; a rubric row's upper band can raise it | Repo spend cap for both providers; Codex uses estimated cost. An explicit launch budget overrides it; raising it needs a reason. |
| `timeout_minutes` | positive number | `45` | Wall-clock cap. |
| `max_turns` | positive integer | `200` | Turn cap (Claude workers). |
| `max_tool_items` | positive integer | `400` | Codex workers' cap on completed tool items (commands, file changes, MCP calls); Codex has no turn cap. |
| `extra_args` | list of strings | `[]` | Extra `claude` flags. |
| `allowed_tools` | list of permission strings | `[]` | What the worker may run on top of the standard edit, read and commit set, such as `Bash(npm test:*)`. |
| `required_checks` | retired | — | Refused whenever present, even empty or null. Workers no longer report commands; write each check as a `verify` command instead. |
| `verify` | list of `{command, timeout_seconds}` | unconfigured | Commands the supervisor runs itself after the worker finishes. Omitting it blocks non-exempt launches. Use real, measured commands, never invented ones ([[harness/safety#Verification]]). |
| `verify_timeout_seconds` | positive number | required with `verify` | The total bound on verification. |
| `max_concurrent` | positive integer | `3` | How many workers may run in this repo at once, each in its own worktree. A value that is not a positive integer reads as `1`. |
| `max_concurrent_verify` | positive integer | `1` | How many full or scoped verification runs may execute in this repo at once: supervisor verifiers and pass-record runs (landings) share these lanes. A run waits for a free lane before its command starts. |
| `max_pending_verify` | positive integer | twice the lanes, at least `2` | How many finished dispatches may wait for verification at once. A worker gives its repo slot back when its model exits, so new work can start while it waits; once this many are waiting, a new launch is refused with their names. |
| `verify_wait_timeout_s` | positive number | `1800` | Seconds the supervisor waits for a free verification lane. This is separate from the verifier command's own timeout, which starts when the command does. |
| `setup` | list of shell commands | `[]` | Run once in a fresh worktree, such as installing dependencies. |
| `probes` | list of shell commands | `[]` | Commands a worker will need, such as `node --version`. Preflight scores each against the worker's rules, and for a Codex worker against its sandbox, before launch. The harness never runs them. |
| `env` | mapping of NAME to value | `{}` | Variables set for the worker. |
| `env_allow` | list of names or `NAME_*` prefixes | `[]` | Variables the worker may inherit from your shell. Everything else is withheld, credentials included. |
| `branch_template` | format string | `feature/{slug}{suffix}` | The worker's branch name, over `{slug}`, `{number}`, `{ticket}` and `{suffix}`. It must name one of the first three, and it must name `{suffix}`: that renders `-<tag>-SS`, such as `-mac-01`, so each machine's dispatches get their own branch (ADR-0036). `dispatch new` refuses a template without it. |
| `repo_mcp` | list of server names | none | Servers from the repo's own `.mcp.json` a worker gets. Omitted, null or `[]` loads none. |
| `diff_rules` | list of `{paths, change, forbid, doc, rule}`, each with optional `lines` | `[]` | A change a worker's diff must not make: `paths` globs, `change` is `added`, `modified` or `any`, `forbid` a regex no added line may match, and `doc` and `rule` quote where the repo says so. `lines: removed` checks removed lines instead, so a rule can forbid deleting what matches; `change` is then `modified`, `deleted` or `any`, and a finding names the line in the base file. `lines` defaults to `added`. |
| `codex_network_access` | boolean | `false` | Lets a Codex worker's sandbox reach the network. |

## `worker_mcp:` profiles

Each key is a profile name, and its value is one server:

| Key | Type | Default | What it does |
|---|---|---|---|
| `server` | string | required | The server's name, as the worker sees it. |
| `command` | string | required | What starts it. |
| `args` | list of strings | `[]` | Its arguments. `{root}`, `{output_dir}` and `{dispatch_id}` expand at launch. |
| `env` | mapping of NAME to string | `{}` | Its environment. |
| `allow` | list of strings | `[]` | The tool rules the profile grants, such as `mcp__playwright__*`. |
| `deny` | list of strings | `[]` | Single tools withheld from a server `allow` grants whole. |
| `requires_file` | path | none | The launch is refused while this file is missing, such as a saved browser login. |
| `requires_hint` | string | none | How to create that file. |

```yaml
worker_mcp:
  qa-staging:
    server: playwright
    command: npx
    args: [--yes, "@playwright/mcp@0.0.81", --headless, --isolated,
           --allowed-origins, "https://staging.example.com", --output-dir, "{output_dir}"]
    allow: [mcp__playwright__*]
    deny: [mcp__playwright__browser_evaluate]
    requires_file: .agent/browser/staging.json
    requires_hint: "npx --yes playwright open --save-storage .agent/browser/staging.json https://staging.example.com/"
```

Pin the package version and limit where a browser may go: a worker runs unattended.

## `agent.local.yaml`

```yaml
notes_sync: pull                # off (the default), pull, or on
machine: mac                    # this machine's dispatch label; different on each machine
obsidian_vault: Agent           # optional; the vault Workers links open (default: the main checkout's folder name)
repos:
  web:                          # a repo's short or name from agent.yaml
    path: ~/code/web            # absolute, ~, or relative to the clone root
    worktrees_dir: D:\worktrees\web
```

`notes_sync` switches the automatic notes repo sync on this machine (ADR-0034). `off` runs no git
command. `pull` fetches and integrates the notes repo at session start and never commits.
`on` also commits every notes change after a turn and pushes it. A missing or unknown value
means `off`.

`machine` is this machine's label in new dispatch ids (ADR-0036). Ids read `NNN-<tag>-SS`, such
as `017-mac-01`; handoffs are `<tag>-SS-<short>.md`, and every derived branch and worktree path
ends in `-<tag>-SS`. Legacy `NNN-SS` ids stay valid. The value is lowercased and must be 1-8
letters and digits starting with a letter. Without it, the ADR-0035 enrolled device name is used
if it fits. With neither, `dispatch new` refuses. Give each machine its own
label: two machines with one label collide as if they had none. Each handoff also records the
install that wrote it (`install:`, a random token minted once under `.agent/`), and `dispatch
new` refuses to reuse another install's label. Notes auto-sync commits name it in their body, and
their subject lists the features they staged.

`obsidian_vault` names the vault a Workers row's `open` link targets. Without it, the link names
the main checkout's folder, from any worktree. Only a main checkout that has this file rewrites
handoff fields in the shared notes on render, so a worktree, a probe clone or a fresh clone never
does. `notes_writer: false` opts a copied checkout out.

Under `repos`, only `path` and `worktrees_dir` can be set, and only for repos `agent.yaml`
lists. A key you leave out comes from `agent.yaml`. A value must be a non-empty path: `null` is
an error, not a way to unset something. Anything else unexpected fails loudly rather than being
ignored.

`maintenance` holds dispatch starts back on this machine. Only the owner edits it: the file is
outside every worker's write grant and no MCP tool sets it.

```yaml
maintenance:
  window: "toolchain upgrade in progress"   # refuse every launch, resume and auto-resume
  parked: ["012", "014"]                    # refuse build and resume for these features
  reason: "waiting on an upstream fix"
```

A refusal happens before the dispatch has any effect. Read-only commands, cancel, collect and
abandon still work. A parked feature shows as `blocked (parked)` in Dashboard with no next task,
and session start tells a session mapped to it to stop. A malformed block refuses every start
rather than quietly stopping to gate. `blocked` alone never stopped a launch: it is an open
status. Each status change is logged with its `reason`, and Dashboard, session start and doctor
flag a blocked feature that has a running dispatch, a dispatch, landing or tick since the block.

`hooks` switches removable hooks off on this machine, over the shared `agent.yaml` value. A
switched-off hook does not run at all. The safety core (commit, push and chaining guards, generated-file protection and
the rest) cannot be switched.

```yaml
hooks:
  thrash-guard: false
```

`harness` says what this install is to the harness repo (ADR-0043). The default, `source`, is where
harness changes are made. A `consumer` runs the published harness and contributes nothing back:
its agents may not commit, merge, rebase, land or push in the harness checkout, the one main move
allowed is a fast-forward to the upstream. Its lessons go to the same local queue every install
keeps, and `/lesson review` files the harness ones upstream as issues (ADR-0048). Product repos are
unaffected. A malformed block reads as `consumer`.

```yaml
harness:
  role: consumer
```
