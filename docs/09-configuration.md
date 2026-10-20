# Configuration

Describe the change you want to the manager. It can inspect a repo, propose settings, and validate them before launching workers.

## Find the right file

`agent.yaml` holds shared settings: your name, the repo registry, forge choice, worker defaults, worker MCP profiles, and the journal machine. Setup links it from the notes repo.

`agent.local.yaml` holds this machine's settings and stays in the harness clone. Use it for absolute repo paths, worktree locations, the machine label, notes sync, and local hook settings. For each repo, it can override `path` and `worktrees_dir`; other settings, including worker limits, come from `agent.yaml`.

Shared repo paths must be relative to the harness root. If a repo already lives elsewhere, ask the manager to put its absolute path in the local file:

> Register my API repo at ~/code/api. Keep that path local to this machine.

## Add or change a repo

The manager inspects the repo's default branch, existing instructions, and tooling, then asks about anything unresolved. It records the remote, path, and worker configuration under the repo's name and optional short name.

Worker settings can include commands for preparing a fresh worktree, permission rules, environment variables, model choices, budget and time limits, and concurrency. The repo's forge selects GitHub or GitLab; a shared forge setting supplies the default.

If the build needs a credential or an environment variable, tell the manager what access is intended. It can add explicit values to the worker's allowlisted environment.

## Configure checks workers can run

Tell the manager which checks the repo needs:

> Workers need to run the unit tests and type check. Configure both with timeouts and show me the preview.

The manager records checks under `dispatch.verify`, with a timeout for each and an overall `dispatch.verify_timeout_seconds` limit.

A checked launch needs this configuration. You see the commands in the preview and their results after the supervisor runs them. A run approved as exempt skips these checks and is labelled exempt.

## Give a worker extra tools

Manager integrations live in `.mcp.json`. Worker access is configured separately, so an integration available in your conversation is not automatically available to a worker.

Ask for the capability you need:

> Add a browser profile for staging, limited to that site's origin. Show which tools it allows before using it.

The manager configures profiles under `worker_mcp` in `agent.yaml` and selects them for each launch. A repo's own servers can be included through `dispatch.repo_mcp`.

The preview shows the selected tools and any missing access or login prerequisites. See [approving a concrete launch](13-safety.md#approve-a-concrete-launch).

## Change hooks deliberately

Hooks supply session context, capture activity, and guard operations. Describe the behavior you want changed; the manager proposes a setting and explains its effect.

These hooks can be turned off under `hooks` in shared `agent.yaml` or this machine's `agent.local.yaml`. Set the hook's name to `false` to disable it; local settings override shared ones.

- `thrash-guard` nudges the manager when commands, edits, or errors repeat.
- `peer-route` directs messages to managers through the harness inbox.
- `plan-gate` checks that a plan has been reviewed before it reaches you for approval. It runs for Claude managers only.
- `mcp-stale` warns when changed tool definitions need a reconnect.
- `pull-warn` warns before reading a registered repo that has not been pulled in the session.
- `journal-pending` reminds the manager about milestones awaiting a journal entry.
- `ask-guard` checks that questions waiting on you are registered so they show in the vault.

Automatic notes sync has its own local `notes_sync` setting in `agent.local.yaml`; see [sync modes](08-your-notes-repo.md#choose-how-it-syncs). Core safety hooks stay active.

For Windows sandbox and scrolling settings, see the recovery steps in [troubleshooting](14-troubleshooting.md#check-the-windows-sandbox).

Next, choose and prepare your [agent clients](10-agent-clients.md), including hook trust after updates.
