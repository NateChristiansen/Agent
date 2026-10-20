---
id: 30
title: agent.yaml replaces repos.yaml, with a per-machine agent.local.yaml
status: accepted
date: 2026-09-26
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0030: agent.yaml replaces repos.yaml, with a per-machine agent.local.yaml

## Context

`repos.yaml` began as a list of repos. It came to hold the owner, the forge, eval
settings and worker MCP profiles as well. The owner asked to rename it because it had become
the harness's config file. They also asked for absolute repo paths, since repo paths
already differ from machine to machine. The file lives in the notes repo, which can be shared by
machines whose folders differ.

## Decision

The config is `agent.yaml`, with `agent.example.yaml` as the tracked template. A per-machine,
gitignored `agent.local.yaml` in the clone root overrides it.
- `agent.yaml` stays in the notes repo and holds what every machine shares. Its paths stay
  relative.
- `agent.local.yaml` is never linked. It sets `path` and `worktrees_dir` per repo, may be
  absolute or use `~`, and fails on anything else.
- Transition rules:
  - Only `repos.yaml` present: it is read, and doctor warns.
  - Both present: `agent.yaml` is read, `repos.yaml` is ignored, and they are never merged.
  - An invalid `agent.yaml` fails and never falls back.
  - `agent notes migrate-config` does the `git mv` in the notes repo and relinks the clone.
    It refuses when both names exist there.
  - The fallback is removed in the first release after 2026-10-31.
- A repo may also set its own `forge:`, which overrides the top-level value.

## Consequences

- Readers go through `config_file.registry_path`. `repos.yaml` stays gitignored until the
  fallback is removed.
- An unmigrated notes repo keeps `repos.yaml` linked under its old name, so the example is never
  copied in beside the real file.
- Keys are documented in `harness/agent-yaml.md`, and a test fails when a key the code reads is
  missing there.
- The skill that edits the file is `agent-config`: `/agent-config` in Claude Code, and
  `$agent-config` in Codex.
- Amended by [[adrs/0058-harness-state-after-audit-5|ADR-0058]]: the `repos.yaml` reader and `notes migrate-config` are deleted; the name stays private.

## Alternatives considered

- Absolute paths in `agent.yaml`: this breaks a notes repo shared by several machines.
- A fully per-machine config: this loses version history, and every machine repeats the setup.
- Names: `config.yaml` blurs with other tools' config files, and `harness.yaml` is less
  obvious. `agent.yaml` matches `bin/agent`.
