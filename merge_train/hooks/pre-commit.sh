#!/usr/bin/env bash
# merge_train: local pre-commit conflict prediction gate.
#
# Runs predict-conflicts across all open PRs and WARNS (stderr) if staged
# files overlap with another open PR's domain. Does NOT block the commit —
# the conflict warning is informational only.
#
# Install:
#   ln -s ../../hooks/pre-commit.sh .git/hooks/pre-commit
#   chmod +x hooks/pre-commit.sh
#
# Env overrides:
#   MERGE_TRAIN_PR       override detected PR number (included in analysis)
#   MERGE_TRAIN_REGISTRY YAML path
#   MERGE_TRAIN_REPO     OWNER/REPO for GitHub API (auto-detected from remote)

set -euo pipefail

# Collect staged files
STAGED=$(git diff --cached --name-only --diff-filter=ACMRT)
if [[ -z "${STAGED}" ]]; then
  exit 0
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [[ -z "$REPO_ROOT" ]]; then exit 0; fi

REGISTRY="${MERGE_TRAIN_REGISTRY:-$REPO_ROOT/file_domains.yaml}"
REGISTRY_ARG=()
if [[ -f "$REGISTRY" ]]; then
  REGISTRY_ARG=(--registry "$REGISTRY")
fi

# ── Resolve predict-conflicts CLI ─────────────────────────────────────────────
if command -v predict-conflicts >/dev/null 2>&1; then
  CLI_PREFIX="predict-conflicts"
else
  CLI_PREFIX="python3 -m merge_train.predict"
fi

# ── Collect open PR numbers ───────────────────────────────────────────────────
OPEN_PRS=""
if command -v gh >/dev/null 2>&1; then
  OPEN_PRS="$(gh pr list --state open --json number --jq '.[].number' 2>/dev/null | tr '\n' ',' | sed 's/,$//' || true)"
fi

# Include the current branch's PR if known
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo "")"
PR_FROM_BRANCH=""
if [[ -n "$BRANCH" ]]; then
  PR_FROM_BRANCH="$(printf '%s\n' "$BRANCH" | grep -oE '[0-9]{2,}' | head -1 || true)"
fi
PR_VALUE="${MERGE_TRAIN_PR:-${PR_FROM_BRANCH}}"

if [[ -n "$PR_VALUE" ]]; then
  if [[ -n "$OPEN_PRS" ]]; then
    OPEN_PRS="${OPEN_PRS},${PR_VALUE}"
  else
    OPEN_PRS="${PR_VALUE}"
  fi
fi

if [[ -z "$OPEN_PRS" ]]; then
  # No PRs to compare against — nothing to predict
  exit 0
fi

# ── Auto-detect OWNER/REPO ────────────────────────────────────────────────────
REPO="${MERGE_TRAIN_REPO:-}"
if [[ -z "$REPO" ]]; then
  REPO="$(git remote get-url origin 2>/dev/null | python3 -c "
import sys, re
url = sys.stdin.read().strip()
if url.endswith('.git'): url = url[:-4]
m = re.search(r'github\.com[:/]([^/]+/[^/]+)\$', url)
if m: print(m.group(1))
" 2>/dev/null || true)"
fi

REPO_ARG=()
if [[ -n "$REPO" ]]; then
  REPO_ARG=(--repo "$REPO")
fi

# ── Run predict-conflicts ─────────────────────────────────────────────────────
echo "merge_train: pre-commit — predicting conflicts across PRs: $OPEN_PRS ..." >&2

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

# ── Surface conflicts as warnings (non-blocking) ──────────────────────────────
python3 - "$PREDICT_JSON" >&2 <<'PYEOF'
import sys, json

raw = sys.argv[1]
try:
    plan = json.loads(raw)
except json.JSONDecodeError:
    sys.exit(0)

conflicts = [pc for pc in plan.get("pairwise_conflicts", []) if pc.get("is_conflict")]
if not conflicts:
    print("merge_train: no blocking pairwise conflicts detected")
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
print("merge_train: commit proceeding (conflict warnings are non-blocking)")
PYEOF

# Always exit 0 — warnings are informational; they do NOT block the commit.
exit 0
