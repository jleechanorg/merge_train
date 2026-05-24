#!/usr/bin/env bash
# merge_train: session-start domain lock gate.
#
# Called at the START of a coding session by:
#   - Claude Code (SessionStart hook in settings.json)
#   - Codex (SessionStart hook in hooks.json)
#   - Antigravity/Gemini (first-write guard via domain-lock-guard.sh)
#   - OpenCode (taskStart hook in .opencode.json)
#   - AO workers (wired in session-manager.ts — this script is the fallback)
#
# What it does:
#   1. Detects PR number from env or branch name
#   2. Determines files-to-check from $MERGE_TRAIN_FILES or git diff vs origin/main
#   3. Checks all touched domains — exits 1 if any are HELD
#   4. Reserves all free domains for this PR/agent/session
#
# Exit codes:
#   0  all domains free and reserved (or no file_domains.yaml — skip silently)
#   1  at least one domain is HELD — session must be refused
#   2  configuration error (bad registry, CLI not found)
#
# Env vars (all optional):
#   MERGE_TRAIN_PR        PR number (derived from branch name if absent)
#   MERGE_TRAIN_AGENT     agent name (derived from CLI env vars if absent)
#   MERGE_TRAIN_BRANCH    branch name (git symbolic-ref if absent)
#   MERGE_TRAIN_FILES     space-separated list of files to check
#                         (if empty: uses git diff origin/main --name-only)
#   MERGE_TRAIN_REGISTRY  path to file_domains.yaml (default: repo root)
#   MERGE_TRAIN_LOG       path to lock JSONL (default: ~/.merge_train/locks/...)
#   MERGE_TRAIN_DRY_RUN   "1" = check-only, no reserve (default: "0")

set -euo pipefail

# ── 1. Find repo root and registry ───────────────────────────────────────────
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [[ -z "$REPO_ROOT" ]]; then
  # Not inside a git repo — nothing to check
  exit 0
fi

REGISTRY="${MERGE_TRAIN_REGISTRY:-$REPO_ROOT/file_domains.yaml}"
if [[ ! -f "$REGISTRY" ]]; then
  # No domain registry in this repo — skip silently
  exit 0
fi

# ── 2. Detect PR number ───────────────────────────────────────────────────────
BRANCH="${MERGE_TRAIN_BRANCH:-$(git symbolic-ref --short HEAD 2>/dev/null || echo "")}"
PR="${MERGE_TRAIN_PR:-}"
if [[ -z "$PR" && -n "$BRANCH" ]]; then
  # Extract first 2+ digit number from branch name (e.g. feat/pr-7000-foo → 7000)
  PR="$(printf '%s\n' "$BRANCH" | grep -oE '[0-9]{2,}' | head -1 || true)"
fi
# If still no PR, use 0 (own-PR check — non-blocking for self-owned locks)
PR="${PR:-0}"

# ── 3. Detect agent name ──────────────────────────────────────────────────────
AGENT="${MERGE_TRAIN_AGENT:-}"
if [[ -z "$AGENT" ]]; then
  # Detect from known CLI env vars
  if [[ -n "${CLAUDE_SESSION_ID:-}" ]]; then
    AGENT="claude-${CLAUDE_SESSION_ID:0:8}"
  elif [[ -n "${CODEX_SESSION_ID:-}" ]]; then
    AGENT="codex-${CODEX_SESSION_ID:0:8}"
  elif [[ -n "${GEMINI_SESSION:-}" || -n "${AO_HOOK_EVENT_NAME:-}" ]]; then
    AGENT="antigravity-${AO_SESSION_ID:-$(hostname)-$$}"
  elif [[ -n "${OPENCODE_SESSION:-}" ]]; then
    AGENT="opencode-${OPENCODE_SESSION:0:8}"
  else
    AGENT="agent-$(whoami)-$$"
  fi
fi

# ── 4. Determine files to check ───────────────────────────────────────────────
if [[ -n "${MERGE_TRAIN_FILES:-}" ]]; then
  # Explicit file list from AO or caller
  # shellcheck disable=SC2206
  FILES=( ${MERGE_TRAIN_FILES} )
else
  # Derive from what this branch has changed vs main
  CHANGED="$(git diff --name-only "origin/main...${BRANCH}" 2>/dev/null || git diff --name-only "main...${BRANCH}" 2>/dev/null || echo "")"
  if [[ -z "$CHANGED" ]]; then
    # Fresh branch with no commits vs main — nothing to check
    echo "merge_train: no changed files vs main, domain check skipped" >&2
    exit 0
  fi
  mapfile -t FILES <<< "$CHANGED"
fi

if [[ ${#FILES[@]} -eq 0 ]]; then
  exit 0
fi

# ── 5. Build CLI args ─────────────────────────────────────────────────────────
REG_ARG=( --registry "$REGISTRY" )
LOG_ARG=()
if [[ -n "${MERGE_TRAIN_LOG:-}" ]]; then
  LOG_ARG=( --log "$MERGE_TRAIN_LOG" )
fi
PR_ARG=( --pr "$PR" )

DRY_RUN="${MERGE_TRAIN_DRY_RUN:-0}"

# ── 6. Run the domain lock CLI ────────────────────────────────────────────────
if command -v domain_lock >/dev/null 2>&1; then
  CLI="domain_lock"
else
  CLI="python3 -c 'import sys; from merge_train.domain_lock import main; sys.exit(main())'"
fi

# CHECK first — fail fast if held
echo "merge_train: checking domains for ${#FILES[@]} file(s) (PR #${PR}, agent=${AGENT})..." >&2
if ! eval "$CLI" "${REG_ARG[@]}" "${LOG_ARG[@]}" check --files "${FILES[@]}" "${PR_ARG[@]}"; then
  RC=$?
  if [[ $RC -eq 1 ]]; then
    echo "merge_train: REFUSED — a domain is held by another PR. Start a different task." >&2
    exit 1
  else
    echo "merge_train: WARNING — domain_lock check failed with exit $RC (config error?), allowing session to proceed." >&2
    exit 0
  fi
fi

# RESERVE if not dry-run
if [[ "$DRY_RUN" != "1" ]]; then
  echo "merge_train: reserving domains for PR #${PR}..." >&2
  # Get unique domains for these files
  DOMAINS="$(eval "$CLI" "${REG_ARG[@]}" "${LOG_ARG[@]}" audit --files "${FILES[@]}" 2>/dev/null \
    | python3 -c "import sys,json; data=json.load(sys.stdin); print(' '.join({e['domain'] for e in data.get('entries',[]) if e.get('domain') and e.get('domain')!='__unmapped__'}))" 2>/dev/null || true)"

  if [[ -n "$DOMAINS" ]]; then
    for DOMAIN in $DOMAINS; do
      eval "$CLI" "${REG_ARG[@]}" "${LOG_ARG[@]}" reserve \
        --domain "$DOMAIN" --pr "$PR" --agent "$AGENT" --branch "$BRANCH" 2>/dev/null || true
    done
    echo "merge_train: domains reserved for PR #${PR}: ${DOMAINS}" >&2
  fi
fi

exit 0
