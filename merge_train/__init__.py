"""merge_train: spawn-time file-domain lock registry for AI-agent PR pipelines."""

__version__ = "0.1.0"

from merge_train.domain_lock import (
    Registry,
    LockLog,
    LockEntry,
    DomainHeldError,
    UnknownPathError,
    load_registry,
    reserve,
    release,
    check,
    list_locks,
    audit,
)

__all__ = [
    "Registry",
    "LockLog",
    "LockEntry",
    "DomainHeldError",
    "UnknownPathError",
    "load_registry",
    "reserve",
    "release",
    "check",
    "list_locks",
    "audit",
]
