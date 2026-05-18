# Evidence-Reviewer Audit — merge_train v0.2

**Reviewer:** evidence-reviewer subagent (Sonnet) | **Subject:** commit `7b0c154` | **Verdict:** **PASS**

## Phase 1 — Structure
- **Required canonical files:** PARTIAL — initial bundle was a single flat
  `EVIDENCE_v02.md` in `/tmp/wa_dry_run/`, missing `metadata.json`,
  `.sha256` sidecars, methodology.md, artifacts/ dir, latest/ symlink.
- **Severity:** NEEDS-WORK (bundle hygiene), not FAIL (evidence integrity).

**Addressed in this revision:**
- Moved to in-repo `evidence/v0.2/` layout
- Generated SHA-256 sidecars for all 5 artifacts
- Added `metadata.json` with git_head, merge_base, headline_metrics
- Added `reviews/` subdir with this report + codex report

## Phase 2 — Integrity
- Claim → artifact map: **PASS**. No circular citations; every row points
  to a file:line range, named test, or JSON key.
- Raw artifact existence: **PASS**. All 5 files exist and are non-empty.
- Collection log: N/A (commands are copy-pasteable in methodology).

## Phase 3 — Claim verification (re-derived independently)

| # | Claim                                                    | Verdict | Re-derivation |
|---|----------------------------------------------------------|---------|---------------|
| C1 | YAML parses `per_pr_unique`/`advisory` onto `Domain`     | PASS    | `domain_lock.py:69-70, 85-89` exact match |
| C2 | per_pr_unique skipped in pair conflicts                  | PASS    | `predict.py:215-216` `continue` |
| C3 | advisory emits `DomainConflict(advisory=True)`, not in `is_conflict` | PASS | `predict.py:159-161, 217, 220, 224`; JSON key `advisory_conflicts` confirmed present in `result_v2.json` (absent in `result.json`) |
| C4 | advisory doesn't affect batch scheduling                 | PASS    | test passes |
| C5 | symbol_discovery exports three named symbols             | PASS    | `symbol_discovery.py:70, 164, 206` |
| C6 | CLI flags wired                                          | PASS    | `domain_lock.py:670, 673, 676` |
| **C7** | **371→217 (42%)**                                    | **PASS — re-derived** | `len(v1.pairwise_conflicts)=371`, `len(v2.pairwise_conflicts)=217`. 154/371 = 41.5%, rounded 42% ✓. v2 advisory = 46 ✓. |
| C8 | 27→17 batches, first batch 4→8                          | PASS — re-derived | exact match |
| C9 | 193 passing                                              | PASS — re-ran | `Pytest: 193 passed` |

**Spot-checked tests** (independently re-ran 3): `test_per_pr_unique_domain_parsed_from_yaml`,
`test_advisory_domain_appears_in_advisory_conflicts_json`, `test_split_diff_by_file_basic` → all PASS.

**Top-domain table re-derived from `result_v2.json`:**
`136 level-up-pipeline / 55 testing-mcp / 36 ci-workflows / 36 prompt-contracts / 28 llm-parser / 15 agent-instructions` — exact match.

## Honesty / overclaim audit

- "plausibly real" (line 115, 141): correctly hedged.
- C7 "42% reduction" mechanically true for predictor output. Does NOT claim
  42% fewer *true* merge conflicts — only 42% fewer *flagged blocking pairs*.
- Limitations section (4 items): all nice-to-have, not blockers.

## Recommendations addressed in this revision

- ✅ Emit `metadata.json` with git_head, branch, merge_base, bundle_timestamp.
- ✅ Generate SHA-256 sidecars on all artifacts.
- ✅ Move artifacts out of `/tmp/` into `evidence/v0.2/`.
- ⏸️ "Run `git merge-tree` on subsample to upgrade plausibly-real → measured
  agreement rate" — deferred to v0.3 (out of scope for v0.2 expressivity).
