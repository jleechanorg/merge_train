# `acquire --files` spec (predict-conflicts era)

**Status:** Draft (bead `orch-9le6`)
**Date:** 2026-06-02
**Replaces:** Original `acquire --files` scoped against the deleted `domain_lock` API (PR #19, 2026-06-02).

---

## Motivation

`acquire --files` was originally planned to acquire a persistent
per-file lock against the in-memory `domain_lock` log. After PR #19
removed `domain_lock` entirely, the only remaining conflict-detection
mechanism is `merge_train/predict.py` (`predict-conflicts`).

This document **re-specs** the command against the predict-conflicts
world so a draft PR can implement and test it without depending on any
deleted module.

## Semantics

`acquire --files FILE [FILE ...]` is a **declarative collision check**:
*"Can I acquire permission to modify these files, given the current
in-flight PR set?"*

It is **not** a persistent lock. There is no lock log, no reservation
side-effect, and no release path. The answer is binary: **allow** or
**deny**, derived deterministically from:

1. The requested files (with their resolved symbols).
2. The in-flight PR set (declared via `--plan` or fetched via `--from-prs`).
3. The same domain registry and conflict semantics that `predict-conflicts` already uses.

## Inputs

| Flag | Meaning | Default |
| --- | --- | --- |
| `FILE [FILE ...]` | Positional list of files to acquire | (required, ≥1) |
| `--plan FILE` | In-flight PR set (YAML) | mutually exclusive with `--from-prs` |
| `--from-prs N,M,P` | In-flight PRs fetched from GitHub | mutually exclusive with `--plan` |
| `--registry FILE` | Domain registry YAML | `Registry.empty()` if omitted |
| `--repo OWNER/REPO` | GitHub repo (for `--from-prs` + symbol enrichment) | auto-detected |
| `--agent NAME` | Agent name requesting acquisition | `acquire` |
| `--branch NAME` | Branch name requesting acquisition | `acquire` |
| `--json` | Emit JSON | human-readable by default |
| `--lock-path FILE` | Path for `flock` | `~/.merge_train/acquire.lock` |
| `--no-flock` | Skip `flock` (for tests/CI) | flock on |
| `--allow-unmapped` | Allow unmapped files without flagging fallback | warn-and-continue |

## Process

1. **Acquire exclusive `flock`** on `--lock-path` (skip if `--no-flock`).
2. **Load in-flight PRs** from `--plan` or `--from-prs` using the same
   loaders as `predict-conflicts`. Returns exit 2 on plan/load error.
3. **Resolve symbols** for each requested file:
   - Mapped file in registry → run `touched_symbols_for_staged_file`
     and emit the symbol set.
   - Unmapped file → use file-level fallback lock
     (`{file:<path>}` for symbol-aware, or whole-file semantics).
   - Resolution failure → file-level fallback (fail-closed).
4. **Build candidate `PRSpec`** with branch=`--branch` and the resolved
   `symbols_by_file` mapping.
5. **Run `predict_conflicts([candidate, *in_flight])`** to detect any
   pairwise conflict between the candidate and the in-flight PRs.
6. **Decide**:
   - Any `is_conflict` between the candidate and an in-flight PR → **deny** (exit 1).
   - No conflicts → **allow** (exit 0).
7. **Release flock** and return.

## Atomicity

The whole transaction is atomic. If **any** file in the input list would
conflict with **any** in-flight PR, the **entire** request is denied —
no partial accept. This is enforced by routing through a single
`predict_conflicts` call with one candidate spec, rather than per-file
checks.

## Concurrency

`flock(LOCK_EX)` on `--lock-path` serializes concurrent `acquire`
invocations. This guards against a window where two agents check the
same plan simultaneously and both pass. The lock is **advisory** —
agents that don't take the lock can still race; this matches the
best-effort nature of the predict-conflicts system.

The lock file lives outside the repo (default
`~/.merge_train/acquire.lock`); its parent directory is created on
demand.

## Output

### Human (default)

```
acquire: branch=feat/x agent=claude
  Resolved 2/2 files.
  src/foo.py     -> [func1, Class1.method1]
  src/bar.py     -> (file-level fallback)
  No conflicts with in-flight PRs [1, 2, 3].
  Decision: allow (exit 0)
```

When denied:

```
acquire: branch=feat/x agent=claude
  Resolved 2/2 files.
  src/foo.py     -> [func1, Class1.method1]
  src/bar.py     -> (file-level fallback)
  Conflict with PR#42 on domain=core symbols=[func1].
  Decision: deny (exit 1)
```

### JSON

```json
{
  "decision": "allow" | "deny",
  "files": ["src/foo.py", "src/bar.py"],
  "resolved": {
    "src/foo.py": ["func1", "Class1.method1"],
    "src/bar.py": []
  },
  "fallback_files": ["src/bar.py"],
  "conflicts": [
    {"domain": "core", "symbols": ["func1"], "conflicting_pr": 42}
  ],
  "in_flight_prs": [1, 2, 3],
  "candidate": {"branch": "feat/x", "agent": "claude"},
  "flock_path": "/Users/.../acquire.lock"
}
```

## Exit codes

- `0` — allow (no conflict, transaction succeeds atomically)
- `1` — deny (at least one conflict; whole transaction fails)
- `2` — config error (bad plan, missing file, parser error)

## Why not write a lock log?

The predict-conflicts world is intentionally **stateless**. A persistent
lock log would:

1. Duplicate state that `predict.py` already derives from declared scopes.
2. Re-introduce a "lock file drift" failure mode that PR #19 deleted.
3. Conflict with `--from-prs` (live GitHub queries) which has no natural
   on-disk side effect.

The `acquire --files` answer is "yes/no" plus a deterministic
explanation; that's enough to gate a `git push` or PR creation without
re-creating the legacy lock log.

## Open design questions (tracked separately)

- Should `--allow-unmapped` warn and continue, or hard-error? Current
  default: **warn and continue** (matches `predict-conflicts`).
- Should `acquire` be a separate `predict-acquire` subcommand, or
  re-use the `predict-conflicts` parser? Current: separate command,
  shared internals.
- Multi-PR candidate: out of scope for v1; current `acquire --files`
  models a single PR.

## Test plan (TDD red baseline)

| Test | Asserts |
| --- | --- |
| `test_acquire_files_mapped_no_conflict_exit0` | mapped files, no conflict → exit 0 |
| `test_acquire_files_mapped_with_conflict_exit1` | mapped files, conflict → exit 1, conflict reported |
| `test_acquire_files_unmapped_uses_fallback` | unmapped file → `fallback_files` populated |
| `test_acquire_files_mixed_mapped_and_unmapped` | mix → both correct, atomic decision |
| `test_acquire_files_atomic_partial_conflict_denies_all` | 1-of-5 conflict → deny the whole list |
| `test_acquire_files_no_inflight_prs_allow` | empty in-flight → allow |
| `test_acquire_files_cli_human_output` | human output includes "Decision: allow/deny" |
| `test_acquire_files_cli_json_output` | JSON shape matches spec |
| `test_acquire_files_missing_plan_exit2` | config error → exit 2 |
| `test_acquire_files_flock_serializes_concurrent` | concurrent invocations serialize |
| `test_acquire_files_no_flock_skips_flock` | `--no-flock` skips the lock file |
| `test_acquire_files_resolve_uses_touched_symbols` | resolver calls `touched_symbols_for_staged_file` |
