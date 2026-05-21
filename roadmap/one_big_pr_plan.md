# One-big-PR implementation plan (TDD + hooks)

Date: 2026-05-21

## Roadmap status from README

### Done
- MVP CLI + YAML + JSONL + spawn hook + pre-commit hook
- External lock state (default log path outside repo tree)
- Symbol-level (Python AST) domains
- Atomic multi-domain `reserve-plan`
- Concurrency safety via `flock`
- Dry-run / replay `predict-conflicts` (greedy MIS + optional `git merge-tree`)
- `predict-conflicts --from-prs N,M,P`

### Not done
- `acquire --files` (atomic file-list acquisition with automatic per-file fallback)
- Claude / OpenCode / Codex hook installers
- End-to-end hook tests for Claude / OpenCode / Codex
- `predict-conflicts --from-active` (derive plan from live lock log)
- Refactoring-aware semantic edges (callers-of-deleted-symbol)
- MCP server wrapper (agent-native schema'd tools)
- Post-merge cascade rebase webhook
- AO integration reference (live deployment)
- Non-Python AST adapters (TS/Go)

## Recommendation

Given the request to do everything in one large PR using TDD and with hooks included, the highest-leverage path is to deliver in this exact order:

1. **Core correctness first**: implement `acquire --files` with fail-closed semantics and deterministic per-file fallback lock IDs.
2. **Operational visibility**: add `predict-conflicts --from-active` so teams can inspect current in-flight PR lock risks without curating YAML.
3. **Hook UX**: add `install-hooks --agent {claude,opencode,codex}` and `test-hooks --agent all` with clear idempotency guarantees.
4. **Proof that hooks work**: ship end-to-end tests that install each hook into fixtures, assert denial under contention, then assert success after release.
5. **Language coverage increment**: add AST adapters (start TS then Go) behind a common interface and fail-closed fallback.
6. **Higher-order semantic safety**: add optional callers-of-deleted-symbol conflict edges to reduce “safe but actually risky” co-tenancy.
7. **Integration surfaces**: add MCP wrapper + post-merge cascade rebase webhook + AO reference deployment docs.

## TDD workstream (single PR)

### Phase A — `acquire --files`
- Write failing tests:
  - mapped files resolve to existing domains
  - unmapped files become deterministic fallback locks
  - mixed mapped/unmapped acquisition is atomic
  - collision on any leg fails entire transaction
  - release semantics remain append-only and compatible with existing log readers
- Implement minimal command + resolver + atomic transaction logic.
- Refactor only after green.

### Phase B — `predict-conflicts --from-active`
- Write failing tests for:
  - deriving synthetic plan from active lock-log entries
  - stable PR ordering and deterministic output
  - compatibility with `--json` and current exit codes
- Implement with shared plan-building helper.

### Phase C — Hook installers + hook e2e
- Write failing installer tests per agent fixture:
  - install idempotency
  - expected file patches/shell hooks present
  - uninstall/overwrite behavior where applicable
- Write failing e2e tests:
  - lock held => spawn denied
  - lock released => spawn allowed
- Implement `install-hooks`/`test-hooks` commands and fixture harness.

### Phase D — Non-Python adapters + semantic edges
- Add adapter-interface tests first (contract tests).
- Add TS adapter tests, then implementation.
- Add Go adapter tests, then implementation.
- Add callers-of-deleted-symbol tests on representative mini-repos.

### Phase E — MCP + webhook + AO reference
- Start with behavior tests for tool schemas and payloads.
- Add webhook dry-run tests for cascade ordering logic.
- Add AO integration reference validation checks (scripted smoke).

## Guardrails for a single large PR

- Keep existing exit-code contract (`0` success, `1` held/conflict, `2` config error).
- Preserve position-independent global flags on all new subcommands.
- Keep fail-closed behavior for uncertain symbol resolution.
- Require green test suite at each phase checkpoint before proceeding.
- Maintain changelog + docs updates per phase to reduce review risk.

## Suggested PR decomposition *inside* one branch

Even for one final PR, develop as stacked commits to keep reviewability:
1. tests for `acquire --files`
2. implementation for `acquire --files`
3. tests/impl for `--from-active`
4. tests/impl for hook installers
5. tests/impl for hook e2e harness
6. tests/impl for TS+Go adapters
7. tests/impl for semantic edges
8. tests/impl for MCP + webhook + AO docs
9. final doc/changelog polish

This preserves one-PR delivery while keeping bisectability and reviewer sanity.
