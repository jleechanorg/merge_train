#!/usr/bin/env bash
# merge_train: PreToolUse hook for Claude Code — conflict WARN mode.
#
# Replaces domain-lock-pre-tool.sh. Domain locking (reserve/check/release)
# has been removed. This hook NEVER blocks edits. Conflict detection now
# happens at spawn time (predict-spawn-check.sh) before the agent is started.
#
# This file is intentionally minimal. It exists so existing hook wiring in
# Claude Code settings.json continues to work — just update the path from
# domain-lock-pre-tool.sh to conflict-warn-pre-tool.sh.
#
# Input : JSON on stdin (Claude Code PreToolUse payload)
# Output: permissionDecision="allow" always

set -euo pipefail

# Always allow — read and discard stdin so the pipe doesn't break.
cat > /dev/null

echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
exit 0
