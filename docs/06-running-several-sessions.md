# Running several sessions

Open several managers from the ribbon when you want to work on different features at once. Each has its own conversation, with the plans and results shared through the vault.

## Give each session its work

Open another manager and describe its assignment:

> Work on the API cleanup. The other manager is handling the login-test fix.

One manager takes responsibility for each feature's plan and workers. To hand a feature to another session, describe the transfer in the conversation.

Tab titles show session names, and the Dashboard lists managers by feature and session. Use either to find the conversation responsible for a result. A manager can also list the other live sessions when you need to reach one.

## Have managers coordinate

Tell a manager what another session needs to know:

> Ask the API manager whether its change touches the password-reset handler. Wait for its answer before planning that part of the fix.

Managers can message each other across clients. In this example, the API manager's reply tells the login-test manager whether their plans overlap. Sending a message alone does not confirm it was read, so work that depends on the answer waits for the reply.

You can steer a worker through its manager while it runs. The instruction stays with the worker's record.

## Avoid two writers on the same files

Independent tasks can run together. When two workers would change the same files in a repo, the harness flags the conflict before launch so the manager can sequence that work.

Managers reserve shared files while editing them too. If a reservation holds up another session, the managers coordinate when it can continue.

## Continue after a session ends

After restarting Obsidian, open a new manager from the ribbon and name the feature to continue. Manager tabs do not resume automatically. If a worker result needs a manager, use **resume feature manager** under **Needs you** on the Dashboard.

The replacement manager reads the feature's plan, log, and pending results before continuing.

For sessions on separate machines, first set up [pairing](11-pairing-two-machines.md). Sharing the notes repo carries the records; pairing adds messages between the machines.

Next, get to know the [Obsidian views and manager tabs](07-working-in-obsidian.md).
