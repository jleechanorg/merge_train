#!/usr/bin/env bash
# merge_train: pre-spawn conflict prediction hook.
#
# Wire into your agent orchestrator (AO, OpenHands, custom) BEFORE
# spawning an agent. Runs predict-conflicts against all currently open
# PRs and emits a JSON warning block if the new agent's files overlap
# with files already being modified.
#
# This hook NEVER blocks (always exits 0). Conflicts are reported as
# warnings on stderr. The orchestrator can surface them to the user
# but must not refuse the spawn based solely on this output.
#
# Required env vars:
#   MERGE_TRAIN_FILES    space-separated list of files the agent plans to touch
# Optional:
#   MERGE_TRAIN_PR       PR number being spawned (omit if not yet created)
#   MERGE_TRAIN_REGISTRY path to YAML (default: file_domains.yaml in repo root)
#   MERGE_TRAIN_REPO     OWNER/REPO for GitHub API (auto-detected from git remote)
#   MERGE_TRAIN_LOG      unused (kept for env-var compatibility with old hooks)

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [[ -z "$REPO_ROOT" ]]; then
  # Not inside a git repo — nothing to predict
  exit 0
fi

REGISTRY="${MERGE_TRAIN_REGISTRY:-$REPO_ROOT/file_domains.yaml}"
REGISTRY_ARG=()
if [[ -f "$REGISTRY" ]]; then
  REGISTRY_ARG=(--registry "$REGISTRY")
fi

if [[ -z "${MERGE_TRAIN_FILES:-}" ]]; then
  echo "merge_train: MERGE_TRAIN_FILES is empty — no files to check, skipping conflict prediction" >&2
  exit 0
fi

# ── Resolve the predict-conflicts CLI ────────────────────────────────────────
# predict-conflicts is a standalone entry point (pyproject.toml entry point)
# or callable as: python3 -m merge_train.predict predict-conflicts
if command -v predict-conflicts >/dev/null 2>&1; then
  CLI_PREFIX="predict-conflicts"
else
  CLI_PREFIX="python3 -m merge_train.predict"
fi

# ── Collect open PR numbers from GitHub ──────────────────────────────────────
OPEN_PRS=""
if command -v gh >/dev/null 2>&1; then
  OPEN_PRS="$(gh pr list --state open --json number --jq '.[].number' 2>/dev/null | tr '\n' ',' | sed 's/,$//' || true)"
fi

# If the spawned PR itself is in the list, include it so predict-conflicts
# knows about this agent's own files (already declared in MERGE_TRAIN_FILES).
if [[ -n "${MERGE_TRAIN_PR:-}" ]]; then
  # Ensure the spawning PR is represented — gh may not have it yet if brand new
  if [[ -n "$OPEN_PRS" ]]; then
    OPEN_PRS="${OPEN_PRS},${MERGE_TRAIN_PR}"
  else
    OPEN_PRS="${MERGE_TRAIN_PR}"
  fi
fi

if [[ -z "$OPEN_PRS" ]]; then
  echo "merge_train: no open PRs found — conflict prediction skipped" >&2
  exit 0
fi

# ── Auto-detect repo OWNER/REPO ──────────────────────────────────────────────
REPO="${MERGE_TRAIN_REPO:-}"
if [[ -z "$REPO" ]]; then
  REPO="$(git remote get-url origin 2>/dev/null | python3 -c "
import sys
url = sys.stdin.read().strip()
if url.endswith('.git'): url = url[:-4]
# Handle both https://github.com/ORG/REPO and git@github.com:ORG/REPO
import re
m = re.search(r'github\.com[:/]([^/]+/[^/]+)\$', url)
if m: print(m.group(1))
" 2>/dev/null || true)"
fi

REPO_ARG=()
if [[ -n "$REPO" ]]; then
  REPO_ARG=(--repo "$REPO")
fi

# ── Run predict-conflicts (warn-only) ────────────────────────────────────────
echo "merge_train: predicting conflicts across open PRs: $OPEN_PRS ..." >&2

PREDICT_JSON="$(eval "$CLI_PREFIX" \
  "${REGISTRY_ARG[@]}" \
  predict-conflicts \
  --from-prs "$OPEN_PRS" \
  "${REPO_ARG[@]}" \
  --json \
  2>/dev/null || true)"

if [[ -z "$PREDICT_JSON" ]]; then
  echo "merge_train: conflict prediction returned no data — proceeding" >&2
  exit 0
fi

# ── Parse and surface any real conflicts as warnings ─────────────────────────
python3 - "$PREDICT_JSON" >&2 <<'PYEOF'
import sys, json

raw = sys.argv[1]
try:
    plan = json.loads(raw)
except json.JSONDecodeError:
    # Unparseable output is not a blocker
    sys.exit(0)

conflicts = [pc for pc in plan.get("pairwise_conflicts", []) if pc.get("is_conflict")]
if not conflicts:
    print("merge_train: no blocking pairwise conflicts detected — spawn approved")
    sys.exit(0)

print("merge_train: WARNING — conflict prediction found overlapping changes:")
for pc in conflicts:
    pr_a = pc.get("pr_a")
    pr_b = pc.get("pr_b")
    for dc in pc.get("domain_conflicts", []):
        domain = dc.get("domain", "?")
        syms = dc.get("overlapping_symbols", [])
        sym_note = f" (symbols: {', '.join(syms)})" if syms else ""
        print(f"  WARN  PR#{pr_a} vs PR#{pr_b} — domain '{domain}'{sym_note}")
print("merge_train: proceeding with spawn (warnings are non-blocking)")
PYEOF

exit 0
