---
name: supervisor-protocol
description: "Use when a tool result or steer is tagged [supervisor]."
version: 1.0.0
author: hermes
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [autonomy, protocol]
---
# Supervisor protocol (child-side contract)

When the supervisor plugin is enabled, delegated children (and their
orchestrator parent) receive tagged directives. These are tooling signals,
not human messages and not noise — act on them.

## If you are a delegated child

- A tool result ending in `[supervisor] Checkpoint required ...` means your
  edit was BLOCKED before running. Run a verification command (tests/lint)
  covering the listed files, READ its output, fix anything it reports, then
  retry the edit. A failing test run does not satisfy the checkpoint.
- Any `[supervisor]` text appended to a tool result (annotation) is a
  correction for THIS iteration: follow it before starting new edits.
- `[supervisor]` steers arrive mid-run at the next iteration boundary; they
  are corrections to the plan, not cancellations. Continue after acting.
- Blocks you receive at `interrupt`/halt: stop and return your summary.

## If you are the orchestrator (parent)

- `[supervisor escalation] worker ...` steers into your session when a child
  is stuck or rule-blocked repeatedly. Review with
  `delegate_task(action='list')`, then `steer` or `stop` the named child.
- Unverified-write warnings in your own sessions are advisory: the audit
  lives in `~/.hermes/supervisor/audit.jsonl`.

## Settings reference

`plugins.entries.supervisor.settings` in config.yaml (example: bundled
`plugins/supervisor/config.example.yaml`). LLM judge is off by default;
rule triggers + verify-after-write gate are the hard layer. See
`plugins/supervisor/README.md` for the full contract.
