# OpenCode Markdown Area-Lock E2E

This is the acceptance runbook for proving `merge_train` works with real
OpenCode agents against `jleechanorg/mctrl_test`.

The test is intentionally stronger than a shell-only proof:

- Use real OpenCode workers launched through AO, or direct `openw run` if AO is
  unavailable.
- Create about 20 real pull requests against `mctrl_test`.
- Make all workers edit different areas of the same Markdown file.
- Prove the file itself is not globally locked: concurrent workers must hold
  separate area locks under the same file/domain.
- Capture lock acquisition, refusal, release, PR URLs, and agent transcripts as
  reproducible evidence.

## Non-Negotiable Claim

Passing this E2E means:

> Multiple real OpenCode agents can concurrently edit disjoint sections of the
> same Markdown file because `merge_train` acquires and releases area-level
> locks, not whole-file locks.

If the implementation falls back to a whole-file lock for the shared Markdown
file, this E2E fails even if conflicts are avoided.

## Current Gap This Test Must Catch

`merge_train` currently has Python symbol-level locks. Markdown area locks are
not yet discovered automatically from diffs. For the first E2E, represent
Markdown areas as explicit lock symbols in the plan:

```yaml
plan:
  - domain: e2e_shared_markdown
    symbols: [md:e2e_shared_plan.slot_01]
```

That proves the lock model and agent integration without pretending Markdown
parsing exists. A later version can replace explicit task areas with automatic
Markdown heading extraction.

## Repos and Paths

Use these absolute paths:

```bash
MERGE_TRAIN_REPO=/Users/jleechan/projects/merge_train
MCTRL_REPO=/Users/jleechan/projects/mctrl_test
REMOTE_REPO=jleechanorg/mctrl_test
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
SCRATCH="/tmp/merge_train_opencode_md_area_lock/${RUN_ID}"
EVIDENCE="/tmp/merge_train_evidence/opencode_md_area_lock/${RUN_ID}"
LOCK_LOG="${EVIDENCE}/pr_domain_locks.jsonl"
BRANCH_PREFIX="merge-train-e2e/${RUN_ID}"
```

Do not run this in the existing `mctrl_test` checkout. That checkout may have
unrelated local evidence files. Create isolated worktrees from `origin/main`.

## Test Fixture

Create one setup branch and PR that adds the shared Markdown file and registry:

```text
merge_train_e2e/shared_plan.md
merge_train_e2e/tasks.md
file_domains.yaml
```

`merge_train_e2e/shared_plan.md` must contain 20 stable headings:

```markdown
# Shared plan

## slot-01
status: pending

## slot-02
status: pending

...

## slot-20
status: pending
```

`merge_train_e2e/tasks.md` is the file agents follow. It must include one task
block per PR:

```markdown
## slot-01 task

- branch: merge-train-e2e/${RUN_ID}/slot-01
- lock-domain: e2e_shared_markdown
- lock-symbol: md:e2e_shared_plan.slot_01
- file: merge_train_e2e/shared_plan.md
- heading: ## slot-01
- required edit: replace `status: pending` with
  `status: complete by slot-01`
- forbidden edit: any other heading
```

`file_domains.yaml` must group the shared Markdown file into one domain:

```yaml
domains:
  e2e_shared_markdown:
    paths:
      - merge_train_e2e/shared_plan.md
  all_other_files:
    paths:
      - "*"
```

The important detail: all 20 PRs touch the same file and the same domain, but
each PR reserves a different symbol.

## Hook Contract Under Test

The test must install a launch hook/wrapper that does acquisition before the
agent starts writing.

Required behavior:

1. Read the task block from `merge_train_e2e/tasks.md`.
2. Build a one-leg plan with `domain=e2e_shared_markdown` and the task's
   `lock-symbol`.
3. Atomically reserve the plan:

   ```bash
   domain_lock --registry file_domains.yaml --log "${LOCK_LOG}" \
     reserve-plan --pr "${PR_NUM_OR_SYNTHETIC_ID}" \
     --agent "${AGENT_ID}" \
     --branch "${BRANCH}" \
     --plan "${PLAN_FILE}"
   ```

4. Launch the real OpenCode worker only if the reservation exits 0.
5. Keep the reservation active while the worker edits, commits, pushes, and
   opens the PR.
6. Release the reservation only when the PR is merged, closed, or explicitly
   abandoned:

   ```bash
   domain_lock --registry file_domains.yaml --log "${LOCK_LOG}" \
     release --pr "${PR_NUM_OR_SYNTHETIC_ID}"
   ```

`check` alone is not acceptable. The evidence must show `reserve-plan` or
`acquire` wrote active lock entries before the agent edited the file.

## Launch Lane A: AO + OpenCode

Preferred lane:

```bash
cd "${MCTRL_REPO}"
ao start "${MCTRL_REPO}"
ao spawn --agent opencode --project mctrl_test \
  "Follow merge_train_e2e/tasks.md for slot-01 only. Do not edit any other slot."
```

The AO integration must prove the worker is really OpenCode:

```bash
ao session ls
# For each session, capture session metadata showing:
# agent=opencode
# launch command includes opencode/openw
# worktree path
# branch name
```

If AO cannot launch `opencode`, use Lane B and record the AO failure in
`evidence.md`.

## Launch Lane B: Direct `openw run`

Fallback lane:

```bash
cd "${WORKTREE}"
openw run --dangerously-skip-permissions \
  "Follow merge_train_e2e/tasks.md for slot-01 only. \
   Edit only merge_train_e2e/shared_plan.md under heading ## slot-01. \
   Commit, push, and open a PR."
```

The wrapper path must be captured:

```bash
command -v openw
command -v opencode
openw run --help
```

## Twenty-PR Matrix

Create 20 branches from the setup branch or from `origin/main` after the setup
PR is merged:

| PR Lane | Branch Suffix | Required Lock Symbol |
|---------|---------------|----------------------|
| 01 | slot-01 | `md:e2e_shared_plan.slot_01` |
| 02 | slot-02 | `md:e2e_shared_plan.slot_02` |
| 03 | slot-03 | `md:e2e_shared_plan.slot_03` |
| 04 | slot-04 | `md:e2e_shared_plan.slot_04` |
| 05 | slot-05 | `md:e2e_shared_plan.slot_05` |
| 06 | slot-06 | `md:e2e_shared_plan.slot_06` |
| 07 | slot-07 | `md:e2e_shared_plan.slot_07` |
| 08 | slot-08 | `md:e2e_shared_plan.slot_08` |
| 09 | slot-09 | `md:e2e_shared_plan.slot_09` |
| 10 | slot-10 | `md:e2e_shared_plan.slot_10` |
| 11 | slot-11 | `md:e2e_shared_plan.slot_11` |
| 12 | slot-12 | `md:e2e_shared_plan.slot_12` |
| 13 | slot-13 | `md:e2e_shared_plan.slot_13` |
| 14 | slot-14 | `md:e2e_shared_plan.slot_14` |
| 15 | slot-15 | `md:e2e_shared_plan.slot_15` |
| 16 | slot-16 | `md:e2e_shared_plan.slot_16` |
| 17 | slot-17 | `md:e2e_shared_plan.slot_17` |
| 18 | slot-18 | `md:e2e_shared_plan.slot_18` |
| 19 | slot-19 | `md:e2e_shared_plan.slot_19` |
| 20 | slot-20 | `md:e2e_shared_plan.slot_20` |

Success condition: while the 20 workers are active, this command shows 20 active
entries on the same domain with 20 distinct symbols:

```bash
domain_lock --registry file_domains.yaml --log "${LOCK_LOG}" \
  list --status active --json | jq .
```

## Negative Controls

Run these after at least one slot lock is active:

1. Try to acquire `md:e2e_shared_plan.slot_01` for a second worker. It must be
   denied before OpenCode starts.
2. Try a whole-domain reservation for `e2e_shared_markdown`. It must be denied
   while any area lock is active.
3. Try a different area, for example `md:e2e_shared_plan.slot_20`. It must be
   allowed while slot-01 is active.

These controls prove the system is neither fail-open nor whole-file locked.

## PR Verification

For each PR:

```bash
gh pr view "${PR_URL_OR_NUMBER}" --repo "${REMOTE_REPO}" \
  --json number,url,headRefName,headRefOid,baseRefName,files

git diff --name-only "origin/main...${BRANCH}"
git diff "origin/main...${BRANCH}" -- merge_train_e2e/shared_plan.md
```

Each PR must:

- Touch `merge_train_e2e/shared_plan.md`.
- Modify exactly one `## slot-NN` section.
- Leave all other slots unchanged.
- Have a matching lock-log entry whose `branch` and symbol match the task.

## Mergeability Verification

Before merging anything, run pairwise and sequential merge simulation:

```bash
git fetch origin main
for branch in $(cat "${EVIDENCE}/branches.txt"); do
  git fetch origin "${branch}:${branch}"
done

# Pairwise textual simulation.
python "${MERGE_TRAIN_REPO}/scripts/e2e_pairwise_merge_tree.py" \
  --base origin/main \
  --branches-file "${EVIDENCE}/branches.txt" \
  --output "${EVIDENCE}/pairwise_merge_tree.json"

# Sequential batch simulation.
python "${MERGE_TRAIN_REPO}/scripts/e2e_sequential_merge_tree.py" \
  --base origin/main \
  --branches-file "${EVIDENCE}/branches.txt" \
  --output "${EVIDENCE}/sequential_merge_tree.json"
```

If these helper scripts do not exist yet, implement them before running the E2E.
Do not replace this with a handwritten claim.

## Release Verification

After PRs are merged or closed:

```bash
for pr in $(cat "${EVIDENCE}/prs.txt"); do
  domain_lock --registry file_domains.yaml --log "${LOCK_LOG}" release --pr "${pr}"
done

domain_lock --registry file_domains.yaml --log "${LOCK_LOG}" \
  list --status active --json > "${EVIDENCE}/active_after_release.json"
```

`active_after_release.json` must contain zero active locks for the 20 PRs.

## Evidence Bundle

Write artifacts to:

```bash
/tmp/merge_train_evidence/opencode_md_area_lock/${RUN_ID}/
```

Required files:

- `README.md` - run manifest, exact repo SHAs, operator, command summary.
- `metadata.json` - `merge_train` SHA, `mctrl_test` base SHA, branch prefix,
  OpenCode version/help output, AO version/help output, environment paths.
- `run.json` - scenario array with 20 PR lanes plus negative controls.
- `lock_log.jsonl` - copy of `${LOCK_LOG}`.
- `active_during_run.json` - active locks while workers are running.
- `active_after_release.json` - active locks after release.
- `prs.json` - GitHub API export for all created PRs.
- `branches.txt` and `prs.txt`.
- `pairwise_merge_tree.json`.
- `sequential_merge_tree.json`.
- `agent_transcripts/` - one transcript per OpenCode/AO worker.
- `hook_config/` - copied hook wrapper, AO config, OpenCode discovery output.
- `checksums.txt` plus one `.sha256` sidecar for each required artifact.

The summary may say "real OpenCode E2E passed" only if every file above exists
and the checksums verify.

## Pass/Fail Gates

Fail the run if any of these occur:

- Any worker starts without an active lock entry.
- Any lock entry for a slot uses a whole-domain lock instead of a symbol.
- Two workers acquire the same slot symbol.
- A duplicate-slot negative control starts OpenCode instead of refusing.
- A whole-domain negative control succeeds while area locks are active.
- Any PR edits a slot other than its assigned slot.
- Any PR omits a matching GitHub URL or head SHA in `prs.json`.
- Release leaves active locks behind for the test PRs.
- Evidence is missing raw transcripts, lock log, or checksum sidecars.

## Cleanup

Do not delete evidence. Cleanup only worktrees and temporary launch files:

```bash
git -C "${MCTRL_REPO}" worktree prune
rm -rf "${SCRATCH}"
```

If test PRs should not remain open, close them with an explicit comment:

```bash
gh pr close "${PR}" --repo "${REMOTE_REPO}" \
  --comment "Closing merge_train OpenCode area-lock E2E test PR."
```
