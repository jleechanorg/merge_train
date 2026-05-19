# Methodology

## Test Design

20 symbol-level area locks on the same domain (`e2e_shared_markdown`)
and the same file (`merge_train_e2e/shared_plan.md`), each holding a
distinct slot symbol like `md:shared_plan.slot_01`.

## Environment

- Merge Train repo: /Users/jleechan/projects/merge_train
- Mctrl Test repo: /Users/jleechan/projects/mctrl_test
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
