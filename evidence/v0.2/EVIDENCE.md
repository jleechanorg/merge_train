# Evidence Bundle — merge_train v0.2

**Subject:** merge_train v0.2 — registry expressivity flags + symbol discovery
**Commit:** `7b0c154ab6da8aa21902bdd74e15b4a240eb064d` (HEAD of `main`)
**Date:** 2026-05-18
**Repo:** `jleechanorg/merge_train`
**Bundle location:** `evidence/v0.2/`

This is the canonical evidence bundle for v0.2. All artifacts are in `evidence/v0.2/artifacts/`
with SHA-256 sidecars; bundle metadata is in `metadata.json`.

---

## Claim graph

| # | Claim                                                                                                                      | Evidence type   | Artifact                                                       |
|---|----------------------------------------------------------------------------------------------------------------------------|-----------------|----------------------------------------------------------------|
| C1 | Two new YAML registry flags (`per_pr_unique`, `advisory`) are parsed and stored on `Domain`.                              | Code + unit test | `merge_train/domain_lock.py:64-90`; `tests/test_predict.py::test_per_pr_unique_domain_parsed_from_yaml`, `::test_advisory_domain_parsed_from_yaml` |
| C2 | `per_pr_unique` domains never contribute to pairwise conflicts.                                                            | Code + unit test | `merge_train/predict.py:213-225`; `tests/test_predict.py::test_per_pr_unique_domain_not_flagged_as_conflict`, `::test_pair_domain_conflicts_skips_per_pr_unique` |
| C3 | `advisory` domains emit `DomainConflict(advisory=True)`, do **not** set `is_conflict`, and surface in a separate JSON key. | Code + unit test | `merge_train/predict.py:158-161, 217-225`; `tests/test_predict.py::test_advisory_domain_not_counted_as_blocking_conflict`, `::test_advisory_domain_appears_in_advisory_conflicts_json` |
| C4 | Advisory conflicts do not affect parallel-batch scheduling.                                                                | Unit test       | `tests/test_predict.py::test_advisory_domain_does_not_affect_batch_scheduling` |
| C5 | Module `merge_train/symbol_discovery.py` exposes `symbols_from_staged_diff`, `symbols_from_pr_diff`, `symbols_from_files_in_pr`. | Code + unit test | `merge_train/symbol_discovery.py`; `tests/test_predict.py::test_split_diff_by_file_basic`, `::test_symbols_from_staged_diff_no_git` |
| C6 | CLI `predict-conflicts` accepts `--from-prs`, `--enrich-symbols`, `--repo`.                                                | Code + smoke    | `merge_train/domain_lock.py:668-687`; `python -m merge_train.domain_lock predict-conflicts --help` |
| C7 | Empirical dry-run on worldarchitect.ai (30 real PRs) shows blocking conflicts drop from 371 to 217 (-42%).                 | JSON artifacts  | `artifacts/result_v1.json` + `artifacts/result_v2.json` (+ SHA-256 sidecars) |
| C8 | First parallel batch grows from 4 PRs to 8 PRs; total batches drop from 27 to 17.                                          | JSON artifacts  | `artifacts/result_v1.json::parallel_batches[0]`, `artifacts/result_v2.json::parallel_batches[0]` |
| C9 | Full test suite: 193 passing (180 prior + 13 new for v0.2).                                                                | pytest run      | `python -m pytest tests/ -q` from repo root → `Pytest: 193 passed` |

---

## Test verification (reproducible)

```
$ python -m pytest tests/ -q --tb=no
Pytest: 193 passed
```

Test files changed:
```
tests/test_predict.py | +204 / -0 (193 total, 13 new)
```

New tests (13):
1. `test_per_pr_unique_domain_not_flagged_as_conflict`
2. `test_per_pr_unique_domain_not_in_pair_conflicts`
3. `test_per_pr_unique_domain_does_not_affect_unrelated_conflict`
4. `test_advisory_domain_not_counted_as_blocking_conflict`
5. `test_advisory_domain_appears_in_advisory_conflicts_json`
6. `test_advisory_domain_does_not_affect_batch_scheduling`
7. `test_per_pr_unique_domain_parsed_from_yaml`
8. `test_advisory_domain_parsed_from_yaml`
9. `test_pair_domain_conflicts_skips_per_pr_unique`
10. `test_pair_domain_conflicts_marks_advisory`
11. `test_split_diff_by_file_basic`
12. `test_split_diff_by_file_empty`
13. `test_symbols_from_staged_diff_no_git`

---

## Dry-run methodology

**Input PR set:** Top 30 open PRs in `jleechanorg/worldarchitect.ai` snapshot 2026-05-18.
File lists captured via `gh pr diff <pr> --name-only` per PR and serialized to
`artifacts/prs.yaml` (30 entries, 58K).

**Registries compared:**
- `artifacts/file_domains_v1.yaml` (originally hand-authored, 23 domains)
- `artifacts/file_domains_v2.yaml` (revised per dry-run findings, 27 domains)

**v2 diff from v1:**
- `pr-design-docs`: added `per_pr_unique: true` (was emitting 351 spurious pairs on `pr-N.md` files)
- `beads`: added `advisory: true` (`.beads/*.jsonl` is append-only — conflicts are review-time merges, not spawn-time blocks)
- Added: `evidence-artifacts`, `codex-automation`, `npm-deps`, `frontend-assets`

**Reproducer commands** (from repo root, on commit `7b0c154`):
```bash
python -m merge_train.domain_lock \
  --registry evidence/v0.2/artifacts/file_domains_v1.yaml \
  --log /tmp/locks_repro_v1.jsonl \
  predict-conflicts --plan evidence/v0.2/artifacts/prs.yaml --no-textual --json \
  > /tmp/result_v1_repro.json

python -m merge_train.domain_lock \
  --registry evidence/v0.2/artifacts/file_domains_v2.yaml \
  --log /tmp/locks_repro_v2.jsonl \
  predict-conflicts --plan evidence/v0.2/artifacts/prs.yaml --no-textual --json \
  > /tmp/result_v2_repro.json

diff /tmp/result_v1_repro.json evidence/v0.2/artifacts/result_v1.json  # empty
diff /tmp/result_v2_repro.json evidence/v0.2/artifacts/result_v2.json  # empty
```

Both diffs are empty — the dry-run is fully deterministic (greedy MIS keyed on `(degree, node_id)`, no RNG).

---

## Quantitative results

| Metric                            | v1   | v2   | Delta             |
|-----------------------------------|------|------|-------------------|
| PRs analyzed                      | 30   | 30   | —                 |
| Total pairs (N choose 2)          | 435  | 435  | —                 |
| **Blocking** conflicts            | 371  | 217  | -154 (-42%)       |
| **Advisory** conflicts (new)      | n/a  | 46   | (informational)   |
| Clean pairs                       | 18   | 172  | +154              |
| Parallel batches                  | 27   | 17   | -10               |
| Size of first parallel batch      | 4    | 8    | +4 (2x)           |
| PRs with unmapped files (warning) | 23   | 20   | -3                |

**Top remaining blocking domains (post-v2):**

| Count | Domain               | Notes |
|-------|----------------------|-------|
| 136   | `level-up-pipeline`  | Real hot path (`world_logic.py`, `rewards_engine.py`); next target for symbol-level locking |
| 55    | `testing-mcp`        | Real concurrent edits to testing framework |
| 36    | `ci-workflows`       | Real concurrent CI edits |
| 36    | `prompt-contracts`   | Real (single JSON file) |
| 28    | `llm-parser`         | Real |
| 15    | `agent-instructions` | Real (CLAUDE.md / README.md) |

These are **plausibly real** conflicts — the 154 false positives from v1 are gone, and
the remaining domains all map to single hot files. A future audit could spot-check N
random pairs by running `git merge-tree` against the actual PR branches to quantify the
real vs predicted agreement rate.

---

## SHA-256 integrity

Each artifact has a `.sha256` sidecar alongside it. Verify on read:

```bash
cd evidence/v0.2/artifacts
shasum -a 256 -c *.sha256
```

---

## Limitations / known-unknowns

- The 217 remaining blocking conflicts are **plausibly real** but not exhaustively
  spot-checked against actual git merge outcomes. A future audit could run
  `git merge-tree` against the 30 PR branches and compare predict-conflicts'
  domain-level signal to actual textual conflicts.
- `--enrich-symbols` and `--from-prs` paths are CLI-wired but not exercised in
  this evidence bundle (would require live GitHub API calls). Unit tests cover
  the splitter and the no-git fallback; the gh-CLI path is integration-only.
- `symbols_from_pr_diff` requires `gh api` access to read post-edit file content;
  rate limits apply for large PR sets. No back-off implemented. Files >1 MB
  silently skip (GitHub Contents API limit).
- `symbols.py` covers Python only; `.go`, `.ts`, `.rs` files always fall back to
  whole-file/whole-domain locking. Out of scope for v0.2.

---

## Reviewer disposition

- **evidence-reviewer (subagent):** PASS. All 9 claims independently re-derived from
  cited artifacts. Top-domain table (136/55/36/36/28/15) reproduces exactly. 193
  tests confirmed passing on `7b0c154`. NEEDS-WORK items (missing `metadata.json`
  and `.sha256` sidecars) were addressed by this revision (`evidence/v0.2/` layout).
- **codex-consultant (subagent):** see `reviews/codex_v0_2.md` (added after that
  reviewer completes).
