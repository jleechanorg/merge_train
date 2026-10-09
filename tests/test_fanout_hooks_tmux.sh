#!/usr/bin/env bash
# merge_train: tmux proof harness — prove the conflict hook FIRES for every
# CLI fanout runtime (claude / codex / gemini / cursor / opencode).
#
# Method (runtime-agnostic ground truth): each runtime is wired to invoke
# ~/.local/bin/conflict-warn-pre-tool.sh on file edits, which ALWAYS appends a
# line to /tmp/merge_train/<repo>/<branch>/hook-YYYY-MM-DD.log. We run each CLI
# non-interactively in its own tmux session against a throwaway git repo, ask it
# to edit one file, then assert that log grew. A grown log == the hook fired.
#
# We assert on the /tmp log (not on chat output) because it is the ONE signal
# that is identical across all five runtimes and independent of whether the CLI
# surfaces systemMessage in its UI.
#
# Usage:
#   tests/test_fanout_hooks_tmux.sh                 # all runtimes
#   tests/test_fanout_hooks_tmux.sh claude codex    # subset
#
# Each runtime gets a per-CLI timeout; an un-authable / hung CLI is reported
# SKIP (with the captured pane) rather than hanging the whole run.
set -uo pipefail

RUNTIMES=("$@")
[[ ${#RUNTIMES[@]} -eq 0 ]] && RUNTIMES=(claude codex gemini cursor opencode)

WRAP="$HOME/.local/bin/conflict-warn-pre-tool.sh"
PER_CLI_TIMEOUT="${PER_CLI_TIMEOUT:-150}"   # seconds per CLI
EVIDENCE_DIR="$(mktemp -d "/tmp/mt_fanout_proof.XXXXXX")"
PROMPT='Use your file-editing tool to create a file named hooktest.txt containing exactly the line: HOOKFIRE. Do not run any shell commands; use the edit/write tool only. Then stop.'

echo "merge_train fanout-hook proof harness"
echo "  wrapper:   $WRAP"
echo "  evidence:  $EVIDENCE_DIR"
echo "  timeout:   ${PER_CLI_TIMEOUT}s per CLI"
echo "  runtimes:  ${RUNTIMES[*]}"
echo

[[ -x "$WRAP" ]] || { echo "FATAL: wrapper not executable at $WRAP"; exit 2; }

# --- helpers ---------------------------------------------------------------- #

make_repo() {
  # A throwaway git repo (no remote). The wrapper's gh query just fails -> the
  # hook still fires and logs "gh CLI error; allowing" / "no other open PRs".
  local dir; dir="$(mktemp -d "/tmp/mt_repo_$1.XXXXXX")"
  git -C "$dir" init -q
  # Use the real noreply identity, not a *.example.com placeholder — the global
  # pre-commit guard blocks placeholder emails and would fail the seed commit.
  git -C "$dir" config user.email "jleechan2015@users.noreply.github.com"
  git -C "$dir" config user.name "jleechan2015"
  echo "seed" > "$dir/seed.txt"
  # Cursor's CLI only loads hooks from the PROJECT-level .cursor/hooks.json (the
  # global ~/.cursor/hooks.json is NOT read in headless mode — proven empirically:
  # global-only => preToolUse never fires; project-level copy => it fires). A real
  # installed cursor repo therefore carries its own .cursor/hooks.json (install.sh
  # writes it per-repo). Seed it here so the throwaway repo matches a real install.
  # Other runtimes (codex/gemini/opencode/claude) fire from their GLOBAL config, so
  # they need no per-repo seed.
  if [[ "$1" == cursor ]]; then
    mkdir -p "$dir/.cursor"
    "${PYTHON_BIN:-python3}" \
      "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/merge_train/hooks/wire_hook_config.py" \
      --config "$dir/.cursor/hooks.json" --event preToolUse \
      --command "bash $WRAP" --style cursor >/dev/null 2>&1
  fi
  git -C "$dir" add -A && git -C "$dir" commit -qm "seed" >/dev/null 2>&1
  echo "$dir"
}

log_dir_for() {
  local repo="$1"; local name branch
  name="$(basename "$repo")"
  branch="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null || echo detached)"
  echo "/tmp/merge_train/${name}/${branch}"
}

count_log_lines() {
  local ld="$1"
  find "$ld" -name 'hook-*.log' -exec cat {} + 2>/dev/null | wc -l | tr -d ' '
}

run_tmux() {
  # $1=session name  $2=workdir  $3=command-string  -> returns when session dies
  local sess="$1" wd="$2" cmd="$3"
  tmux kill-session -t "$sess" 2>/dev/null
  # bash -c (NOT -lc): avoid re-sourcing the user's interactive profile, which
  # can hang a non-interactive spawn. Inherit PATH from the current env (which
  # already has the CLIs) by exporting it into the session. stdin from /dev/null
  # so no CLI blocks waiting on input.
  tmux new-session -d -s "$sess" -c "$wd" -e "PATH=$PATH" \
    "bash -c '($cmd) </dev/null; echo MT_CLI_EXIT=\$? > $EVIDENCE_DIR/$sess.exit'"
  local waited=0
  while tmux has-session -t "$sess" 2>/dev/null; do
    sleep 3; waited=$((waited+3))
    if [[ $waited -ge $PER_CLI_TIMEOUT ]]; then
      tmux capture-pane -p -t "$sess" > "$EVIDENCE_DIR/$sess.pane" 2>/dev/null
      tmux kill-session -t "$sess" 2>/dev/null
      return 124
    fi
  done
  tmux capture-pane -p -t "$sess" > "$EVIDENCE_DIR/$sess.pane" 2>/dev/null
  return 0
}

# Per-runtime non-interactive command builders. Each edits hooktest.txt.
cli_cmd() {
  case "$1" in
    claude)   echo "claude --dangerously-skip-permissions -p \"$PROMPT\"" ;;
    # --dangerously-bypass-hook-trust: editing ~/.codex/hooks.json invalidates
    # codex's persisted hook-trust hash, so an automated run needs this to fire
    # the PreToolUse hook. Real users approve the trust prompt once interactively.
    codex)    echo "codex exec --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust --skip-git-repo-check \"$PROMPT\"" ;;
    gemini)   echo "gemini --yolo -p \"$PROMPT\"" ;;
    # cursor-agent needs -p (print/headless) for hooks to run; -f alone launches
    # the interactive TUI which does not execute preToolUse. --force auto-approves.
    cursor)   echo "cursor-agent -p --force --output-format text \"$PROMPT\"" ;;
    opencode) echo "opencode run \"$PROMPT\"" ;;
    *)        echo "" ;;
  esac
}

# --- run -------------------------------------------------------------------- #

declare -A RESULT
declare -A DELTA

for rt in "${RUNTIMES[@]}"; do
  echo "──────── $rt ────────"
  repo="$(make_repo "$rt")"
  ld="$(log_dir_for "$repo")"
  before="$(count_log_lines "$ld")"
  cmd="$(cli_cmd "$rt")"
  if [[ -z "$cmd" ]]; then RESULT[$rt]="SKIP(unknown runtime)"; continue; fi

  echo "  repo:    $repo"
  echo "  logdir:  $ld  (before=$before)"
  echo "  running: $rt (≤${PER_CLI_TIMEOUT}s)…"
  run_tmux "mt_$rt" "$repo" "$cmd"; rc=$?

  after="$(count_log_lines "$ld")"
  delta=$((after - before))
  DELTA[$rt]=$delta
  edited="no"; [[ -f "$repo/hooktest.txt" ]] && edited="yes"

  echo "  logdir after=$after (Δ=$delta)  hooktest.txt created=$edited  cli_rc=$rc"
  if [[ $delta -gt 0 ]]; then
    RESULT[$rt]="PASS (hook fired, Δ=$delta log lines)"
    # show the firing line(s) as evidence
    find "$ld" -name 'hook-*.log' -exec grep -h "merge_train" {} + 2>/dev/null | tail -3 | sed 's/^/    log| /'
  elif [[ $rc -eq 124 ]]; then
    RESULT[$rt]="SKIP (timeout — see $EVIDENCE_DIR/mt_$rt.pane)"
  else
    RESULT[$rt]="FAIL (no log growth; edited=$edited; pane: $EVIDENCE_DIR/mt_$rt.pane)"
  fi
  echo
done

# --- matrix ----------------------------------------------------------------- #
echo "════════ RESULT MATRIX ════════"
pass=0; total=0
for rt in "${RUNTIMES[@]}"; do
  total=$((total+1))
  r="${RESULT[$rt]:-?}"
  printf '  %-9s %s\n' "$rt" "$r"
  [[ "$r" == PASS* ]] && pass=$((pass+1))
done
echo "  ───────────────────────────"
echo "  $pass/$total runtimes proven firing"
echo "  evidence dir: $EVIDENCE_DIR"
[[ $pass -eq $total ]]
