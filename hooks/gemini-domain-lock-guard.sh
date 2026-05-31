#!/usr/bin/env bash
# merge_train: Antigravity/Gemini domain-lock session guard.
#
# This script is placed in <repo>/.gemini/domain-lock-guard.sh by install.sh.
# It is called from the BeforeTool hook in .gemini/settings.json.
#
# Uses a per-session sentinel file so the domain-lock check fires ONCE
# at the start of each session (not on every tool call).
#
# The sentinel is keyed by repo root + git worktree SHA so each worktree
# spawned for a different PR gets its own check.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [[ -z "$REPO_ROOT" ]]; then exit 0; fi

# Bail out fast if no domain registry in this repo
if [[ ! -f "$REPO_ROOT/file_domains.yaml" ]]; then exit 0; fi

# Build a unique sentinel key for this session
REPO_HASH="$(echo "$REPO_ROOT" | md5 -q 2>/dev/null || echo "$REPO_ROOT" | md5sum 2>/dev/null | cut -d' ' -f1)"
SESSION_KEY="${ANTIGRAVITY_SESSION_ID:-${AO_SESSION_ID:-$$}}"
SENTINEL="/tmp/mt_domain_lock_guard_${REPO_HASH}_${SESSION_KEY}"

# Only run once per session
if [[ -f "$SENTINEL" ]]; then
  exit 0
fi
touch "$SENTINEL"

# Delegate to the shared start hook
MERGE_TRAIN_AGENT="${AO_SESSION_ID:+antigravity-${AO_SESSION_ID}}"
export MERGE_TRAIN_AGENT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SCRIPT="$SCRIPT_DIR/domain-lock-session-start.sh"
if [[ ! -f "$HOOK_SCRIPT" ]]; then
  # merge_train not installed locally — skip silently
  exit 0
fi

bash "$HOOK_SCRIPT"
