---
id: 24
title: Findings are tracked until they earn a mechanism or justify prose
status: accepted
date: 2026-09-23
supersedes: 21
superseded_by: null
tags:
- adr
links:
- '[[adrs/0021-lessons-live-where-they-load|ADR-0021]]'
---

# ADR-0024: Findings are tracked until they earn a mechanism or justify prose

## Context

[[adrs/0021-lessons-live-where-they-load|ADR-0021]] said a lesson lives where it loads or it
does not exist. That holds. It never said where a finding goes *before* it has a home, it was
argued rather than sourced, and it has two internal errors (it says "Four homes" and lists
three; it says `AGENTS.md` imports three files, when `CLAUDE.md` is what imports them).

The playbook's entries are a mix of enforced, advisory and proposed, and nothing loads it.
Dozens of files cite its `Pxxx` ids. The principle: the least documentation that can rot is
best; track unhoused findings; prose only if justified; prefer a mechanism that needs no AI
judgement. It was checked against the literature from independent sources, which corrected
some of the original claims.

## Decision

**A finding is unhoused by default and stays tracked until it earns a home.** Three states:

**Tracked** — the default. A task with a named owner and a verifiable end state. Not an inbox:
every finding is triaged into an accepted task, an open question, or an explicit drop that says
why. A finding in none of those is the failure this ADR exists to stop.

**Mechanism** — the goal. An enforceable boundary that decides without AI judgement: a hook, a
CLI validation, a test, or a sandbox/permission rule. The test is whether the rule can be
decided from the tool call, the diff, or the file contents alone. If it can, prose is strictly
worse. The evidence goes in the mechanism's docstring, test name, or deny message, so nobody
deletes the guard later without seeing what it cost.

**Prose** — the exception. Both conditions required: the lesson needs judgement that cannot be
reduced to a check, **and** it attaches to a decision point a skill already loads. If nothing
loads there, it stays tracked. The justification is recorded with it. Phrase it concretely and
modally: "never import from X", not "be careful with imports".

`Pxxx` ids are stable identifiers with moving targets. A live normative reference is rewritten
to the destination mechanism. An entry naming a mechanism carries a check that fails when that
mechanism disappears — resolution proves presence, not behaviour, so a critical claim pairs it
with a behavioural test.

**Amended 2026-09-24: the id index is gone too.** This ADR originally kept the ids resolvable
through a small generated index built from a hand-maintained manifest
(`harness/lessons.yaml`), with a test asserting every cited id resolved. The manifest was a
hand-edited file with no automatic writer whose only job was to decode ids the same decision
had just retired, and the test existed only to keep it in step with the citations — the shape
this ADR was written to remove, smaller and with a CI gate attached. The manifest, the
index, the module and the test are deleted; the numbering series ends here, and a new finding
never gets an id. Ids already in the tree stay as inert tags beside the mechanism they belong
to, and are dropped or replaced with their incident phrase whenever that line is next edited.
The cost is accepted: the published repo is a squashed export, so a bare id there resolves
nowhere and a reader has only the surrounding sentence. That is what the sentence is for.

ADR-0021's other decisions stand unchanged: both stores go; `.claude/rules/` and nested
`AGENTS.md` are not homes; `@path` imports go; `CLAUDE.md` is deleted rather than shimmed,
because a `CLAUDE.md` at or above cwd makes Claude skip `AGENTS.md` entirely. Floor is v2.1.277+.

## Consequences

"True" stops being sufficient reason to write something down. Proposals become triaged work
rather than prose.

Risks: the tracker can become the new graveyard, and triage is the only thing preventing it — if
tasks pile up ownerless, revisit this rather than defend it. The prose bar is stricter than the
literature requires; if real judgement lessons get dropped for want of a loader, add the skill
rather than loosen the bar. "Enforceable boundary" is abusable, so a row claiming mechanism
names which of the four it is. Routing every entry is one-shot judgement and will get rows wrong;
the index exists to make each drop reversible. Some entries route to mechanisms that do not
exist yet — work, sequenced separately, not part of this cleanup.

Amended by [[adrs/0028-lesson-queue|ADR-0028]]: a finding with no open feature is tracked as a
lesson queue entry, not a task.

Amended by [[adrs/0058-harness-state-after-audit-5|ADR-0058]]: doctor's project-instructions check is the guard against `CLAUDE.md`/`claude.md`.

## Alternatives considered

**Advisory stays prose by default**, as ADR-0021 implied. Rejected: the advisory entries that
existed never changed behaviour, because nothing loaded them.

**Every unhoused finding becomes a task automatically.** Rejected — that is the same decay one
level up. Triage is the difference.

**Delete the ids with the prose.** Rejected: the citations were live, and an identifier should outlive
its target's location.
