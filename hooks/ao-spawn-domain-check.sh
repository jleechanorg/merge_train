#!/usr/bin/env bash
# merge_train: pre-spawn gate hook.
#
# Wire into your agent orchestrator (AO, OpenHands, custom) BEFORE
# spawning an agent. The orchestrator must supply the list of files
# the agent is expected to modify (e.g. derived from the task spec,
# bead, or PR design doc).
#
# Behavior:
#   - exit 0  -> all files are in free domains, spawn allowed
#   - exit 1  -> at least one domain is held, spawn refused
#   - exit 2  -> configuration error (missing registry, bad args)
#
# Required env vars:
#   MERGE_TRAIN_FILES    space-separated list of files
#   MERGE_TRAIN_PR       PR number (optional — own-PR re-checks are free)
# Optional:
#   MERGE_TRAIN_REGISTRY path to YAML (default: file_domains.yaml)
#   MERGE_TRAIN_LOG      path to JSONL (default: ~/.merge_train/locks/<repo-hash>/pr_domain_locks.jsonl)
#   MERGE_TRAIN_DIFF_MODE  "1" (default) resolves Python symbols from
#                        the *staged* diff; "0" falls back to file-level.
#                        At spawn time there's no staged diff yet, so the
#                        spawn hook always runs file-level — diff-mode is
#                        only meaningful at commit time.

set -euo pipefail

if [[ -z "${MERGE_TRAIN_FILES:-}" ]]; then
  echo "merge_train: MERGE_TRAIN_FILES is empty — no files to check, allowing spawn" >&2
  exit 0
fi

# shellcheck disable=SC2206
FILES=( ${MERGE_TRAIN_FILES} )

ARGS=( check --files "${FILES[@]}" --no-diff-mode )
if [[ -n "${MERGE_TRAIN_PR:-}" ]]; then
  ARGS+=( --pr "${MERGE_TRAIN_PR}" )
fi
if [[ -n "${MERGE_TRAIN_REGISTRY:-}" ]]; then
  ARGS=( --registry "${MERGE_TRAIN_REGISTRY}" "${ARGS[@]}" )
fi
if [[ -n "${MERGE_TRAIN_LOG:-}" ]]; then
  ARGS=( --log "${MERGE_TRAIN_LOG}" "${ARGS[@]}" )
fi

if command -v domain_lock >/dev/null 2>&1; then
  exec domain_lock "${ARGS[@]}"
fi

# Fallback: run as module from repo root
exec python3 -c "import sys; from merge_train.domain_lock import main; sys.exit(main())" "${ARGS[@]}"
