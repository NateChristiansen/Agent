# Dispatch lessons

Read with `/dispatch`. These incidents explain the work-order and review checks.

## Choose model and effort for the task

A dispatch that records no model or effort cannot be judged later, and a session left on
defaults runs on defaults for hours. Name the rubric row, model, effort and reason in the preview.
Choosing a repo default still requires saying why it fits.

## Give subjective feedback a checkable bar

A worker given a subjective bar ("fewer comments") can go round after round on its own
summary without meeting it. State a count, length or command result the worker
can check. Re-run that check on the branch before pushing. A third round on the same issue
needs a fresh dispatch with a stricter bar.

## Give precedents and constraints, not guessed mechanisms

Review asks that reverse a mechanism the work order prescribed cost more dispatches to undo
than the original work. Name the outcome and sibling paths to inspect; have the worker return the
precedent used for each new file. The preflight's precedent warning only checks that paths
resolve, not that they are the right examples.

## Show one real output before automating documents

A document writer built from a sketch can generate days of notes in a shape the owner rejects
on seeing the first real one, and the rewrite deletes most of it. When a
dispatch builds a document writer or backfill, make one artifact from real records and show
it for approval before the pipeline work.

## Ask when shared helpers cannot express a requirement

When a work order requires more than the suite's shared helpers can express, a worker invents
a mechanism beside them, and reviewers reject it. Ask whether to change the helper or return `question`. Review new assertions for
fit with the suite as well as strength.

## Inspect the sibling itself

A faithful copy of a defective sibling passes the build, the precedent table and the review
dispatch. Ask what the sibling gets wrong. Record whether the
worker copied it, deviated with a reason or filed the defect. Copy remains the default when
behaviour preservation is the bar; written repo conventions still outrank a sibling.

## Default copy ships with the first slice that renders it

Wording deferred to a later step ships wrong in every slice before it. When user-visible text
has a source (the owner's ruling or existing wording), put the exact strings in the
done-criteria of the first slice that renders them, and tell the review dispatch to read every
rendered label against that source.

## Read the repo's naming rule before proposing an id

A name copied from the nearest existing one can predate the repo's written naming rule. Before
you propose an id, branch or directory name that a target repo governs, read that repo's
convention doc on a fresh clone and cite its line. An existing name is a precedent, not the
rule.

## Use the file clock for a live-code claim

A manager ruled out a verification result because its process started before the fix was
committed. The file had been written before the process started, and the run used it. Compare
the file's write time with process start before claiming a run could not see a change. Those
times can reject an impossible timeline, but do not prove which bytes a long-lived process
imported. Inspect the process or rerun in a fresh one when that distinction matters. Commit
time is when the change was published, not when the file became available to the process.

## Only the owner's own words approve a live or permission-widening action

A manager ran a live dispatch that widened worker permissions. It ran five minutes after
another session told it to hold, and one minute before the owner approved. It treated a
second session's report of approval as approval. Before a live launch or a permission change,
read the owner's own message for that action. Another session's report, a missing prompt, or a
yes given for a different action is not approval.

## Check the shared wait constants before calling a run stuck

A worker returned a question in 15 seconds. The run then looked hung for 10 minutes, because
`QUESTION_IDLE_SECONDS` in `dispatch/state.py` is 600 and the shell timeout was also 600. A fix
was started before the report was retracted. Before reporting a hang, read the dispatch record
with `dispatch show` and compare the elapsed time with the wait constants in `dispatch/state.py`.

## An ambiguous reply is not a yes to speak on GitHub

A post goes out under the owner's account and cannot be taken back. A question with two
options cannot be answered by "sounds good". Ask again and name one action. Read the thread for the owner's own comments
before posting. Post only on a reply that names the post.

## Derive each slice's target from the goal, and profile before naming a lever

A suite-speed goal of 120 s at `-n 8` allowed 15% of the measured 6,281 CPU-s, an 85% cut.
The work orders asked each area for 60%, so even a full success could not reach the goal, and
after more than 30 dispatches the suite stopped at 283 s. A time split by cause (process
launches, git, fixtures) came only at the end and showed a floor near 174 s. Separately, a
"shared repo templates" lever was recommended from one worker's per-file gains; the build found
most helpers already used templates and saved about 11 s of 2,300. Before writing per-slice
targets, compute what the goal needs from each slice. Before recommending a lever, measure its
share of the time on the current base.

## Name the load a timing criterion is measured under

Three Windows dispatches had done criteria that could pass only at a given full-suite speed.
Other sessions were also loading the machine's CPU, so speeds differed between runs and the
criteria were out of reach. None of the three could finish, about $13 in all, and high effort
did not change that. When acceptance
depends on load or timing, name the load it is measured under, or have the worker measure an
idle baseline first.

## Say which copy of a draft the owner should read

A first documentation draft lived on a worker branch. The owner read the main checkout instead,
so the first round of notes was on a stale copy. When you hand the owner a worker's draft, give
the exact branch or worktree path to read.

## A reading round re-audits against the whole brief

Over five rounds of owner reading, a note on condescension came back in round 4, and a note on
duplication came back in round 5. Each writer had re-checked only the newest round's notes. In
an iterative writing order, have the writer re-audit against the whole style brief and every
earlier round's notes, not only the newest.

## A changed preview needs a fresh yes

The owner approved one preview. Preflight refused that launch. The manager changed the order's
fence and added a decision, then launched the new preview without asking again, and disclosed
the change only after launch. When a preview's bytes change after the owner's yes, show the new
preview and ask again before launching.

## Look at a rendered UI change before review

A dashboard health section passed its scoped tests, review and the full suite twice. Both of
the owner's live checks still found visual defects: a per-machine summary printed twice, a
healthy hook row labelled "A harness hook raised", a stale sync shown as ok, a status and its
explanation naming different time windows, and a label column wide enough to wrap the status.
The tests checked the data, not the rendered page. An order that changes what the owner sees
asks for a screenshot with realistic data, and the manager looks at it before the review round.

## Split a shared numeric bound before parallel nodes launch

Two parallel branches each added sections to one text report with an 80-line bound. Each sized
its own sections alone (45 and 30 lines) and passed its own test. The batch landing rendered
91 lines and failed, and the fix took a follow-up task and four dispatches. When parallel nodes
share a numeric bound, the plan gives each node its share and adds one combined test before
they launch.

## Probe the target before an expensive build

An onboarding experiment built an answer key, two runner rounds and a scorecard. The first
worker launch on the target machine then failed before the worker started. Another build
cell spent $8.84 on a worker whose MCP tools never loaded; a $0.10 read-only probe later found
the next blocker before any build hit it. Before a build or experiment on a new machine,
repo or cell, run one dry run and one cheap probe launch there.

## Name the whole matrix for a preservation contract

A comment-preserving YAML save went through ten reviews in a day. Each build fixed the
reported case, and the next reviewer found the adjacent container case. The implementers had
listed some of those cases as known limits. An outside audit later deleted the splicer. For a
preservation contract, the first build order names every syntax case for every operation,
and an implementer's stated limit counts as a finding before review. A third rejection in one
defect class goes to the owner instead of a fresh dispatch: ask whether the approach should
exist.

## Give a review one lens per hook event a shared emitter serves

A change added a suffix to every hook denial in the shared entry point. The review order named
lenses only for PreToolUse and Stop. The review passed, and the landing then failed because
UserPromptSubmit and continuation outputs also changed. It took four rounds across two
dispatches. When a change touches a function every hook output passes through, the review
order lists each hook event it serves as a lens.

## Name a probe deletion the worker may run

A work order told a Claude worker to write a probe under `/tmp` and delete it afterwards. The
worker's `rm` was denied in two attempts, and each handoff asked the manager to clean up.
Workers may now run a single-file `rm`, but recursive and forced removal stay denied. Keep a
probe to files the worker can remove one at a time, and name each deletion in the order.

## State a number for every invariant in the first build order

A build order named an invariant ("never inside a hook timeout") with no number. Two reviews
found nine and then four defects, including a trigger with no total time bound. The escalation
carried a 2 s worst-case bar and came back clean in one pass. Each invariant a build order
names gets a measurable bar in the first order, not after review.

## Prose for people carries its own style brief

Chapter work orders for a user manual passed on the manager's own rules: short sentences, a
caveat on every claim, harness files as sources. The owner found it verbose and condescending,
"written by ai for ai". A work order for human-facing prose carries a style brief for that
reader. Read `harness/voice.md` first, and ask the owner for samples when it is empty.

## Public tests use generic fixture identifiers

A work order named a real private repository to reproduce a denial. The implementer copied
that name into a public regression fixture, and review caught it before landing. Keep the real
incident in private notes, and give the worker a generic command shape for the public test.

## Stop probing when every remaining cause leads to one decision

Four probes chased one sandbox error. After network and cache were excluded, every remaining
cause still led to the same three routes the owner had to choose from. Two probes would have
been enough. Once each remaining cause leads to the same owner decision, stop and present the
decision.

## Give generated-file regeneration to the manager when the worker cannot run it

A worker stopped on a permission denial for the env-prefixed command that regenerates a
snapshot. Its resume then failed verification on the stale snapshot, and the manager
regenerated it on a separate branch. Before launch, check that the worker can run each
generator its change needs. If it cannot, the work order says the manager regenerates the
file, and when.

## Check a placement constraint against the repo's convention docs

A build order said "do not move this wrapper into a shared package". The convention doc named
in the repo notes requires that move for helpers duplicated across packages. Review flagged the
copy as blocking, which cost one more build and an owner decision. Before writing a constraint
on where code lives (move, share, duplicate or keep), read the convention docs the repo notes
name. Cite the deciding line if the constraint stands, and raise a conflict before launch.

## A `say` approval cannot widen a dispatch's writes

A worker asked to edit a test file outside its declared `writes`. The manager approved it with
`dispatch say`, the worker made the edit, and the supervisor failed the dispatch: "paths outside
declared writes". `resume` can only narrow `writes`. The order had missed a test that used the
deleted file as an example. Name every file in `writes` at launch; a file found later means a
new dispatch.

## A whole-tree sweep that needs judgment is not mechanical

A sweep of about 400 files for private details ran on the mechanical row. The first worker
lost its forks and hit the turn cap twice, and an opus escalation finished it: $28 against a
$6-12 band. Each file needed judgment: neutral fixtures, a frozen baseline, remapped ids. Start
a sweep like that on the build row, or split it by directory.

## A research brief on a process change asks what the control prevents

Research on turning off "require branches to be up to date before merging" studied only stale
green PRs. Two days later, back-to-back merges ran overlapping deploys that collided, and the
setting went back on. A research brief on changing or removing a CI or process control asks
what the control prevents or serialises today, and what removing it unlocks, alongside the
owner's question.

The publishing lessons (a force-push does not remove a commit from GitHub; a pre-publish audit
reads every added line) are in the Publication section of `harness/safety.md`.
