---
id: 21
title: Lessons live where they load; the playbook and the improvements log go
status: superseded
date: 2026-09-22
supersedes: null
superseded_by: 24
tags:
- adr
links:
- '[[adrs/0024-findings-are-tracked-until-they-earn-a-home|ADR-0024]]'
---

# ADR-0021: Lessons live where they load; the playbook and the improvements log go

## Context

The harness records what it learns in two append-only files, `harness/playbook.md` and
`harness/improvements.md`. Neither is loaded into a session.

`CLAUDE.md` imports four files: `AGENTS.md`, `harness/rules.md`, `harness/owner/rules.md` and
`harness/voice.md`. The playbook and the improvements log are not among them. A `git grep` for
what references `improvements.md` returns `hooks/stop.py`, `/lesson`, `/retro`, the
`retro-reader` agent, `.claude/rules/harness.md` and `AGENTS.md` — every one of them an
instruction to *write* to it. Nothing reads it. It is a write-only file, and `git log` already
carries the same content attached to the diff that caused it.

The playbook's own `how:` field says where its entries belong: a test, a CLI validation or a
hook; a skill; a context line; or a procedure. About half the entries name a home that is not
the playbook. For those the entry is a second copy of something that already exists where it
is read: a guard test's docstring tells the same story as its playbook entry, and only the
docstring is read.

The `where:` field is `manager` on nearly every entry. It costs a line each and carries no
information.

Prose can be useful, but not as uncategorised lines that bloat context and are never touched
again; and where an enforcement mechanism exists, the lesson does not need recording
separately at all.

## Decision

**A lesson lives where it loads, or it does not exist.**

Four homes, chosen by what holds the lesson:

1. **Enforced by code** — a test, a hook, a CLI validation. The record is the docstring, the
   comment, or the message the check prints. No separate entry. If the mechanism is deleted, the
   story goes with it, which is correct: the story existed to justify the mechanism.
2. **Loaded when the work happens** — a skill. This is the primary home for anything procedural,
   and it is the only mechanism verified to load on demand on both providers. Claude reads
   `.claude/skills/`, Codex reads `.agents/skills/`, and this repo already keeps the source of
   truth in `.agents/skills/` with `.claude/skills/` symlinking into it. Codex follows symlinks
   and uses progressive disclosure — every skill's name and description are in context, the
   `SKILL.md` body loads only when it is chosen — which is exactly "available when relevant,
   costing nothing otherwise".
3. **Always true** — the root `AGENTS.md`, with its content **inlined rather than imported**
   (see below).

`harness/playbook.md` and `harness/improvements.md` are removed as stores. Existing entries are
routed to the four homes above; an entry with no home is dropped rather than kept.

The `where:` field goes. An entry keeps symptom, cause, mechanism and status, and its symptom
carries the number or quote that makes it checkable (ADR-0021 supersedes nothing here; that rule
came from an earlier entry and stays).

**`.claude/rules/` is not a home.** It is Claude Code-specific and Codex does not read it.

**Nested `AGENTS.md` is not a home either.** This was the original proposal and the evidence
killed it. The two providers load nested files on incompatible triggers: Claude loads a
subdirectory's `AGENTS.md` when it opens a file there with the Read tool, while Codex walks root
to cwd once at launch and loads nothing below the directory it was started in. A manager session
started at the repo root therefore gets none of them under Codex, which is the main case. A
mechanism that fires on different events per provider, and not at all for the most common
session shape, cannot carry rules anyone depends on.

**`@path` imports go; `AGENTS.md` inlines its content.** Codex does not expand them — verified
empirically, the line renders as literal text and the target is never read. Today `AGENTS.md`
imports `harness/rules.md`, `harness/owner/rules.md` and `harness/voice.md`. Deleting
`CLAUDE.md` while keeping those imports would produce a file that looks provider-neutral and
silently drops the owner's standing rules on one provider. The standing rules move into
`AGENTS.md` itself; `harness/voice.md` and the owner's private rules, which are not in the public
tree, are loaded by the skills that need them instead.

**`CLAUDE.md` is deleted, not shimmed.** Claude Code reads `AGENTS.md` natively from v2.1.277
(installed: 2.1.280), and `@path` imports expand inside it. But the documented precedence is that
a `CLAUDE.md` in the working directory *or above it* makes Claude read `CLAUDE.md` files only and
skip `AGENTS.md` entirely — nested ones included. So a one-line `@AGENTS.md` compatibility shim
would silently disable the whole path-scoped mechanism this ADR depends on. The floor becomes
v2.1.277+, deliberately.

## Consequences

The harness stops paying a per-change tax that buys nothing: thousands of lines of never-loaded
prose, and an entry written into two or three places for every lesson.

A lesson now arrives when it is relevant rather than never. A nested `AGENTS.md` under a
directory reaches an agent working in that directory, on either provider.

Provenance moves to git. "What did this cost and why" lives in the commit message, attached to
the diff, plus the feature log for the narrative. Both already exist and both are read.

Costs and risks, stated rather than discovered later:

- **Deleting `CLAUDE.md` sets a version floor.** Anyone below v2.1.277, or on a provider path
  that reads only `CLAUDE.md`, gets no project instructions at all, silently. For a repo about to
  be cloned by other people this is the sharpest edge in this ADR.
- **The existing skills layout is exactly right and neither provider can be dropped from it.**
  Claude reads `.claude/skills/` only and has never read `.agents/skills/`; Codex reads
  `.agents/skills/` and does not read `.claude/skills/`. Both verified against current docs, the
  Codex side empirically. The source of truth stays `.agents/skills/` with `.claude/skills/`
  symlinking into it, Codex follows the symlinks, and removing either half breaks one provider.
- **More lessons land in skills than first estimated.** With nested `AGENTS.md` ruled out, the
  homeless procedures join the skill-bound ones. That is the right place for them: a procedure is by definition something done during a
  particular kind of work, which is when its skill loads.
- **Nested `AGENTS.md` under Codex is unverified** in the same way. The Claude side is confirmed
  from its docs; the Codex side is not.
- **Codex reads `CLAUDE.md` only as an opt-in fallback**, when `AGENTS.md` is absent in that
  directory and `project_doc_fallback_filenames` names it. Default is empty, so today it reads
  nothing from `CLAUDE.md`. That is one more reason the file has no future here.
- **Codex has hooks, on a different path and with a trust model.** `.codex/hooks.json` or a
  `[hooks]` table in `config.toml`, with the same event names this harness already uses, plus a
  requirement that each hook definition be reviewed and trusted by hash before it runs. Nothing
  in `.claude/settings.json` is read at runtime. Enforced lessons that live in a Claude hook are
  therefore *not* portable today, which this ADR does not solve and should not pretend to.
- **A product repo carrying both files has its `AGENTS.md` suppressed for Claude.** The
  precedence rule is about the working directory and everything above it, so a repo with a root
  `CLAUDE.md` and a root `AGENTS.md` gets only the former, nested `AGENTS.md` included. It does
  not block this ADR, which is about this harness.
- **History is not rewritten.** `improvements.md` stops being written and is removed from the
  tracked tree; the existing file moves to the notes repo as historical record rather than being
  deleted outright, so nothing already written is lost.
- `/lesson`, `/retro`, `hooks/stop.py`, `.claude/rules/harness.md` and `AGENTS.md` all change.
  The stop hook currently nags for an improvements entry, which becomes wrong.

## Alternatives considered

**Keep both files and shrink them.** Rejected. The problem is not length, it is that nothing
loads them. A shorter file nothing reads is still a file nothing reads.

**Load the playbook into every session.** Rejected. Hundreds of lines of mostly situational prose in
every context window, to deliver the two or three entries relevant to any given task. That is the
context bloat this ADR removes, paid deliberately.

**Keep the playbook as a searchable archive.** Rejected on evidence. It has been greppable all
along; the entries that changed behaviour were the handful promoted into `AGENTS.md`
and the hooks, not the ones sitting in the file.

**A one-line `CLAUDE.md` shim for compatibility.** Rejected once the precedence rule was read: a
root `CLAUDE.md` suppresses every `AGENTS.md`, so the shim would cost the entire mechanism. This
was the recommendation until the documentation contradicted it.

**Nested `AGENTS.md` as the path-scoped home.** This was the decision until the Codex behaviour
was measured. Claude loads a nested file when it reads something in that directory; Codex builds
its instruction chain once at launch by walking root to cwd, so a session started at the repo
root never sees a nested file below it. Two different triggers, and the common case — a manager
at the root — gets nothing. Rejected on that evidence; the work moved to skills.

**Keep recording enforced lessons as entries, for provenance.** Rejected. The docstring on the
test is the provenance, and it is the copy that gets read. Two copies drift, and the one that
drifts is the one nobody opens.
