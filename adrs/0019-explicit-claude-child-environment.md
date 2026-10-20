---
id: 19
title: Launch Claude children with an explicit environment
status: accepted
date: 2026-09-18
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0019: Launch Claude children with an explicit environment

## Context

The SDK 0.2.152 main launch merges ambient environment variables, and its version probe inherits them. The harness allowlist therefore did not isolate either subprocess. Removing variables from the supervisor would interrupt notifications and peer launches.

## Decision

Use a small POSIX launch adapter with explicit child environments and dedicated process groups. Keep the SDK host in the trusted supervisor. Reuse SDK framing, command construction, control routing, and normal close; own the version/main spawn calls and final group cleanup.

The adapter audits and pins its compatibility guard to installed `claude-agent-sdk` 0.2.152 and checks required private interfaces. Unsupported versions fail with an actionable error and no ambient fallback. Adapted launch/stream setup retains the upstream Anthropic MIT notice in its source. This does not select a repository license.

Apply the SDK's permission-callback options copy before custom transport construction. Preserve canonical harness ownership and SDK tags, reject explicit manager session identity, and preview effective environment names without values.

## Consequences

Version probing has a two-second bound and rejects unrecognized or pre-2.0.0 output; upstream only warned about old versions. Probe cleanup and final process-group reaping are bounded. Unconfirmed cleanup prevents successful acceptance, including budget/turn-cap done returns; cancellation remains cancelled. Synthetic real-process tests cover watch/detach, initialization failure, cancellation and descendants, concurrent supervisor notifications, and callback denial routing.

The trusted Python SDK host retains ambient credentials. This isolates inherited CLI environments, not host access or filesystem credentials. POSIX support, an absolute resolved CLI path, and no session-store materialization are explicit limits. Authentication rollout remains owner-gated; no authentication grants or private registry settings are changed. No live authenticated dispatch has been run.

## Alternatives considered

Shared environment mutation races with notifications and peer launches. A child wrapper that purges inherited secrets starts outside the required boundary. Copying the whole SDK transport expands maintenance risk. A provider-neutral transport contract remains a later design task.
