# Codex Adversarial Review — merge_train v0.2

**Consultant:** codex (gpt-5.5, high reasoning) | **Subject:** commit `7b0c154`

## Findings (severity-ranked)

### MAJOR (2)

**M1. `--enrich-symbols` silently does nothing without `--repo`.**
`symbols_from_pr_diff(..., repo=None)` only resolves `head_ref` when `repo`
is truthy. When `repo` is None, `_gh_file_content_at_ref` is never called,
content stays empty, every Python file is skipped, and the function
returns `{}` with no error. CLI also accepted `--enrich-symbols` without
`--repo` and silently fell back to whole-file specs.

**Status: FIXED** (follow-up commit)
- `merge_train/symbol_discovery.py`: added `_detect_repo_from_git_remote()`
  that parses `owner/repo` from `git remote get-url origin` (SSH and HTTPS).
- `merge_train/predict.py:cli_predict_conflicts`: when `--enrich-symbols`
  is set and `--repo` is absent, auto-detect from git remote. If neither
  succeeds, exit 2 with `error: --enrich-symbols requires --repo OWNER/REPO`.
- Tests added: `test_detect_repo_from_git_remote_{ssh,https,no_match,no_git}`,
  `test_enrich_symbols_errors_without_repo_or_remote`.

**M2. Large GitHub files silently under-deliver symbols.**
`_gh_file_content_at_ref()` used the Contents API `.content` base64 field.
For files >1 MB the API returns `encoding=none` with empty `content`; old
code base64-decoded empty string and returned "". Callers omitted those
files with no warning.

**Status: FIXED** (follow-up commit)
- Primary path now uses `gh api -H "Accept: application/vnd.github.raw" ...`
  which streams raw bytes regardless of size (up to repo blob limit).
- Fallback to Contents API with explicit `encoding=="base64"` check;
  encoding != "base64" → `logging.warning(...)` + return "".
- Tests added: `test_gh_file_content_at_ref_uses_raw_accept_header`,
  `test_gh_file_content_at_ref_handles_encoding_none`.

### MINOR (2)

**N1. Catch-all `except Exception:` hides defects.**
Three locations swallowed all exceptions in symbol discovery.

**Status: FIXED** (follow-up commit)
- Narrowed to `except SyntaxError` (separate `logging.debug` message)
  followed by `except Exception` (still catches, but now logs).
- `_log = logging.getLogger(__name__)` set at module top.

**N2. `_pair_domain_conflicts(registry=None)` path doesn't apply v0.2 flags.**
Production `predict_conflicts()` always passes `registry`. The default-None
path is intentional for legacy tests, but was undocumented.

**Status: DOCUMENTED** (follow-up commit)
- Added a `.. note::` block in the docstring stating that callers wanting
  v0.2 semantics MUST pass `registry`, and the no-registry path is only
  preserved for legacy v0.1 tests.

## Clean items (no issues found)

| Q | Topic | Verdict |
|---|-------|---------|
| Q1 | Backwards compat (dataclass field additions) | Safe — appended w/ defaults |
| Q4 | `--plan` CLI regression | No break; help text sensible; runtime guard works |
| Q5 | `_print_human()` double-print | No bug; advisory section filters `is_conflict=False` |
| Q6 | `Plan.to_json_dict()` shape change | No downstream consumer; tests already updated |

## Summary

| Severity | Initial | Post-fix |
|----------|---------|----------|
| BLOCKER  | 0       | 0        |
| MAJOR    | 2       | 0        |
| MINOR    | 2       | 0 (1 fixed + 1 documented) |

**Initial verdict:** shippable.
**Post-fix verdict:** all 4 findings addressed. 200 tests passing (193 + 7 new).
