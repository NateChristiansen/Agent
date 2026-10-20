# Troubleshooting

Bring the error or symptom to the manager, with the machine and client where it happened. It checks the recorded state, explains what failed, and proposes recovery.

## Check the harness's health

Ask in the manager conversation:

> Run the health checks and explain the failures before changing anything.

The manager runs doctor to check configuration, notes, tools, and recorded run state. It explains the failures and checks recovery against the symptom you reported.

If the manager tools are unavailable, run this yourself from a terminal in the harness folder:

```sh
bin/agent doctor
```

On Windows from PowerShell or cmd:

```powershell
bin\agent.cmd doctor
```

## Finish an incomplete setup

Read the end of the setup report first. Install anything it names as missing, complete its Obsidian and client approvals, then rerun:

```sh
./setup.sh
```

On Windows, rerun:

```powershell
.\setup.cmd
```

To check requirements without configuring anything:

```sh
./setup.sh --check
```

The check reports a missing uv or environment; normal setup creates them. If a plugin download failed, rerun with network access and check the plugin step in the report.

If setup finds a file where a notes link should go, it preserves it and reports the conflict. The manager can help compare the copies.

## Restore manager tools or hooks

If the manager cannot reach the harness tools, it checks MCP registration where possible. You can rerun setup yourself, then start a new manager or reconnect the server after correcting registration.

A notice tells the manager when changed tool definitions need a reconnect. Wait for any long-running tool call to finish first, so its result is recorded.

If your agent's hooks stopped after an update, follow the hook-trust steps in the setup report. The manager can then check whether session context is restored.

## Find out why a worker stopped

The manager reads the worker state, handoff, and supervisor output, then reports what stopped the run and what it needs from you. That may be an answer, a budget decision, or approval for different access.

> Show why the worker stopped, what it committed, and the smallest next step that can finish the task.

If the worker asked a question, it waits for your answer. The manager passes your reply to the connected session, or resumes the worker on its existing branch and worktree if that session has ended. Failed supervisor checks go back to the implementer, and the manager brings back the next result.

A finished worker may still be waiting for verification. The manager checks whether that work is progressing before proposing cancellation or cleanup.

## Repair a stale vault view

For an unknown or stale live pane, check that Obsidian's command line interface and the Agent Terminal plugin are enabled. The manager can inspect recorded render and tab-paint failures.

The manager can refresh status to rebuild generated views. After restarting Obsidian, open managers from the ribbon and name the work to continue.

If startup is slow, check that Show Hidden Files is off. It indexes large dot-folder caches when enabled; Agent Hidden Files exposes the selected harness folders without that full scan.

## Resolve notes or pairing failures

Offline notes publication keeps local commits. The manager inspects the latest error and resolves conflicts when you ask. [Your notes repo](08-your-notes-repo.md#handle-a-sync-failure) covers sync modes and failures.

For pairing, read both route results and the acceptance message. Check SSH reachability, receiving being enabled, the confirmed host key, and the remote harness path. An interrupted enrollment may need recovery; ask the manager to explain the pending operation before choosing a recovery action.

If the report names the SSH shell, pairing requires cmd.exe on Windows or sh, bash, or zsh on macOS. For a fingerprint mismatch, compare with the fingerprint printed on the other machine before confirming.

## Investigate slow hooks

Hook errors and timings are recorded. Tell the manager which operation pauses; it can use those records to measure the delay. Notes sync can also wait on network git operations.

Some process hooks can be disabled in configuration, but core safety hooks cannot. The manager can propose a setting change once it has identified the delay.

## Check the Windows sandbox

If you use Codex on Windows, setup selects the MXC sandbox in the project's `.codex/config.toml`. Doctor checks for Windows 11 24H2 build 26100.9278+ or 25H2 build 26200.9278+; device capabilities and policy also need to permit MXC. See the [Windows sandbox documentation](https://learn.chatgpt.com/docs/windows/windows-sandbox).

Run this from the harness folder after setup:

```powershell
bin\agent.cmd doctor
```

Doctor shows the user and project sandbox settings and their expected effective value. Restart your agent in the trusted project and check its sandbox logs: the expected project-over-user precedence still needs confirmation in a live session. Launch overrides and managed policy can also affect the runtime.

If doctor warns that the user setting is `elevated` while the project selects `mxc`, check which wins in that session. If the project key does not win, set `sandbox = "mxc"` under `[windows]` in your user config, then restart. That file is under `$CODEX_HOME` when set, otherwise `~/.codex/config.toml`. Setup edits the project config, preserving its network proxy and manager permission profile.

To return to the legacy elevated sandbox:

```powershell
.\setup.cmd --codex-sandbox elevated
```

Later setup runs keep that choice. If you changed the user key too, restore it to `elevated`, then restart and check doctor. To retry MXC:

```powershell
.\setup.cmd --codex-sandbox mxc
```

## Try inline mode for slow Windows scrolling

For slow wheel scrolling in Codex, try this once in Windows Terminal and once in VS Code:

```powershell
codex.cmd --no-alt-screen
```

If it helps, save the choice from the harness folder:

```powershell
.\setup.cmd --codex-inline
```

Setup sets `[tui] alternate_screen = "never"` and keeps the choice on later runs. A fresh setup leaves inline mode off. To remove the generated setting:

```powershell
.\setup.cmd --no-codex-inline
```

Setup reports conflicting project tables you maintain yourself and preserves them. If inline mode does not help, keep the default and capture a reproduction for upstream.
