# Reviewing and shipping

A worker's finished branch goes through the manager's checks and a fresh review. You receive the result with its evidence and decide when to publish.

## Check the result against the plan

The manager reports what changed, how it was checked, and whether it meets the plan. For the login-test fix, that includes the cause and the result of trying the original failure again.

You can inspect the worker's results yourself through its handoff and branch diff. The handoff contains the summary, questions, and remaining steps; the verification record shows the checks for that attempt and commit.

When the outcome needs a live observation, such as a UI prompt appearing, the manager reports what was observed or asks you to check it.

## Read the fresh review

The manager sends the artifact and its acceptance criteria to a fresh reviewer. You receive the review's blocking findings and optional suggestions, with the results of its probes.

The manager routes blocking findings back to the implementer and brings you the updated result after its checks.

You can ask for changes yourself:

> The timeout change hides the failure. Send it back and ask for a fix to the reset path.

The manager sends your feedback to the worker and reports the response.

## Publish the branch

The manager asks before pushing a branch or opening a pull or merge request. You can also request both:

> Push this branch and open a draft pull request.

Workers commit locally. Publishing happens after your approval.

After the work has shipped and the feature's verification is complete, you can [close the feature](05-day-to-day.md#close-a-feature).
