# AGENTS.md — merge_train

Recipes for AI agents working with this repo. Two audiences below — pick one.

---

## A. Agents *using* `merge_train` as a spawn / pre-commit gate

You're an agent (Aider / OpenHands / Devin / Claude / Codex / AO worker) about to modify someone else's code. Before you spawn a task or commit a change, gate your work through `merge_train` so two of you don't trample the same file/symbol scope.

The upstream `merge_train` package installs two CLI scripts:
1. `acquire` — check-and-reserve tool (spawn-time hook)
2. `predict-conflicts` — read-only pairwise analysis tool (pre-commit / CI gate)

### Minimum integration (spawn time)

At spawn time, run `acquire` against the in-flight plan:

```bash
# 1. PRE-SPAWN check-and-reserve:
acquire --plan pr_domain_locks.yaml \
        --registry file_domains.yaml \
        --branch feat/dice-fix \
        --agent $(whoami) \
        mvp_site/world_logic.py

test $? -eq 0 || { echo "REFUSE: file/domain held or conflicts with another PR"; exit 1; }
```

If successful, `acquire` writes the branch's reservation to the plan file (`pr_domain_locks.yaml`).

### Symbol-level check (commit time / pre-commit hook)

To predict conflicts before committing, run `predict-conflicts` (which automatically checks staged symbol diffs when running inside a git repo):

```bash
# 2. PRE-COMMIT check:
predict-conflicts --plan pr_domain_locks.yaml --registry file_domains.yaml
```

If the staged changes conflict with any *other* PR in the plan, `predict-conflicts` exits with `1` and prints the conflicting PR numbers and symbols.

### Exit codes (memorize these)

| Code | Meaning | Agent action |
|---|---|---|
| 0 | free / allowed / successfully acquired | proceed |
| 1 | held / conflict | refuse spawn / commit |
| 2 | config error (missing plan, bad args) | fix the call, do not retry blindly |

---

## B. Agents *modifying* `merge_train` itself

You're working on this codebase. Rules:

### Test discipline

```bash
pytest       # must stay green, currently 239 passed
```

- Add a regression test for every bug fix.
- Existing tests: `tests/test_acquire_files.py` (acquire CLI), `tests/test_predict.py` (predict-conflicts CLI), `tests/test_symbols.py` (AST symbol unit tests), `tests/test_lang_extractors.py` (multi-language parser checks).

### Style

- No new dependencies beyond `PyYAML` without explicit approval.
- CLI exit codes: 0 (success), 1 (collision/held), 2 (config error). Don't invent new codes.

### Push policy

- Commit on `main`. `git push --force-with-lease` only with explicit human approval naming the branch.
- After push, report old SHA → new SHA and the `https://github.com/jleechanorg/merge_train/commit/<sha>` URL.

### Evidence

When fixing a non-trivial bug or shipping a feature, drop a bundle at `/tmp/merge_train_evidence/proofs/<topic>/`:

- `raw.txt` — before/after CLI output with git SHAs.
- `SUMMARY.md` — claim, fix location (file:line), evidence map, verification commands.

The `/es` (evidence-standards) and `/er` (evidence-reviewer) skills audit these bundles — both must `PASS` for non-trivial work.

---

## C. Useful one-liners

```bash
# Predict conflicts for an in-flight plan:
predict-conflicts --plan pr_domain_locks.yaml --registry file_domains.yaml

# Run predict-conflicts emitting JSON output:
predict-conflicts --plan pr_domain_locks.yaml --registry file_domains.yaml --json | jq

# Acquire files programmatically:
python -m merge_train.acquire --plan pr_domain_locks.yaml --branch feat/test-branch --agent my-agent file1.py
```
