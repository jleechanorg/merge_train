"""merge_train: spawn-time file-domain lock registry for AI-agent PR pipelines."""

__version__ = "0.1.0"

from merge_train.domain_lock import (
    Registry,
    LockLog,
    LockEntry,
    PlanItem,
    DomainHeldError,
    UnknownPathError,
    load_registry,
    reserve,
    reserve_plan,
    release,
    check,
    list_locks,
    audit,
)

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
