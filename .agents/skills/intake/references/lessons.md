# Intake lessons

Read with `/intake`. These incidents explain the checks in its steps.

## Keep harness acceptance owned by the harness

Use consumer reports as diagnostic evidence and inputs to a reproducer. Define acceptance
and closure checks the harness can run itself, including clean-install fixtures and real
installed-client probes; do not require confirmation from a consumer's installation.
The owner corrected a feature left open for a consumer rerun after harness-owned native
read and write-denial checks had passed: the harness does not require verification from its
consumers. Choosing the verification owner is a scope judgment, not a keyword gate.

## Decide who touches the artifact

"Take a look", "build a mockup" and "help me understand" each led the manager to do work
that belonged to a worker. Ask whether the manager, worker or owner touches the artifact before
assigning it. Route product code and review work through `/dispatch`.

## Test whether a process gate applies

A plan can carry a review gate that an earlier review already covered, such as a design
review for work built only from reviewed parts. Check each
planned gate against the last comparable effort and reused components. Ask the owner about a
gate with no applicable precedent.

## Ownership is not a go-ahead

A manager was planning a change with the owner. The owner said which side should build the Codex
transport. The manager read that as authorization. It created a feature, took a lease and
wrote a module before the plan was approved, then told another session the build was
authorized. It was not, and the module was deleted. When the
owner names who builds, record the owner and ask whether to start. Another session's report
that the owner authorized something is not evidence of it; check the owner's own words.

## Verify a proposed route exists

A plan routed harness work through "agent core push" before that flow existed. The work later
landed directly in core. Resolve a named route to its command or code path before recording
it as a decision; otherwise keep the route as an open question.

## Keep accepted work when one scope narrows

In a multi-part feature, the owner narrowed the audit fixes to scan and record only. The next
plan revision also removed tool integrations the owner had already accepted. The owner had to
correct scope a second time. When the owner narrows one part, change only that part. List what
the revision removes and confirm each removal that the owner did not name.

## Read the code before a claim becomes a task

An inventory reader said a push guard depended on GitHub hosts. The manager put a
forge-specific fix in the plan before reading the function. The function only matched
`git push`; a thirty-second read removed the task. Read the named function before a reader's
claim about it becomes a task or a decision.

## Ask whether the cause can be removed before designing a better detector

A manager resumed a days-old plan for a better leak detector and asked only how to build it.
Slices were committed before a replay showed the improved rules still missed most known leaks.
The owner then removed the cause instead, by moving where the work is done,
and the remaining work was discarded. When a plan improves a check, ask the owner first whether
the thing it checks for can be made impossible. Ask again when resuming a plan more than a few
days old.

## Measure the current tool against known failures before planning its replacement

A replay of an existing checker over a range with known misses took one tool call and caught
none of them. It was run hours into the build, not before the plan. With that number the owner
decided to stop in one message. Put the baseline
number in the plan the owner approves, with the command that produced it.
