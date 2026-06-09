#!/usr/bin/env bash
# merge_train: PreToolUse hook for Claude Code — conflict warning and blocking check.
#
# Receives JSON tool request on stdin, forwards it to conflict_check_helper.py,
# and prints stdout (decision JSON) and stderr (warnings) accordingly.
set -euo pipefail

# Read stdin to pass to helper script
INPUT=$(cat)

# Run the python helper script (which is copied to ~/.local/bin/ by install.sh)
echo "$INPUT" | python3 ~/.local/bin/conflict_check_helper.py
