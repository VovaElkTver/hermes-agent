#!/usr/bin/env bash
# ============================================================================
# Hermes Agent (VovaElkTver fork) one-line installer.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/VovaElkTver/hermes-autonomous-agent/main/fork-install.sh)
#
# Clones the FORK (not upstream) into ~/.hermes/hermes-agent and hands over to
# setup-hermes.sh, which owns all real installation (uv/pm/venv). This script
# never duplicates setup logic — it only resolves the repo and the destination.
#
# Env overrides:
#   HERMES_FORK_URL   clone source (default: the fork below)
#   HERMES_FORK_BRANCH branch (default: main)
#   HERMES_HOME       base dir (default: $HOME/.hermes)
#   DRY_RUN=1         print actions, touch nothing (smoke-testable anywhere)
# ============================================================================
set -e

FORK_URL="${HERMES_FORK_URL:-https://github.com/VovaElkTver/hermes-autonomous-agent}"
BRANCH="${HERMES_FORK_BRANCH:-main}"
BASE="${HERMES_HOME:-$HOME/.hermes}"
DEST="$BASE/hermes-agent"

say() { printf '\033[0;36m\033[0m%s\n' "$*"; }
run() { if [ "${DRY_RUN:-0}" = "1" ]; then say "[dry-run] $*"; else eval "$@"; fi; }

if [ -d "$DEST/.git" ]; then
  cur="$(git -C "$DEST" remote get-url origin 2>/dev/null || true)"
  # Accept checkouts created before the 2026-10 rename (GitHub redirects them).
  cur_norm="$(printf '%s' "$cur" | sed 's|VovaElkTver/hermes-agent\b|VovaElkTver/hermes-autonomous-agent|')"
  if [ "$cur_norm" != "$FORK_URL" ] && [ "${DRY_RUN:-0}" != "1" ]; then
    say "existing checkout origin is: $cur"
    say "expected:                    $FORK_URL"
    say "refusing to touch it — remove/move $DEST or set HERMES_FORK_URL."
    exit 1
  fi
  say "Updating existing checkout ($BRANCH)..."
  run "git -C '$DEST' fetch -q origin '$BRANCH' && git -C '$DEST' checkout -q '$BRANCH' && git -C '$DEST' merge --ff-only -q 'origin/$BRANCH'"
else
  say "Cloning $FORK_URL ($BRANCH) -> $DEST"
  run "mkdir -p '$BASE' && git clone -q --branch '$BRANCH' '$FORK_URL' '$DEST'"
fi

run "'$DEST/setup-hermes.sh' ${*:1}"

# Fork default (vovak, 2026-10-13): the shared-metrics consent is answered "No thanks"
# at install time, so fork nodes never see the upstream first-run offer. Written into
# raw config.yaml because save_config() strips false-equal-to-default values; skipped
# when the owner already has a telemetry section (their answer wins, both directions).
SEED="$BASE/config.yaml"
if [ "${DRY_RUN:-0}" = "1" ]; then
  say "[dry-run] seed shared-metrics opt-out into $SEED"
elif [ -f "$SEED" ] && ! grep -q '^telemetry:' "$SEED"; then
  printf '\ntelemetry:\n  shared_metrics:\n    enabled: false\n    send: false\n    offer_version: 2\n' >> "$SEED"
  say "Shared-metrics: opted out (fork default). Change any time: hermes setup telemetry"
fi

say "Then: source '$DEST/activate' && hermes gateway install"
