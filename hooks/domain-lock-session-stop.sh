#!/usr/bin/env bash
# merge_train: session-stop domain lock release.
#
# Called at the END of a coding session to release all locks held by this PR.
# Silently skips if no file_domains.yaml in repo or no locks held.
#
# Env vars:
#   MERGE_TRAIN_PR    PR number (derived from branch if absent)
#   MERGE_TRAIN_LOG   lock JSONL path

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [[ -z "$REPO_ROOT" ]]; then exit 0; fi

REGISTRY="${MERGE_TRAIN_REGISTRY:-$REPO_ROOT/file_domains.yaml}"
if [[ ! -f "$REGISTRY" ]]; then exit 0; fi

BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo "")"
PR="${MERGE_TRAIN_PR:-}"
if [[ -z "$PR" && -n "$BRANCH" ]]; then
  PR="$(printf '%s\n' "$BRANCH" | grep -oE '[0-9]{2,}' | head -1 || true)"
fi
if [[ -z "$PR" ]]; then exit 0; fi

LOG_ARG=()
if [[ -n "${MERGE_TRAIN_LOG:-}" ]]; then
  LOG_ARG=( --log "$MERGE_TRAIN_LOG" )
fi

REG_ARG=( --registry "$REGISTRY" )

echo "merge_train: releasing domain locks for PR #${PR}..." >&2
if command -v domain_lock >/dev/null 2>&1; then
  domain_lock "${REG_ARG[@]}" "${LOG_ARG[@]}" release --pr "$PR" 2>/dev/null || true
else
  python3 -c "import sys; from merge_train.domain_lock import main; sys.exit(main())" \
    "${REG_ARG[@]}" "${LOG_ARG[@]}" release --pr "$PR" 2>/dev/null || true
fi

exit 0
