# merge_train

[![tests](https://github.com/jleechanorg/merge_train/actions/workflows/tests.yml/badge.svg)](https://github.com/jleechanorg/merge_train/actions/workflows/tests.yml)
[![Python ≥ 3.10](https://img.shields.io/badge/python-%E2%89%A53.10-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Spawn-time file-domain lock registry for AI-agent PR pipelines.

Stops two agents from grabbing the same files when they're spawned in parallel — before either writes a line of code. Symbol-level locks let two agents edit disjoint functions inside the same file.

## Why

AI-agent PR pipelines (Aider, OpenHands, Devin, custom AO setups) spawn many agents in parallel against the same repo. They collide:

- Two agents edit `mvp_site/world_logic.py` → one rebases or gets dropped at merge time.
- Conflict surfaces after both agents have burned tokens.
- Existing tools (Mergify, Graphite, jj, ghstack) all work at **merge time** or **commit time**, not **spawn time**.

`merge_train` puts the gate at spawn time: resolve every file to a lock scope, reserve that scope when an agent is spawned, refuse spawn if held.

Production use should cover **all files**. The registry is not meant to be a partial list of interesting YAML entries; it is the policy that decides which edits can safely run together. Use explicit domains for known hot spots and a final catch-all domain for everything else until automatic per-file fallback lands.

## Prior art

| Tool | Where it acts | What it does |
|---|---|---|
| Mergify | merge time | rule-based merge queue |
| Graphite / ghstack | commit time | stacked PR cascade |
| jj (Jujutsu) | commit time | conflict-tolerant rebase |
| Uber SubmitQueue | merge time | speculative-tree CI batching |
| Aviator MergeQueue | merge time | affected-targets parallel queues |
| OpenHands Large Codebase SDK | spawn time | dep-graph partitioning (intra-SDK) |
| **merge_train** | **spawn time** | **declarative file→domain registry (any pipeline)** |

## Install

**Into another repo (recommended):**

```bash
git clone https://github.com/jleechanorg/merge_train.git ~/merge_train
cd /path/to/your/repo
~/merge_train/install.sh
```

`install.sh` is idempotent: it `pip install -e`s the package, drops a starter `file_domains.yaml` skeleton, symlinks the pre-commit hook into `.git/hooks/pre-commit`, and smoke-tests the CLI. The skeleton is not production-complete until you add your real domains plus a final catch-all domain. Flags: `--no-hook`, `--no-yaml`, `--force-hook`, `--python <bin>`.

**Dev install (working on `merge_train` itself):**

```bash
git clone https://github.com/jleechanorg/merge_train.git
cd merge_train
pip install -e '.[dev]'
python -m pytest tests/ -q   # 202 passed
```

Requires Python ≥ 3.10, `PyYAML`, and `git` on `PATH`.

## CLI surface

```
domain_lock reserve            reserve a domain for a PR/agent (--symbols for sub-file locks)
domain_lock reserve-plan       atomically reserve multiple (domain, symbols) legs for one PR
domain_lock release            release a PR's reservations
domain_lock check              check files against active reservations (--diff-mode for symbol res.)
domain_lock list               list locks (active|released|all)
domain_lock audit              dump full registry + lock-log audit JSON
domain_lock predict-conflicts  dry-run: predict pairwise conflicts + recommend merge order
                               for a set of PRs declared in a YAML plan
```

Global flags work **both** before and after the subcommand:

```
--registry FILE   YAML file→domain map (default: ./file_domains.yaml, env: MERGE_TRAIN_REGISTRY)
--log FILE        JSONL append-only lock log (default: ~/.merge_train/locks/<repo-hash>/, env: MERGE_TRAIN_LOG)
--git-cwd DIR     git working tree used to resolve the default log path
```

## Quick start

```bash
# 1. Declare domains
cat > file_domains.yaml <<EOF
domains:
  level_up_core:
    paths:
      - mvp_site/rewards_engine.py
      - mvp_site/world_logic.py
    owners: [jleechan2015]

  # Required for production-style fail-closed coverage:
  # keep this LAST so more specific domains win first.
  all_other_files:
    paths:
      - "*"
EOF

# 2. Reserve before spawning an agent (whole-domain lock)
domain_lock reserve --domain level_up_core \
  --pr 6926 --agent claude-1 --branch feat/level-up

# 3. Another agent checks before being spawned
domain_lock check --files mvp_site/world_logic.py --pr 7000
# exit 0 = free, 1 = held (prints holder PR + agent)

# 4. List active reservations
domain_lock list --status active

# 5. Release after merge
domain_lock release --pr 6926
```

`all_other_files` is intentionally conservative: every otherwise-unmapped file shares one fallback domain. That avoids silent gaps, but it can over-block unrelated work. The planned `acquire --files` command should replace this with automatic per-file fallback locks such as `file:README.md`, while still honoring explicit grouped domains.

## Agent lock contract

Agents should not only run `check`. `check` is a read-only gate; it does not acquire anything. The orchestrator must reserve before the agent starts writing, and release when the PR merges, aborts, or is abandoned.

Current supported flow:

```bash
# 1. Orchestrator predicts the files/domains for the task.
# 2. Reserve one domain:
domain_lock reserve --domain level_up_core \
  --pr 7000 --agent codex-1 --branch feat/foo

# Or reserve several domains atomically:
cat > /tmp/merge_train_plan.yaml <<EOF
plan:
  - domain: level_up_core
  - domain: all_other_files
EOF

domain_lock reserve-plan --pr 7000 --agent codex-1 \
  --branch feat/foo --plan /tmp/merge_train_plan.yaml

# 3. Spawn the agent only if reserve/reserve-plan exits 0.
# 4. Release when done:
domain_lock release --pr 7000
```

Target flow for the next integration release:

```bash
domain_lock acquire --files mvp_site/world_logic.py README.md \
  --pr 7000 --agent codex-1 --branch feat/foo
```

`acquire` should resolve mapped files to registry domains, resolve unmapped files to deterministic per-file fallback locks, and reserve all required scopes atomically.

## Symbol-level locks (sub-file granularity)

Two PRs can co-edit the *same file* if they touch *disjoint Python symbols*. Lock the symbols you'll modify, not the whole file:

```bash
# PR #6926 reserves just `level_up()` in world_logic.py
domain_lock reserve --domain level_up_core \
  --symbols level_up,_apply_xp_bonus \
  --pr 6926 --agent claude-1 --branch feat/level-up

# PR #7000 can still claim `compute_dice()` in the same domain
domain_lock reserve --domain level_up_core \
  --symbols compute_dice \
  --pr 7000 --agent claude-2 --branch feat/dice-fix
```

At commit time, `check --diff-mode` resolves the *staged diff* down to Python symbols actually touched and matches them against active reservations:

```bash
git add mvp_site/world_logic.py
domain_lock check --files mvp_site/world_logic.py --pr 7000 --diff-mode
# Only refuses if your staged diff touches symbols reserved by another PR.
# Non-Python files, parse errors, and missing AST fall back fail-closed
# to the whole-domain lock (printed as `WARN: symbol-resolution fallback`).
```

## Dry-run conflict prediction (`predict-conflicts`)

Replay a set of PRs through `merge_train` to see how they'd merge, what order to land them, and what conflicts to expect — before any agent spawns.

```yaml
# prs.yaml
prs:
  - pr: 100
    branch: feat/level-up
    files: [mvp_site/world_logic.py]
    symbols: {mvp_site/world_logic.py: [level_up]}
  - pr: 101
    branch: feat/dice-fix
    files: [mvp_site/world_logic.py]
    symbols: {mvp_site/world_logic.py: [compute_dice]}
  - pr: 103
    branch: feat/rewards-overhaul
    files: [mvp_site/rewards_engine.py, mvp_site/world_logic.py]  # whole-file
```

```bash
domain_lock predict-conflicts --plan prs.yaml --no-textual
```

Output:

```
2 pairwise conflict(s):
  PR#100 <-> PR#103: domain=level-up-pipeline (whole-domain)
  PR#101 <-> PR#103: domain=level-up-pipeline (whole-domain)

Parallel batches: [[100, 101], [103]]
Recommended order: [100, 101, 103]

Risk-reduction signal, not a merge guarantee. Run CI + human review before merging.
```

Drop `--no-textual` to additionally run `git merge-tree` between each pair and catch textual conflicts (imports, configs) that symbol analysis misses. `--json` emits machine-readable output.

The recommended-order algorithm is a greedy maximal-independent-set sweep on the symbol-domain conflict graph: pick the largest batch of disjoint PRs, peel them off, repeat. Deterministic, polynomial-time, and tie-broken by PR id for reproducibility.

Exit codes: `0` (no conflicts), `1` (at least one pair conflicts), `2` (plan file missing/malformed).

## Atomic multi-domain reservation (`reserve-plan`)

When one PR needs *several* domains, reserve them in one transaction. Either all legs are reserved or none — no partial state.

```yaml
# plan.yaml
plan:
  - domain: level_up_core
    symbols: [level_up]
  - domain: agents
    symbols: [Agent.tick]
```

```bash
domain_lock reserve-plan --pr 6926 --agent claude-1 \
  --branch feat/level-up-and-agents --plan plan.yaml
```

If any single leg collides with an existing holder, the whole `reserve-plan` aborts with `DENIED` and exit 1.

## Registry YAML

```yaml
domains:
  level_up_core:
    paths:
      - mvp_site/rewards_engine.py
      - mvp_site/world_logic.py
    owners: [jleechan2015]
  ci_infra:
    paths:
      - .github/workflows/**
    owners: [jleechan2015]
  all_other_files:
    paths:
      - "*"
```

- Domains are checked in declaration order; the first matching domain wins.
- `paths` uses Python `fnmatch`-style globs.
- Put a catch-all domain last for all-file coverage.
- If a file is truly unmapped, current CLI behavior is only `WARN: unmapped files (no domain)` and it does **not** block. Treat that as a development-mode warning, not production-safe coverage.
- See `examples/file_domains.yaml` for a working sample.

## What is protected

- Spawn-time collisions when two agents try to reserve the same domain.
- Same-file Python edits when agents reserve disjoint symbols.
- Multi-domain tasks when the orchestrator uses `reserve-plan`.
- Local commits touching domains held by another PR, when the pre-commit hook is installed.
- Concurrent lock-log writes, because writes are serialized with `flock(2)`.
- Dry-run planning over declared PR file lists through `predict-conflicts`.

## What is not protected

- Agents that do not call `reserve` / `reserve-plan` before writing.
- Files omitted from the registry when there is no catch-all domain.
- Semantic conflicts across different files unless the registry groups those files into the same domain.
- Runtime failures, test failures, CI failures, or reviewer objections.
- Stale locks unless the orchestrator releases or cleans them up.
- Non-Python symbol-level analysis; non-Python files fall back to whole-domain/file-level locking.

## Lock log (JSONL, append-only)

Lives **outside the repo tree** at `~/.merge_train/locks/<repo-hash>/pr_domain_locks.jsonl` by default (`<repo-hash>` = SHA-256 of `git remote get-url origin`). This keeps the JSONL out of merge conflicts and out of the working tree.

```json
{"domain":"level_up_core","pr":6926,"agent":"claude-1","branch":"feat/level-up","opened_at":"2026-05-16T12:34:56Z","status":"active","symbols":["level_up"]}
{"domain":"level_up_core","pr":6926,"closed_at":"2026-05-16T14:01:00Z","status":"released"}
```

- `release` writes a new line — history is never mutated.
- Comment lines (`# ...`) are skipped by readers.
- Concurrent writers serialize via `flock(2)` on the log file.

Override with `MERGE_TRAIN_LOG=/path/to/locks.jsonl` or `--log /path/to/locks.jsonl`.

## Hooks

### Pre-spawn gate (orchestrator integration)

Wire `hooks/ao-spawn-domain-check.sh` into your agent spawner. The orchestrator supplies the files the agent will modify (from the task spec / bead / PR design doc):

```bash
export MERGE_TRAIN_FILES="mvp_site/world_logic.py mvp_site/rewards_engine.py"
export MERGE_TRAIN_PR=7000
./hooks/ao-spawn-domain-check.sh
# exit 0 = spawn allowed, 1 = held (refuse spawn), 2 = config error
```

This hook is a pre-spawn **gate**, not the full acquisition protocol. A production orchestrator should use it as a fast refusal check, then call `reserve` or `reserve-plan` before actually launching the agent. Otherwise two orchestrators can both observe "free" and race each other.

### Claude / OpenCode / Codex hook installers

Current repo state:

- Supported now: generic shell integration through `hooks/ao-spawn-domain-check.sh` plus the Git pre-commit hook.
- Not supported yet: first-class installers that patch Claude Code, OpenCode, or Codex config files for you.
- Not proven yet: an end-to-end fixture that installs each agent hook, attempts a conflicting spawn, observes refusal, releases the lock, and observes a clean spawn.

The integration target is:

```bash
merge_train install-hooks --agent claude --repo /path/to/repo
merge_train install-hooks --agent opencode --repo /path/to/repo
merge_train install-hooks --agent codex --repo /path/to/repo
merge_train test-hooks --agent all --repo /path/to/repo
```

Each installer should configure that agent's native pre-spawn/pre-write hook to call `domain_lock acquire --files ...` once `acquire` exists. Until then, wire the generic shell hook in the orchestrator and follow it with `reserve` / `reserve-plan`.

### Pre-commit hook (local guard)

Refuses commits that touch domains held by a *different* PR. Diff-mode is on by default — only blocks on symbol overlap:

```bash
ln -s ../../hooks/pre-commit.sh .git/hooks/pre-commit
chmod +x hooks/pre-commit.sh
```

Disable diff-mode with `MERGE_TRAIN_DIFF_MODE=0` for whole-file locking.

## Default log resolution + `--git-cwd`

The default log path is derived from the current git working tree's remote URL. When invoked from a different directory (e.g. an AO worker in a worktree), pass `--git-cwd` so the right repo is used:

```bash
domain_lock --git-cwd /path/to/worktree check --files mvp_site/foo.py
# OR equivalently:
domain_lock check --files mvp_site/foo.py --git-cwd /path/to/worktree
```

If no git remote can be resolved, the log falls back to `~/.merge_train/locks/default/pr_domain_locks.jsonl`.

## Production hardening (current)

- **External-by-default log path** — outside the repo tree, no merge conflicts on JSONL.
- **`flock(2)` on the JSONL** — concurrent reservers serialize cleanly; `tests/test_domain_lock.py::test_concurrent_reserve_only_one_wins` proves only-one-wins.
- **Fail-closed `--diff-mode`** — parse errors, non-Python files, and missing AST fall back to whole-domain locks (never silently allow co-tenancy).
- **Position-independent global flags** — `--registry`, `--log`, `--git-cwd` accepted before or after the subcommand.

## Tests

```bash
pip install -e .
python -m pytest tests/ -q     # 202 passed
```

## Roadmap

- [x] MVP: CLI + YAML + JSONL + spawn hook + pre-commit hook
- [x] Move lock state out of PR branches (default log path outside repo tree)
- [x] Symbol-level (Python AST) domains
- [x] Atomic multi-domain `reserve-plan`
- [x] Concurrency safety (flock)
- [x] Dry-run / replay mode (`predict-conflicts` — greedy MIS + optional `git merge-tree`)
- [x] `predict-conflicts --from-prs N,M,P` (gh-cli integration — fetch files from PRs)
- [ ] `acquire --files` (atomic file-list acquisition with automatic per-file fallback)
- [ ] Claude / OpenCode / Codex hook installers
- [ ] End-to-end hook tests for Claude / OpenCode / Codex
- [ ] `predict-conflicts --from-active` (derive plan from live `LockLog` instead of YAML)
- [ ] Refactoring-aware semantic edges (callers-of-deleted-symbol)
- [ ] MCP server wrapper (agent-native schema'd tools — same lib)
- [ ] Post-merge cascade rebase webhook
- [ ] AO integration reference (live deployment)
- [ ] Non-Python AST adapters (TS/Go)

## See also

- [`docs/AGENTS.md`](docs/AGENTS.md) — recipes for AI agents using or modifying this repo (paste-in integration snippets)
- [`docs/CLAUDE.md`](docs/CLAUDE.md) — repo-local Claude Code policy (test discipline, evidence, adversarial review)
- [`docs/opencode_md_area_lock_e2e.md`](docs/opencode_md_area_lock_e2e.md) — real OpenCode/AO E2E runbook for 20 Markdown area-lock PRs against `mctrl_test`
- [`CHANGELOG.md`](CHANGELOG.md) — release notes
- [`roadmap/README.md`](roadmap/README.md) — rolling activity log
- [`examples/file_domains.yaml`](examples/file_domains.yaml) — sample registry

## License

MIT
