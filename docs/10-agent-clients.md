# Choosing and using agent clients

Choose a manager from the ribbon to start a conversation in your preferred client. Plans, worker records, and messages stay in the harness, so changing clients keeps your work history available.

## Prepare a manager

Install your chosen client from the links in [getting started](01-getting-started.md#what-you-need), then run setup. Setup configures the harness's manager tools and vault launchers. Complete the project and hook approvals named in its report before working.

## Choose a worker independently

Your choice of manager leaves the worker choice open. The manager selects a worker for the task and explains that choice in the launch preview, alongside its access and planned checks.

If the selected worker needs different permissions or capabilities, the manager brings you a concrete proposal before launch. See [approving a concrete launch](13-safety.md#approve-a-concrete-launch).

Workers receive a work order, commit locally, and return a result for the manager to check. The manager handles publishing with your approval. Model and budget choices are covered in [sending work to workers](03-sending-work-to-workers.md).

## Know what access the worker gets

The preview shows file, command, and network access for the selected client and repo.

Extra MCP servers have their own access controls. A worker's shell network restriction does not restrict those servers; their selection appears separately in the preview.

## Keep hooks trusted after updates

After setup or an update, complete the hook-trust steps named in the setup report for your agent. [Troubleshooting](14-troubleshooting.md#restore-manager-tools-or-hooks) covers missing session context or tools.

If a worker is denied an operation, the manager reports the denial and the change needed to continue.

Next, connect managers across machines with [pairing](11-pairing-two-machines.md).
