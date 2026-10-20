---
id: 26
title: Git hooks gate commits on staged content and pushes from the source checkout
status: accepted
date: 2026-09-24
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0026: Git hooks gate commits on staged content and pushes from the source checkout

## Context

Commits could land unformatted code, or a file under the size cap that formatting took over it. Every check involved already existed, but each ran after the commit: format in CI, the size cap as a pytest test that only fires on a full run. This checkout's `origin` is also the public repo while its history is private, so a plain `git push` would publish that history.

## Decision

This repo installs two git hooks, and `setup` installs them while `doctor` fails without them. `pre-commit` reads each staged Python blob under `src/` and `tests/`, never the working tree. It fails on unformatted code, on more than 500 lines after formatting, and on lint errors. It also fails when a staged instruction file takes the total over the Codex byte budget. It never writes the index or the working tree, because several managers share this checkout. `pre-push` refuses every push from a checkout with local config `agent.exportOnly=true`. `agent export <dir> --remote <new-repo-url> -m <message>` is the one public route; it refuses a `--remote` that names the source repository. It squashes `HEAD` into a fresh repo, restores gitlinks, and scrubs files and commit metadata. It checks the private set is absent, then prints the push command and stops. `commit-guard` denies `--no-verify`, `commit -n` and `core.hooksPath` overrides in this checkout.

## Consequences

A format or size failure now stops the commit that causes it. The limits live in `gitgate.py`, and `tests/test_file_sizes.py` imports them. Local hooks remain bypassable by plumbing or an edited hook, so CI stays the backstop. A public clone gets the same hooks, and its pre-push does nothing. `doctor` fails when a notes repo is linked and `agent.exportOnly` is unset, so the checkout most likely to hold private history must declare itself. The hook runs the worktree's own `.venv` and falls back to the main checkout's, since the hooks dir is shared and `.venv` is not. That fallback can run older gate code against a newer worktree branch; it still reads that worktree's staged blobs, and CI backs it. An owner who opted in to the `scrub hook` pre-push sees ours reported as foreign; the export's scrub replaces that hook's job for this repo.
Amended by ADR-0041: where the registry lists the checkout itself, pre-push also requires a full pass record for each pushed tip; a public clone's pre-push still does nothing.
Amended by [[adrs/0052-no-python-line-cap|ADR-0052]]: no 500-line cap; doctor no longer checks the git hooks or `agent.exportOnly`.
Amended by [[adrs/0060-public-clones-push-directly|ADR-0060]]: after the public start, public clones push directly; a registered one gets the pass-record and scrub gates.
Amended by [[adrs/0061-remove-scrub-check|ADR-0061]]: the scrub is removed from export and pre-push, and the standalone scrub hook is gone.

## Alternatives considered

A hook that ran `ruff format` and re-staged was rejected: it would stage other sessions' hunks. The `pre-commit` framework was rejected as a new dependency for four checks. Leaving pyright and pytest to CI was deliberate, since both are too slow per commit and depend on unstaged state.
