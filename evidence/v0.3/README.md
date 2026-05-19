# Evidence Package — OpenCode Markdown Area-Lock E2E

- Run ID: 20260519T075806Z
- Merge Train SHA: 30446565a58ecc232e255ef5a3c4003069411ee0
- Branch: main (synced to origin/main)
- Collected At: 2026-05-19T07:59:41Z
- Slots: 20
- PR Range: #275–#294
- Mctrl Test SHA: 6f89bef3b823bc74c89c260264396a7f16c62b55

## Files

- `metadata.json` — git provenance, run config, evidence mode
- `run.json` — test results, scenarios, reserve/PR data, negative controls
- `evidence.md` — human-readable summary with claim→artifact map
- `methodology.md` — test methodology, environment, negative control design
- `lock_log.jsonl` — raw lock log (reservations + releases)
- `active_during_run.json` — 20 active locks during execution
- `active_after_release.json` — 0 active locks after release
- `branches.txt` — 20 slot branch names
- `prs.txt` — 20 PR numbers
- `prs.json` — full PR data with URLs and head SHAs
- `pairwise_merge_tree.json` — pairwise merge simulation results
- `sequential_merge_tree.json` — sequential merge simulation results
- `checksums.txt` — master checksum file

Per-file `.sha256` sidecars exist for each artifact above.
