#!/usr/bin/env bash
# merge_train: PreToolUse hook for Claude Code — conflict warning and blocking check.
#
# Receives JSON tool request on stdin, forwards it to conflict_check_helper.py,
# and prints stdout (decision JSON) and stderr (warnings) accordingly.
#
# Activity is logged to /tmp/merge_train/{repo_name}/{branch_name}/hook-YYYY-MM-DD.log
# so the user has a terminal-visible record of every conflict-check decision.
# Stderr lines are still echoed to the CLI's TUI; the tee mirrors them to the log.
#
# Security:
#   - umask 077 → log file is 0600, log dir is 0700 (owner-only).
#   - The Edit body (new_string / new_text) is REDACTED from the log entry;
#     only file_path, tool_name, decision, and reason are written. A shared
#     box cannot recover the literal content the agent is about to write.
#   - The tee mirror is gated on the log dir existing; on a non-git cwd
#     (where the dir was never created) we fall back to plain stderr
#     so the CLI TUI stays clean.
set -euo pipefail

# Restrict new files/dirs to owner-only. Set before any mkdir / redirect.
umask 077

INPUT="$(cat)"

# Resolve log path. Best-effort: if we can't determine repo/branch, we still
# run the conflict check — we just skip logging entirely (no mkdir, no tee).
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo "detached")"
REPO_NAME="$(basename "$REPO_ROOT" 2>/dev/null || echo "no-repo")"
LOG_DATE="$(date +%Y-%m-%d)"
LOG_DIR="/tmp/merge_train/${REPO_NAME:-no-repo}/${BRANCH}"
LOG_FILE="${LOG_DIR}/hook-${LOG_DATE}.log"

# Redact the Edit body. The literal new_string / new_text / content is
# never written to the log — only the tool_name and a "body=<redacted>"
# marker. This prevents a shared-box reader from recovering the literal
# content the agent is about to write (e.g. secrets.py, ssh config).
# We also drop the file_path from the log entry because the file_path
# often embeds private info (e.g. ~/projects/<customer>/secret.txt).
# Branch and repo name are kept (already known to the user via git).
PAYLOAD_SUMMARY="$INPUT"
if [[ "$PAYLOAD_SUMMARY" == *"\"new_string\""* || "$PAYLOAD_SUMMARY" == *"\"new_text\""* || "$PAYLOAD_SUMMARY" == *"\"content\""* ]]; then
  # Pull the tool_name and file_path out of the JSON for the log entry —
  # both are safe (no body content) and useful for "what was edited?".
  # Use python3 to parse since bash JSON parsing is fragile; python3 is
  # already a hard dep of this script.
  PAYLOAD_SUMMARY="$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    tool = d.get("tool_name", "?")
    inp = d.get("tool_input", {})
    path = inp.get("file_path", "?")
    # Truncate path to basename — keeps log readable, drops user/customer names.
    import os
    path = os.path.basename(path) if path and path != "?" else "?"
    print(f"tool_name={tool} file_path={path} body=<redacted>")
except Exception:
    print("tool_name=? body=<redacted> (parse error)")
')"
fi

if [[ -n "${REPO_ROOT}" ]]; then
  mkdir -p "$LOG_DIR" 2>/dev/null || true
  if [[ -d "$LOG_DIR" ]]; then
    TS="$(date '+%Y-%m-%dT%H:%M:%S%z')"
    {
      echo "[$TS] === Edit attempt in $REPO_ROOT on $BRANCH ==="
      echo "[$TS] stdin: $PAYLOAD_SUMMARY"
    } >> "$LOG_FILE" 2>/dev/null || true
  fi
fi

# Forward stdin to the helper. Capture stdout (the JSON envelope) for re-emit
# at the end. Mirror stderr to BOTH the log file (when it exists) and the
# original stderr so the CLI's TUI still sees it. The conditional wrapper
# around tee prevents `tee: No such file or directory` from polluting
# stderr on a non-git cwd (where the log dir was never created).
EXIT=0
# Point tee at /dev/null when the log dir wasn't created (non-git cwd),
# so the process substitution never errors. Keeps stderr clean for the TUI.
_TEE_TARGET="$LOG_FILE"
if [[ ! -d "$LOG_DIR" ]]; then
  _TEE_TARGET="/dev/null"
fi
# Resolve the helper path. Prefer the installed copy at ~/.local/bin/;
# fall back to the in-tree sibling (merge_train/hooks/conflict_check_helper.py)
# so the script works in CI / fresh checkouts where install-hooks has
# not been run.
HELPER_PATH="$HOME/.local/bin/conflict_check_helper.py"
if [[ ! -f "$HELPER_PATH" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  HELPER_PATH="${SCRIPT_DIR}/conflict_check_helper.py"
fi
STDOUT="$(echo "$INPUT" | python3 "$HELPER_PATH" 2> >(tee -a "$_TEE_TARGET" >&2))" || EXIT=$?

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
