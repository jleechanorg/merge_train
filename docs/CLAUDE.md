# CLAUDE.md — merge_train (repo-local policy)

Repo-local Claude Code instructions. Layers on top of the user's global `~/.claude/CLAUDE.md`.

## Primary reference

Read **`docs/AGENTS.md`** first. It contains the full how-to for:
- Using `merge_train` as a spawn / pre-commit gate (Section A)
- Modifying this repo (Section B)
- Useful one-liners (Section C)

This file (`docs/CLAUDE.md`) only carries the Claude-specific deltas.

## Quick facts

- **Package:** `merge_train` (installable via `pip install -e .`)
- **CLI:** `predict-conflicts` (standalone) and `acquire` (standalone)
- **Tests:** `pytest` — must stay green (currently 277).
- **Git remote:** `https://github.com/jleechanorg/merge_train.git`
- **Main branch:** `main` (no PR pipeline for this repo yet; commits land on main directly).

## What this repo IS

A spawn-time file-domain lock registry. Stops two AI agents from grabbing the same files in parallel. Symbol-level locks let two PRs co-edit disjoint functions in the same file.

## What this repo IS NOT

- Not a merge queue (that's Mergify / Gas Town).
- Not a commit-time tool (that's Graphite / jj).
- Not coupled to AO / OpenHands / Aider — works with any pipeline that can call a CLI before spawn.

## Test before push

Every commit on `main` must pass `pytest`. No exceptions — there is no CI yet for this repo, so the local pytest is the only gate.

## Evidence convention

For non-trivial work (bug fix, feature, hardening), drop a bundle at `/tmp/merge_train_evidence/proofs/<topic>/` with `raw.txt` (before/after with SHAs) and `SUMMARY.md` (claim + verification commands). `/es` and `/er` must both `PASS`.

## Adversarial-review convention

After any non-trivial change, spawn the `code-review` subagent with adversarial framing. Address `CRITICAL` and `MAJOR` findings before declaring done. Example pattern: parser-compat fix `ce47114` → adversarial review → follow-up `15e2d0c`.

## Force-push policy

`git push --force-with-lease=main:<old-sha> origin main` only with explicit in-thread human approval. After force-push, report old SHA → new SHA and the commit URL.

## File-touch sensitivity (this repo's own domains)

Production code lives in `merge_train/`:
- `acquire.py` — atomic check CLI
- `predict.py` — conflict prediction engine
- `symbols.py` — Python AST symbol resolution + git-diff hunk parser
- `lang_extractors.py` — Multi-language symbol extraction (TS/Go/etc.)
- `symbol_discovery.py` — Symbol database discovery
- `hook_install.py` — Hooks installer

Tests pair 1:1 with module concerns:
- `tests/test_acquire_files.py` — acquire unit tests
- `tests/test_predict.py` — predict-conflicts unit tests
- `tests/test_symbols.py` — AST-symbol unit tests
- `tests/test_lang_extractors.py` — language parser tests
- `tests/test_hook_install.py` — hook installation checks
- `tests/test_evidence_bundle.py` — evidence bundle schema validation
- `tests/test_domain_recommender.py` — domain recommender checks

## Roadmap discipline

Append rolling activity to `roadmap/README.md` (newest entry first). Link commit SHAs and PR URLs (full `https://github.com/jleechanorg/merge_train/commit/<sha>` form, per user-global policy).
