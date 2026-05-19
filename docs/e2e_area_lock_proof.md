# Proof: OpenCode Markdown Area-Lock E2E

**Date:** 2026-05-19
**Merge Train SHA:** `1ef058e` ([commit](https://github.com/jleechanorg/merge_train/commit/1ef058ef70f88f3250ed14572d7baf70c1e6c5ec))
**Run ID:** `20260519T082805Z`
**Evidence bundle:** `evidence/v0.3/`
**Result:** 9/9 PASS

## Claim

> Multiple real OpenCode agents can concurrently edit disjoint sections of the
> same Markdown file because `merge_train` acquires and releases area-level locks,
> not whole-file locks.

## What Was Proven

### 1. Real OpenCode Agents (not inline Python fallback)

All 20 slots used `openw run --dangerously-skip-permissions` to perform the edit.
The `e2e_slot_worker.sh` hook launches `openw run` for each slot.

**Evidence:** `prs.json` records `agent_mode: "real_agent"` for all 20 slots.
`agent_transcripts/slot-*.log` captures real agent output including the PR creation URL.

Sample transcript (`agent_transcripts/slot-01.log`):
```
slot: 01
symbol: md:shared_plan.slot_01
branch: merge-train-e2e/20260519T082805Z/slot-01
pr: 50001
lock_result: ALREADY_RESERVED: slot-01 by PR#50001
agent_exit: 0
agent_output_last_200: ...PR created: https://github.com/jleechanorg/mctrl_test/pull/340
timestamp: 2026-05-19T08:28:32Z
```

### 2. Auto-Extracted Markdown Symbols

`extract_markdown_symbols(source, file_stem="shared_plan")` parses `## slot-NN`
headings into `md:shared_plan.slot_NN` symbols. The runner auto-extracts before
reserving — no hardcoded plan YAML.

**Evidence:** `run.json` `reserve_results[].auto_extracted = true` for all 20 slots.
`scenarios[1]` (`auto_extraction`) = PASS (20/20 symbols found).

### 3. Lock-Before-Agent Contract

`e2e_slot_worker.sh` acquires the area lock before launching `openw run`.
If lock acquisition fails (DENIED), the agent is NOT started and the worker exits 1.
A trap releases the lock on early exit (e.g., worktree failure).

**Evidence:** Agent transcripts show `lock_result` before `agent_exit`.
Negative control 1 proves the agent would not start: duplicate slot-01 is DENIED.

### 4. Area Locks, Not Whole-File Locks

20 concurrent locks held simultaneously on the same file (`shared_plan.md`)
and same domain (`e2e_shared_markdown`), each with a distinct symbol.
0 whole-domain locks.

**Evidence:** `active_during_run.json` contains 20 entries, each with a distinct
symbol (`md:shared_plan.slot_01` through `slot_20`). No entry has an empty
symbols array.

### 5. Negative Controls

| Control | Expected | Actual | Result |
|---------|----------|--------|--------|
| Duplicate slot-01 (PR 99999) | DENIED | DENIED (exit=1) | PASS |
| Whole-domain reservation (PR 99998) | DENIED | DENIED (exit=1) | PASS |
| Different area slot-21 (PR 99997) | ALLOWED | ALLOWED (exit=0) | PASS |

**Evidence:** `run.json` `negative_controls[]` with `passed: true` for all three.

### 6. Real PRs Created

20 real PRs on `jleechanorg/mctrl_test`: #340 through #359.

**Evidence:** `prs.json` contains PR URLs and numbers. Verified via
`gh pr view 340 --repo jleechanorg/mctrl_test --json number,title,state`:
```
PR #340: chore: mark slot-01 complete in shared_plan.md [OPEN]
  branch=merge-train-e2e/20260519T082805Z/slot-01 files=1
```

### 7. Merge Simulations Clean

Pairwise and sequential merge simulations found no conflicts.

**Evidence:** `pairwise_merge_tree.json` and `sequential_merge_tree.json`.

### 8. All Locks Released

After release, 0 active test PR locks remain.

**Evidence:** `active_after_release.json` is empty (`[]`).

## Scenario Results

| Scenario | Passed | Errors |
|----------|--------|--------|
| fixture_setup | PASS | none |
| auto_extraction | PASS | none |
| area_lock_reservation | PASS | none |
| active_lock_verification | PASS | none |
| negative_controls | PASS | none |
| pr_creation | PASS | none |
| pr_verification | PASS | none |
| merge_simulation | PASS | none |
| lock_release | PASS | none |

## Provenance

| Field | Value |
|-------|-------|
| merge_train SHA | `1ef058ef70f88f3250ed14572d7baf70c1e6c5ec` |
| mctrl_test base SHA | `7f391fbc6fb99a7bbaf57d867e6accb2d15daa9d` |
| Branch | main (synced to origin/main) |
| Python | 3.13.7 |
| Agent | openw run (real OpenCode) |
| Symbol discovery | extract_markdown_symbols() (auto) |
| Checksums | All 13 artifacts verified via sha256sum |

## What This Proof Does NOT Cover

- **Production AO worker orchestration** — hooks tested via direct invocation, not
  through `ao spawn`. Tracked as bead `orch-66my`.

## Reproduce

```bash
cd /Users/jleechan/projects/merge_train
python3 scripts/e2e_md_area_lock_runner.py --slots 20
```

Evidence written to `/tmp/merge_train_evidence/opencode_md_area_lock/<RUN_ID>/`.
