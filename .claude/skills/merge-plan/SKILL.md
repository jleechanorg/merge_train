---
name: merge-plan
description: Plan how to group, order, and prepare related pull requests using merge_train conflict predictions and current PR evidence. Use when deciding whether work belongs in one PR, coarse independent slices, or a serial merge sequence; planning is read-only.
---

# Merge plan

Produce a concrete, read-only plan for a set of related PRs. Use the existing `predict-conflicts` CLI for changed-file, supported-symbol, and textual conflict signals; make the grouping and ordering decision from the full task semantics and current PR state. The CLI schedule is advisory, not an execution order or merge guarantee.

## Gather current evidence

1. Read the target repository's instructions and identify the feature scope and acceptance requirements. Find candidate open PRs for that scope with the repository's supported GitHub tooling; include any explicitly named closed or draft PR only when it affects dependency or duplicate analysis.
2. At one recorded planning time, collect each candidate's PR number, title, draft/ready state, head SHA, base ref/SHA, mergeability, current required checks, review/evidence state, and explicit holds. Use fresh GitHub data. Unknown or unavailable fields stay `unknown`; do not infer readiness from title, old reports, or the conflict predictor.
3. Run `predict-conflicts --help` if its interface is unfamiliar. For a GitHub-backed candidate set, the supported file/symbol prediction form is:

   ```bash
   predict-conflicts --from-prs 123,124 --repo OWNER/REPO --json --no-textual
   ```

   `--from-prs` loads changed paths from GitHub and resolves current head branch **names**, not immutable head SHAs. The CLI output gives pairwise conflicts, overlap symbols where detected, predicted batches/order, and unmapped paths; it does **not** print a full per-PR changed-file list or all touched symbols. Read the current PR diff/file list separately when the plan needs that inventory. Symbol enrichment covers Python and Markdown when symbols can be read; other file types are treated as whole-file edits. Report any failed fetch, unmapped file, or missing symbol coverage. The optional registry changes grouping semantics; use it only when the target repo's registry is current and relevant.

   Use `--no-textual` for ordinary prediction from GitHub PRs: it prevents a same-named local branch from being mistaken for that PR's current head. Treat textual results as verified only when every PR head branch name is freshly resolved to its captured head SHA in a **disposable Git context**, and the exact common integration base SHA is pinned with `--git-base` while `--git-cwd` points at that isolated context. Confirm those values match the live PR reads. Do not run textual analysis against the shared/root checkout: `git merge-tree --write-tree` may write objects into that repository. If any ref is absent, a command fails, or head/base pins do not match, mark textual status `unknown` (not conflict-free) and retain the error/status. `predict-conflicts` exit status is `0` when no conflicts were detected, `1` when at least one was found, and `2` for usage/load errors; preserve stderr and output for nonzero statuses. A textual merge-tree failure can be silently omitted by the CLI, so empty textual results alone do not establish a clean merge.

   When candidates have mixed target bases, choose and document one integration target and compare all heads against that exact base for a shared landing plan. If they intentionally target different bases, analyze those groups separately; do not combine their conflict results or describe them as one common-base check.
4. Inspect actual diff and requirements for semantic coupling, generated/derived artifacts, schema or prompt contracts, hash/length pins, shared fixtures, and cumulative re-pin or rebase work. A file overlap is evidence to evaluate, not by itself a reason to split, serialize, or merge first.

## Decide the shape and order

- Default to **one cohesive PR** for one acceptance outcome.
- Split only into independently reviewable vertical slices with distinct acceptance and a clear integration order. Prefer no more than **3 coarse PRs targeting `main`** when the slices are genuinely independent. Use up to **5 coarse vertical slices only when the user explicitly asks to split**. Never create a stack of 10 or more PRs, micro-PRs, or a split that evades review-size or code-review thresholds. Check the target repo's PR quantity rules and existing open work before recommending new PRs.
- Build two separate views: (a) which PRs can be prepared concurrently, and (b) which PRs are currently eligible to land and in what sequence. A draft, blocked, or held PR may have parallel preparation work, but it cannot be treated as landable. Respect explicit dependency edges and acceptance contracts even where file/symbol predictions show no conflict.
- Treat `parallel_batches` and `recommended_order` as heuristics. The CLI's current schedule is a greedy lowest-conflict-degree maximal-independent-set heuristic. Do not mechanically put the PR with the most predicted overlaps first or last. Weigh current merge eligibility, dependency order, whether an overlap is a common contract or merely generated churn, re-pin/rebase cost accumulated across the sequence, and the cost of waiting for upstream slices. Explain the tradeoff behind the chosen order.
- A shared file such as a schema, prompt contract, pinned hash/length, or generated snapshot can make several PRs appear highly connected. Inspect which PR owns the source contract and which merely refreshes derived pins. Prefer a single cohesive source-contract change with its required derived updates, or order independent consumers after that contract, when this matches acceptance and review boundaries. Do not infer this ownership from filenames alone.
- If predictor results disagree with a direct Git merge-tree check, compare only after freshly pinning both PR head SHAs and the exact base SHA and confirming the checks use those same values. Run equivalent pairwise checks in an isolated disposable Git environment so planning does not change the working tree, index, refs, or shared repo state. Distinguish a conflict between two PR tips when integrated together from each PR's separate conflict against the base; these are different questions. If refs, bases, or tool support cannot be matched, report the discrepancy as unresolved and lower confidence rather than choosing one result as truth.

## Report

Return a concise plan with:

- **Snapshot and coverage:** timestamp, repository, candidate set, pinned head/base SHAs, data sources, and anything unavailable or omitted.
- **Decision:** one cohesive PR or proposed coarse slices, with a short reason tied to acceptance and review boundaries.
- **Dependency/order view:** explicit prerequisite edges, proposed landing sequence, and why that sequence minimizes risk or rework.
- **Preparation batches:** which independent work can proceed concurrently, separate from landing eligibility; include semantic coupling even when the tool reports no overlap.
- **Conflict evidence:** the CLI's pairwise predictions, changed files/symbols as actually available, textual results, and material uncertainty or predictor/check disagreement.
- **Current gate state:** draft/ready, mergeability, CI, required review/evidence, and holds, each sourced to the fresh read. Do not equate conflict-free with green, reviewed, mergeable, or authorized to merge.
- **Next steps:** concrete planning recommendations only. This skill invocation does not merge, push, create PRs or branches, alter hook/config state, or start implementation. Those actions require a separate task authorization and their normal gates.

## Behavioral examples

- User: “These four PRs touch the same schema and snapshot files; decide how to split and order them.” Inspect current PRs, source-vs-derived ownership, statuses, diffs, and predictor output; recommend one cohesive PR or a justified coarse sequence with separate preparation batches. Do not ask the user to choose an obvious split.
- User: “Can these PRs be merged in parallel?” Report the current conflict and semantic-dependency evidence, separate parallel preparation from landing eligibility, and give a confidence-qualified answer. Do not treat a clean predictor result as proof that checks, reviews, evidence, or merge authority are satisfied.
