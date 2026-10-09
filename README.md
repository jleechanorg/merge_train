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

1. Runs `uv tool install --reinstall` to install or update the `merge_train` package and place the `acquire` and `predict-conflicts` binaries on your `PATH` (shared across all repos — no virtualenv per repo).
2. Skips creating a `file_domains.yaml` registry (not needed for symbol-level prediction); copy `examples/file_domains.yaml` if you want one.
3. Wires one effective hook scope per coding CLI: user scope for Claude, Codex, and Gemini; project scope for Cursor; and the OpenCode plugin/instructions. Tool hooks match file mutations only and stay silent unless a conflict or error needs attention.
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

`acquire` resolves your *staged diff* down to the AST symbols actually touched and matches them against active reservations:

```bash
git add mvp_site/world_logic.py
acquire --plan pr_domain_locks.yaml --registry file_domains.yaml --branch <your-branch> --agent <agent> mvp_site/world_logic.py
# Refuses when your staged diff touches symbols reserved by another PR; an edit outside any extracted symbol is conservatively treated as a conflict.
# Files with no supported extractor (like JSON) fall back to whole-file locking.
```

### Supported languages

Symbol resolution uses tree-sitter AST parsing (via `tree_sitter_languages`) when the `multilang` extra is installed, with a regex fallback that ships by default (so the package works on minimal installs).

| Language | File extensions | Extractor |
|---|---|---|
| Python | `.py` | `merge_train/symbols.py` (stdlib `ast`) |
| TypeScript | `.ts`, `.tsx` | `merge_train/lang_extractors.py` |
| JavaScript | `.js`, `.jsx`, `.mjs` | `merge_train/lang_extractors.py` |
| Go | `.go` | `merge_train/lang_extractors.py` |
| Rust | `.rs` | `merge_train/lang_extractors.py` |
| Java | `.java` | `merge_train/lang_extractors.py` |
| C | `.c`, `.h` | `merge_train/lang_extractors.py` |
| C++ | `.cpp`, `.cc`, `.cxx`, `.hpp` | `merge_train/lang_extractors.py` |
| C# | `.cs` | `merge_train/lang_extractors.py` |
| Markdown | `.md` | heading-based symbols |

Anything else (including JSON) gets a whole-file lock.

Regex fallback is intentional: `predict-conflicts` and `acquire` never crash on an unsupported file — they degrade to whole-file locking. Install `[multilang]` for higher precision.

### Auto symbol discovery

Agents don't have to hand-author the `symbols:` field in `pr_domain_locks.yaml`. `merge_train/symbol_discovery.py` ships two entry points that extract touched symbols directly from git:

```python
from merge_train.symbol_discovery import (
    symbols_from_staged_diff,   # git index → dict[path, set[symbols]]
    symbols_from_pr_diff,       # gh pr diff <N> → dict[path, set[symbols]]
)
```

A pre-spawn or pre-commit agent can call these, then merge the result into the `PRSpec.symbols_by_file` field of its reservation. Files in any language from the table above are resolved by the matching extractor; unsupported files and files that fail to parse are omitted — callers fall back to whole-file locking for them, which is the safe default.

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
- `hooks/conflict-warn-pre-tool.sh` — shared mutation hook for Claude, Codex, Gemini/Antigravity, and Cursor.
- `hooks/gemini-conflict-warn.sh` — Gemini / Antigravity session guard.
- `hooks/pre-commit.sh` — Git pre-commit hook (runs `predict-conflicts`).

`install.sh` copies the tool-hook scripts to `~/.local/bin` (the pre-commit hook is symlinked into the target repo's `.git/hooks` instead) and wires the mutation hook for Claude Code, Codex, Gemini/Antigravity, Cursor, and OpenCode in a single run. `predict-spawn-check.sh` is only copied: your spawner must call it, so spawn-time protection is not active until you wire it.

## Tests

```bash
pytest                       # unit + integration tests
./scripts/refresh_evidence.sh   # refresh evidence metadata SHAs + checksums
```

Tests must stay green; the badge at the top shows the CI workflow status.

## Docs & Evidence

| Path | What it is |
|---|---|
| [`docs/AGENTS.md`](docs/AGENTS.md) | Agent integration recipes (spawn-time and commit-time patterns for Aider / OpenHands / Devin / Claude / Codex / AO workers) |
| [`docs/CLAUDE.md`](docs/CLAUDE.md) | Claude Code repo-local policy for working *on* `merge_train` |
| [`docs/acquire_files_spec.md`](docs/acquire_files_spec.md) | Behavioral spec for the `acquire` CLI |
| [`docs/ao-live-deployment.md`](docs/ao-live-deployment.md) | Live Agent-Orchestrator deployment story (v0.6) |
| [`docs/e2e_area_lock_proof.md`](docs/e2e_area_lock_proof.md) | End-to-end proof of area locking under real merge pressure |
| [`evidence/`](evidence/) | Per-release reproducible evidence bundles (v0.2 → v0.6) with sha256 checksums |
| [`scripts/refresh_evidence.sh`](scripts/refresh_evidence.sh) | Refresh each `evidence/v*/metadata.json` SHA and its checksum |
| [`examples/file_domains.yaml`](examples/file_domains.yaml) | Minimal starter registry |
| [`roadmap/`](roadmap/) | Roadmap notes and the one-big-PR consolidation plan |
| [`CHANGELOG.md`](CHANGELOG.md) | Keep-a-Changelog history |

### Verified evidence

The `evidence/v*-ao/` directories (v0.4-ao, v0.5-ao, v0.6-ao) bundle the files below; older directories (v0.2, v0.2.2, v0.3, v0.4) hold only some of them (v0.2.2 has just `EVIDENCE.md`), and the recordings exist only for v0.6-ao:

- `run.json` + sha256 — what was run
- `prs.json` + sha256 — the input PR plan
- `lock_log.jsonl` + sha256 — the resulting lock state
- `*.cast` / `*.gif` / `*.mp4` + sha256 — human-verifiable recordings (e.g. `evidence/v0.6-ao/v0.6_verify.cast`)
- `checksums.txt` + `checksums.txt.sha256` — manifest of the above

`scripts/refresh_evidence.sh` refreshes the `metadata.json` (the recorded merge_train SHA) and `.sha256` sidecar of each bundle that has one (v0.2.2 has none and is skipped); it does not regenerate the other artifacts. Bundles from v0.3 on pair each artifact with its own sha256 plus a manifest of those, the same shape as in-toto / SLSA provenance; v0.2 hashes only some files and has no manifest, and v0.2.2 is just `EVIDENCE.md`.

## License

MIT
