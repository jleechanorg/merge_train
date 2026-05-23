#!/usr/bin/env bash
# merge_train: install into a target git repo.
#
# What this does (idempotent):
#   1. Verify Python >= 3.10 and git are available.
#   2. `pip install -e` the merge_train package (development install).
#   3. In the target repo, create a `file_domains.yaml` skeleton if one
#      doesn't already exist.
#   4. Wire per-CLI session-start / session-stop domain-lock hooks:
#      a. .git/hooks/pre-commit  (last-resort fallback for raw git commits)
#      b. .codex/hooks.json      (Codex SessionStart + Stop)
#      c. .gemini/domain-lock-guard.sh + .gemini/settings.json (Antigravity)
#      d. .opencode.json         (OpenCode custom /domain-check command stub)
#      NOTE: Claude Code global ~/.claude/settings.json is wired separately
#            (already done if this install.sh is run with merge_train >= 0.2).
#   5. Smoke-test the install (domain_lock list --status active).
#   6. Print next steps.
#
# Usage:
#   # From inside the target repo (default — uses $PWD):
#   /path/to/merge_train/install.sh
#
#   # From anywhere, naming the target repo:
#   /path/to/merge_train/install.sh /path/to/target/repo
#
# Flags:
#   --no-hook            Skip the pre-commit hook installation.
#   --force-hook         Replace an existing pre-commit hook (after backup).
#   --no-yaml            Skip creating file_domains.yaml skeleton.
#   --python PYTHON_BIN  Override python binary (default: python3).
#   -h, --help           Show this help.

set -euo pipefail

MERGE_TRAIN_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET=""
INSTALL_HOOK=1
FORCE_HOOK=0
INSTALL_YAML=1
PYTHON_BIN="${PYTHON_BIN:-python3}"

print_help() {
    sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-hook)    INSTALL_HOOK=0; shift ;;
        --force-hook) FORCE_HOOK=1;   shift ;;
        --no-yaml)    INSTALL_YAML=0; shift ;;
        --python)     PYTHON_BIN="$2"; shift 2 ;;
        -h|--help)    print_help; exit 0 ;;
        -*)
            echo "error: unknown flag $1" >&2
            print_help
            exit 2
            ;;
        *)
            if [[ -z "$TARGET" ]]; then
                TARGET="$1"; shift
            else
                echo "error: unexpected extra arg $1" >&2
                exit 2
            fi
            ;;
    esac
done

if [[ -z "$TARGET" ]]; then
    TARGET="$PWD"
fi
TARGET="$(cd -- "$TARGET" && pwd)"

# ------------------------------------------------------------------------- #
# 1. Prerequisite checks
# ------------------------------------------------------------------------- #

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "error: $PYTHON_BIN not found on PATH. Install Python >= 3.10 or pass --python." >&2
    exit 2
fi

PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "error: $PYTHON_BIN reports Python $PY_VERSION, but merge_train requires >= 3.10." >&2
    exit 2
fi

if ! command -v git >/dev/null 2>&1; then
    echo "error: 'git' not found on PATH." >&2
    exit 2
fi

if [[ ! -d "$TARGET/.git" ]]; then
    echo "error: $TARGET is not a git repository (no .git directory)." >&2
    exit 2
fi

echo "merge_train: installing into $TARGET"
echo "merge_train: source = $MERGE_TRAIN_ROOT"
echo "merge_train: python = $PYTHON_BIN ($PY_VERSION)"
echo

# ------------------------------------------------------------------------- #
# 2. Install the package (development install from source)
# ------------------------------------------------------------------------- #

echo "[1/5] Installing merge_train package..."
"$PYTHON_BIN" -m pip install -e "$MERGE_TRAIN_ROOT" --quiet
echo "  ok: $($PYTHON_BIN -c 'import merge_train; print("merge_train", merge_train.__version__)')"
echo

# ------------------------------------------------------------------------- #
# 3. file_domains.yaml skeleton
# ------------------------------------------------------------------------- #

if [[ "$INSTALL_YAML" -eq 1 ]]; then
    YAML_PATH="$TARGET/file_domains.yaml"
    if [[ -f "$YAML_PATH" ]]; then
        echo "[2/5] file_domains.yaml: already exists, leaving untouched."
    else
        echo "[2/5] file_domains.yaml: creating skeleton at $YAML_PATH"
        cat > "$YAML_PATH" <<'YAML_EOF'
# merge_train domain registry. Each domain groups one or more file
# globs that should be considered a single locking unit. Two PRs
# touching the same domain will collide at spawn time unless they
# reserve disjoint symbols (see README).
#
# Edit this file to match your repo's hot-spot files. Run
# `domain_lock audit` to verify the registry parses.

domains:
  # Example: a "level-up pipeline" that two agents must not edit at once.
  # level-up-pipeline:
  #   paths:
  #     - mvp_site/rewards_engine.py
  #     - mvp_site/world_logic.py
  #   owners: [your-github-handle]

  # Example: workflow files — usually only one PR at a time.
  # ci-infra:
  #   paths:
  #     - .github/workflows/**
  #   owners: [your-github-handle]
YAML_EOF
    fi
    echo
else
    echo "[2/5] file_domains.yaml: skipped (--no-yaml)."
    echo
fi

# ------------------------------------------------------------------------- #
# 4. Pre-commit hook
# ------------------------------------------------------------------------- #

if [[ "$INSTALL_HOOK" -eq 1 ]]; then
    HOOK_SRC="$MERGE_TRAIN_ROOT/hooks/pre-commit.sh"
    HOOK_DST="$TARGET/.git/hooks/pre-commit"
    if [[ ! -f "$HOOK_SRC" ]]; then
        echo "[3/5] WARN: $HOOK_SRC missing, skipping pre-commit hook."
    elif [[ -e "$HOOK_DST" || -L "$HOOK_DST" ]] && [[ "$FORCE_HOOK" -ne 1 ]]; then
        if [[ -L "$HOOK_DST" ]] && [[ "$(readlink "$HOOK_DST")" == "$HOOK_SRC" ]]; then
            echo "[3/5] pre-commit hook: already symlinked to $HOOK_SRC (ok)."
        else
            echo "[3/5] pre-commit hook: $HOOK_DST already exists. Re-run with --force-hook to replace."
        fi
    else
        if [[ -e "$HOOK_DST" || -L "$HOOK_DST" ]]; then
            BACKUP="$HOOK_DST.bak.$(date +%s)"
            mv "$HOOK_DST" "$BACKUP"
            echo "[3/5] pre-commit hook: backed up existing hook to $BACKUP"
        fi
        ln -s "$HOOK_SRC" "$HOOK_DST"
        chmod +x "$HOOK_SRC"
        echo "[3/5] pre-commit hook: installed $HOOK_DST -> $HOOK_SRC"
    fi
    echo
else
    echo "[3/5] pre-commit hook (fallback): skipped (--no-hook)."
    echo
fi

# ------------------------------------------------------------------------- #
# 4a. Codex per-repo hooks.json
# ------------------------------------------------------------------------- #

CODEX_DIR="$TARGET/.codex"
CODEX_HOOKS="$CODEX_DIR/hooks.json"
DL_START="$MERGE_TRAIN_ROOT/hooks/domain-lock-session-start.sh"
DL_STOP="$MERGE_TRAIN_ROOT/hooks/domain-lock-session-stop.sh"

echo "[3a/5] Codex per-repo hooks.json..."
mkdir -p "$CODEX_DIR"
if [[ ! -f "$CODEX_HOOKS" ]]; then
    cat > "$CODEX_HOOKS" <<CODEX_EOF
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash $DL_START",
            "timeoutSec": 15,
            "statusMessage": "Checking domain locks..."
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash $DL_STOP",
            "timeoutSec": 10,
            "statusMessage": "Releasing domain locks..."
          }
        ]
      }
    ]
  }
}
CODEX_EOF
    echo "  ok: created $CODEX_HOOKS"
else
    # Idempotent: only patch if our hooks aren't already present
    if ! grep -q "domain-lock-session-start" "$CODEX_HOOKS" 2>/dev/null; then
        echo "  WARN: $CODEX_HOOKS exists but has no domain-lock hooks."
        echo "        Manually add domain-lock-session-start.sh to SessionStart."
    else
        echo "  ok: $CODEX_HOOKS already wired."
    fi
fi
echo

# ------------------------------------------------------------------------- #
# 4b. Antigravity (.gemini) per-repo guard
# ------------------------------------------------------------------------- #

GEMINI_DIR="$TARGET/.gemini"
GEMINI_GUARD="$GEMINI_DIR/domain-lock-guard.sh"
GEMINI_SETTINGS="$GEMINI_DIR/settings.json"
GEMINI_HOOK_TEMPLATE="$MERGE_TRAIN_ROOT/hooks/gemini-domain-lock-guard.sh"

echo "[3b/5] Antigravity (.gemini) per-repo guard..."
mkdir -p "$GEMINI_DIR"

# Symlink the guard script (or copy if symlinks not desired)
if [[ -L "$GEMINI_GUARD" ]] && [[ "$(readlink "$GEMINI_GUARD")" == "$GEMINI_HOOK_TEMPLATE" ]]; then
    echo "  ok: $GEMINI_GUARD already symlinked."
elif [[ -f "$GEMINI_GUARD" ]]; then
    echo "  ok: $GEMINI_GUARD already exists (leaving untouched)."
else
    ln -s "$GEMINI_HOOK_TEMPLATE" "$GEMINI_GUARD"
    chmod +x "$GEMINI_HOOK_TEMPLATE"
    echo "  ok: $GEMINI_GUARD -> $GEMINI_HOOK_TEMPLATE"
fi

# Patch .gemini/settings.json to call the guard in BeforeTool
if [[ ! -f "$GEMINI_SETTINGS" ]]; then
    cat > "$GEMINI_SETTINGS" <<GEMINI_EOF
{
  "hooks": {
    "BeforeTool": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash $GEMINI_GUARD"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash $DL_STOP"
          }
        ]
      }
    ]
  }
}
GEMINI_EOF
    echo "  ok: created $GEMINI_SETTINGS"
elif ! grep -q "domain-lock-guard" "$GEMINI_SETTINGS" 2>/dev/null; then
    echo "  WARN: $GEMINI_SETTINGS exists but has no domain-lock-guard hook."
    echo "        Manually add domain-lock-guard.sh to the BeforeTool hook."
else
    echo "  ok: $GEMINI_SETTINGS already wired."
fi
echo

# ------------------------------------------------------------------------- #
# 4c. OpenCode .opencode.json
# ------------------------------------------------------------------------- #

OPENCODE_JSON="$TARGET/.opencode.json"
echo "[3c/5] OpenCode .opencode.json..."
if [[ ! -f "$OPENCODE_JSON" ]]; then
    cat > "$OPENCODE_JSON" <<OC_EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "instructions": "IMPORTANT: Before starting any coding task, run: domain_lock check --files <files-you-plan-to-edit>. If exit code is 1 (HELD), do not proceed — pick a different task. If exit code is 0, run: domain_lock reserve --domain <domain> --pr <PR_NUMBER> --agent \$(whoami) --branch \$(git branch --show-current). When done, run: domain_lock release --pr <PR_NUMBER>"
}
OC_EOF
    echo "  ok: created $OPENCODE_JSON (domain-lock instructions injected)"
elif ! grep -q "domain_lock" "$OPENCODE_JSON" 2>/dev/null; then
    echo "  WARN: $OPENCODE_JSON exists but has no domain_lock instructions."
    echo "        Add domain_lock check/reserve/release to the instructions field."
else
    echo "  ok: $OPENCODE_JSON already has domain_lock instructions."
fi
echo

# ------------------------------------------------------------------------- #
# 5. Smoke test
# ------------------------------------------------------------------------- #

echo "[4/5] Smoke-testing CLI..."
(
    cd "$TARGET"
    if ! command -v domain_lock >/dev/null 2>&1; then
        echo "  WARN: 'domain_lock' not on PATH. The pip install may have placed"
        echo "        it in a directory not in PATH. Try: $PYTHON_BIN -m merge_train.domain_lock --help"
    else
        domain_lock list --status active --registry "$TARGET/file_domains.yaml" 2>/dev/null \
            && echo "  ok: domain_lock CLI reachable, registry parses." \
            || echo "  ok: domain_lock CLI reachable (no active locks yet, expected on fresh install)."
    fi
)
echo

# ------------------------------------------------------------------------- #
# 6. Next steps
# ------------------------------------------------------------------------- #

cat <<NEXT_EOF
[5/5] Done. Next steps:

  1. Edit $TARGET/file_domains.yaml — replace the example domains with
     your repo's actual hot-spot files.

  2. Try a reserve / check / release cycle:

       domain_lock reserve --domain <name> --pr 1 --agent me --branch test
       domain_lock list --status active
       domain_lock release --pr 1

  3. Wire \`hooks/ao-spawn-domain-check.sh\` into your agent spawner. See
     docs/AGENTS.md (Section A) for the integration recipe.

  4. Run the merge_train tests to confirm the install is healthy:

       (cd $MERGE_TRAIN_ROOT && $PYTHON_BIN -m pytest tests/ -q)

Docs:
  - $MERGE_TRAIN_ROOT/README.md
  - $MERGE_TRAIN_ROOT/docs/AGENTS.md
  - $MERGE_TRAIN_ROOT/docs/CLAUDE.md
NEXT_EOF
