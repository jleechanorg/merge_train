# merge_train

[![tests](https://github.com/jleechanorg/merge_train/actions/workflows/tests.yml/badge.svg)](https://github.com/jleechanorg/merge_train/actions/workflows/tests.yml)
[![Python ≥ 3.10](https://img.shields.io/badge/python-%E2%89%A53.10-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Spawn-time conflict prediction and atomic file-list acquisition for AI-agent PR pipelines.

Stops two agents from grabbing the same files or symbol scopes when they're spawned in parallel — before either writes a line of code. Symbol-level locks (Python, TypeScript, JavaScript, Go, Rust, Java, C, C++, C#) let two agents edit disjoint functions inside the same file.

## Why

AI-agent PR pipelines (Aider, OpenHands, Devin, custom Agent Orchestrator setups) spawn many agents in parallel against the same repo. They collide:

- Two agents edit `mvp_site/world_logic.py` → one rebases or gets dropped at merge time.
- Conflict surfaces after both agents have burned tokens.
- Existing tools (Mergify, Graphite, jj, ghstack) all work at **merge time** or **commit time**, not **spawn time**.

`merge_train` puts the gate at spawn time: resolves every file to a lock scope, reserves that scope when an agent is spawned, and warns or refuses spawn if a conflict is detected.

## Prior Art

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

**Prerequisite:** [`uv`](https://docs.astral.sh/) must be on your `PATH`.

**Into another repo (recommended):**

```bash
git clone https://github.com/jleechanorg/merge_train.git ~/merge_train
cd /path/to/your/repo
~/merge_train/install.sh
```

`install.sh` is idempotent and does the following:

1. Runs `uv tool install` to install the `merge_train` package and place the `acquire` and `predict-conflicts` binaries on your `PATH` (shared across all repos — no virtualenv per repo).
2. Drops a starter `file_domains.yaml` skeleton in the target repo if one doesn't already exist.
3. Wires git hooks and per-agent session hooks (pre-commit, Codex, Antigravity/Gemini, OpenCode, Claude Code) so all agents call `acquire` or `predict-conflicts` at session start/stop.
4. Smoke-tests the CLI.

**Dev install (working on `merge_train` itself):**

```bash
git clone https://github.com/jleechanorg/merge_train.git
cd merge_train
uv pip install -e '.[dev]'
pytest
```

For multi-language symbol extraction (TypeScript, JavaScript, Go, Rust, Java, C, C++, C#) install the optional extra:

```bash
uv pip install -e '.[dev,multilang]'
```

Requires Python ≥ 3.10, `uv`, and `git` on `PATH`.

## CLI Surface

The package exposes two primary standalone binaries:

### 1. `acquire`
Check-and-reserve transaction tool used at spawn time:

```bash
acquire --plan pr_domain_locks.yaml \
        --registry file_domains.yaml \
        --branch feat/my-branch \
        --agent claude-1 \
        mvp_site/world_logic.py
```

### 2. `predict-conflicts`
Read-only conflict prediction and merge ordering recommendation tool:

```bash
predict-conflicts --plan pr_domain_locks.yaml \
                  --registry file_domains.yaml
```

Exit codes (both `acquire` and `predict-conflicts`):
- `0` — allow (no conflict)
- `1` — deny (at least one conflict, listed on stdout)
- `2` — config error (missing plan, bad YAML, missing file)

## Quick Start (Default: Symbol-based Locking)

```bash
# 1. Declare domains in file_domains.yaml
cat > file_domains.yaml <<EOF
domains:
  level_up_core:
    paths:
      - mvp_site/rewards_engine.py
      - mvp_site/world_logic.py
    owners: [jleechan2015]

  # Catch-all domain for fail-closed coverage
  all_other_files:
    paths:
      - "*"
EOF

# 2. Declare in-flight PRs in pr_domain_locks.yaml
cat > pr_domain_locks.yaml <<EOF
prs:
  - pr: 202
    branch: feat/hello-greeting-v2
    agent: claude-1
    files: [hello.py]
    symbols: {hello.py: [greet]}
EOF

# 3. Check for conflicts at spawn time
acquire --plan pr_domain_locks.yaml --registry file_domains.yaml --branch feat/level-up --agent claude-2 mvp_site/world_logic.py
# exit 0 = free (no conflicts); 1 = held/conflict
```

## Symbol-Level Locks (Sub-File Granularity)

Two PRs can co-edit the *same file* if they touch *disjoint symbols in a supported language*. Lock only the symbols you modify, not the whole file.

At commit time, `predict-conflicts` resolves the *staged diff* down to AST symbols actually touched and matches them against active reservations:

```bash
git add mvp_site/world_logic.py
predict-conflicts --plan pr_domain_locks.yaml --registry file_domains.yaml
# Only refuses if your staged diff touches symbols reserved by another PR.
# Non-AST files (like Markdown or JSON) fall back to whole-file locking.
```

### Supported languages

Symbol resolution uses tree-sitter AST parsing when the `multilang` extra is installed, with a regex fallback that ships by default (so the package works on minimal installs).

| Language | File extensions | Extractor | AST via `multilang` extra |
|---|---|---|---|
| Python | `.py` | `merge_train/symbols.py` (stdlib `ast`) | built-in |
| TypeScript | `.ts`, `.tsx` | `merge_train/lang_extractors.py` | yes (tree-sitter-typescript) |
| JavaScript | `.js`, `.jsx`, `.mjs`, `.cjs` | `merge_train/lang_extractors.py` | yes (tree-sitter-languages) |
| Go | `.go` | `merge_train/lang_extractors.py` | yes (tree-sitter-go) |
| Rust | `.rs` | `merge_train/lang_extractors.py` | yes (tree-sitter-rust) |
| Java | `.java` | `merge_train/lang_extractors.py` | yes (tree-sitter-java) |
| C | `.c`, `.h` | `merge_train/lang_extractors.py` | yes (tree-sitter-c) |
| C++ | `.cpp`, `.cc`, `.cxx`, `.hpp`, `.hh` | `merge_train/lang_extractors.py` | yes (tree-sitter-cpp) |
| C# | `.cs` | `merge_train/lang_extractors.py` | yes (tree-sitter-c-sharp) |
| Anything else | — | whole-file lock | n/a |

Regex fallback is intentional: `predict-conflicts` and `acquire` never crash on an unsupported file — they degrade to whole-file locking. Install `[multilang]` for higher precision.

### Auto symbol discovery

Agents don't have to hand-author the `symbols:` field in `pr_domain_locks.yaml`. `merge_train/symbol_discovery.py` ships two entry points that extract touched symbols directly from git:

```python
from merge_train.symbol_discovery import (
    symbols_from_staged_diff,   # git index → dict[path, set[symbols]]
    symbols_from_pr_diff,       # gh pr diff <N> → dict[path, set[symbols]]
)
```

A pre-spawn or pre-commit agent can call these, then merge the result into the `PRSpec.symbols_by_file` field of its reservation. Non-Python files and files that fail to parse are silently omitted — callers fall back to whole-file locking for them, which is the safe default.

## Registry YAML: `file_domains.yaml`

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

## Plan YAML: `pr_domain_locks.yaml`

```yaml
prs:
  - pr: 202
    branch: feat/hello-greeting-v2
    agent: claude-1
    files: [hello.py, test_hello.py]
    symbols: {hello.py: [greet]}
  - pr: 203
    branch: feat/algo-bfs-optimization
    agent: claude-2
    files: [shortest_path_binary_matrix.py]
```

## What is Protected

- Spawn-time collisions when two agents try to reserve the same domain/symbol scope.
- Same-file edits in any supported language (Python, TypeScript, JavaScript, Go, Rust, Java, C, C++, C#) when agents reserve disjoint symbols.
- Local commits touching domains held by another PR, when the pre-commit hook is installed.
- Concurrent checks, because checks are serialized with `flock(2)` on the advisory lock file.

## What is Not Protected

- Agents that do not run `acquire` before writing.
- Files omitted from the registry when there is no catch-all domain.
- Semantic conflicts across different files unless the registry groups those files into the same domain.
- Runtime failures, test failures, CI failures, or reviewer objections.

## Hooks

All hooks are configured as warnings or validation gates:

- `hooks/predict-spawn-check.sh` — pre-spawn gate check.
- `hooks/conflict-warn-pre-tool.sh` — Claude Code `PreToolUse` hook.
- `hooks/gemini-conflict-warn.sh` — Gemini / Antigravity session guard.
- `hooks/pre-commit.sh` — Git pre-commit hook (runs `predict-conflicts`).

`install.sh` wires all of the above for Codex, Antigravity/Gemini, OpenCode, and Claude Code in a single run.

## Tests

```bash
pytest                       # unit + integration tests
./scripts/refresh_evidence.sh   # regenerate + checksum evidence artifacts
```

Tests must stay green. The current pass count is shown on the badge at the top of this README.

## Docs & Evidence

| Path | What it is |
|---|---|
| [`docs/AGENTS.md`](docs/AGENTS.md) | Agent integration recipes (spawn-time and commit-time patterns for Aider / OpenHands / Devin / Claude / Codex / AO workers) |
| [`docs/CLAUDE.md`](docs/CLAUDE.md) | Claude Code repo-local policy for working *on* `merge_train` |
| [`docs/acquire_files_spec.md`](docs/acquire_files_spec.md) | Behavioral spec for the `acquire` CLI |
| [`docs/ao-live-deployment.md`](docs/ao-live-deployment.md) | Live Agent-Orchestrator deployment story (v0.6) |
| [`docs/e2e_area_lock_proof.md`](docs/e2e_area_lock_proof.md) | End-to-end proof of area locking under real merge pressure |
| [`evidence/`](evidence/) | Per-release reproducible evidence bundles (v0.2 → v0.6) with sha256 checksums |
| [`scripts/refresh_evidence.sh`](scripts/refresh_evidence.sh) | Regenerate the `evidence/v*/` artifacts and re-checksum them |
| [`examples/file_domains.yaml`](examples/file_domains.yaml) | Minimal starter registry |
| [`roadmap/`](roadmap/) | Roadmap notes and the one-big-PR consolidation plan |
| [`CHANGELOG.md`](CHANGELOG.md) | Keep-a-Changelog history |

### Verified evidence

Each `evidence/v*/` directory bundles:

- `run.json` + sha256 — what was run
- `prs.json` + sha256 — the input PR plan
- `lock_log.jsonl` + sha256 — the resulting lock state
- `*.cast` / `*.gif` / `*.mp4` + sha256 — human-verifiable recordings (e.g. `evidence/v0.6-ao/v0.6_verify.cast`)
- `checksums.txt` + `checksums.txt.sha256` — manifest of the above

Re-run `scripts/refresh_evidence.sh` to regenerate any of these and verify the checksums. The integrity pattern (every artifact next to its own sha256 + a manifest of those) is the same shape as in-toto / SLSA provenance — it lets a reviewer prove that the recorded run is the one the README claims.

## License

MIT
