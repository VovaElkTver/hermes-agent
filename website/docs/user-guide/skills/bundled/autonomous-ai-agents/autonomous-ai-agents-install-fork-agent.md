---
title: "Install Fork Agent — Install or update the VovaElkTver Hermes fork on a host"
sidebar_label: "Install Fork Agent"
description: "Install or update the VovaElkTver Hermes fork on a host"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Install Fork Agent

Install or update the VovaElkTver Hermes fork on a host.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/autonomous-ai-agents/install-fork-agent` |
| Version | `1.0.0` |
| Author | Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `hermes`, `install`, `fork`, `setup`, `gateway`, `windows` |
| Related skills | [`hermes-agent`](../../bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Install Fork Agent Skill

Self-install of the VovaElkTver Hermes Agent fork on a new or existing host using the
two one-line installers at the repository root (`fork-install.sh`, `fork-install.ps1`,
added in PR#21). The installers only resolve the repo and destination, then hand over to
`setup-hermes.sh` / `setup-hermes.ps1`, which owns all real installation (uv/pm/venv).
This skill does not cover upstream-Nous installs or profile management.

## When to Use

- A fresh host needs this fork installed in one command (Linux/WSL or Windows).
- An existing fork checkout should be fast-forwarded to `main` and re-setup.
- Deciding what to do when the installer refuses a directory (foreign origin).

## Prerequisites

- `git` on PATH and network access to GitHub.
- Linux/WSL: bash. Windows: PowerShell 5.1+ (`iwr`/`iex`).
- The repo was renamed to `VovaElkTver/hermes-autonomous-agent`; GitHub redirects the old
  `VovaElkTver/hermes-agent` URL that the installers default to, so fresh clones via the
  one-liners below keep working. Canonical README lines use the new name.

## How to Run

Linux / WSL / macOS:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/VovaElkTver/hermes-agent/main/fork-install.sh)
```

Windows (PowerShell):

```powershell
iwr -useb https://raw.githubusercontent.com/VovaElkTver/hermes-agent/main/fork-install.ps1 | iex
```

Extra arguments after the script name are forwarded to `setup-hermes.sh` (bash variant
only — the ps1 hands over with no argument pass-through).

## Quick Reference

Env overrides (identical names in both installers):

| Var | Default (sh / ps1) | Effect |
|---|---|---|
| `HERMES_FORK_URL` | `https://github.com/VovaElkTver/hermes-agent` | clone source |
| `HERMES_FORK_BRANCH` | `main` | branch to clone/fast-forward |
| `HERMES_HOME` | `$HOME/.hermes` / `$env:LOCALAPPDATA\hermes` | base dir; checkout goes to `<base>/hermes-agent` |
| `DRY_RUN=1` | off | print actions, touch nothing (smoke-testable anywhere) |

Installer decision tree (both variants):

- No `<dest>/.git` → `git clone --branch $BRANCH $FORK_URL <dest>`, then hand over to setup.
- Existing checkout, `origin` equals `FORK_URL` → `fetch` + `checkout` + `merge --ff-only`
  onto the branch, then hand over to setup. A non-fast-forwardable tree fails here; do not
  force it.
- Existing checkout, foreign `origin` → prints the found vs expected URL and exits 1
  ("refusing to touch it"). Remove/move the checkout or set `HERMES_FORK_URL`. Never
  repoint someone else's checkout silently.

## Procedure

1. Smoke-test first on an unfamiliar host: prefix `DRY_RUN=1` and confirm the printed
   plan (clone vs update vs refusal) matches expectations. Done when the dry-run output
   names the destination you intend.
2. Run the one-liner for the platform. Done when setup-hermes exits 0.
3. Activate the environment: `source <dest>/activate` (bash; fish: `activate.fish`) or
   `& '<dest>\venv\Scripts\Activate.ps1'` (Windows). Done when `hermes --version` answers.
4. Post-install checklist, in order:
   - Secrets: put provider/messaging keys in `~/.hermes/.env` (secrets only — e.g.
     `OPENROUTER_API_KEY`, platform tokens); settings live in `config.yaml`, not `.env`.
   - Config: `hermes config set` for non-secret settings; never hand-edit protected
     `config.yaml` with raw tools.
   - Gateway (only if messaging surfaces are wanted): `hermes gateway install`, then
     verify with `hermes gateway status`.
   - Close out with `hermes --version` on the activated install.

## Pitfalls

- Never silently patch a failed install (no improvised `git reset --hard`, remote swaps,
  or dependency pokes). Capture the installer/setup log and report it to the operator;
  wait for an explicit instruction before repairing.
- The ps1 path is NOT yet verified end-to-end on real Windows — the acceptance run on
  hermes-win is still pending. Treat any Windows failure as expected-unverified, not a
  mystery bug, and report rather than work around.
- `HERMES_HOME` relocates only the base dir; the installer always uses `<base>/hermes-agent`
  as the checkout. A checkout elsewhere is "existing directory with foreign layout" — stop.
- Refusal on foreign origin is a feature (protects other installs); the fix belongs to the
  operator's decision, not the agent's.
- Post-rename wrinkle (verified live): a checkout whose `origin` is the new
  `hermes-autonomous-agent` URL is refused by the default installer, which expects the
  literal `.../hermes-agent` URL. For such hosts run with
  `HERMES_FORK_URL=https://github.com/VovaElkTver/hermes-autonomous-agent` prepended.

## Verification

- `hermes --version` prints the fork version from the activated environment.
- `git -C <dest> remote get-url origin` returns exactly the expected `FORK_URL`.
- `git -C <dest> log --oneline -1` matches `origin/$BRANCH` head (ff-update landed).
- `hermes gateway status` (where installed) reports the service running.
