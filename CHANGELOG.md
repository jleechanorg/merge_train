# Changelog

All notable changes to this project will be documented in this file.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed

- **CLI surface** (`domain_lock`): Deleted the `domain_lock` CLI stack (`reserve`, `reserve-plan`, `release`, `check`, `list`, `audit`), replacing it with two standalone binaries: `acquire` and `predict-conflicts`.
- **Log file**: Removed the persistent jsonl lock log (`pr_domain_locks.jsonl`). Declarative plan file (`pr_domain_locks.yaml`) is now the single source of truth.
- **Workflow**: Deleted the `release-domain-lock-on-merge` GitHub Actions workflow.

### Changed

- **Docs & Hooks**: Cleaned up stale `domain_lock` references in documentation (`README.md`, `docs/AGENTS.md`, `docs/CLAUDE.md`) and hooks.

## [0.1.0] — 2026-05-18


First MVP release. Spawn-time file-domain lock registry for AI-agent PR pipelines.

### Added

- **CLI surface** (`domain_lock`): `reserve`, `reserve-plan`, `release`,
  `check`, `list`, `audit`, `predict-conflicts`.
- **Declarative registry** (`file_domains.yaml`) mapping glob paths →
  named domains.
- **Symbol-level locks** — Python AST resolution lets two PRs co-edit
  the same file if they touch disjoint symbols.
- **Atomic multi-domain reservation** (`reserve-plan`) — all-or-nothing
  legs in one transaction.
- **`check --diff-mode`** — resolves the staged git diff down to touched
  Python symbols; fail-closed on parse error / non-Python files.
- **`predict-conflicts`** — dry-run: take a YAML of PRs and output
  pairwise conflicts (symbol + optional `git merge-tree` textual),
  approximate-maximum parallel batches (greedy maximal IS), and a
  recommended merge order. Disclaimer-framed as a risk-reduction signal.
- **External lock log** — JSONL stored at
  `~/.merge_train/locks/<repo-hash>/pr_domain_locks.jsonl` outside the
  repo tree; never a merge-conflict hotspot.
- **`flock(2)` serialization** — concurrent reservers write safely;
  only-one-wins proven by a multiprocessing test.
- **Position-independent global flags** — `--registry`, `--log`,
  `--git-cwd` parse on either side of the subcommand.
- **Hooks**: `hooks/ao-spawn-domain-check.sh` (pre-spawn gate),
  `hooks/pre-commit.sh` (local guard).
- **`install.sh`** — idempotent installer that pip-installs the
  package, drops a `file_domains.yaml` skeleton, and symlinks the
  pre-commit hook into a target git repo.
- **Docs**: `README.md`, `docs/AGENTS.md` (agent integration recipes),
  `docs/CLAUDE.md` (Claude Code repo-local policy),
  `examples/file_domains.yaml`.
- **CI**: GitHub Actions matrix (Ubuntu + macOS × Python 3.10–3.12) +
  `install.sh` smoke + idempotency check.

### Quality

- **180 tests** covering registry parsing, lock-log append/release,
  symbol resolution, concurrency safety, atomic plans, parser
  backward-compat, dry-run prediction (including real `git merge-tree`
  subprocesses on Apple Git 2.39 legacy fallback), JSON output contracts,
  and installer idempotency.
- Adversarial code-review and evidence-standards reviews passed for
  the parser-compat fix and the `predict-conflicts` MVP.

### Notes

- `predict-conflicts` output is explicitly framed as a risk-reduction
  signal, **not** a merge guarantee. CI and human review remain the
  authoritative gates.
- Default registry path is `./file_domains.yaml`; override with
  `MERGE_TRAIN_REGISTRY=`.
- Default log path is
  `~/.merge_train/locks/<sha256(remote-url)[:12]>/pr_domain_locks.jsonl`;
  override with `MERGE_TRAIN_LOG=` or `--log`.

[0.1.0]: https://github.com/jleechanorg/merge_train/releases/tag/v0.1.0
