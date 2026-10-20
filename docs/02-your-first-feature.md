# Your first feature

A feature holds one piece of work: its plan, tasks, decisions, and worker results. This chapter follows the login-test request from [getting started](01-getting-started.md) to a finished branch you can review.

## Describe the outcome

Open a manager from the Obsidian ribbon and describe what you want:

> Fix the flaky login test. It fails intermittently in CI after a password reset. Start by finding out why.

Include any ticket or failure output you have. To continue existing work, name its feature.

The manager restates the request and asks about anything unresolved. It reads linked tickets through a configured tracker, or asks you to paste them when access is unavailable.

## Read the plan

The manager creates a feature folder with a numbered name and opens its plan. The plan explains why the work matters, lists verifiable tasks, keeps open questions, and says how the result will be checked. Its log records decisions and findings as work proceeds.

For the login test, the plan can start with an investigation and leave implementation until the cause is known.

The proposal includes verification of the reported failure. You can change the scope before approving:

> The plan looks right. Find the cause first, then bring me the proposed fix.

The feature appears in the Dashboard's open list. You can keep its plan beside the conversation while the work runs.

## Approve the worker

The manager writes the work order and shows a launch preview: the repo and starting point, model, budget, permissions, and planned checks. You approve the launch after the plan.

Once you approve, the harness gives the worker its own branch and worktree. The manager stays with your plan and records; the worker investigates or changes the product repo.

If the worker needs a decision, the manager brings back its question. Answer in the manager conversation. You can also change direction there while the worker is running.

## Read what came back

The worker returns a summary and a committed branch. The harness runs the configured checks, and the manager compares the result with the plan before marking the task complete. A blocked result can contain committed work; the manager explains what remains.

The manager brings back the finding and evidence. For the login test, you see the cause, the change, and what happened when the failure was tried again.

The branch is now ready for [reviewing and shipping](04-reviewing-and-shipping.md). The next chapter covers [sending work to workers](03-sending-work-to-workers.md) in more detail.
