#!/usr/bin/env bash
# merge_train: PreToolUse hook for Claude Code.
#
# Intercepts Edit/Write tool calls to dynamically check and reserve domains.
# Exits with a JSON block decision if a domain is held by another PR.
#
# Input: Reads tool request JSON on stdin.
# Output: JSON object with hookSpecificOutput.
#

set -euo pipefail

# Read stdin payload
payload="$(cat)"

# Extract tool name and target file path (fast regex parse)
tool_name="$(echo "$payload" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4 2>/dev/null || true)"
file_path="$(echo "$payload" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4 2>/dev/null || true)"

# Fallback to python if fast-path parsing fails
if [[ -z "$tool_name" || -z "$file_path" ]]; then
  tool_name="$(echo "$payload" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null || true)"
  file_path="$(echo "$payload" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input', {}).get('file_path',''))" 2>/dev/null || true)"
fi

# Only check Edit and Write tools
case "$tool_name" in
  Edit|Write) ;;
  *)
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
    exit 0
    ;;
esac

if [[ -z "$file_path" ]]; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
  exit 0
fi

# Find repository root
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [[ -z "$REPO_ROOT" ]]; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
  exit 0
fi

# Relativize absolute file_path against REPO_ROOT so fnmatch patterns like
# "tests/*.py" match correctly (absolute paths are never matched otherwise).
if [[ "$file_path" == "$REPO_ROOT/"* ]]; then
  file_path="${file_path#$REPO_ROOT/}"
fi

# ── Caching Layer ────────────────────────────────────────────────────────────
SESSION_KEY="${CLAUDE_SESSION_ID:-${AO_SESSION_ID:-$$}}"
REPO_HASH="$(echo "$REPO_ROOT" | md5 -q 2>/dev/null || echo "$REPO_ROOT" | md5sum 2>/dev/null | cut -d' ' -f1 || echo "unknown")"
CACHE_FILE="/tmp/mt_session_allowed_${REPO_HASH}_${SESSION_KEY}"

if [[ -f "$CACHE_FILE" ]] && grep -Fxq "$file_path" "$CACHE_FILE" 2>/dev/null; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
  exit 0
fi

# Check for domain registry (MERGE_TRAIN_REGISTRY overrides default for testing)
REGISTRY="${MERGE_TRAIN_REGISTRY:-$REPO_ROOT/file_domains.yaml}"
if [[ ! -f "$REGISTRY" ]]; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
  exit 0
fi

# Detect PR number and branch name
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo "")"
PR=""
if [[ -n "$BRANCH" ]]; then
  PR="$(printf '%s\n' "$BRANCH" | grep -oE '[0-9]{2,}' | head -1 || true)"
fi
PR="${PR:-0}"

# Detect session ID / agent name
AGENT="claude-code"
if [[ -n "${CLAUDE_SESSION_ID:-}" ]]; then
  AGENT="claude-${CLAUDE_SESSION_ID:0:8}"
fi

# Resolve the domain lock CLI
if command -v domain_lock >/dev/null 2>&1; then
  CLI="domain_lock"
else
  CLI="python3 -c 'import sys; from merge_train.domain_lock import main; sys.exit(main())'"
fi

# Check domain lock status for this file — capture stdout for the denial message.
# Use `if !` to avoid set -e trapping the non-zero exit from the check command.
CHECK_JSON="$(eval "$CLI" --registry "$REGISTRY" check --files "$file_path" --pr "$PR" --json 2>/dev/null || true)"
if ! eval "$CLI" --registry "$REGISTRY" check --files "$file_path" --pr "$PR" >/dev/null 2>&1; then
  # check exits 1: real enforced domain is held.
  HELD_INFO="$(echo "$CHECK_JSON" | python3 -c "
import sys,json
try:
  d=json.load(sys.stdin)
  parts=[f\"HELD: {x['domain']} by PR#{x['holder']['pr']} agent={x['holder']['agent']} branch={x['holder']['branch']}\" for x in d.get('held',[])]
  print('; '.join(parts))
except: print('held')
" 2>/dev/null || echo "held")"
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"reason\":\"merge_train: REFUSED — $HELD_INFO. Start a different task.\"}}"
  exit 0
fi

# If domain is free, automatically reserve it.
DOMAINS="$(echo "$CHECK_JSON" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(' '.join(d.get('free_domains',[]))) " 2>/dev/null || true)"

if [[ -n "$DOMAINS" ]]; then
  for DOMAIN in $DOMAINS; do
    eval "$CLI" --registry "$REGISTRY" reserve --domain "$DOMAIN" --pr "$PR" --agent "$AGENT" --branch "$BRANCH" >/dev/null 2>&1 || true
  done
fi

# Emit advisory warning if any advisory-mode domains had conflicts (allow, but warn).
# Advisory files are NOT cached so the warning fires on every edit.
ADVISORY_INFO="$(echo "$CHECK_JSON" | python3 -c "
import sys,json
try:
  d=json.load(sys.stdin)
  parts=[f\"{x['domain']} by PR#{x['holder']['pr']} agent={x['holder']['agent']}\" for x in d.get('advisory_held',[])]
  print('; '.join(parts))
except: print('')
" 2>/dev/null || true)"

if [[ -n "$ADVISORY_INFO" ]]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\",\"reason\":\"merge_train: ADVISORY WARN — $ADVISORY_INFO. Proceeding (warn-only mode).\"}}"
  exit 0
fi

# Cache the allowed file path to avoid Python invocation in subsequent calls (clean allow only).
mkdir -p "$(dirname "$CACHE_FILE")" 2>/dev/null || true
echo "$file_path" >> "$CACHE_FILE"

echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
exit 0

