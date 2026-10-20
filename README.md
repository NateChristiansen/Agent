# Agent

Agent is a harness for managing coding work through an agent and an Obsidian vault. You describe what you want; the manager tracks it as a feature, plans the work with you, and sends code changes to workers in their own git worktrees. It brings back the results for review and keeps the plans, decisions, and history alongside the conversation.

This repo holds the harness. Setup creates a separate notes repo for your configuration and work records, and connects it to the vault. Your product repos can stay wherever you already keep them.

## Quick start

Install the [requirements](docs/01-getting-started.md#what-you-need), then clone the harness and run setup:

```sh
git clone https://github.com/NateChristiansen/Agent.git
cd Agent
./setup.sh
```

Setup offers to install missing tools, asks where to keep your notes and what to call you, and configures the vault. On Windows, run `setup.cmd` instead, from PowerShell, cmd or Explorer. Run from Git Bash, `./setup.sh` skips its questions.

Open the `Agent` folder as a vault in Obsidian, then use your agent's manager launcher on the ribbon to open a conversation. Ask it to add your repo: “Add my repo at ~/code/api.” Once it is configured, describe your first piece of work: “Fix the flaky login test.” The manager asks for what it needs and proposes a plan for your approval.

[Getting started](docs/01-getting-started.md) walks through installation and your first conversation.

## Manual

[Architecture](docs/architecture.md) maps the code, work-order lifecycle, state stores and checks.

1. [Getting started](docs/01-getting-started.md): install, open the vault, add a repo, and start work.
2. [Your first feature](docs/02-your-first-feature.md): take one task from request to a worker's finished branch.
3. [Sending work to workers](docs/03-sending-work-to-workers.md): work orders, models and budgets, watching a worker and answering it.
4. [Reviewing and shipping](docs/04-reviewing-and-shipping.md): check a worker's result, ask for fixes and open the pull request.
5. [Day to day](docs/05-day-to-day.md): status, the journal, spend, lessons and closing features.
6. [Running several sessions](docs/06-running-several-sessions.md): parallel managers and messages between them.
7. [Working in Obsidian](docs/07-working-in-obsidian.md): the dashboard, feature views and terminal tabs.
8. [Your notes repo](docs/08-your-notes-repo.md): what it holds and how to sync it.
9. [Configuration](docs/09-configuration.md): repos, worker permissions, checks, MCP servers and hooks.
10. [Choosing and using agent clients](docs/10-agent-clients.md): client setup and differences when running managers and workers.
11. [Pairing two machines](docs/11-pairing-two-machines.md): share your notes and let sessions on different machines message each other.
12. [Windows](docs/12-windows.md): setup and daily use on Windows.
13. [Safety](docs/13-safety.md): worker permissions, checks, and your role in approving changes.
14. [Troubleshooting](docs/14-troubleshooting.md): health checks, setup failures, stuck workers and slow hooks.

## License

See [LICENSE](LICENSE) and, for third-party material, [NOTICE](NOTICE). Security reports: [SECURITY.md](SECURITY.md).
