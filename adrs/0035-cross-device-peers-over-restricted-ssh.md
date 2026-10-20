---
id: 35
title: Cross-device peer messages travel over a restricted SSH hop and wake locally
status: accepted
date: 2026-10-03
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0035: Cross-device peer messages travel over a restricted SSH hop and wake locally

## Context

`peer send` stops at the machine, because its queue, pids and inbox pipes are local. Owners
want Claude and Codex managers on different machines, for example a Mac and a Windows machine,
to message each other without a vendor relay that carries one client only. A probe used
key-auth SSH as the same Windows
admin user, with Claude Code 2.1.288 and codex-cli 0.160.0. It woke an idle Codex (`codex
queue`) and an idle Claude (its `\\.\pipe\LOCAL\cc-msg-*` inbox). From that logon, Windows
refused uv's minor-version Python junction ("untrusted mount point"), so the venv launcher
failed. Other accounts and session layouts are untested.

## Decision

A cross-device message is a small versioned envelope sent over SSH to a forced receiver
command; the target device's own `peer` code stores it and wakes the recipient.

- Opt-in per machine, off by default. Person-only enroll and revoke write `agent.local.yaml`
  and keep existing key entries and ACLs. Enroll installs a dedicated key as
  `restrict,command="<receiver>"` and pins the host key after the owner confirms it.
- That key must deny a shell, a PTY, port and agent forwarding, and other commands; enrollment
  and the live test verify it.
- The receiver reads stdin only, with verbs `enqueue`, `list` and `status`. It refuses
  malformed, oversized, expired, rate-exceeded or unknown input. A repeated id returns
  `already-accepted` with no second enqueue or wake. Wakes coalesce. An off switch keeps the queue.
- Sender identity comes from the key's forced-command config, never the envelope. Messages keep
  remote provenance; replies go only to the origin device. Peer text stays below the owner.
- Enqueued, injected into context, and replied are distinct states. Discovery exports device,
  client, name, feature and idle state only; an offline device is unknown. Addresses are
  device-qualified (`windows/cx1234`); an ambiguous short name is refused.
- ADR-0033 holds: models call the typed `peer` tool, and its handler runs the fixed SSH call.
- The receiver starts Python without uv's junction; enroll or doctor checks it from an SSH logon.

## Consequences

The tested setup needs no Windows user-session helper. Each enrolled device runs an SSH server.
The tested Windows OpenSSH negotiated no post-quantum key exchange; other builds are unchecked.
If the key restrictions hold, a stolen key can still steer a model with text and read discovery,
within the rate limits. Malware running as the owner can already rewrite the queue. A VPN or
mesh is an optional route beyond the LAN, never required. Receiver acceptance is committed in a
local SQLite store. Inbox queueing is retry-driven within the expiry window and may duplicate
after an interrupted attempt. A wake request may repeat after an interruption or claim expiry.
Acceptance does not guarantee context injection, read or reply.

Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: doctor has no peer-net row; enroll resolves the receiver's interpreter.

## Amendment 2026-10-13

The pairing code differs from the text above in six places. The decision stands;
read the sections above with these corrections.

1. Enrollment state lives in the CLI-owned store `.agent/peer-net/state.db`, not
   `agent.local.yaml` (`src/agent_cli/peer_net/config.py`, `store.py`). A `peer_net:` block left
   in `agent.local.yaml` makes the receiver refuse.
2. Nothing live-tests that the key denies a shell, a terminal or forwarding. The code compares
   the `authorized_keys` line's options text exactly with what the managed line writes
   (`src/agent_cli/peer_net/grants.py`, `authorized.py`), so an extra option is caught and the
   behaviour of `restrict` itself is trusted.
3. The rate limit covers only new enqueues: 30 per sending device per 600 s
   (`src/agent_cli/peer_net/store.py`, `receiver.py`). `list` and `status` are not limited.
4. The ADR does not describe `peer-net pair`, `accept`, `setup-server` or the one-time password
   bootstrap, where ssh asks for the owner's password on its own terminal prompt and the harness
   never reads it (`src/agent_cli/cli/peer_net.py`, `peer_net/bootstrap.py`,
   `peer_net/server_setup.py`, `peer_net/pairing.py`). [[docs/11-pairing-two-machines|docs/11-pairing-two-machines.md]]
   is the walk-through.
5. Discovery rows carry device, client (`provider`), name, tab, feature and idle state, not
   just the five the Decision lists (`src/agent_cli/peer_net/ack.py`, `receiver.py`).
6. The interpreter check is local: `launcher.resolve` runs when the key line is written and
   refuses a linked or mismatched interpreter (`src/agent_cli/peer_net/launcher.py`). The status
   probe over SSH is what proves the receiver starts from an SSH logon
   (`src/agent_cli/peer_net/routes.py`, `enroll.py`). [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]
   already amends the "enroll or doctor checks it" line; this does not change it.

## Alternatives considered

Provider Remote Control: the checked docs set out no cross-client messaging API the harness
could call. A peer HTTP service needs its own auth and a daemon. A git mailbox has no wake and
keeps permanent history. A synced `.agent/` never wakes an idle peer. A hosted relay adds a
trust party and the most build.
