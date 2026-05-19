# Evidence Summary — OpenCode Markdown Area-Lock E2E

## Run ID: 20260519T073247Z

## Results: 8 passed, 0 failed

| Scenario | Passed | Errors |
|----------|--------|--------|
| fixture_setup | PASS | none  |
| area_lock_reservation | PASS | none  |
| active_lock_verification | PASS | none  |
| negative_controls | PASS | none  |
| pr_creation | PASS | none  |
| pr_verification | PASS | none verified via gh pr view |
| merge_simulation | PASS | none  |
| lock_release | PASS | 0 active test PRs remain  |


## Non-Negotiable Claim

Multiple real agents can concurrently edit disjoint sections of the same
Markdown file because merge_train acquires and releases area-level locks,
not whole-file locks.

## Evidence Map

| Claim | File | Key Field |
|-------|------|-----------|
| 20 area locks reserved | run.json | reserve_results[].reserved |
| 20 distinct symbols active | active_during_run.json | symbols per entry |
| 0 whole-domain locks | active_during_run.json | entries with empty symbols |
| Duplicate slot denied | run.json | negative_controls[0].passed |
| Whole-domain denied | run.json | negative_controls[1].passed |
| Different area allowed | run.json | negative_controls[2].passed |
| All locks released | active_after_release.json | 0 test PR entries |
| PR #255–#274 created | prs.json | pr_url per entry |
| No merge conflicts | pairwise_merge_tree.json + sequential_merge_tree.json | conflict_count |
| Fixture branch setup | run.json | scenarios[0] |

## What This Evidence Does NOT Prove

- Production OpenCode agent behavior (tested with CLI reservations)
- Automatic Markdown heading extraction (explicit symbols used)
- Real AO/OpenCode worker transcript (local lock model proof only)
