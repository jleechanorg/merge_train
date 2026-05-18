# merge_train

Spawn-time file-domain lock registry for AI-agent PR pipelines.

Stops two agents from grabbing the same files when they're spawned in parallel — before either writes a line of code.

## Why

AI-agent PR pipelines (Aider, OpenHands, Devin, custom AO setups) spawn many agents in parallel against the same repo. They collide:

- Two agents edit `mvp_site/world_logic.py` → one rebases or gets dropped at merge time.
- Conflict surfaces after both agents have burned tokens.
- Existing tools (Mergify, Graphite, jj, ghstack) all work at **merge time** or **commit time**, not **spawn time**.

`merge_train` puts the gate at spawn time: declare which file → which domain, reserve the domain when an agent is spawned, refuse spawn if held.

## Prior art

| Tool | Where it acts | What it does |
|---|---|---|
| Mergify | merge time | rule-based merge queue |
| Graphite / ghstack | commit time | stacked PR cascade |
| jj (Jujutsu) | commit time | conflict-tolerant rebase |
| Gas Town Refinery | merge time | bisecting merge queue |
| OpenHands Large Codebase SDK | spawn time | dep-graph partitioning (intra-SDK) |
| **merge_train** | **spawn time** | **declarative file→domain registry (any pipeline)** |

## Components

- `merge_train/domain_lock.py` — CLI (`reserve | release | list | check | audit`)
- `file_domains.yaml` — declarative file → domain map
- `~/.merge_train/locks/<repo-hash>/pr_domain_locks.jsonl` — append-only lock log (outside repo tree)
- `hooks/ao-spawn-domain-check.sh` — pre-spawn gate
- `hooks/pre-commit.sh` — local gate

## Quick start

```bash
pip install -e .

# define domains
cat > file_domains.yaml <<EOF
domains:
  level_up_core:
    paths:
      - mvp_site/rewards_engine.py
      - mvp_site/world_logic.py
EOF

# reserve before spawning agent
domain_lock reserve --domain level_up_core --pr 6926 --agent claude-1 --branch feat/level-up

# check before letting another agent touch the same files
domain_lock check --files mvp_site/world_logic.py
# exit 0 = free, 1 = held (prints holder PR + agent)

# list active reservations
domain_lock list --status active

# release after PR merges
domain_lock release --pr 6926
```

## File domain YAML

```yaml
domains:
  level_up_core:
    paths:
      - mvp_site/rewards_engine.py
      - mvp_site/world_logic.py
    owners: [jleechan2015]
  ci_infra:
    paths:
      - .github/workflows/**
    owners: [jleechan2015]
```

## Lock log (JSONL, append-only)

The lock log lives **outside the repo tree** at `~/.merge_train/locks/<repo-hash>/pr_domain_locks.jsonl` by default, where `<repo-hash>` is a short SHA-256 of the git remote URL. This prevents the JSONL file from becoming a merge-conflict hotspot when two PRs both reserve domains.

Override with `MERGE_TRAIN_LOG` env var or `--log` flag to use a different path (e.g. the legacy `pr_domain_locks.jsonl` in-repo path).

```json
{"domain":"level_up_core","pr":6926,"agent":"claude-1","branch":"feat/level-up","opened_at":"2026-05-16T12:34:56Z","status":"active"}
{"domain":"level_up_core","pr":6926,"closed_at":"2026-05-16T14:01:00Z","status":"released"}
```

`release` writes a new line — never mutates history.

## Spawn-gate hook

Wire `hooks/ao-spawn-domain-check.sh` into your agent spawner (AO, OpenHands, custom). Before spawn, the gate calls `domain_lock check --files <changed_paths>`. If exit≠0, spawn is refused with the holder PR.

## Pre-commit hook

Local guard. Refuses commit if you're writing files outside your reserved domain.

```bash
ln -s ../../hooks/pre-commit.sh .git/hooks/pre-commit
```

## Roadmap

- [x] MVP: CLI + YAML + JSONL + spawn hook + pre-commit hook
- [x] Move lock state out of PR branches (default log path outside repo tree)
- [ ] MCP server wrapper (agent-native schema'd tools — same lib)
- [ ] Post-merge cascade rebase webhook
- [ ] Per-region AST-level domains (function-granularity locks)
- [ ] AO integration reference

## License

MIT
