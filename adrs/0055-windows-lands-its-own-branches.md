---
id: 55
title: Windows lands its own branches
status: accepted
date: 2026-10-12
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0055: Windows lands its own branches

## Context

[[adrs/0041-tree-keyed-pass-records|ADR-0041]] gates every main move and publication on a
tree-keyed pass record. The record's per-key lock imported `fcntl` bare, and `agent land` refused
any non-POSIX checkout. No ADR recorded that limit. Windows could not make a pass record or land,
so every Windows branch landed through a POSIX machine. Most cross-machine manager messages
existed only because one side could not act, many of them review and land requests, and Windows
pushes ran by hand with `--no-verify`.

## Decision

A Windows checkout makes its own pass records and lands its own branches. On Windows the record's
key lock takes `win_lock`'s byte-range lock on a file in the verify store, creating the store
first; only the acquisition has a timeout, and errors from the locked body pass through.
`agent land` no longer refuses a non-POSIX checkout.

## Consequences

- The verifier, lanes and land journal already had Windows paths; this removes the last block.
- The change is not yet proven natively. Acceptance is a native Windows land.
  Until it passes, a Windows lead still sends a POSIX machine one land request per branch
  (`harness/sessions.md`).
- The consumer refusal ([[adrs/0043-consumer-installs|ADR-0043]]) applies on Windows as anywhere.

## Amendment 2026-10-14

The text above said nothing else blocked a Windows land. Three things did, found running the land
tests natively:

- Land refused `core.fileMode=false` and `core.symlinks=false`, which Git for Windows writes into
  every checkout. Land now reads them as git does: the mode comes from the tree and only content
  is observed. A changed file whose old and new entries differ only in the hidden mode (an
  executable bit, or a link against a file with the same bytes) is still refused, by name.
- Land's git ran with `GIT_CONFIG_NOSYSTEM`, so it missed Git for Windows' system
  `core.autocrlf=true` and read CRLF working files as edits. It still reads no system config. On
  Windows `core.autocrlf` comes from the repo's own config (local or worktree), whatever its
  value, including empty; otherwise land passes `core.autocrlf=true`, Git for Windows' system
  default. A global setting never decides it. `core.autocrlf` now refuses only a
  changed text file with no `eol` attribute. This repo's `* text=auto eol=lf` covers every file.
- The land journal fsynced its directory with `os.open`, which Windows refuses. On Windows the
  directory is not flushed; NTFS metadata journaling is all that covers the rename, which is
  weaker than an fsync.

## Alternatives considered

- Keeping landing delegation to a POSIX machine as the design: it doubles every Windows landing
  into a relay and needs that machine's manager awake.
