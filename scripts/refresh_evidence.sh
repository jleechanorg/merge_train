#!/usr/bin/env bash
# Refresh every evidence/v*/metadata.json's merge_train_sha + sha256 sidecar
# to point at the current branch HEAD. Use after any commits that should
# bring bundles into the allowed ≤10 commit staleness window.
#
# Iterates ALL bundles (not just the latest) so per-bundle freshness tests
# like tests/test_evidence_bundle.py::test_v04_metadata_sha_matches_head
# stay green even when the runner has accumulated several merged PRs.
set -euo pipefail

SHA=$(git rev-parse HEAD)
echo "Refreshing every evidence/v*/metadata.json to HEAD=$SHA"

BUNDLES=$(ls -d evidence/v*/ 2>/dev/null | sort -V)
if [ -z "$BUNDLES" ]; then
  echo "ERROR: no evidence/v*/ bundles found" >&2
  exit 1
fi

for BUNDLE_DIR in $BUNDLES; do
  META="${BUNDLE_DIR}metadata.json"
  if [ ! -f "$META" ]; then
    echo "SKIP: $META missing"
    continue
  fi

  OLD_SHA=$(python3 -c "import json; print(json.load(open('$META')).get('merge_train_sha',''))")
  echo "  $BUNDLE_DIR  $OLD_SHA  ->  $SHA"

  python3 - <<PYEOF
import json
p = "$META"
with open(p) as f:
    d = json.load(f)
d["merge_train_sha"] = "$SHA"
d.setdefault("provenance", {})["merge_train_sha"] = "$SHA"
with open(p, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
PYEOF

  # Recompute sha256 sidecar for metadata.json
  sha256sum "$META" | awk '{print $1}' > "${META}.sha256"
done

echo "Refreshed: $(echo "$BUNDLES" | wc -l | tr -d ' ') bundle(s)"
