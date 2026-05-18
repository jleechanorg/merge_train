#!/usr/bin/env bash
# merge_train: local pre-commit gate.
#
# Refuses commits that touch domains held by a different PR. Treats
# the currently-checked-out branch's PR number as "self" (own-PR
# reservations don't block).
#
# Install:
#   ln -s ../../hooks/pre-commit.sh .git/hooks/pre-commit
#   chmod +x hooks/pre-commit.sh
#
# Env overrides:
#   MERGE_TRAIN_PR       override detected PR number
#   MERGE_TRAIN_REGISTRY YAML path
#   MERGE_TRAIN_LOG      JSONL path (default: ~/.merge_train/locks/<repo-hash>/pr_domain_locks.jsonl)

set -euo pipefail

# Collect staged files
STAGED=$(git diff --cached --name-only --diff-filter=ACMRT)
if [[ -z "${STAGED}" ]]; then
  exit 0
fi

# Derive PR number from branch name when possible (e.g. feat/pr-6926-foo or pr/6926-foo).
PR_FROM_BRANCH=""
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "")
if [[ -n "${BRANCH}" ]]; then
  PR_FROM_BRANCH=$(printf '%s\n' "${BRANCH}" | grep -oE '[0-9]{2,}' | head -1 || true)
fi

PR_ARG=()
PR_VALUE="${MERGE_TRAIN_PR:-${PR_FROM_BRANCH}}"
if [[ -n "${PR_VALUE}" ]]; then
  PR_ARG=( --pr "${PR_VALUE}" )
fi

REG_ARG=()
if [[ -n "${MERGE_TRAIN_REGISTRY:-}" ]]; then
  REG_ARG=( --registry "${MERGE_TRAIN_REGISTRY}" )
fi
LOG_ARG=()
if [[ -n "${MERGE_TRAIN_LOG:-}" ]]; then
  LOG_ARG=( --log "${MERGE_TRAIN_LOG}" )
fi

# shellcheck disable=SC2206
FILES=( ${STAGED} )

# Diff-mode lets two PRs edit the same file but disjoint Python symbols.
# Set MERGE_TRAIN_DIFF_MODE=0 to fall back to file-level checks.
DIFF_ARG=()
if [[ "${MERGE_TRAIN_DIFF_MODE:-1}" != "0" ]]; then
  DIFF_ARG=( --diff-mode )
fi

if command -v domain_lock >/dev/null 2>&1; then
  domain_lock "${REG_ARG[@]}" "${LOG_ARG[@]}" \
    check --files "${FILES[@]}" "${PR_ARG[@]}" "${DIFF_ARG[@]}"
else
  python3 -c "import sys; from merge_train.domain_lock import main; sys.exit(main())" "${REG_ARG[@]}" "${LOG_ARG[@]}" \
    check --files "${FILES[@]}" "${PR_ARG[@]}" "${DIFF_ARG[@]}"
fi
