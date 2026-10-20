# Sending work to workers

The manager turns an approved task into a work order and launches a worker in the repo. You decide what it should achieve and what it may spend; the manager handles the launch and brings back questions and results.

## Check the work order

The manager writes the work order from the approved plan. The preview includes the task, starting commit, allowed file changes, and acceptance criteria.

You approve this concrete launch. If a permission or prerequisite is missing, the manager resolves it and brings back the updated proposal. See [approving a concrete launch](13-safety.md#approve-a-concrete-launch).

Workers get their own worktrees. The manager launches independent tasks together and waits where one needs another's result.

## Change how many workers can run

Each repo's `dispatch.max_concurrent` setting limits its workers on this machine. Separate worktrees can still share ports, locks, or a test database, and every finished change adds verification work. The limit lets you match parallel work to what the repo and machine can handle.

The default is three. For a repo whose worktrees share a port, a lock or a test database, lower it:

> Allow only one worker at a time in the API repo.

The manager sets that repo's value in `agent.yaml`. Use one for work that shares a test service; raise it as you see how parallel runs affect the machine and verification time. See [configuration](09-configuration.md).

## Choose a model and budget

The harness groups work into probe, review, mechanical, build, design, and max rows. The manager chooses a row for the task and explains why. Each row has model choices for the supported clients, and most have a budget band.

You can ask for a specific model or budget when the work calls for it:

> Use a stronger model for the implementation, with a $20 budget. Explain the choice in the preview.

Budget and reasoning effort are separate settings. The preview shows the effective budget and where it came from. A budget above the repo's default needs a reason. The max row is reserved for an explicit choice from you.

A worker that reaches its budget stops; the harness does not raise it or answer the worker's questions for you. The manager can propose continuing with a new budget or a narrower task.

## Watch or steer a run

The Dashboard lists workers with their state, latest activity, goal, and cost or tokens when available. Use a worker's **open** link to watch it in a terminal tab. Its handoff link leads to the recorded result.

Keep decisions in the manager conversation:

> Tell the worker to check the password-reset path before changing the test timeout.

The manager can send that instruction into the running turn. It is recorded with the dispatch, so the work order and later steering can be read together.

When the worker asks a question, it pauses for your answer. Reply through the manager, which passes your answer to the worker so it can continue. The session stays connected for about ten minutes; if you answer later, the manager resumes the worker on its existing branch and worktree with your answer.

## Decide whether to continue

A return can be done, blocked, or a question. Done still goes through the harness's verification; a claimed completion without acceptable check evidence is held back.

The manager explains what remains and proposes the next step. It can resume the worker for a fix or answer, or propose a fresh worker for different work.

Once the result meets the work order, move to [reviewing and shipping](04-reviewing-and-shipping.md).
