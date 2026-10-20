---
id: 14
title: Workers load only the product repo MCP servers the registry lists
status: accepted
date: 2026-09-16
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0014: Workers load only the product repo MCP servers the registry lists

## Context

A worker runs in a product repo's worktree, and `strict_mcp_config` was off so the repo's own harness loads. That included every server in the repo's `.mcp.json`, written for humans who confirm each action. A repo's servers can be configured more loosely than a headless worker should get, and nothing in the registry could say no.

## Decision

A repo's `dispatch.repo_mcp` in `repos.yaml` lists the servers from its own `.mcp.json` a worker may load. When the key is set, even to `[]`, the worker runs with strict MCP config: the harness's `agent` server, the dispatch's `worker_mcp` profiles, and exactly the listed repo servers. When the key is absent, behaviour is unchanged and every repo server loads.

## Consequences

- The dry-run preview shows `repo mcp servers` (kept) and `repo mcp dropped`, so the owner sees the cut before launch.
- A listed name the file does not have is skipped silently. The preview is where that shows.
- A listed name that collides with `agent` or a profile's server refuses the dispatch.
- The kept servers' config is read from the worktree at launch, and from the clone on a dry run. A repo that changes its `.mcp.json` changes what the worker gets, but only for servers already listed.
- The same change added `deny` to `worker_mcp` profiles. A profile can grant a server whole and withhold single tools, such as a browser's `browser_evaluate`.
- Core stays neutral. Which servers a repo allows is the owner's registry, not code.
- Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: an absent, null or empty `repo_mcp` now selects no product servers.

## Alternatives considered

- Strict for every worker. Rejected: it would silently break repos whose workflow depends on their own servers.
- A deny list of repo servers. Rejected: a server the repo adds later would load by default, which is the failure this closes.
- Fixing each product repo's `.mcp.json`. Still worth doing, but that config serves the repo's humans, and the harness should not depend on a file it does not own for its workers' exposure.
