---
tags: [doc]
---

# harness/

How this harness behaves today. The repo README is the tour; these are the details. History
lives in ADRs, feature logs and git, not here.

| Doc | What it is |
|---|---|
| [[AGENTS#Standing rules\|rules]] | The owner's rules, loaded in every manager session. |
| [[harness/standing-rules]] | Each standing rule's date, incident and enforcement limits; read before proposing a change. |
| [[harness/maintenance]] | Editing this harness: clients, private and generated paths, tests, the CLI surface. |
| [[harness/rules-enforcement]] | Which prose rules a check holds, and which are still prose only. |
| [[harness/testing]] | Testing ordering changes, and the fence that keeps tests off live state. |
| [[harness/layout]] | Where each record lives and who writes it. |
| [[harness/sessions]] | Session records, leases, peer messages and plan review. |
| [[harness/safety]] | What is protected and by what: threats, verification, pass records, landing, the pre-push gate and the public export. |
| [[harness/worker-contract]] | The system prompt appended to every worker. Change it and every future dispatch changes. |
| [[harness/dispatch-rubric]] | Which row, model, effort and budget for which task shape; `tests/test_rubric.py` checks it against `src/agent_cli/dispatch/rubric.py`. |
| [[harness/worker-permissions]] | Worker MCP grants, outside services, and what a Codex manager may run without asking. |
| [[harness/provider-gaps]] | Where Claude Code and Codex differ, what the harness does about it, and when to look again. |
| [[harness/agent-yaml]] | Every `agent.yaml` and `agent.local.yaml` key. |
| [[harness/journal]] | The daily note: the generated block, the numbers, and who writes it. |
| [[harness/obsidian]] | The vault: generated views, setup, plugins and caveats. |
| [[harness/document-editing]] | The `harness` tool's `notes_document` action: a reviewable replacement of a feature document. |

`obsidian/` holds the snippet, template, view, plugin and seed-config sources
that `agent obsidian setup` installs, and `owner/lessons-queue.md` is the local lesson queue (ADR-0048). `owner/` and `voice.md` are the owner's and are
not in the public repo; `owner.example/` and `voice.example.md` are the tracked starting points.
