# AO Live Deployment Reference — `merge_train`

**Date:** 2026-06-02
**Bead:** [`orch-pw46`](https://github.com/jleechanorg/merge_train) (this doc) /
[`orch-66my`](https://github.com/jleechanorg/merge_train) (parent goal)
**Audience:** operators wiring Agent Orchestrator (AO) workers against a
repo that uses `merge_train predict-conflicts` as the conflict gate.
**Status:** docs only — no AO worker code is added or modified by this PR.

## 1. Overview

### 1.1 What `merge_train` does

`merge_train` is a **spawn-time conflict-prediction gate** for AI-agent
PR pipelines. Its single CLI is `predict-conflicts` (installed as
`/Users/jleechan/.local/bin/predict-conflicts` on this machine; the
console-script entry point is declared in `pyproject.toml`):

```bash
predict-conflicts --from-prs 100,101,103 --repo OWNER/REPO [--registry file_domains.yaml] [--json]
```

It loads the file list for each PR via `gh pr diff --name-only`, runs
**symbol-level** conflict detection (Python AST + Markdown headings) on
the registry's domain map, optionally augments with `git merge-tree`
for textual safety, and emits:

- the set of pairwise blocking conflicts,
- an approximate-maximum **parallel batch schedule** (greedy maximal
  independent set),
- a recommended **merge order** (peel off MIS batches iteratively).

Exit codes are `0` (no conflicts), `1` (≥1 conflict), `2` (plan/CLI
error). See `merge_train/predict.py` (`cli_predict_conflicts`,
`main`, ~line 702 / 827).

Domain locking (`domain_lock reserve/release/check`) was removed in
PR [#19](https://github.com/jleechanorg/merge_train/pull/19) — the
remaining conflict surface is *pure prediction* against declared PR
scopes, not a persistent lock log.

### 1.2 What Agent Orchestrator (AO) provides

Agent Orchestrator (`jleechanorg/agent-orchestrator`) is a tmux-backed
worker dispatcher. Operators configure one or more *projects* in
`~/.hermes/agent-orchestrator.yaml` and spawn worker sessions with
`ao spawn`. Each session is an isolated git worktree running a
configurable agent plugin (`claude-code`, `codex`, `antigravity`, …).

The critical operator invariant AO provides is **structured session
lifecycle**: `ao spawn` returns a session name (e.g. `mt-001`),
`ao session ls` enumerates live sessions, and `ao session kill`
releases them. This is what makes the conflict gate useful — without
it, "spawn N agents in parallel" is unbounded and collisions are
inevitable.

### 1.3 Why they integrate

`merge_train` is the *signal* (this PR's set of files is likely to
conflict with PR #X). AO is the *actuator* (spawn, monitor, kill,
batch). The integration point is the worker's **pre-spawn gate**: an
AO worker that edits `mvp_site/world_logic.py` should refuse to start
(or warn loudly) if `predict-conflicts` says another open PR is
already editing the same file or symbol.

This is what `scripts/e2e_ao_orchestrated_runner.py` already
demonstrates end-to-end: it reserves a synthetic slot, calls
`ao spawn --agent claude-code`, polls for PR creation, and then
releases. The remaining gap (per `orch-66my`) is the **operator-facing
reference doc** that says "here is the command, here are the env
vars, here is how to smoke-test it, here is what fails."

## 2. Concrete AO worker command

The minimum viable AO integration against a `merge_train`-gated repo:

```bash
# 0. One-time: ensure predict-conflicts is installed
pip install -e /Users/jleechan/projects/merge_train
which predict-conflicts   # should resolve

# 1. Configure the AO project (one time, in ~/.hermes/agent-orchestrator.yaml)
#    under `projects:` add an entry like:
#
#    merge_train_smoke:
#      name: merge_train smoke harness
#      repo: jleechanorg/mctrl_test
#      path: ~/projects/mctrl_test
#      defaultBranch: main
#      sessionPrefix: mt
#      workspace: worktree
#
# 2. Run `ao start merge_train_smoke` (assumes AO daemon not yet up)
#    (skip if `ao status` already shows the project)

# 3. Run the conflict-prediction gate as a standalone step
#    (this is what an AO worker would call BEFORE writing code).
#    Use --json so the worker can parse the response.
predict-conflicts \
  --from-prs 7000,7001,7002 \
  --repo jleechanorg/mctrl_test \
  --registry ~/projects/mctrl_test/file_domains.yaml \
  --git-cwd ~/projects/mctrl_test \
  --json
# Exit 0 = safe to proceed; exit 1 = conflicts; exit 2 = CLI error.

# 4. Spawn the AO worker, gated on the result
if predict-conflicts \
     --from-prs 7000,7001,7002 \
     --repo jleechanorg/mctrl_test \
     --registry ~/projects/mctrl_test/file_domains.yaml \
     --json > /tmp/predict.json 2>/dev/null; then

  ao spawn -p merge_train_smoke \
    "Edit mvp_site/rewards_engine.py to add the new dice formula. \
     DO NOT touch any file outside this single file."
else
  echo "predict-conflicts reports conflicts; refusing to spawn worker"
  jq '.pairwise_conflicts' /tmp/predict.json
  exit 1
fi
```

The `--claim-pr 7000` flag is the cleanest way to bind the AO session
to a specific PR so the worker can `git push` to its branch without
race conditions:

```bash
ao spawn -p merge_train_smoke --claim-pr 7000 \
  "Edit ONLY mvp_site/rewards_engine.py to add the new dice formula."
```

This pattern is what `scripts/e2e_ao_orchestrated_runner.py` runs in
production today (see its `_ao_spawn_slot` helper, line ~119). That
runner is the *real* proof-of-integration artifact — this doc is the
operator recipe.

> **Note on agent selection:** `ao spawn --agent` defaults come from
> `~/.hermes/agent-orchestrator.yaml`'s `defaults.agent` key. On this
> machine that resolves to `antigravity`. Override with
> `--agent codex` or `--agent claude-code` for explicit control.
> See the `ao-operator-discipline` skill for the full parameter-fidelity
> rules.

## 3. Environment variables

The `predict-conflicts` CLI itself reads only a few env vars; the
AO/hook surface around it uses more. The table below lists every env
var the integration touches, regardless of which component owns it.

| Env var | Owner | Default | Purpose |
|---|---|---|---|
| `MERGE_TRAIN_REPO` | merge_train hooks + docs | auto-detected from `git remote get-url origin` | `OWNER/REPO` passed to `gh pr diff` / `gh pr view` when symbol enrichment runs. Without it, `--from-prs` falls back to whatever the worker's `git remote` is. |
| `MERGE_TRAIN_REGISTRY` | merge_train hooks | `./file_domains.yaml` in repo root | Path to the YAML file→domain map. Required for symbol-level conflict detection; without it `predict-conflicts` runs in file-level-only mode. |
| `MERGE_TRAIN_PR` | merge_train hooks (pre-commit, pre-spawn) | unset | Override the PR number included in the open-PR set. Useful when the agent's PR was just opened and `gh pr list` may not have indexed it yet. |
| `MERGE_TRAIN_FILES` | `hooks/predict-spawn-check.sh` | unset | Space-separated list of files the agent plans to touch. If unset, the pre-spawn hook is a no-op. |
| `MERGE_TRAIN_LOG` | legacy hooks (kept for env-var compatibility) | n/a | Unused after PR #19 (domain locking removed). Present in env-var surface only; safe to ignore. |
| `MERGE_TRAIN_DIFF_MODE` | legacy pre-commit hook | `1` | Pre-#19 toggle. Documented here only because some agents still set it; the current pre-commit hook ignores it. |
| `AO_PROJECT_ID` | Agent Orchestrator | unset (auto-detected from cwd) | Set automatically by AO inside spawned sessions. Don't export it manually. |
| `GITHUB_TOKEN` | AO `plugins.scm-github` | n/a | Required by AO for any `--claim-pr` flow that hits the GitHub API. Must have `repo` scope. |
| `GITHUB_SERVER_URL` / `GITHUB_REPOSITORY` / `GITHUB_RUN_ID` | CI only | unset | Populated by GitHub Actions on runners; used by `e2e_ao_orchestrated_runner.py` to stamp evidence provenance. Irrelevant for local AO runs. |

**Minimum for an AO worker to call `predict-conflicts`:** `gh` CLI
authenticated + `MERGE_TRAIN_REPO` (or `--repo` flag) + a registry
file (or accept file-level-only mode).

## 4. Reproducible smoke test

A self-contained smoke test that runs the predict-conflicts gate
against the current `merge_train` repo (no AO worker, no real PRs —
uses a fixture pair of PR-shaped YAML plans). Designed to be runnable
without GitHub auth.

### 4.1 Inline shell script

Save as `scripts/smoke_ao_merge_train.sh` (or paste into a shell):

```bash
#!/usr/bin/env bash
# merge_train × AO smoke test (no live AO worker required).
#
# Verifies the predict-conflicts CLI:
#  - exit 0 on a non-conflicting fixture
#  - exit 1 on a conflicting fixture
#  - exit 2 on a malformed fixture
#  - JSON output is parseable and contains the expected fields
#
# No GitHub API calls; uses a temporary plan YAML so it's safe in CI.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$(mktemp -d -t merge_train_smoke.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

cd "$REPO_ROOT"

# ── 1. CLI resolves ────────────────────────────────────────────────────────
if ! command -v predict-conflicts >/dev/null 2>&1; then
  echo "FAIL: predict-conflicts not on PATH" >&2
  exit 1
fi
echo "OK: predict-conflicts resolves to $(command -v predict-conflicts)"

# ── 2. Non-conflicting plan exits 0 ────────────────────────────────────────
cat > "$TMP_DIR/plan_ok.yaml" <<'YAML'
prs:
  - pr: 9001
    branch: feat/safe-a
    files: [merge_train/__init__.py]
    symbols: {merge_train/__init__.py: [__version__]}
  - pr: 9002
    branch: feat/safe-b
    files: [merge_train/predict.py]
    symbols: {merge_train/predict.py: [Plan]}
YAML

set +e
predict-conflicts --plan "$TMP_DIR/plan_ok.yaml" --no-textual --json > "$TMP_DIR/ok.json" 2>"$TMP_DIR/ok.err"
OK_RC=$?
set -e

if [[ "$OK_RC" -ne 0 ]]; then
  echo "FAIL: expected exit 0 on non-conflicting plan, got $OK_RC" >&2
  cat "$TMP_DIR/ok.err" >&2
  exit 1
fi
echo "OK: non-conflicting plan exit=0"

# Validate JSON shape
python3 - <<'PY' "$TMP_DIR/ok.json"
import json, sys
plan = json.load(open(sys.argv[1]))
assert plan["input_prs"] == [9001, 9002], plan["input_prs"]
assert plan["pairwise_conflicts"] == [], plan["pairwise_conflicts"]
assert len(plan["parallel_batches"]) >= 1
print("OK: JSON shape valid (input_prs, pairwise_conflicts=[], parallel_batches present)")
PY

# ── 3. Conflicting plan exits 1 ────────────────────────────────────────────
cat > "$TMP_DIR/plan_conflict.yaml" <<'YAML'
prs:
  - pr: 9003
    branch: feat/whole-a
    files: [merge_train/predict.py]   # whole-file, no symbols = whole-domain lock
  - pr: 9004
    branch: feat/whole-b
    files: [merge_train/predict.py]   # also whole-file = guaranteed conflict
YAML

set +e
predict-conflicts --plan "$TMP_DIR/plan_conflict.yaml" --no-textual --json > "$TMP_DIR/conflict.json" 2>"$TMP_DIR/conflict.err"
CONFLICT_RC=$?
set -e

if [[ "$CONFLICT_RC" -ne 1 ]]; then
  echo "FAIL: expected exit 1 on conflicting plan, got $CONFLICT_RC" >&2
  cat "$TMP_DIR/conflict.err" >&2
  exit 1
fi
echo "OK: conflicting plan exit=1"

# ── 4. Malformed plan exits 2 ─────────────────────────────────────────────
echo "not-a-yaml-mapping: [" > "$TMP_DIR/plan_bad.yaml"

set +e
predict-conflicts --plan "$TMP_DIR/plan_bad.yaml" --no-textual --json > /dev/null 2>"$TMP_DIR/bad.err"
BAD_RC=$?
set -e

if [[ "$BAD_RC" -ne 2 ]]; then
  echo "FAIL: expected exit 2 on malformed plan, got $BAD_RC" >&2
  cat "$TMP_DIR/bad.err" >&2
  exit 1
fi
echo "OK: malformed plan exit=2"

echo
echo "=== SMOKE TEST PASSED ==="
echo "predict-conflicts behaves correctly across all 3 exit codes."
```

### 4.2 Manual mode (real GitHub + real AO)

If you want to exercise the *full* AO + predict-conflicts loop with
real PRs and a real `ao spawn` session, follow this checklist. Mark
this section "manual" — it requires GitHub auth, AO daemon running,
and at least 2 open PRs on the target repo.

**Prereqs (verify before each step):**

```bash
# 1. AO daemon is up
ao status    # should list the project

# 2. gh is authenticated
gh auth status

# 3. predict-conflicts is on PATH
which predict-conflicts

# 4. At least 2 open PRs exist on the target repo
gh pr list --repo jleechanorg/mctrl_test --state open --json number
```

**Run the loop:**

```bash
# 1. Collect open PR numbers
PRS="$(gh pr list --repo jleechanorg/mctrl_test --state open --json number --jq '.[].number' | tr '\n' ',' | sed 's/,$//')"
echo "Open PRs: $PRS"

# 2. Run the gate (manual inspection: do you see any conflicts?)
predict-conflicts \
  --from-prs "$PRS" \
  --repo jleechanorg/mctrl_test \
  --registry ~/projects/mctrl_test/file_domains.yaml \
  --json | jq '.pairwise_conflicts | length'
# 0  = no conflicts; proceed.
# >0 = conflicts; do not spawn a worker that would add to the same domains.

# 3. Spawn a worker
SESSION="$(ao spawn -p merge_train_smoke \
  --claim-pr "$(echo "$PRS" | cut -d, -f1)" \
  'Edit ONLY mvp_site/rewards_engine.py — touch no other file.' \
  | tee /dev/stderr | grep -oE 'mt-[0-9]+' | head -1)"
echo "Spawned: $SESSION"

# 4. Watch it
ao session ls
ao send "$SESSION" "Status?"

# 5. Clean up (NP6: only when done)
ao session kill "$SESSION" --keep-session
```

**What to check:**

| Check | Pass criterion |
|---|---|
| `predict-conflicts` returns 0 when no PR pair overlaps | `echo $?` = 0 |
| `predict-conflicts` returns 1 when at least one pair overlaps | `echo $?` = 1 |
| `ao spawn` echoes a `SESSION=mt-NNN` line | grep `mt-` in stdout |
| `ao session ls` shows the session within 30 s | `ao session ls \| grep $SESSION` |
| Worker PR has *only* the file the task named | `gh pr view <N> --json files` |
| `ao session kill` removes the session | `ao session ls \| grep $SESSION` returns nothing |

## 5. Failure modes

Five common failure modes, what they look like, and how to debug.

### 5.1 `predict-conflicts: error: could not load requested PRs: <N>`

**Symptom:** exit code 2, stderr contains `error: could not load requested PRs: 7003`.

**Cause:** `gh pr diff --name-only 7003 --repo OWNER/REPO` returned non-zero. Usually one of:

- PR number is wrong (typo, or PR is closed/merged).
- `gh` is not authenticated (`gh auth status` will say "not logged in").
- `MERGE_TRAIN_REPO` / `--repo` resolves to a repo you don't have read access to.

**Debug:**

```bash
gh pr view 7003 --repo OWNER/REPO --json number  # does the PR exist?
gh auth status                                    # are you logged in?
gh pr diff 7003 --repo OWNER/REPO --name-only    # does the diff command work standalone?
```

### 5.2 `gh: command not found` warning, then no PRs analyzed

**Symptom:** `predict-conflicts` exits 0 with no conflicts, but a warning was printed: `warning: gh CLI not found; skipping PR#N`.

**Cause:** `gh` is not on the AO worker's `PATH`. The CLI degrades to "no specs loaded" → empty plan → 0 conflicts. This is a *silent failure* — the gate says "go" when it should say "I don't know."

**Debug:** ensure `gh` is installed and on PATH in the AO worker's tmux session. From the worker shell: `which gh && gh --version`.

### 5.3 `error: --enrich-symbols requires --repo OWNER/REPO`

**Symptom:** exit code 2, stderr contains the `--enrich-symbols` / `--repo` complaint.

**Cause:** The symbol-enrichment path needs an explicit `--repo`. It tries to auto-detect from `git remote`, but the AO worker is in a fresh worktree whose remote may not match the production repo, or auto-detection failed (e.g. no `origin` remote).

**Debug:**

```bash
# Inside the worker's git working dir:
git remote -v
# If empty or wrong, set --repo OWNER/REPO explicitly or fix the remote.
```

Pass `--repo OWNER/REPO` on the command line or set `MERGE_TRAIN_REPO=OWNER/REPO` in the AO worker's environment.

### 5.4 AO spawn returns no `SESSION=` line / silent timeout

**Symptom:** `ao spawn -p ... "..."` returns exit 0 but no `mt-NNN` session name appears in stdout.

**Cause (most likely):** the AO daemon is not running for that project. `ao spawn` will queue the work, not fail loudly. Check:

```bash
ao status
# If your project is not listed, run:
ao start merge_train_smoke
```

**Other causes:**

- `--agent` resolved to a plugin that isn't installed. Check
  `~/.local/bin` for `agy`, `claude`, `codex`, etc. Run
  `ao doctor` for a full check.
- The `kanban.max_spawn` cap is hit (default 8 active sessions).
  Wait for a session to complete or run `ao session ls` and kill
  the oldest. See the `ao-spawn-gate` skill.

### 5.5 Worker spawns, but PR isolation fails (worker touches forbidden files)

**Symptom:** `ao spawn` succeeds, the worker creates a PR, but `gh pr view --json files` shows files outside the task scope.

**Cause:** the task string did not enumerate a *single-file allowlist*, **or** the worker chose to ignore the constraint (ZFC violation in the prompt, or agent plugin has weak instruction-following).

**Debug:**

1. Tighten the task string: `Edit ONLY mvp_site/foo.py. DO NOT modify, create, or delete ANY other file. If you discover a need to touch a second file, ABORT and report it back.`
2. Use the repo's `--registry file_domains.yaml` so the worker (and the gate) have a *machine-checkable* definition of "the domain this task is allowed to modify."
3. Add a post-merge check: `gh pr view <N> --json files | jq -r '.files[].path' | comm -23 - <(echo mvp_site/foo.py)` should be empty.

This is the failure mode the `e2e_ao_orchestrated_runner.py` proof is
specifically designed to detect — see its `ao_pr_isolation` scenario.

## 6. Limitations

This doc explicitly does **not** cover:

- **Real concurrent multi-agent collision.** Predict-conflicts is
  *advisory* — it tells you two PRs *would* conflict. It does not
  prevent two AO workers from spawning in parallel and both
  editing the same file before either hits the gate. The gate
  must run **before** `ao spawn` to be meaningful. The
  `e2e_ao_orchestrated_runner.py` proof serializes reservations
  for this reason; production AO orchestration must do the same
  (and AO's worktree-per-session default helps — different
  worktrees can't push to the same branch, but they can still
  create competing PRs against the same files).
- **Recovery from a corrupt lock log.** There is no lock log.
  Domain locking was removed in PR
  [#19](https://github.com/jleechanorg/merge_train/pull/19);
  `predict-conflicts` is pure read-only analysis of declared PR
  scopes. There is nothing to corrupt and nothing to recover.
- **Real-time race between `predict-conflicts` and the next push.**
  The gate answers the question "do these PRs *currently*
  conflict?" Between the gate running and the agent pushing,
  another PR may have merged or been opened. The recommended
  mitigation is to re-run the gate as a CI check on the PR
  itself (a `predict-conflicts --from-prs <this,open>` step
  inside the PR's `actions` workflow). This doc does not
  describe that wiring — see the `e2e_pairwise_merge_tree.py`
  and `e2e_sequential_merge_tree.py` scripts in this repo for
  the local equivalent.
- **Non-Python / non-Markdown symbol extraction.** Symbol
  enrichment only auto-resolves Python (AST) and Markdown
  (heading-based) files. Other extensions are treated as
  whole-file edits (fail-closed). TypeScript and Go symbol
  extractors exist (per
  [#17](https://github.com/jleechanorg/merge_train/pull/17)) but
  require explicit opt-in paths that this doc does not describe.
- **AO worker code changes.** This PR is documentation only.
  No AO worker implementation is added, modified, or
  recommended for upstreaming. The `e2e_ao_orchestrated_runner.py`
  script remains the canonical proof-of-integration artifact;
  this doc is the operator-facing reference that points at it.
- **`ao --model` overrides and one-shot model swaps.** Out of
  scope. See the `ao-model-override` skill if a specific
  spawn needs a different model than `defaults.agent` configures.

## 7. Related artifacts

- **Beads:** [`orch-pw46`](https://github.com/jleechanorg/merge_train)
  (this doc), [`orch-66my`](https://github.com/jleechanorg/merge_train)
  (parent goal — real AO spawn/release proof).
- **Source of truth:** `merge_train/predict.py` (CLI at `main()`,
  ~line 827; core algorithm at `predict_conflicts`, ~line 556;
  `cli_predict_conflicts` at ~line 702).
- **AO spawn contract:** `ao spawn -p <project> [--agent X]
  [--claim-pr N] [--runtime Y] "task string"`. See
  [`docs/CLI.md`](https://github.com/jleechanorg/agent-orchestrator/blob/main/docs/CLI.md)
  in the AO fork.
- **E2E proof:** `scripts/e2e_ao_orchestrated_runner.py` — the
  existing orchestration proof against `jleechanorg/mctrl_test`.
  See `docs/e2e_area_lock_proof.md` for the bundle written by
  that runner.
- **Pre-spawn hook:** `hooks/predict-spawn-check.sh` — the
  non-blocking warning hook operators wire into their spawner
  (warns on stderr, never refuses a spawn).
- **Pre-commit hook:** `hooks/pre-commit.sh` — local guard
  variant for the same gate.
- **AO config example:** `agent-orchestrator` projects entry
  for `merge_train` lives at
  `~/.hermes/agent-orchestrator.yaml` under `projects:`.
