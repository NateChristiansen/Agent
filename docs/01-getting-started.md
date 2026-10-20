# Getting started

You work from an Obsidian vault, with a manager conversation in a terminal tab beside your plans and records. This chapter takes you from a fresh clone to a registered repo and your first request.

## What you need

Install these before setup:

- [Git](https://git-scm.com/downloads), to clone the harness.
- An agent client: [Claude Code](https://code.claude.com/docs/en/overview) or [Codex](https://developers.openai.com/codex/cli). Choose either, or install both.
- [Obsidian](https://obsidian.md/download), with **Settings → General → Command line interface** enabled.

Setup offers to install [uv](https://docs.astral.sh/uv/), [Node.js](https://nodejs.org/en/download), and [jq](https://jqlang.github.io/jq/download/) if they are missing, and uv supplies the harness's Python environment. If setup cannot install a tool on your system, its report gives the install command to use before running setup again.

These tools are optional:

- [gh](https://cli.github.com), for GitHub pull requests and filing harness lessons as upstream issues. Without it, lessons can still be recorded locally.
- [glab](https://docs.gitlab.com/cli/), for GitLab merge requests.

Setup offers to install gh where supported; install glab yourself if you use GitLab.

## Install the harness

```sh
git clone https://github.com/NateChristiansen/Agent.git
cd Agent
./setup.sh
```

Setup checks your tools and asks once before installing anything missing that it can install. On Windows, run setup from PowerShell or cmd instead:

```bat
setup.cmd
```

In PowerShell, type `.\setup.cmd`. You can also double-click `setup.cmd` in Explorer. Run from Git Bash, `./setup.sh` skips its questions and fills in placeholders.

Setup asks where to keep your notes repo and what name to use for you. Press Enter at the notes prompt to create `../Agent-notes`, or give another path. This repo holds your configuration, feature plans, decisions, and journal; setup connects those records to the harness vault. Managers run in the main harness folder, and your product repos can live anywhere outside it. The default location for new product clones is `../repos/`.

Read the end of the setup report. It says **Setup is complete.** or **Setup is incomplete.**, with any remaining steps. Complete those steps before starting work. You can rerun setup after installing a missing tool or updating the harness.

## Open the vault

In Obsidian, choose **Open folder as vault** and select the `Agent` folder. Setup installs and configures the vault's plugins, including its dashboard, feature views, and terminal tabs. Enable community plugins if Obsidian asks.

Use the manager launcher for your installed agent on the left ribbon. Hover over the icons to find the one labelled **New … manager**. It opens a pinned terminal tab in the harness folder. Complete any project approval prompts there, and follow the setup report's instructions for trusting project hooks.

The conversation and your records now share the same window. You can open a plan or log beside the manager as you work.

## Add your first repo

Tell the manager which repo you want to use:

> Add my repo at ~/code/api.

You can give a remote URL instead if you want a new clone. The manager inspects the repo's default branch, checks, and existing agent setup, then asks you about any settings it cannot determine. You choose the clone location, worker permissions, and any unresolved settings before it writes the configuration.

The manager validates the configuration and reports any prerequisites still needed for workers. Ask it to complete those before starting your first code change.

## Start your first piece of work

Describe the outcome you want in the same conversation:

> Fix the flaky login test. It fails intermittently in CI after a password reset.

The manager clarifies the request, creates a feature to track it, and proposes a plan. Once you approve the plan and the worker launch, it sends the work to a worker and brings the result back for review.

The next chapter follows [your first feature](02-your-first-feature.md) through that process.
