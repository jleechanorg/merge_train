"""merge_train: spawn-time file-domain lock registry for AI-agent PR pipelines."""

__version__ = "0.1.0"

# Names re-exported from merge_train.domain_lock
_DOMAIN_LOCK_EXPORTS = (
    "Registry",
    "LockLog",
    "LockEntry",
    "PlanItem",
    "DomainHeldError",
    "UnknownPathError",
    "load_registry",
    "reserve",
    "reserve_plan",
    "release",
    "check",
    "list_locks",
    "audit",
)

# Names re-exported from merge_train.predict (dry-run / replay mode)
_PREDICT_EXPORTS = (
    "PRSpec",
    "Plan",
    "DomainConflict",
    "TextualConflict",
    "PairConflict",
    "predict_conflicts",
    "load_plan",
    "DISCLAIMER",
)

# Names re-exported from merge_train.symbol_discovery
_SYMBOL_DISCOVERY_EXPORTS = (
    "symbols_from_staged_diff",
    "symbols_from_pr_diff",
    "symbols_from_files_in_pr",
)

__all__ = (
    list(_DOMAIN_LOCK_EXPORTS)
    + list(_PREDICT_EXPORTS)
    + list(_SYMBOL_DISCOVERY_EXPORTS)
)


def __getattr__(name):
    if name in _DOMAIN_LOCK_EXPORTS:
        from merge_train import domain_lock
        return getattr(domain_lock, name)
    if name in _PREDICT_EXPORTS:
        from merge_train import predict
        return getattr(predict, name)
    if name in _SYMBOL_DISCOVERY_EXPORTS:
        from merge_train import symbol_discovery
        return getattr(symbol_discovery, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
