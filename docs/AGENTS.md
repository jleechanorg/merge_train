# AGENTS.md — merge_train

Recipes for AI agents working with this repo. Two audiences below — pick one.

---

## A. Agents *using* `merge_train` as a spawn / pre-commit gate

You're an agent (Aider / OpenHands / Devin / Claude / Codex / AO worker) about to modify someone else's code. Before you spawn a task or commit a change, gate your work through `merge_train` so two of you don't trample the same file.

### Minimum integration (3 calls)

```bash
# 1. PRE-SPAWN: refuse to start if the file is held
domain_lock check --files mvp_site/world_logic.py --pr 7000
test $? -eq 0 || { echo "REFUSE: file held by another PR"; exit 1; }

# 2. RESERVE: claim it before writing
domain_lock reserve --domain level_up_core \
  --pr 7000 --agent $(whoami) --branch feat/dice-fix

# 3. RELEASE: after PR merges (or you give up)
domain_lock release --pr 7000
```

### Symbol-level locking (preferred when two agents need the same file)

```bash
# Reserve only the functions you'll modify
domain_lock reserve --domain level_up_core \
  --symbols compute_dice,_roll_die \
  --pr 7000 --agent claude-1 --branch feat/dice-fix
```

Other agents can co-tenant the same domain if their symbol sets don't overlap.

At commit time, use `--diff-mode` to scope the check to the staged diff's symbols (not the whole file):

```bash
git add mvp_site/world_logic.py
domain_lock check --files mvp_site/world_logic.py --pr 7000 --diff-mode
```

### Multi-domain task (use `reserve-plan`)

If your task touches several domains, **don't** call `reserve` in a loop — it's not atomic. Use `reserve-plan` so either all legs succeed or none do:

```yaml
# plan.yaml
plan:
  - domain: level_up_core
    symbols: [level_up]
  - domain: agents
    symbols: [Agent.tick]
```

```bash
domain_lock reserve-plan --pr 7000 --agent claude-1 \
  --branch feat/cross-domain --plan plan.yaml
```

### Working from a different git worktree

If your `cwd` is a worktree (`~/.agent-orchestrator/.../worker-foo/`) but the canonical repo is elsewhere, pass `--git-cwd` so the right remote-hash resolves the log path:

```bash
domain_lock --git-cwd /path/to/main/repo \
  check --files mvp_site/world_logic.py --pr 7000
```

`--git-cwd` works **before or after** the subcommand — both shapes parse (see commit `ce47114` for the backward-compat fix).

### How to respond to a `DENIED` / exit 1

You hit `HELD: <domain> by PR#<N> agent=<who> branch=<b>`. **Do not retry, do not force.** Three legitimate responses:

1. **Pick a different scope.** Reserve a sibling domain or disjoint symbols on the same file.
2. **Wait.** Re-check in a few minutes; the holder may release.
3. **Coordinate.** Ping the holder PR / agent. Many domains can be re-scoped to symbols if you ask.

### Exit codes (memorize these)

| Code | Meaning | Agent action |
|---|---|---|
| 0 | free / reserved / released | proceed |
| 1 | held (collision) | refuse spawn / commit |
| 2 | config error (missing registry, bad args) | fix the call, do not retry blindly |

---

## B. Agents *modifying* `merge_train` itself

You're working on this codebase. Rules:

### Test discipline

```bash
python -m pytest tests/ -q       # must stay green, currently 134 passed
```

- Add a regression test for every bug fix. Existing pattern: `tests/test_domain_lock.py` for parser/CLI, `tests/test_symbol_locks.py` for symbol resolution, `tests/test_reserve_plan.py` for atomic plans.
- Concurrency claims need a `multiprocessing.Pool`-style test (see `test_concurrent_reserve_only_one_wins`).

### Style

- No new dependencies beyond `PyYAML` without explicit approval.
- CLI subcommands re-register the three global flags (`--registry`, `--log`, `--git-cwd`) via `_add_global_opts_to_subparser` so both positions parse. New subcommands must follow this pattern.
- Lock-log writes go through `LockLog.append()` — never write JSONL by hand. `append()` holds `flock(2)`.
- CLI exit codes: 0 (success), 1 (collision/held), 2 (config error). Don't invent new codes.

### Push policy

- Commit on `main`. `git push --force-with-lease` only with explicit human approval naming the branch.
- After push, report old SHA → new SHA and the `https://github.com/jleechanorg/merge_train/commit/<sha>` URL.

### Evidence

When fixing a non-trivial bug or shipping a feature, drop a bundle at `/tmp/merge_train_evidence/proofs/<topic>/`:

- `raw.txt` — before/after CLI output with git SHAs.
- `SUMMARY.md` — claim, fix location (file:line), evidence map, verification commands.

The `/es` (evidence-standards) and `/er` (evidence-reviewer) skills audit these bundles — both must `PASS` for non-trivial work.

### Adversarial review

For any non-trivial change, spawn a `code-review` subagent with adversarial framing (find real problems, not nits). Address `CRITICAL` and `MAJOR` findings before declaring done. Pattern in `/tmp/merge_train_evidence/proofs/parser_compat_fix/` (commit `ce47114` → review → follow-up `15e2d0c`).

---

## C. Useful one-liners

```bash
# What's currently held in this repo?
domain_lock list --status active --json | jq

# Full audit (registry + log) for debugging
domain_lock audit | jq

# Where will the log actually be written?
python -c "from merge_train.domain_lock import _resolve_default_log; print(_resolve_default_log())"

# Smoke-test the parser-compat fix
python -m merge_train.domain_lock check --files mvp_site/world_logic.py \
  --registry examples/file_domains.yaml --log /tmp/d.jsonl --git-cwd /tmp
# expected: FREE: 1 domain(s) clear (level-up-pipeline)
```
