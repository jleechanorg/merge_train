# Methodology

## Test Design

20 symbol-level area locks on the same domain (`e2e_shared_markdown`)
and the same file (`merge_train_e2e/shared_plan.md`), each holding a
distinct slot symbol like `md:shared_plan.slot_01`.

Symbols are auto-extracted from shared_plan.md using
`extract_markdown_symbols()`, not hardcoded in plan YAML.

Real OpenCode agents (`openw run`) perform the edits, not inline Python.

## Full Git Provenance

- merge_train SHA: 1ef058ef70f88f3250ed14572d7baf70c1e6c5ec
- merge_train branch: main (synced to origin/main)
- mctrl_test base SHA: 7f391fbc6fb99a7bbaf57d867e6accb2d15daa9d
- commits_ahead_of_main: 0

## Environment

- Merge Train repo: <merge_train_repo>
- Mctrl Test repo: <mctrl_test_repo>
- Python: 3.13.7
- Platform: darwin
- Runner: e2e_md_area_lock_runner.py
- Agent: openw run (real OpenCode)

## Auto-Extraction

`extract_markdown_symbols(source, file_stem="shared_plan")` parses
`## slot-NN` headings into `md:shared_plan.slot_NN` symbols. The runner
verifies the count matches `--slots` before reserving. Phase 2 records
`auto_extracted: true` per reservation.

## Real Agent Integration

`e2e_slot_worker.sh` is invoked per slot:
1. Auto-extracts the symbol from shared_plan.md
2. Acquires the lock (or verifies existing lock held by same PR)
3. Creates a worktree from the setup branch
4. Launches `openw run` with the slot edit task
5. On failure, falls back to inline Python edit
6. Lock stays active until PR merge/close
7. Agent transcript recorded to agent_transcripts/

## Negative Control Design

- Control 1 (duplicate slot-01): PR 99999 tries md:shared_plan.slot_01,
  already held by PR 50001. Must be DENIED.
- Control 2 (whole-domain): PR 99998 tries domain-wide reservation on
  e2e_shared_markdown while symbol locks are active. Must be DENIED.
- Control 3 (different area): PR 99997 tries slot-21 (total_slots+1),
  which is beyond the 20 reserved slots. Must be ALLOWED, then released.

## Steps

1. Create file_domains.yaml with e2e_shared_markdown domain
2. Auto-extract symbols from shared_plan.md
3. Reserve 20 area locks via domain_lock reserve-plan
4. List active locks, verify 20 distinct symbols, 0 whole-domain
5. Run 3 negative controls
6. Launch 20 real openw agents via e2e_slot_worker.sh
7. Verify PRs via gh pr view
8. Run pairwise + sequential merge simulations
9. Release all locks
10. Verify 0 active test PR locks remain
