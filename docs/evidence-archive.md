# Evidence archive scope

`evidence/v0.2` and `evidence/v0.2.2` are legacy records from retired
domain-lock work. They predate the per-file SHA256 sidecar convention and are
retained without being covered by the archive-integrity workflow.

The workflow validates the later sidecar-backed bundles (`v0.3`, `v0.4`,
`v0.4-ao`, `v0.5-ao`, and `v0.6-ao`) for byte integrity. These artifacts
record historical execution metadata only: some recorded Git SHAs were
rewritten historically and may no longer be fetchable, so they must not be
treated as evidence of the current implementation.
