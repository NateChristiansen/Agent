---
id: 20
title: Worker MCP servers require explicit grants
status: accepted
date: 2026-09-19
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0020: Worker MCP servers require explicit grants

## Context

Workers currently inherit product MCP servers when no selection is configured.
That makes a human's repository configuration an unattended worker grant.
The owner chose opt-in product MCP; concrete server, tool and authentication grants remain separate decisions.

## Decision

Worker MCP servers require explicit selection; user and project configurations never implicitly add servers.
The launch combines the restricted harness `agent` server, explicitly requested worker profiles,
and product servers named by `dispatch.repo_mcp`. Absent or empty product selection adds none.
Configuration supplies a selected server's definition, not permission to load other servers or call its tools.
Unknown selected names, unreadable selected definitions and conflicting server names refuse launch.
The harness name is reserved; profiles and product definitions cannot override it or each other.
Explicit tool denies take precedence over broader tool grants; unmatched use remains unapproved.
Record each effective selection and tool decision with its source and controlling deny, without secret values.
Apply the composed list through a verified provider control that excludes inherited servers.
An unavailable or unverified required control blocks claimed protection; do not claim equivalent enforcement.

## Consequences

Existing repositories may need reviewed migration grants before their workers can use product MCP.
Keep target instructions and skills available without treating them as authority to add MCP servers.
Server loading, tool permission, authentication, filesystem access and network reach remain distinct.
Child publication remains prohibited; MCP selection is not proof of containment or account-scope restriction.
This decision does not grant credentials, narrow live manager permissions, or establish Claude/Codex parity.
Fixture implementation can proceed; live rollout and provider enforcement still require owner decisions and probes.

## Alternatives considered

Implicit inheritance preserves convenience but hides grants behind mutable human configuration.
Name-based override precedence makes collisions silently change authority; refuse collisions instead.
Disabling all target configuration would also discard instructions and skills that workers need.
