# Evidence Addendum — merge_train v0.2.2

**Subject:** Review-blocker fixes for v0.2/v0.2.1
**Code commit verified:** `286f7d88442ab9d348be7cf24a8fcc0d82e0f4e1`
**Generated:** 2026-05-19T00:54:03Z

## Fixes

1. `symbols_from_staged_diff()` now omits staged Python files whose symbols cannot be resolved because of `SymbolResolutionError`, preserving the helper contract that unresolved files are omitted for caller-side whole-file fallback.
2. `predict-conflicts --from-prs` now fails closed if any requested PR cannot be loaded, instead of returning success for a partial PR set.

## TDD Proof

Regression tests were added first and observed failing:

```text
tests/test_predict.py::test_symbols_from_staged_diff_parse_failure_is_omitted
tests/test_predict.py::test_from_prs_fails_closed_when_any_requested_pr_cannot_load
```

Initial red run:

```text
2 failed in 0.54s
```

Post-fix targeted run:

```text
2 passed in 0.50s
```

## Verification

Full suite:

```text
python -m pytest -q
202 passed in 6.17s
```

Dry-run reproducibility for the v2 artifact:

```text
python -m merge_train.domain_lock \
  --registry evidence/v0.2/artifacts/file_domains_v2.yaml \
  --log /tmp/merge_train_v2_fix_locks.jsonl \
  predict-conflicts --plan evidence/v0.2/artifacts/prs.yaml --no-textual --json \
  > /tmp/result_v2_fix_repro.json

cmp -s /tmp/result_v2_fix_repro.json evidence/v0.2/artifacts/result_v2.json
# exit 0
```

SHA-256 sidecars:

```text
file_domains_v1.yaml: OK
file_domains_v2.yaml: OK
prs.yaml: OK
result_v1.json: OK
result_v2.json: OK
```

Whitespace check:

```text
git diff --check
# pass
```

## Notes

This addendum supersedes the stale test-count wording in `evidence/v0.2/EVIDENCE.md` for the review-fix commits. The original v0.2 bundle remains the canonical dry-run artifact bundle; this file records the follow-up review-blocker fixes and their current verification.
