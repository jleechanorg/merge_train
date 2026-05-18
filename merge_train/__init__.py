"""merge_train: spawn-time file-domain lock registry for AI-agent PR pipelines."""

__version__ = "0.1.0"

__all__ = [
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
]


def __getattr__(name):
    if name in __all__:
        from merge_train import domain_lock
        return getattr(domain_lock, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
