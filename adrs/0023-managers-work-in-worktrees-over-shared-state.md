---
id: 23
title: Managers work in their own worktrees over one shared coordination state
status: accepted
date: 2026-09-23
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0023: Managers work in their own worktrees over one shared coordination state

## Context

Several manager sessions, Claude and Codex, can run at once, and until this decision they all
edited the same checkout. Leases arbitrated that well: `vault:src` could pass between sessions
without the owner brokering, and `lease wait --ask` meant the request went to the holder
instead of to the owner.

What leases cannot do is isolate breakage. A lease serializes *edits*; it says nothing about the
state of the tree between two of them. One session left a `SyntaxError` in a CLI module
mid-edit, and for every session on the machine at once:

- `bin/agent` stopped importing, so every hook in both clients failed open — no
  `protect-generated`, no `no-chaining`, no `push-guard`, no lease guard;
- `doctor`, `scrub check` and `status render` died;
- the peer channel went down, because it runs through the CLI being edited.

The last one is the reason this is an ADR rather than a preference. The coordination mechanism
depends on the artifact being coordinated on, so the failure removes the means of reporting
itself. The message telling that session what it had broken had to go out through
`codex queue` — a client-native channel — because `bin/agent peer send` was part of the
casualty. The blast radius of one transient typo was every session and every guard.

A worktree gives each session its own `src/`, `bin/agent` and `.venv`, so that edit would have
been visible only to the session making it.

The catch is that a naive worktree makes things worse. `.agent/` is gitignored, so
`git worktree add` hands each session an empty one — and sessions, leases, peers, dispatch
records and `live.json` all live there. Left alone, `peer list` shows one session instead of
all of them, leases arbitrate nothing, and two dispatches can take the same id. That trade would
buy breakage isolation by giving up every coordination mechanism.

## Decision

**Code is per worktree. Coordination state is shared.**

A manager worktree is a sibling of the main checkout, `../<main>-wt-<name>`, on branch
`agent/<name>`. Its `.agent` is a **symlink** to the main checkout's. `bin/agent` exports
`AGENT_ROOT` from its own location, so a worktree's CLI runs that worktree's code against the
shared state with nothing to configure.

A sibling rather than a child because every `CLAUDE.md` and `AGENTS.md` from cwd up to root
loads into a session, so a checkout nested inside the vault would inherit the manager context
twice — the same rule that keeps product repos in `../repos/`.

`bin/agent worktree new <name>` does the whole setup in one command: the worktree, the `.agent`
symlink, the private set relinked through the existing notes machinery, `uv sync`, and a
`status render` for the generated files. One command because the steps are individually
forgettable and one of them fails silently.

**`doctor` fails — not warns — when a worktree's `.agent` is a real directory.** That is the
whole reason the check exists. A worktree with its own `.agent` works perfectly and coordinates
with nobody; nothing raises, `peer list` simply shows one session. A failure that produces no
error is the one worth spending a check on.

**Leases stay as they are.** Worktrees stop a broken edit from reaching another session, but two
sessions editing the same module on different branches still collide at merge. The lease moves
that collision to edit time, where it is cheap and a human is present.

## Consequences

**A worktree runs committed code.** Everything uncommitted in another checkout is invisible to
it. That is the isolation working, and it changes how sessions hand work over: reading each
other's in-progress files stops being possible, so a handoff is a commit on a branch, or a
description through the peer channel.

**Generated files drift.** `STATUS.md`, `Dashboard.md`, `Index.md` and `bases/` are gitignored
plain files in every checkout (ADR-0016), but `features/` is a shared symlink into the notes
repo. So any session editing a plan makes every *other* worktree's `STATUS.md` stale, and
`doctor` says so. In practice only one checkout is the Obsidian vault the owner looks at, so
this is cosmetic there and noise elsewhere.

**`.venv` per worktree.** Disk cost, and `uv sync` on creation. Worth it: a worktree on another
branch may not want the main checkout's dependency set.

**Shared state is still shared.** Worktrees isolate code, not `features/`, `journal/` or
`.agent/`. A half-written plan frontmatter, such as a feature's `adrs:` naming an ADR that does
not exist yet, still fails `doctor` everywhere. Worktrees narrow
the blast radius; they do not remove it.

The worktree location is amended by ADR-0037: worktrees now live under `../Agent-worktrees/`.

Amended by [[adrs/0058-harness-state-after-audit-5|ADR-0058]]: worktrees now also stand in for the retired syntax-guard, and doctor no longer checks the `.agent` link.

Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: doctor no longer flags a stale `STATUS.md`.

## Alternatives considered

**Keep one checkout and rely on leases.** What we had. Rejected on evidence rather than theory:
the failure above cost every session's guards at once, and the owner had to be told because the
channel for telling anyone else was down.

**Per-worktree `.agent`, with the main checkout authoritative.** Cleaner separation on paper.
Rejected because `peer list`, the leases and the Managers view would only ever see
same-worktree sessions, so the managers become invisible to each other and every conflict
returns to the owner to broker.

**Symlink only some of `.agent`,** keeping `scratch/` local. Marginally tidier, since scratch is
throwaway drafts. Rejected as a partial symlink set that both `setup` and `doctor` have to know
about and keep correct, for no benefit that anyone would notice.

**Document the manual steps instead of a command.** Five steps, all of which must be right.
Rejected because a missed `.agent` symlink fails silently, which is the worst property a manual
step can have.
