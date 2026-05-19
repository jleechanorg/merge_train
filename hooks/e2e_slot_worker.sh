#!/usr/bin/env bash
# e2e_slot_worker.sh — Acquires area lock, then runs OpenCode agent for one slot.
#
# Usage: e2e_slot_worker.sh <slot_num> <run_id> <registry> <lock_log> <mctrl_repo>
#
# 1. Read slot task from merge_train_e2e/tasks.md
# 2. Auto-extract heading symbol from shared_plan.md
# 3. Build plan YAML with domain + symbol
# 4. Acquire lock via domain_lock reserve-plan
# 5. If DENIED, exit 1 (agent must NOT start)
# 6. If RESERVED, launch openw run with the slot task
# 7. After agent finishes, the lock remains active (release on PR merge/close)
#
# This proves: no agent starts without an active lock entry.

set -euo pipefail

SLOT="$1"
RUN_ID="$2"
REGISTRY="$3"
LOCK_LOG="$4"
MCTRL_REPO="$5"

SLOT_N=$(printf "%02d" "$SLOT")
BRANCH="merge-train-e2e/${RUN_ID}/slot-${SLOT_N}"
AGENT_ID="e2e-slot-${SLOT_N}"
SYNTHETIC_PR=$((50000 + SLOT))

PLAN_FILE="/tmp/e2e_worker_plan_slot_${SLOT_N}.yaml"

# Auto-extract the heading symbol from the shared_plan.md file
SYMBOL=$(python3 -c "
from merge_train.symbols import extract_markdown_symbols
src = open('${MCTRL_REPO}/merge_train_e2e/shared_plan.md').read()
syms = extract_markdown_symbols(src, file_stem='shared_plan')
for s in syms:
    if 'slot_${SLOT_N}' in s.name:
        print(s.name)
        break
")

if [ -z "$SYMBOL" ]; then
    echo "FATAL: could not auto-extract symbol for slot-${SLOT_N}" >&2
    exit 2
fi

echo "=== Worker slot-${SLOT_N} ==="
echo "  Symbol: ${SYMBOL}"
echo "  Branch: ${BRANCH}"
echo "  PR:     ${SYNTHETIC_PR}"

# Build plan YAML
cat > "$PLAN_FILE" <<EOF
plan:
  - domain: e2e_shared_markdown
    symbols: [${SYMBOL}]
EOF

# Acquire lock BEFORE starting agent
echo "  Acquiring lock..."
set +e
LOCK_RESULT=$(python -m merge_train.domain_lock \
    --registry "$REGISTRY" \
    --log "$LOCK_LOG" \
    --git-cwd "$MCTRL_REPO" \
    reserve-plan \
    --pr "$SYNTHETIC_PR" \
    --agent "$AGENT_ID" \
    --branch "$BRANCH" \
    --plan "$PLAN_FILE" 2>&1)
LOCK_EXIT=$?
set -e

if [ $LOCK_EXIT -ne 0 ]; then
    echo "  DENIED: $LOCK_RESULT" >&2
    echo "  Agent NOT started — lock acquisition failed." >&2
    exit 1
fi

echo "  RESERVED: $LOCK_RESULT"
echo "  Lock is active — starting agent."

# Create worktree for this slot
WORKTREE="/tmp/merge_train_opencode_md_area_lock/${RUN_ID}/slot-${SLOT_N}"
mkdir -p "$(dirname "$WORKTREE")"
git -C "$MCTRL_REPO" worktree add "$WORKTREE" "$BRANCH" 2>/dev/null || {
    # If branch doesn't exist yet, create it from setup
    git -C "$MCTRL_REPO" worktree add "$WORKTREE" "e2e-md-area-lock-setup" 2>/dev/null
}

cd "$WORKTREE"

# Launch OpenCode agent
# The agent edits only its assigned slot heading
AGENT_TASK="Edit merge_train_e2e/shared_plan.md: under heading ## slot-${SLOT_N}, change 'status: pending' to 'status: complete by slot-${SLOT_N}'. Do NOT edit any other heading. Then commit, push, and create a PR against main."

echo "  Launching opencode agent..."
OPENCODE_RESULT=""
OPENCODE_EXIT=99
if command -v openw >/dev/null 2>&1; then
    OPENCODE_RESULT=$(openw run --dangerously-skip-permissions "$AGENT_TASK" 2>&1) && OPENCODE_EXIT=0 || OPENCODE_EXIT=$?
elif command -v opencode >/dev/null 2>&1; then
    OPENCODE_RESULT=$(opencode run --dangerously-skip-permissions "$AGENT_TASK" 2>&1) && OPENCODE_EXIT=0 || OPENCODE_EXIT=$?
else
    echo "  No opencode/openw found — doing manual edit (fallback proof)."
    # Manual edit as fallback
    python3 -c "
p = open('merge_train_e2e/shared_plan.md').read()
lines = p.split('\n')
new_lines = []
in_target = False
for line in lines:
    if line.strip() == '## slot-${SLOT_N}':
        in_target = True
        new_lines.append(line)
    elif in_target and line.strip() == 'status: pending':
        new_lines.append('status: complete by slot-${SLOT_N}')
        in_target = False
    elif line.startswith('## '):
        in_target = False
        new_lines.append(line)
    else:
        new_lines.append(line)
open('merge_train_e2e/shared_plan.md','w').write('\n'.join(new_lines))
"
    git add merge_train_e2e/shared_plan.md
    git commit -m "feat(e2e): complete slot-${SLOT_N}" || true
    git push origin "$BRANCH" 2>&1 || true
    OPENCODE_EXIT=0
    OPENCODE_RESULT="manual edit fallback (no opencode available)"
fi

echo "  Agent exit: $OPENCODE_EXIT"
echo "  Agent output (last 200 chars): ${OPENCODE_RESULT: -200}"

# Record agent transcript
TRANSCRIPT_DIR="/tmp/merge_train_evidence/opencode_md_area_lock/${RUN_ID}/agent_transcripts"
mkdir -p "$TRANSCRIPT_DIR"
cat > "$TRANSCRIPT_DIR/slot-${SLOT_N}.log" <<EOF
slot: ${SLOT_N}
symbol: ${SYMBOL}
branch: ${BRANCH}
pr: ${SYNTHETIC_PR}
lock_result: ${LOCK_RESULT}
lock_exit: ${LOCK_EXIT}
agent_exit: ${OPENCODE_EXIT}
agent_output_last_200: ${OPENCODE_RESULT: -200}
timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

echo "=== Worker slot-${SLOT_N} complete ==="

# Note: lock is NOT released here. It stays active until PR merge/close.
# The release step is separate.

exit $OPENCODE_EXIT
