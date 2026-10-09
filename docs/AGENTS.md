# AGENTS.md — merge_train

Recipes for AI agents working with this repo. Two audiences below — pick one.

---

## A. Agents *using* `merge_train` as a spawn / pre-commit gate

You're an agent (Aider / OpenHands / Devin / Claude / Codex / AO worker) about to modify someone else's code. Before you spawn a task or commit a change, gate your work through `merge_train` so two of you don't trample the same file/symbol scope.

The upstream `merge_train` package installs two CLI scripts:
1. `acquire` — atomic file-list check tool (spawn-time hook)
2. `predict-conflicts` — read-only pairwise analysis tool (pre-commit / CI gate)

### Minimum integration (spawn time)

At spawn time, run `acquire` against the in-flight plan:

```bash
# 1. PRE-SPAWN check:
acquire --plan pr_domain_locks.yaml \
        --registry file_domains.yaml \
        --branch feat/dice-fix \
        --agent $(whoami) \
        mvp_site/world_logic.py

test $? -eq 0 || { echo "REFUSE: file/domain held or conflicts with another PR"; exit 1; }
```

Since `acquire` is a stateless decision check, it does not modify the plan file. To persist a reservation, the orchestrator or agent must append the branch's files and symbols to the plan file (`pr_domain_locks.yaml`).

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

### Per-repo enforcement config (Claude Code hook)

The Claude Code PreToolUse hook (`conflict-warn-pre-tool.sh`) reads `~/merge_train/config.json` to decide whether a conflict should **block**, **warn-only**, or **allow**. No per-repo install needed — one user-scope wiring covers every repo you work in.

```bash
# Initialise the config file (idempotent):
merge_train config init

# Add or update a repo entry:
merge_train config add /path/to/myrepo --enforce block
merge_train config add /path/to/other  --enforce warn --alias other

# Inspect / remove:
merge_train config show
merge_train config show /path/to/myrepo
merge_train config remove /path/to/other
```

If the file is missing, the hook falls back to a built-in default (`merge_train` = block, everything else = warn), so existing installs keep working.

### Hook activity log (where to look if you don't see anything in chat)

Every conflict-warn invocation is logged to a per-repo, per-branch daily file:

```
/tmp/merge_train/{repo_name}/{branch_name}/hook-YYYY-MM-DD.log
```

Nested branch names produce nested log directories: `feature/foo` writes to `/tmp/merge_train/<repo>/feature/foo/hook-YYYY-MM-DD.log` (one segment per `/` in the branch name). The `tail -f` snippet below follows the same nesting.

`tail -f` the file in a second terminal to watch conflict-check decisions live:

```bash
tail -f /tmp/merge_train/$(basename "$(git rev-parse --show-toplevel)")/$(git symbolic-ref --short HEAD)/hook-$(date +%Y-%m-%d).log
```

**Note on symlinks:** `git rev-parse --show-toplevel` resolves the **real** path of the repo root, so if you reach the repo via a symlink (e.g. `~/work/myrepo` → `/private/tmp/repo_test`), the log lives at `/tmp/merge_train/<real-basename>/...` — the symlink name is **not** used.

**Note on detached HEAD:** when `git symbolic-ref HEAD` fails (e.g. checking out a commit, a rebase in progress), the script falls back to the branch name `detached`. All detached-HEAD activity for that repo lands in a **single shared** `detached/` subdir. If you need to separate analysis by commit, confirm the branch first with `git symbolic-ref HEAD 2>/dev/null || git rev-parse --short HEAD`.

The hook also emits a top-level `systemMessage` in its JSON output, which Claude Code renders as a chat banner regardless of decision. The log file is a durable record even after the chat scrolls away.

---

## B. Agents *modifying* `merge_train` itself

You're working on this codebase. Rules:

### Test discipline

```bash
pytest       # must stay green, currently 277 passed
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
