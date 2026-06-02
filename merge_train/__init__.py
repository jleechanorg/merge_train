"""merge_train: symbol-level PR conflict prediction for AI-agent merge pipelines."""

__version__ = "0.1.0"

# Names re-exported from merge_train.predict
_PREDICT_EXPORTS = (
    "Domain",
    "Registry",
    "LockEntry",
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

# Names re-exported from merge_train.hook_install
_HOOK_INSTALL_EXPORTS = (
    "AGENT_CHOICES",
    "ALL_HOOK_SCRIPTS",
    "HOOKS_INSTALL_DIR",
    "TEST_HOOKS",
    "install_hooks_for_agent",
    "test_hooks_for_agent",
)

__all__ = (
    list(_PREDICT_EXPORTS)
    + list(_SYMBOL_DISCOVERY_EXPORTS)
    + list(_HOOK_INSTALL_EXPORTS)
)


def __getattr__(name):
    if name in _PREDICT_EXPORTS:
        from merge_train import predict
        return getattr(predict, name)
    if name in _SYMBOL_DISCOVERY_EXPORTS:
        from merge_train import symbol_discovery
        return getattr(symbol_discovery, name)
    if name in _HOOK_INSTALL_EXPORTS:
        from merge_train import hook_install
        return getattr(hook_install, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
