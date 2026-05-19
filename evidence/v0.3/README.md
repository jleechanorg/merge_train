# Evidence Package — OpenCode Markdown Area-Lock E2E

- Run ID: 20260519T082805Z
- Merge Train SHA: 1ef058ef70f88f3250ed14572d7baf70c1e6c5ec
- Branch: main (synced to origin/main)
- Collected At: 2026-05-19T08:35:43Z
- Slots: 20
- PR Range: #340–#359
- Mctrl Test SHA: 7f391fbc6fb99a7bbaf57d867e6accb2d15daa9d
- Agent: openw run (real OpenCode, not inline fallback)
- Symbol Discovery: auto-extracted via extract_markdown_symbols

## Files

- `metadata.json` — git provenance, run config, evidence mode
- `run.json` — test results, scenarios, reserve/PR data, negative controls
- `evidence.md` — human-readable summary with claim→artifact map
- `methodology.md` — test methodology, environment, auto-extraction, real agents
- `lock_log.jsonl` — raw lock log (reservations + releases)
- `active_during_run.json` — 20 active locks during execution
- `active_after_release.json` — 0 active locks after release
- `branches.txt` — 20 slot branch names
- `prs.txt` — 20 PR numbers
- `prs.json` — full PR data with URLs, head SHAs, agent_mode
- `pairwise_merge_tree.json` — pairwise merge simulation results
- `sequential_merge_tree.json` — sequential merge simulation results
- `checksums.txt` — master checksum file
- `agent_transcripts/` — real openw agent output per slot

Per-file `.sha256` sidecars exist for each artifact above.
