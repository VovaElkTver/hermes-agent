---
title: "Guardrails Protocol — Use when fork-guardrails blocks or approves a tool call"
sidebar_label: "Guardrails Protocol"
description: "Use when fork-guardrails blocks or approves a tool call"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Guardrails Protocol

Use when fork-guardrails blocks or approves a tool call.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/autonomous-ai-agents/guardrails-protocol` |
| Version | `1.0.0` |
| Author | hermes |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `autonomy`, `protocol` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Fork-guardrails protocol

Policy guards apply to EVERY session (operator included) and speak through
tool-result blocks/approval prompts. They are not suggestions.

## Delegation blocked ("Delegation only when the user asks")

`delegate_task` needs a fresh user ask (subagents / parallel agents /
delegate) in the CURRENT turn — one successful dispatch consumes it. If the
user said "do not use subagents", that also revokes it. Correct move: do the
work inline, or ask the user to authorize one delegation round.

## Approval prompt before outbound actions

`git push`, `gh pr create/merge`, releases, `docker/twine/npm publish`,
recursive `rm` return an approval gate: confirm with the user (or /approve),
never re-run hoping it passes silently. `git push -f` / `+main` to
main/master is blocked outright — `--force-with-lease` is the allowed form.

## Same call failed 3 times

Identical failing calls are blocked per turn. A plugin `blocked` status does
not count toward the streak; a success clears it. Change approach or ask —
do not retry the same arguments a fourth time.

## Date context

Each turn carries "Current date: ..." in your context — verify post-cutoff
facts with tools instead of trusting memory.
