# Methodology

## Test Design

20 symbol-level area locks on the same domain (`e2e_shared_markdown`)
and the same file (`merge_train_e2e/shared_plan.md`), each holding a
distinct slot symbol like `md:shared_plan.slot_01`.

## Full Git Provenance

- merge_train SHA: 30446565a58ecc232e255ef5a3c4003069411ee0
- merge_train branch: main (synced to origin/main)
- mctrl_test base SHA: 6f89bef3b823bc74c89c260264396a7f16c62b55
- commits_ahead_of_main: 0
- Code changes proven: e2e_slot_worker.sh (push-failure fatal, worktree
  lock trap), e2e_md_area_lock_runner.py (slot collision fix)

## Environment

- Merge Train repo: <merge_train_repo>
- Mctrl Test repo: <mctrl_test_repo>
- Python: 3.13.7
- Platform: darwin
- Runner: e2e_md_area_lock_runner.py

## Negative Control Design

- Control 1 (duplicate slot-01): PR 99999 tries md:shared_plan.slot_01,
  already held by PR 50001. Must be DENIED.
- Control 2 (whole-domain): PR 99998 tries domain-wide reservation on
  e2e_shared_markdown while symbol locks are active. Must be DENIED.
- Control 3 (different area): PR 99997 tries slot-21 (total_slots+1),
  which is beyond the 20 reserved slots. Must be ALLOWED, then released.

## Hook Contract

The e2e_slot_worker.sh hook acquires an area lock BEFORE the agent starts
editing. If lock acquisition fails (DENIED), the agent is NOT launched and
the worker exits 1. A trap releases the lock on early exit (e.g., worktree
failure). On normal completion, the lock remains active until PR merge/close.

## Steps

1. Create file_domains.yaml with e2e_shared_markdown domain
2. Reserve 20 area locks via domain_lock reserve-plan
3. List active locks, verify 20 distinct symbols, 0 whole-domain
4. Run 3 negative controls
5. Create 20 PRs (one per slot)
6. Verify PRs via gh pr view
7. Run pairwise + sequential merge simulations
8. Release all locks
9. Verify 0 active test PR locks remain
