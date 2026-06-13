#!/usr/bin/env bash
# merge_train: PreToolUse hook for Claude Code — conflict warning and blocking check.
#
# Receives JSON tool request on stdin, forwards it to conflict_check_helper.py,
# and prints stdout (decision JSON) and stderr (warnings) accordingly.
#
# Activity is logged to /tmp/merge_train/{repo_name}/{branch_name}/hook-YYYY-MM-DD.log
# so the user has a terminal-visible record of every conflict-check decision.
# Stderr lines are still echoed to the CLI's TUI; the tee mirrors them to the log.
set -euo pipefail

INPUT="$(cat)"

# Resolve log path. Best-effort: if we can't determine repo/branch, we still
# run the conflict check — we just skip logging.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo "detached")"
REPO_NAME="$(basename "$REPO_ROOT" 2>/dev/null || echo "no-repo")"
LOG_DATE="$(date +%Y-%m-%d)"
LOG_DIR="/tmp/merge_train/${REPO_NAME:-no-repo}/${BRANCH}"
LOG_FILE="${LOG_DIR}/hook-${LOG_DATE}.log"

if [[ -n "${REPO_ROOT}" ]]; then
  mkdir -p "$LOG_DIR" 2>/dev/null || true
  if [[ -d "$LOG_DIR" ]]; then
    TS="$(date '+%Y-%m-%dT%H:%M:%S%z')"
    {
      echo "[$TS] === Edit attempt in $REPO_ROOT on $BRANCH ==="
      echo "[$TS] stdin: $INPUT"
    } >> "$LOG_FILE" 2>/dev/null || true
  fi
fi

# Forward stdin to the helper. Capture stdout (the JSON envelope) for re-emit
# at the end. Mirror stderr to BOTH the log file and the original stderr so
# the CLI's TUI still sees it (Codex + Agy sometimes surface stderr).
EXIT=0
STDOUT="$(echo "$INPUT" | python3 ~/.local/bin/conflict_check_helper.py 2> >(tee -a "$LOG_FILE" >&2))" || EXIT=$?

if [[ -n "${REPO_ROOT}" ]] && [[ -d "$LOG_DIR" ]]; then
  TS="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  {
    echo "[$TS] exit=$EXIT"
    echo "[$TS] stdout: $STDOUT"
    echo ""
  } >> "$LOG_FILE" 2>/dev/null || true
fi

echo "$STDOUT"
exit "$EXIT"
