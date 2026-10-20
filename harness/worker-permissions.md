# Permissions: worker MCP grants and the Codex manager

What MCP servers a worker gets, how outside services are configured, and what a Codex manager
session may run without asking. The wider safety model is [[harness/safety]].

## What MCP servers a worker gets

A worker gets the servers its dispatch names and nothing else
([[adrs/0020-explicit-worker-mcp-grants]]). Every Claude worker launches with
`strict_mcp_config=True`, on a first attempt and on a resume.

Three things go into the map: the harness `agent` server, the profiles the dispatch asked for,
and the product servers `dispatch.repo_mcp` names.

| `dispatch.repo_mcp` | What the worker gets |
|---|---|
| missing or null | No product servers. The preview says this was the default, not a choice. |
| `[]` | No product servers. The preview says someone chose empty. |
| names | Only those definitions from the product's `.mcp.json`. |
| an unknown name, an invalid definition, a duplicate, a collision | Refused before launch. |

The harness name is reserved. A profile cannot take it, profiles cannot take each other's, and
a product definition cannot replace a harness or profile server, not even with identical
content. Naming a server does not grant its tools: the tool permission rules and the profile
readiness checks still apply. None of this hands out a credential.

An existing worktree supplies the product definitions. A dry run with no worktree yet reads the
registered clone and says so. Once a worktree exists, a selected file that is missing,
unreadable or malformed refuses; it never falls back to the clone. Each attempt resolves one
composition, and the MCP sidecar, the SDK configuration and the redacted preview are built from
it. A resume resolves again from scratch. Profile launchers still read their own configuration
when they run, so this is not a frozen snapshot of every grant.

The preview shows server names, whether the selection was default or explicit, and where each
came from. It omits commands, URLs, headers, credential paths and environment values, and it does
not read product files nobody selected, so an absent name means "not selected". Treat the full
launch configuration as sensitive. A selected product file must be a regular file (a symlink to
one is fine) under 1 MiB; a FIFO, special or oversized file refuses, and so does a platform
without nonblocking open. The profile registry reader is unbounded.

Registry validation and launch assembly reject the raw flags known to conflict with the managed
controls. Unknown long flags still pass, so this guards known conflicts; it is not a bypass
defense. The tests prove the composition, that sidecar, SDK config and preview agree, that a
resume resolves fresh, and the SDK's strict-MCP serialization. They do not prove native
allow/deny/ask precedence or Claude and Codex parity.

**Before granting a repo's workers a server**, compare what a worker there needs against the
product MCP definitions and profiles it has. A server someone configured for themselves is not a
worker grant. The owner approves the selections, the tool permissions and any authentication;
read a dry-run preview against what was approved. A missing capability needs a reviewed grant or
a smaller task, never inheritance turned back on.

## Outside services

Outside services are vendor MCP servers the owner configures, never code in this repo
([[adrs/0013-integrations-outside-core|ADR-0013]]). A manager server goes in `.mcp.json`;
Claude switches it on per machine in `.claude/settings.local.json`. `doctor` checks MCP
registration. Read tools are allowed; a live-system write asks the owner first. A server only a
worker needs is a `worker_mcp:` profile in `agent.yaml`, handed to one dispatch through the
`dispatch` tool's `mcp` field. The manager never loads it; launch refuses while its
`requires_file` is missing. Ask the owner to run `bin/agent worker-mcp list` for readiness. A
profile's `deny` withholds single tools. Scope each vendor server with its own filter flags to
stay within the manager's tool budget.

## Scope a worker guard by effect

When adding a worker guard, name the effect (a write to a path, a network call, a publish), list
every allowed command that can produce it, and cover each one or record why it stays open. A
guard scoped to the reported command leaves the effect open through the others (`cp` and `mv`
write where a redirect guard does not look). Add each as a row in
`tests/policy_corpus/commands.yaml`.

## Worker read commands

Both clients share `src/agent_cli/dispatch/settings.py` and the read-only fallback in
`src/agent_cli/dispatch/readonly_policy.py`. Workers may run `rg`, `ls`, `cat`, print-only `sed -n`,
`git check-ignore` (including the registered-clone and own-worktree `git -C` forms),
an executable followed only by `--version`, and `agent --help`. Claude's native `Read`
and `Glob` tools are granted in the shared settings. Codex uses shell reads.

Prefix and wildcard matching includes literal newlines inside quoted arguments. This does
not permit command lists: the no-chaining checks still apply, and deny rules still win.
Version queries and `sed -n` use argument checks rather than broad wildcard grants; adding
code arguments or a writing sed script does not qualify for the read-only fallback.

These read grants do not expand durable `writes` or change the redirect, copy, sandbox,
cleanup or scope checks. The read-only fallback still refuses `sed -i`, `rm` and inline
`python -c`. Existing explicit grants remain separate: base rules already allow plain
`rm` and Python invocations, while recursive/forced `rm` stays denied.

## What a Codex manager may run without asking

### Windows MXC trial and rollback

On Windows, setup selects `[windows] sandbox = "mxc"` in the generated project profile.
The existing `network_proxy = true` feature and `agent-manager` permissions still supply
the filesystem and network boundaries. There is no `prefer_mxc` toggle: the trial selects
MXC explicitly. Other operating systems receive no Windows or TUI keys.

The owner can restore the legacy elevated implementation with:

```powershell
.\setup.cmd --codex-sandbox elevated
```

The generated block remembers rollback on later setup runs. `--codex-sandbox mxc` retries
the trial. Both choices keep sandboxing; unelevated and disabled modes are not setup options.
If the owner changed the user key to make the trial work, rollback also requires restoring
that key to `elevated`. Setup never writes the user config.

Doctor's Windows rows show both config layers, the expected winning key, and OS support
from registry `CurrentBuild` plus `UBR`: 24H2 >= 26100.9278 or 25H2 >= 26200.9278.
An unknown build warns. The OS threshold alone does not prove native capabilities or policy
support. Project-over-user precedence is the trial's assumption; user `elevated` against
project `mxc` warns that the project key may not win. Confirm runtime behavior after restart.
If needed, setup prints the user-key change for the owner to make. Launch overrides and
managed policy are outside these two diagnostic layers.

#### Owner checklist on a Windows trial machine

- [ ] Run `.\setup.cmd`, then `bin\agent.cmd doctor` from the harness folder.
- [ ] Record both sandbox keys, the expected effective mode, Windows build and Codex version.
  Restart Codex in the trusted project. Inspect `$CODEX_HOME/.sandbox/sandbox.YYYY-MM-DD.log`
  (default home `~/.codex`). Confirm the selected implementation using available runtime
  diagnostics; an absent MXC log line or legacy setup refresh alone is not proof.
- [ ] If project `mxc` does not win over user `elevated`, set `sandbox = "mxc"` under
  `[windows]` in the user config, restart and recheck before starting the trial day.
- [ ] Run one normal day of Codex manager and worker work. Include repeated launches,
  notes sync, proxy/network operations, loopback services and background children.
- [ ] Count visible UAC prompts separately at first use and afterward (target afterward: 0).
  Count failed sandboxed commands and record each command, failure and workflow impact.
  Setup-refresh log entries are not UAC-dialog counts.
- [ ] Roll back if representative work fails. Rerun doctor after rollback and restart.
- [ ] Try `codex.cmd --no-alt-screen` in Windows Terminal and VS Code. Record whether wheel
  scrolling improves. Only on success, run `.\setup.cmd --codex-inline`; it writes
  `[tui] alternate_screen = "never"` on Windows. Fresh setup leaves it off. Reruns remember
  the choice; `.\setup.cmd --no-codex-inline` removes it. Otherwise capture an upstream repro.

MXC's background-child and loopback behavior needs this live check; see the
[OpenAI Windows sandbox documentation](https://learn.chatgpt.com/docs/windows/windows-sandbox)
and [[harness/provider-gaps]]. This checklist records the owner's observations, not a claim
that fixture tests establish fewer prompts.

### Manager approval review

`agent setup` generates a project `.codex/config.toml` with `approvals_reviewer =
"auto_review"` and the `agent-manager` permission profile. Auto-review handles eligible
approval requests under the same sandbox boundary; it grants no shell commands itself. To opt out
for one launch, start Codex with `-c approvals_reviewer=user`. To keep the opt-out in this clone,
put `approvals_reviewer = "user"` before the first TOML table, outside the generated block, and
remove the generated line; later setup runs keep it. See
[OpenAI's approvals documentation](https://learn.chatgpt.com/docs/agent-approvals-security).

The profile's network proxy allows one domain, `api.github.com`, so `gh` reads run inside the
sandbox. `github.com` is not listed because this vault's remote uses SSH; add it only if a
git-over-HTTPS denial appears.

`.codex/rules/routine-checks.rules` grants four command prefixes:

```python
prefix_rule(pattern=["uv", "run", "pytest"], decision="allow")
prefix_rule(pattern=["uv", "run", "ruff"], decision="allow")
prefix_rule(pattern=["uv", "run", "pyright"], decision="allow")
prefix_rule(pattern=["git", "add"], decision="allow")
```

The owner chose to allow `git add` across repositories while keeping `git commit` under review.
The rule has no cwd or path condition, so `git add -A` matches too. Nothing else is granted: no
general `uv`, Python, shell, dependency install or publish. `allow` means the command runs
**outside the sandbox**: pytest runs project code and plugins, and ruff formatting edits files.
Project scoping decides when a rule loads, not which paths a loaded rule accepts. The `cli-route`
hook refuses a model's `bin/agent` call only while that hook is trusted; remove any user-layer
`bin/agent` allowance left from an older policy.

Rules under `.codex/rules/` load only when the project layer is trusted, and Codex reads them at
startup: restart after a change. A more restrictive rule, including a managed requirement, wins
([OpenAI's rules documentation](https://learn.chatgpt.com/docs/agent-configuration/rules)).
`codex execpolicy check` tests a file without running the command:

```sh
codex execpolicy check --pretty --rules .codex/rules/routine-checks.rules -- uv run pytest tests/test_documents.py -q
```

A file check proves syntax and matching, not that the host loaded it; run the command in a
fresh session to see whether it prompts. If prompts come back, read the active policy and what
loaded. Do not widen a grant or move a cache to make a prompt go away.
