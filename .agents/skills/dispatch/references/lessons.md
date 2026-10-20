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

The publishing lessons (a force-push does not remove a commit from GitHub; a pre-publish audit
reads every added line) are in the Publication section of `harness/safety.md`.
