# Methodology

## Test Design

20 symbol-level area locks on the same domain (`e2e_shared_markdown`)
and the same file (`merge_train_e2e/shared_plan.md`), each holding a
distinct slot symbol like `md:shared_plan.slot_01`.

## Environment

- Merge Train repo: `<merge_train_repo>`
- Mctrl Test repo: `<mctrl_test_repo>`
- Python: 3.13.7 (main, Aug 14 2025, 11:12:11) [Clang 17.0.0 (clang-1700.0.13.3)]
- Runner: e2e_md_area_lock_runner.py

## Steps

1. Create file_domains.yaml with e2e_shared_markdown domain
2. Reserve 20 area locks via domain_lock reserve-plan
3. List active locks, verify 20 distinct symbols, 0 whole-domain
4. Run 3 negative controls
5. Create 20 PRs (one per slot)
6. Verify PRs
7. Run merge simulations
8. Release all locks
9. Verify 0 active test PR locks remain

## Full Git Provenance

| Item | Value |
|------|-------|
| merge_train HEAD | `4758c66db662a880e2f6e8485331c4ccf7e219ec` |
| merge_train branch | `main` (synced to origin/main) |
| merge_base | `4758c66db662a880e2f6e8485331c4ccf7e219ec` (same as HEAD) |
| commits ahead of main | 0 |
| diff stat vs main | (empty — on main) |
| mctrl_test HEAD | `95fd6a763136297d35e8062b81a6429215e3163d` |

## Negative Control Design

The "different area" control uses slot-21 (`md:shared_plan.slot_21`)
because the first 20 slots (01–20) are reserved by the 20 area locks
under test. Slot-21 is guaranteed to be unoccupied, so a reservation
on it must succeed — proving that the lock system permits concurrent
access to disjoint areas while blocking access to already-locked slots.
If the system used whole-file locks, slot-21 would also be denied,
contradicting the observed PASS result.

## Hook Contract

The `e2e_slot_worker.sh` hook enforces that the area lock is acquired
**before** the agent process starts, not after. The sequence is:

1. Hook receives slot assignment
2. Hook calls `domain_lock reserve` to acquire the slot symbol
3. Only after lock acquisition succeeds does the hook launch the agent
4. On agent exit (success or failure), the hook calls `domain_lock release`

This pre-start acquisition order is critical: if the lock were acquired
after the agent started, two agents could read the same section
simultaneously before either lock is held, violating the concurrency
invariant. The hook contract ensures lock-then-edit, not edit-then-lock.
