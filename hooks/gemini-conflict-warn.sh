#!/usr/bin/env bash
# merge_train: Antigravity/Gemini conflict-warn session guard.
#
# Replaces gemini-domain-lock-guard.sh. Domain locking has been removed.
# This script runs once per session (sentinel-gated) and calls
# predict-spawn-check.sh to surface any cross-PR conflicts as warnings.
#
# This script is placed in <repo>/.gemini/conflict-warn.sh by install.sh.
# Wire it from the BeforeTool hook in .gemini/settings.json.
#
# The hook fires ONCE per session (sentinel prevents re-runs on every tool
# call) and NEVER blocks — it emits warnings only.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [[ -z "$REPO_ROOT" ]]; then exit 0; fi

# Bail fast if no domain registry
if [[ ! -f "$REPO_ROOT/file_domains.yaml" ]]; then exit 0; fi

# Per-session sentinel: only run once per repo+session combination
REPO_HASH="$(echo "$REPO_ROOT" | md5 -q 2>/dev/null || echo "$REPO_ROOT" | md5sum 2>/dev/null | cut -d' ' -f1)"
SESSION_KEY="${ANTIGRAVITY_SESSION_ID:-${AO_SESSION_ID:-$$}}"
SENTINEL="/tmp/mt_conflict_warn_guard_${REPO_HASH}_${SESSION_KEY}"

if [[ -f "$SENTINEL" ]]; then
  exit 0
fi
touch "$SENTINEL"

# Propagate Gemini/AO agent identity so predict-spawn-check.sh can log it
MERGE_TRAIN_AGENT="${AO_SESSION_ID:+antigravity-${AO_SESSION_ID}}"
export MERGE_TRAIN_AGENT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPAWN_CHECK="$SCRIPT_DIR/predict-spawn-check.sh"
if [[ ! -f "$SPAWN_CHECK" ]]; then
  # merge_train not fully installed — skip silently
  exit 0
fi

# Derive files from git diff vs main if not provided externally
if [[ -z "${MERGE_TRAIN_FILES:-}" ]]; then
  BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo "")"
  if [[ -n "$BRANCH" ]]; then
    CHANGED="$(git diff --name-only "origin/main...${BRANCH}" 2>/dev/null \
              || git diff --name-only "main...${BRANCH}" 2>/dev/null \
              || echo "")"
    if [[ -n "$CHANGED" ]]; then
      export MERGE_TRAIN_FILES="$CHANGED"
    fi
  fi
fi

bash "$SPAWN_CHECK"
