"""Core registry + lock-log + CLI for merge_train.

Spawn-time file-domain lock registry. Declarative YAML maps file paths
(or glob patterns) to named domains. An append-only JSONL log records
which PR/agent holds which domain. The CLI lets a spawn hook check
whether a set of changed files would collide with an active reservation
before any agent burns tokens.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

import yaml


DEFAULT_REGISTRY = "file_domains.yaml"


DEFAULT_LOG = "<auto>"


def _get_git_toplevel(cwd: Path | str | None = None) -> Path | None:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
            cwd=str(cwd) if cwd is not None else None,
        )
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip()).resolve()
    except FileNotFoundError:
        pass
    return None


def _get_repo_name(remote_url: str) -> str:
    url = remote_url.strip()
    if not url:
        return ""
    if url.endswith(".git"):
        url = url[:-4]
    if "github.com:" in url:
        return url.split("github.com:")[-1]
    elif "github.com/" in url:
        return url.split("github.com/")[-1]
    elif ":" in url:
        return url.split(":")[-1]
    elif url.count("/") >= 2:
        parts = url.split("/")
        return "/".join(parts[-2:])
    return url


def _resolve_default_log(cwd: Path | str | None = None) -> str:
    base = Path.home() / ".merge_train" / "locks"
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False,
            cwd=str(cwd) if cwd is not None else None,
        ).stdout.strip()
    except FileNotFoundError:
        remote = ""
    slug = hashlib.sha256(remote.encode()).hexdigest()[:12] if remote else "default"
    return str(base / slug / "pr_domain_locks.jsonl")


class DomainHeldError(Exception):
    """Raised when a reservation is requested for a domain already held."""


class UnknownPathError(Exception):
    """Raised when a file path is not mapped to any declared domain."""


@dataclass(frozen=True)
class Domain:
    name: str
    paths: tuple[str, ...]
    owners: tuple[str, ...] = ()
    per_pr_unique: bool = False  # files are per-PR unique; never block each other
    advisory: bool = False       # log conflict but don't block spawning
    concurrency_limit: int = 1   # max concurrent PRs allowed to hold this domain (semaphore)
    intra_pr_exclusive: bool = False  # opt-in: siblings on the same claim but a
    # different agent conflict (parallel agents on one PR/branch get exclusive
    # symbol/domain locks); OFF by default for backward compatibility.


@dataclass
class Registry:
    """Declarative file -> domain mapping loaded from YAML."""

    domains: dict[str, Domain] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Registry":
        domains: dict[str, Domain] = {}
        for name, body in (data.get("domains") or {}).items():
            paths = tuple(body.get("paths") or ())
            owners = tuple(body.get("owners") or ())
            per_pr_unique = bool(body.get("per_pr_unique", False))
            advisory = bool(body.get("advisory", False))
            concurrency_limit = int(body.get("concurrency_limit", 1))
            intra_pr_exclusive = bool(body.get("intra_pr_exclusive", False))
            domains[name] = Domain(
                name=name, paths=paths, owners=owners,
                per_pr_unique=per_pr_unique, advisory=advisory,
                concurrency_limit=concurrency_limit,
                intra_pr_exclusive=intra_pr_exclusive,
            )
        return cls(domains=domains)

    def domain_for_path(self, path: str) -> Optional[str]:
        """Return the domain name that owns *path*, or None.

        Paths use glob patterns (fnmatch). If multiple domains match a
        single path, the first declared (insertion order) wins. Two
        domains claiming the same path is a registry-authoring error,
        not enforced here.
        """
        norm = path.lstrip("./")
        for domain in self.domains.values():
            for pattern in domain.paths:
                if fnmatch.fnmatch(norm, pattern.lstrip("./")):
                    return domain.name
        return None

    def domains_for_paths(self, paths: Iterable[str]) -> dict[str, list[str]]:
        """Group *paths* by their resolved domain. Unmapped paths
        appear under the key ``__unmapped__``."""
        grouped: dict[str, list[str]] = {}
        for path in paths:
            name = self.domain_for_path(path) or "__unmapped__"
            grouped.setdefault(name, []).append(path)
        return grouped


def _claim_id(pr: Optional[int], branch: str) -> str:
    """Unified claim identity for conflict partitioning and idempotency.

    A PR-keyed reservation is identified by its PR number; a branch-keyed
    reservation (no PR yet) is identified by ``branch:<name>``. Keeping both in
    a single string namespace lets every partition/idempotency check key on one
    value regardless of whether a PR exists. PR-keyed identities are formatted
    so that, combined with the unchanged conflict logic, PR-only reservations
    behave byte-for-byte as before.
    """
    if pr is not None:
        return f"pr:{pr}"
    return f"branch:{branch}"


@dataclass(frozen=True)
class LockEntry:
    domain: str
    pr: Optional[int]
    agent: str
    branch: str
    opened_at: str
    status: str  # "active" | "released"
    closed_at: Optional[str] = None
    note: Optional[str] = None
    symbols: tuple[str, ...] = ()
    override: bool = False  # True when reserved despite a conflict
    intra_pr_exclusive: bool = False  # True when reserved under agent-aware mode

    def to_json(self) -> str:
        d = dataclasses.asdict(self)
        if not d.get("symbols"):
            d.pop("symbols", None)
        else:
            d["symbols"] = list(d["symbols"])
        if not d.get("override"):
            d.pop("override", None)
        if not d.get("intra_pr_exclusive"):
            d.pop("intra_pr_exclusive", None)
        return json.dumps(d, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "LockEntry":
        data = json.loads(line)
        if "symbols" in data and data["symbols"] is not None:
            data["symbols"] = tuple(data["symbols"])
        else:
            data.pop("symbols", None)
        return cls(**data)

    @property
    def is_whole_domain(self) -> bool:
        """A reservation with no symbols holds the entire domain."""
        return not self.symbols

    @property
    def claim_id(self) -> str:
        """The unified claim identity (PR number or ``branch:<name>``)."""
        return _claim_id(self.pr, self.branch)


class LockLog:
    """Append-only JSONL lock log."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._active_fd = None

    @contextmanager
    def lock(self):
        """Acquire an exclusive flock on the log file for atomic read-modify-write.

        On platforms without fcntl (e.g. Windows), this is a no-op.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = self.path.open("a+", encoding="utf-8")
        try:
            if _HAS_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_EX)
            self._active_fd = fd
            yield fd
        finally:
            self._active_fd = None
            if _HAS_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

    def append(self, entry: LockEntry) -> None:
        if self._active_fd is not None:
            self._active_fd.seek(0, 2)
            self._active_fd.write(entry.to_json() + "\n")
            self._active_fd.flush()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(entry.to_json() + "\n")

    def entries(self) -> list[LockEntry]:
        if self._active_fd is not None:
            self._active_fd.seek(0)
            out: list[LockEntry] = []
            for raw in self._active_fd:
                raw = raw.strip()
                if not raw or raw.startswith("#"):
                    continue
                out.append(LockEntry.from_json(raw))
            return out
        else:
            if not self.path.exists():
                return []
            out: list[LockEntry] = []
            with self.path.open("r", encoding="utf-8") as fh:
                if _HAS_FCNTL:
                    fcntl.flock(fh, fcntl.LOCK_SH)
                try:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw or raw.startswith("#"):
                            continue
                        out.append(LockEntry.from_json(raw))
                finally:
                    if _HAS_FCNTL:
                        fcntl.flock(fh, fcntl.LOCK_UN)
            return out

    def active(self) -> dict[str, LockEntry]:
        """Active reservations keyed by domain — *file-level only*.

        Back-compat shim: returns at most one whole-domain entry per
        domain (most-recent wins). For symbol-aware data, use
        :meth:`active_all` which can return multiple coexisting
        reservations per domain (disjoint symbol sets).
        """
        all_active = self.active_all()
        out: dict[str, LockEntry] = {}
        for entry in all_active:
            if entry.is_whole_domain:
                out[entry.domain] = entry
            else:
                # Surface *something* per domain for callers that still
                # think file-level. Prefer the most recent.
                out.setdefault(entry.domain, entry)
        return out

    def active_all(self) -> list[LockEntry]:
        """Every currently-active reservation, in append order.

        Multiple reservations may coexist on the same domain when each
        carries a disjoint set of symbols. A *whole-domain* reservation
        (``symbols == ()``) coexisting with anything else is a registry
        bug — :func:`reserve` rejects it.

        A release is keyed by ``(domain, pr, symbols)``: it clears the
        single reservation whose key matches the last active entry.
        """
        # Walk entries in order. Treat each (domain, claim_id, symbols) as
        # an independent lock key. Later entries override earlier ones
        # with the same key. The claim_id collapses to the PR number for
        # PR-keyed reservations (back-compat) and to ``branch:<name>`` for
        # branch-keyed reservations (no PR yet).
        latest: dict[tuple[str, str, tuple[str, ...]], LockEntry] = {}
        for entry in self.entries():
            key = (entry.domain, entry.claim_id, tuple(entry.symbols))
            latest[key] = entry
        
        active_entries = []
        for entry in latest.values():
            if entry.status != "active":
                continue
            if entry.symbols:
                try:
                    dt_str = entry.opened_at
                    if dt_str.endswith("Z"):
                        dt_str = dt_str[:-1] + "+00:00"
                    opened_dt = datetime.fromisoformat(dt_str)
                    if opened_dt.tzinfo is None:
                        now_dt = datetime.now()
                    else:
                        now_dt = datetime.now(timezone.utc)
                    if (now_dt - opened_dt).total_seconds() > 14 * 24 * 3600:
                        continue
                except Exception:
                    pass
            active_entries.append(entry)
        return active_entries


def load_registry(path: str | os.PathLike = DEFAULT_REGISTRY) -> Registry:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"registry file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Registry.from_dict(data)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_OVERRIDE_PHRASE = "CONFLICT APPROVED"


def _reserve_locked(
    log: LockLog,
    registry: Registry,
    *,
    domain: str,
    pr: Optional[int],
    agent: str,
    branch: str,
    symbols: Iterable[str] = (),
    now: Optional[str] = None,
    override: str = "",
    intra_pr_exclusive: Optional[bool] = None,
) -> LockEntry:
    """Core reserve logic — caller must hold log.lock().

    Conflict partitioning and idempotency key on a unified *claim identity*
    (:func:`_claim_id`): a PR number when ``pr`` is given, else ``branch:<name>``.
    This lets agents reserve before a PR exists while keeping PR-keyed
    reservations byte-for-byte compatible.

    When the target domain has ``intra_pr_exclusive`` ON (registry setting, or
    forced via the *intra_pr_exclusive* argument), siblings are partitioned on
    ``(claim_id, agent)``: a *different* agent on the *same* claim is treated as
    an "other" holder for conflict purposes, and idempotency requires the same
    claim AND the same agent. When OFF (default), partitioning is on claim
    identity alone — today's PR-ownership model.
    """
    if domain not in registry.domains:
        raise UnknownPathError(f"unknown domain: {domain}")
    syms = tuple(sorted(set(symbols)))

    active_on_domain = [e for e in log.active_all() if e.domain == domain]
    dom = registry.domains.get(domain)
    limit = dom.concurrency_limit if dom is not None else 1

    # Resolve the agent-aware mode: explicit arg (CLI flag) wins, else the
    # per-domain registry setting.
    if intra_pr_exclusive is None:
        intra_pr_exclusive = bool(dom is not None and dom.intra_pr_exclusive)

    claim = _claim_id(pr, branch)

    def _is_own(e: LockEntry) -> bool:
        # In agent-aware mode a sibling agent on the same claim is "other".
        if e.claim_id != claim:
            return False
        # Use the persisted mode from the entry OR the current call's mode.
        # If either the existing lock OR the current request is exclusive,
        # treat different agents as "other".
        exclusive = e.intra_pr_exclusive or intra_pr_exclusive
        if exclusive and e.agent != agent:
            return False
        return True

    # Idempotency: if this claim (agent-aware when ON) already holds the domain
    # with the same or covering symbols, no-op.
    own_active = [e for e in active_on_domain if _is_own(e)]
    if own_active:
        existing = own_active[-1]
        if not syms or (not existing.is_whole_domain and set(syms).issubset(set(existing.symbols))):
            return existing  # already held — don't append duplicate

    other_holders = [e for e in active_on_domain if not _is_own(e)]
    whole_domain_other = [e for e in other_holders if e.is_whole_domain]
    symbol_others = [e for e in other_holders if not e.is_whole_domain]

    _approved = override == _OVERRIDE_PHRASE

    def _who(e: LockEntry) -> str:
        # Human-readable holder identity for error messages: PR# when present,
        # else the branch claim.
        return f"PR #{e.pr}" if e.pr is not None else f"branch '{e.branch}'"

    if not syms:
        # Whole-domain reservation: semaphore applies to concurrent whole-domain holders.
        # Symbol-level holders also block (can't take the whole domain over existing symbols).
        distinct_whole_claims = {
            (h.claim_id, h.agent) if (h.intra_pr_exclusive or intra_pr_exclusive) else h.claim_id
            for h in whole_domain_other
        }
        if len(distinct_whole_claims) >= limit:
            if not _approved:
                conflict = whole_domain_other[0]
                raise DomainHeldError(
                    f"domain '{domain}' is fully held by {_who(conflict)} "
                    f"(agent={conflict.agent}, branch={conflict.branch}, "
                    f"opened_at={conflict.opened_at}) — concurrency limit reached ({limit})"
                )
        if symbol_others and not _approved:
            held = symbol_others[0]
            raise DomainHeldError(
                f"domain '{domain}' has symbol locks held by "
                f"{_who(held)} (agent={held.agent}, symbols={','.join(held.symbols)}) — "
                "whole-domain reservation refused"
            )
    else:
        # Symbol-level reservation: whole-domain holders always block;
        # symbol-level holders block only on overlap. Semaphore does not apply.
        for holder in whole_domain_other:
            if not _approved:
                raise DomainHeldError(
                    f"domain '{domain}' is fully held by {_who(holder)} "
                    f"(agent={holder.agent}, branch={holder.branch}, "
                    f"opened_at={holder.opened_at})"
                )
        for holder in symbol_others:
            overlap = set(holder.symbols).intersection(syms)
            if overlap and not _approved:
                raise DomainHeldError(
                    f"symbol(s) {','.join(sorted(overlap))} in domain "
                    f"'{domain}' held by {_who(holder)} (agent={holder.agent}, "
                    f"branch={holder.branch})"
                )

    entry = LockEntry(
        domain=domain,
        pr=pr,
        agent=agent,
        branch=branch,
        opened_at=now or _utcnow(),
        status="active",
        symbols=syms,
        override=_approved,
        intra_pr_exclusive=intra_pr_exclusive,
    )
    log.append(entry)
    return entry


def reserve(
    log: LockLog,
    registry: Registry,
    *,
    domain: str,
    pr: Optional[int] = None,
    agent: str,
    branch: str,
    symbols: Iterable[str] = (),
    now: Optional[str] = None,
    override: str = "",
    intra_pr_exclusive: Optional[bool] = None,
) -> LockEntry:
    """Reserve *domain* (or specific symbols within it) for *pr*/*agent*.

    Symbol-level rules:
      * ``symbols == ()`` is a whole-domain lock — refused if ANY active
        reservation (whole-domain or symbol-level) exists on this domain.
      * ``symbols != ()`` is a symbol-level lock — refused if a
        whole-domain reservation exists OR if any active symbol
        reservation on this domain overlaps the requested set.

    Re-reserving on a domain you already hold is rejected — callers
    should release first if they need to change symbol scope.

    Pass ``override=_OVERRIDE_PHRASE`` to force-reserve despite conflicts.
    The entry is written with ``override=True`` for audit purposes.

    Pass ``pr=None`` with a ``branch`` to reserve before a PR exists; conflict
    detection then keys on the branch claim (``branch:<name>``). When the
    domain (or *intra_pr_exclusive*) enables agent-aware mode, two different
    agents on the same claim with overlapping symbols conflict.

    Raises :class:`DomainHeldError` on conflict, :class:`UnknownPathError`
    on unknown domain.
    """
    with log.lock():
        return _reserve_locked(
            log, registry,
            domain=domain, pr=pr, agent=agent, branch=branch,
            symbols=symbols, now=now, override=override,
            intra_pr_exclusive=intra_pr_exclusive,
        )


@dataclass(frozen=True)
class PlanItem:
    """One leg of a multi-file reservation plan."""
    domain: str
    symbols: tuple[str, ...] = ()


def reserve_plan(
    log: LockLog,
    registry: Registry,
    *,
    pr: Optional[int] = None,
    agent: str,
    branch: str,
    plan: Iterable[PlanItem | dict],
    now: Optional[str] = None,
    intra_pr_exclusive: Optional[bool] = None,
) -> list[LockEntry]:
    """Atomically reserve every leg of *plan* for the same PR/agent/branch.

    All-or-nothing: if any leg conflicts, every reservation written so far
    in this call is released (with note=``rollback:reserve_plan``) before
    re-raising :class:`DomainHeldError`. Useful for a worker that edits
    several files at once — either the worker holds every required symbol
    set across every file, or it holds none and can be re-spawned.

    Each plan item is either a :class:`PlanItem` or a dict with keys
    ``domain`` and (optional) ``symbols`` (list of names).
    """
    items: list[PlanItem] = []
    for raw in plan:
        if isinstance(raw, PlanItem):
            items.append(raw)
        else:
            items.append(PlanItem(
                domain=raw["domain"],
                symbols=tuple(raw.get("symbols") or ()),
            ))

    written: list[LockEntry] = []
    rollback_note = "rollback:reserve_plan"
    with log.lock():
        for item in items:
            try:
                entry = _reserve_locked(
                    log, registry,
                    domain=item.domain, pr=pr,
                    agent=agent, branch=branch,
                    symbols=item.symbols, now=now,
                    intra_pr_exclusive=intra_pr_exclusive,
                )
            except (DomainHeldError, UnknownPathError):
                # Roll back everything we wrote in this call.
                for done in written:
                    log.append(LockEntry(
                        domain=done.domain,
                        pr=done.pr,
                        agent=done.agent,
                        branch=done.branch,
                        opened_at=done.opened_at,
                        status="released",
                        closed_at=now or _utcnow(),
                        note=rollback_note,
                        symbols=done.symbols,
                        intra_pr_exclusive=done.intra_pr_exclusive,
                    ))
                raise
            written.append(entry)
    return written


def release(
    log: LockLog,
    *,
    pr: Optional[int] = None,
    branch: Optional[str] = None,
    domain: Optional[str] = None,
    note: Optional[str] = None,
    now: Optional[str] = None,
) -> list[LockEntry]:
    """Release active reservations for a claim (optionally filter to a domain).

    The claim is a PR (``pr``) or a branch (``branch``, when no PR exists);
    matching keys on the unified claim identity so branch-keyed reservations
    release cleanly. At least one of *pr* / *branch* must be given.

    Releases every matching active lock — whole-domain AND symbol-level.
    Returns the list of release entries written, preserving the original
    ``symbols`` tuple so the (domain, claim, symbols) key matches.
    """
    if pr is None and branch is None:
        raise ValueError("release() requires one of pr or branch")
    target_claim = _claim_id(pr, branch) if branch is not None or pr is not None else None
    released: list[LockEntry] = []
    with log.lock():
        for entry in log.active_all():
            if entry.claim_id != target_claim:
                continue
            if domain and entry.domain != domain:
                continue
            rel = LockEntry(
                domain=entry.domain,
                pr=entry.pr,
                agent=entry.agent,
                branch=entry.branch,
                opened_at=entry.opened_at,
                status="released",
                closed_at=now or _utcnow(),
                note=note,
                symbols=entry.symbols,
                intra_pr_exclusive=entry.intra_pr_exclusive,
            )
            log.append(rel)
            released.append(rel)
    return released


@dataclass(frozen=True)
class CheckResult:
    free: list[str]
    held: list[tuple[str, LockEntry]]  # (domain, holder)
    unmapped: list[str]
    touched_symbols: dict[str, set[str]] = dataclasses.field(default_factory=dict)
    advisory_held: list[tuple[str, LockEntry]] = dataclasses.field(default_factory=list)  # warn-only conflicts

    @property
    def ok(self) -> bool:
        return not self.held


def check(
    log: LockLog,
    registry: Registry,
    *,
    files: Iterable[str],
    pr: Optional[int] = None,
    branch: Optional[str] = None,
    agent: Optional[str] = None,
    touched_symbols_by_path: Optional[dict[str, Optional[set[str]]]] = None,
    override: str = "",
) -> CheckResult:
    """Check whether *files* collide with any active reservation.

    Two modes:

    1. **File-level** (default — ``touched_symbols_by_path`` omitted or
       empty): a domain collides if ANY active reservation on it exists
       (whole-domain OR symbol-level), unless the holder is *pr*.

    2. **Symbol-level**: pass ``touched_symbols_by_path`` mapping each
       file to its set of touched symbol names. A whole-domain
       reservation always collides. A symbol-level reservation collides
       only if the requested touched-symbols intersect the held set.

       Three states per path in the mapping:
       - **Not in dict** → whole-domain fallback (unknown/unresolvable)
       - **In dict, value ``None``** → whole-domain fallback (parse failure)
       - **In dict, value ``set``** → symbol-level result; even an empty
         set means "resolution succeeded, no symbols touched" (genuinely free)

    Optional *branch* and *agent* parameters enable claim-aware conflict
    detection: when provided, reservations held by the same claim (and,
    if ``intra_pr_exclusive`` is ON, the same agent) are excluded from
    the conflict set (same "own" vs "other" logic as :func:`reserve`).
    """
    grouped = registry.domains_for_paths(files)
    active_all = log.active_all()
    free: list[str] = []
    held: list[tuple[str, LockEntry]] = []
    advisory_held_list: list[tuple[str, LockEntry]] = []
    unmapped = grouped.pop("__unmapped__", [])

    # Aggregate touched symbols per domain. Three states per path:
    #   not in dict          → whole-domain fallback (unknown/unresolvable)
    #   in dict, value None  → whole-domain fallback (parse failure)
    #   in dict, value set   → symbol-level (even empty = genuinely no overlap)
    per_domain_symbols: dict[str, Optional[set[str]]] = {}
    for domain, dpaths in grouped.items():
        if touched_symbols_by_path is None:
            per_domain_symbols[domain] = None
            continue
        agg: set[str] = set()
        whole_domain = False
        for path in dpaths:
            if path not in touched_symbols_by_path:
                whole_domain = True
                break
            sym_set = touched_symbols_by_path[path]
            if sym_set is None:
                whole_domain = True
                break
            agg.update(sym_set)
        per_domain_symbols[domain] = None if whole_domain else agg

    for domain in grouped:
        domain_holders = [e for e in active_all if e.domain == domain]
        if not domain_holders:
            free.append(domain)
            continue

        dom = registry.domains.get(domain)
        limit = dom.concurrency_limit if dom is not None else 1

        # Filter "own" reservations using the same claim-aware logic as reserve.
        # When neither pr nor branch is given, all holders are "other" (no claim identity).
        if pr is None and branch is None:
            other_holders = domain_holders
        else:
            claim = _claim_id(pr, branch)
            # Resolve agent-aware mode from the domain registry setting (no override here).
            intra_pr_exclusive = bool(dom is not None and dom.intra_pr_exclusive)
            
            def _is_own(e: LockEntry) -> bool:
                if e.claim_id != claim:
                    return False
                # When agent-aware mode is ON for the domain, treat different agents as "other".
                # Use the persisted mode from the entry OR the domain's registry setting.
                exclusive = e.intra_pr_exclusive or intra_pr_exclusive
                if exclusive and agent is not None and e.agent != agent:
                    return False
                return True
            
            other_holders = [e for e in domain_holders if not _is_own(e)]
        
        whole_domain_other = [e for e in other_holders if e.is_whole_domain]
        symbol_others = [e for e in other_holders if not e.is_whole_domain]

        touched = per_domain_symbols[domain]
        conflict: Optional[LockEntry] = None

        if touched is None:
            # Whole-domain check: semaphore applies to concurrent whole-domain holders.
            # Symbol-level holders also collide (whole-domain write would stomp them).
            distinct_whole_prs = {e.pr for e in whole_domain_other}
            if len(distinct_whole_prs) >= limit:
                conflict = whole_domain_other[0]
            elif symbol_others:
                conflict = symbol_others[0]
        else:
            # Symbol-level check: whole-domain holders always collide;
            # symbol-level holders collide only on symbol overlap. No semaphore.
            for holder in whole_domain_other:
                conflict = holder
                break
            if conflict is None:
                for holder in symbol_others:
                    if touched.intersection(holder.symbols):
                        conflict = holder
                        break

        if conflict is None or override == _OVERRIDE_PHRASE:
            free.append(domain)
        elif dom is not None and dom.advisory:
            advisory_held_list.append((domain, conflict))
        else:
            held.append((domain, conflict))

    return CheckResult(
        free=free,
        held=held,
        unmapped=unmapped,
        touched_symbols={
            d: (s if s is not None else set())
            for d, s in per_domain_symbols.items()
        },
        advisory_held=advisory_held_list,
    )


def list_locks(log: LockLog, *, status: str = "active") -> list[LockEntry]:
    if status == "active":
        return log.active_all()
    if status == "all":
        return log.entries()
    return [e for e in log.entries() if e.status == status]


def audit(log: LockLog, registry: Registry) -> dict:
    """Return a structured audit report of registry + lock-log state.

    ``active_locks`` groups by domain; each value is a list of holder
    dicts to surface symbol-level co-tenancy.
    """
    active: dict[str, list[dict]] = {}
    for entry in log.active_all():
        active.setdefault(entry.domain, []).append(dataclasses.asdict(entry))
    return {
        "registry": {
            "domains": {
                name: {"paths": list(d.paths), "owners": list(d.owners)}
                for name, d in registry.domains.items()
            }
        },
        "active_locks": active,
        "total_entries": len(log.entries()),
        "generated_at": _utcnow(),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _add_global_opts_to_subparser(sp: argparse.ArgumentParser) -> None:
    """Re-register the top-level globals on a subparser so the legacy
    ``domain_lock <cmd> ... --registry/--log/--git-cwd`` form still parses.

    ``default=argparse.SUPPRESS`` is critical: it leaves the attribute
    unset in the Namespace when the user does NOT pass the flag at the
    subcommand level, so the value parsed at the top-level parser is
    preserved instead of being clobbered by ``None``.
    """
    sp.add_argument("--registry", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sp.add_argument("--log", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sp.add_argument("--git-cwd", default=argparse.SUPPRESS, help=argparse.SUPPRESS)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="domain_lock",
        description="Spawn-time file-domain lock registry for AI-agent PR pipelines.",
    )
    p.add_argument(
        "--registry",
        default=os.environ.get("MERGE_TRAIN_REGISTRY", DEFAULT_REGISTRY),
        help="path to YAML registry (default: %(default)s)",
    )
    p.add_argument(
        "--log",
        default=os.environ.get("MERGE_TRAIN_LOG", DEFAULT_LOG),
        help="path to JSONL lock log (default: %(default)s)",
    )
    p.add_argument(
        "--git-cwd", default=None,
        help="git working tree for default log-path resolution (default: cwd)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pr_re = sub.add_parser("reserve", help="reserve a domain for a PR/agent")
    pr_re.add_argument("--domain", required=True)
    pr_re.add_argument("--pr", type=int, default=None,
                       help="PR number; optional when --branch is given")
    pr_re.add_argument("--agent", required=True)
    pr_re.add_argument("--branch", required=True)
    pr_re.add_argument(
        "--symbols", default="",
        help="comma-separated symbol names within the domain "
             "(empty = whole-domain lock)",
    )
    pr_re.add_argument(
        "--intra-pr-exclusive", action="store_true", dest="intra_pr_exclusive",
        help="force agent-aware mode ON for this reserve: a different agent on "
             "the same PR/branch claim conflicts on overlapping symbols "
             "(overrides the domain's registry setting)",
    )
    pr_re.add_argument(
        "--dry-run", action="store_true",
        help="check for conflicts without acquiring the lock; exits 0 if "
             "the domain is free, 1 if held, and prints WOULD-RESERVE",
    )
    pr_re.add_argument(
        "--override", default="",
        help=f"type '{_OVERRIDE_PHRASE}' to force-reserve despite a held domain "
             "(conflict bypassed; entry logged with override=true)",
    )

    pr_rp = sub.add_parser(
        "reserve-plan",
        help="atomically reserve multiple (domain, symbols) legs for one PR",
    )
    pr_rp.add_argument("--pr", type=int, default=None,
                       help="PR number; optional when --branch is given")
    pr_rp.add_argument("--agent", required=True)
    pr_rp.add_argument("--branch", required=True)
    pr_rp.add_argument(
        "--plan", required=True,
        help="path to YAML/JSON file with a 'plan' (or 'reservations') list "
             "of {domain, symbols} entries",
    )
    pr_rp.add_argument(
        "--intra-pr-exclusive", action="store_true", dest="intra_pr_exclusive",
        help="force agent-aware mode ON for every leg of this plan",
    )
    pr_rp.add_argument(
        "--dry-run", action="store_true",
        help="check for conflicts without acquiring locks; exits 0 if all "
             "legs are free, 1 if any is held, and prints WOULD-RESERVE per leg",
    )

    pr_rl = sub.add_parser("release", help="release a PR's or branch's reservations")
    pr_rl.add_argument("--pr", type=int, default=None,
                       help="PR number to release; optional when --branch is given")
    pr_rl.add_argument("--branch", default=None,
                       help="branch claim to release (for reservations made with no PR)")
    pr_rl.add_argument("--domain", default=None)
    pr_rl.add_argument("--note", default=None)
    pr_rl.add_argument("--force", action="store_true", default=False,
                       help="Force release even if the PR is still open and touches domain files")

    pr_ck = sub.add_parser("check", help="check files against active reservations")
    pr_ck.add_argument("--files", nargs="+", required=True)
    pr_ck.add_argument("--pr", type=int, default=None,
                       help="PR being checked; its own reservations don't self-conflict")
    pr_ck.add_argument("--branch", default=None,
                       help="branch claim being checked (for pre-PR workflows)")
    pr_ck.add_argument("--agent", default=None,
                       help="agent identity; when intra_pr_exclusive is ON, only this agent's "
                            "reservations on the same claim are self-exempt")
    pr_ck.add_argument("--json", action="store_true", help="JSON output")
    pr_ck.add_argument(
        "--diff-mode", action="store_true",
        help="resolve touched Python symbols from staged git diff "
             "(allows symbol-level co-tenancy on the same file)",
    )
    pr_ck.add_argument(
        "--symbols", default="",
        help="comma-separated touched symbol names (overrides --diff-mode; "
             "enables symbol-level check without staged git diff)",
    )
    pr_ck.add_argument(
        "--override", default="",
        help=f"type '{_OVERRIDE_PHRASE}' to treat held domains as free",
    )

    pr_ls = sub.add_parser("list", help="list locks")
    pr_ls.add_argument("--status", default="active",
                       choices=["active", "released", "all"])
    pr_ls.add_argument("--json", action="store_true")

    pr_au = sub.add_parser("audit", help="dump full registry + lock-log audit JSON")

    pr_pc = sub.add_parser(
        "predict-conflicts",
        help="dry-run: predict pairwise conflicts + recommend merge order for a "
             "set of PRs declared in a YAML plan",
    )
    pr_pc.add_argument("--plan",
                       help="path to YAML/JSON file with a 'prs' list")
    pr_pc.add_argument("--from-prs", metavar="N,M,...",
                       help="comma-separated PR numbers; bypasses --plan, "
                            "fetches file lists from GitHub via gh")
    pr_pc.add_argument("--repo", metavar="OWNER/REPO",
                       help="GitHub repo for --from-prs / --enrich-symbols "
                            "(e.g. jleechanorg/worldarchitect.ai)")
    pr_pc.add_argument("--enrich-symbols", action="store_true",
                       help="auto-populate symbols_by_file from gh pr diff "
                            "for PRs that declare no symbols")
    pr_pc.add_argument("--no-textual", action="store_true",
                       help="skip git merge-tree textual conflict check")
    pr_pc.add_argument("--git-base", default="origin/main",
                       help="base ref for git merge-tree (default: origin/main)")
    pr_pc.add_argument("--json", action="store_true", help="JSON output")


    pr_rd = sub.add_parser("recommend-domains", help="analyze recent repo history and suggest file + symbol lock domains")
    pr_rd.add_argument("--repo", default=".", help="path to git repo (default: cwd)")
    pr_rd.add_argument("--since-days", type=int, default=30, help="lookback window in days")
    pr_rd.add_argument("--top-n", type=int, default=8, help="number of hotspot seeds")
    pr_rd.add_argument("--json", action="store_true", help="JSON output")

    for _sp in (pr_re, pr_rp, pr_rl, pr_ck, pr_ls, pr_au, pr_pc, pr_rd):
        _add_global_opts_to_subparser(_sp)
    return p


def _fmt_entry(e: LockEntry) -> str:
    claim = f"PR#{e.pr}" if e.pr is not None else f"branch:{e.branch}"
    base = f"{e.domain}\t{claim}\t{e.agent}\t{e.branch}\t{e.opened_at}\t{e.status}"
    if e.symbols:
        base += f"\tsymbols={','.join(e.symbols)}"
    if e.closed_at:
        base += f"\tclosed={e.closed_at}"
    return base


def _holder_label(e: LockEntry) -> str:
    """Human-readable lock owner — PR and branch only (no agent)."""
    if e.pr is not None:
        if e.branch:
            return f"PR#{e.pr} ({e.branch})"
        return f"PR#{e.pr}"
    return f"branch={e.branch}"


def _file_headline(
    path: str,
    touched_map: Optional[dict[str, Optional[set[str]]]],
) -> str:
    """Primary line for a conflicting file — always path with extension."""
    if touched_map is None:
        return path
    syms = touched_map.get(path)
    if syms is None:
        return f"{path}  (whole file — symbols unavailable)"
    if not syms:
        return f"{path}  (no symbols in diff)"
    return f"{path}  symbols: {', '.join(sorted(syms))}"


def _format_check_report(
    result: CheckResult,
    registry: Registry,
    files: Iterable[str],
    touched_map: Optional[dict[str, Optional[set[str]]]] = None,
) -> str:
    """Plain-text domain check report for hooks and CLI."""
    lines: list[str] = ["", "merge_train domain status", ""]
    grouped = registry.domains_for_paths(files)

    def _conflict_section(
        header: str,
        conflicts: list[tuple[str, LockEntry]],
    ) -> None:
        if not conflicts:
            return
        lines.append(header)
        for domain, holder in conflicts:
            paths = sorted(grouped.get(domain, []))
            if not paths:
                paths = [domain]
            for path in paths:
                lines.append(f"  {_file_headline(path, touched_map)}")
                lines.append(f"    held by {_holder_label(holder)}")
                if holder.symbols:
                    lines.append(
                        f"    holder symbols: {', '.join(sorted(holder.symbols))}"
                    )
                elif holder.is_whole_domain:
                    lines.append("    holder lock: whole domain")
                if touched_map and holder.symbols:
                    sym_set = touched_map.get(path)
                    if sym_set:
                        overlap = sym_set & set(holder.symbols)
                        if overlap:
                            lines.append(
                                f"    overlap: {', '.join(sorted(overlap))}"
                            )
        lines.append("")

    _conflict_section(
        "ADVISORY (informational, not blocking):",
        result.advisory_held,
    )
    _conflict_section("HELD (blocking):", result.held)

    if not result.held:
        free_files: list[str] = []
        for domain in result.free:
            free_files.extend(sorted(grouped.get(domain, [])))
        free_label = ", ".join(free_files) if free_files else "none"
        lines.append(f"FREE: {len(free_files)} file(s) clear ({free_label})")
        lines.append("")

    return "\n".join(lines)


def _log_to_tmp(argv: list[str], exit_code: int, error_msg: str = ""):
    try:
        log_path = "/tmp/merge_train.log"
        import datetime
        import os
        timestamp = datetime.datetime.now().isoformat()
        cmd_str = " ".join(argv)
        pid = os.getpid()
        log_line = f"[{timestamp}] PID={pid} cmd='{cmd_str}' exit={exit_code}"
        if error_msg:
            log_line += f" err='{error_msg}'"
        log_line += "\n"
        
        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
            with open(fd, "a", encoding="utf-8") as f:
                f.write(log_line)
        except PermissionError:
            pass
    except Exception:
        pass


def main(argv: Optional[list[str]] = None) -> int:
    import sys
    run_argv = argv if argv is not None else sys.argv[1:]
    exit_code = 2
    err_msg = ""
    try:
        exit_code = _main_impl(argv)
        return exit_code
    except Exception as e:
        err_msg = str(e)
        exit_code = 2
        raise
    finally:
        _log_to_tmp(run_argv, exit_code, err_msg)


def _main_impl(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if getattr(args, "pr", None) == 0:
        print("error: PR number cannot be 0 (PR 0 is prohibited)", file=sys.stderr)
        return 2

    if args.log == DEFAULT_LOG:
        cwd = Path(args.git_cwd) if args.git_cwd else None
        args.log = _resolve_default_log(cwd)

    if args.log:
        try:
            log_path = Path(args.log).resolve()
            curr = log_path
            while not curr.exists() and curr.parent != curr:
                curr = curr.parent
            if curr.exists():
                toplevel = _get_git_toplevel(curr)
                if toplevel is not None:
                    print(f"error: lock log path '{args.log}' cannot be inside a git repository worktree", file=sys.stderr)
                    return 2
        except Exception:
            pass

    if args.cmd == "reserve":
        try:
            registry = load_registry(args.registry)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        log = LockLog(args.log)
        syms = [s.strip() for s in (args.symbols or "").split(",") if s.strip()]
        if args.dry_run:
            if args.domain not in registry.domains:
                print(f"error: unknown domain: {args.domain}", file=sys.stderr)
                return 2
            active = log.active_all()
            holders = [e for e in active if e.domain == args.domain]
            for h in holders:
                if h.is_whole_domain:
                    print(f"HELD: {args.domain} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                    return 1
                if syms and set(syms).intersection(h.symbols):
                    overlap = sorted(set(syms).intersection(h.symbols))
                    print(f"HELD: {args.domain} symbols {overlap} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                    return 1
                if not syms:
                    print(f"HELD: {args.domain} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                    return 1
            sym_part = f" symbols={syms}" if syms else ""
            claim_label = f"PR#{args.pr}" if args.pr is not None else f"branch:{args.branch}"
            print(f"WOULD-RESERVE: {args.domain}\t{claim_label}\t{args.agent}\t{args.branch}{sym_part}")
            return 0
        try:
            entry = reserve(
                log, registry,
                domain=args.domain, pr=args.pr,
                agent=args.agent, branch=args.branch,
                symbols=syms,
                override=getattr(args, "override", ""),
                intra_pr_exclusive=True if getattr(args, "intra_pr_exclusive", False) else None,
            )
        except DomainHeldError as exc:
            print(f"DENIED: {exc}", file=sys.stderr)
            return 1
        except UnknownPathError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        tag = "OVERRIDE-RESERVED" if entry.override else "RESERVED"
        print(f"{tag}: {_fmt_entry(entry)}")
        return 0

    if args.cmd == "reserve-plan":
        try:
            registry = load_registry(args.registry)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        log = LockLog(args.log)
        try:
            with open(args.plan, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except FileNotFoundError as exc:
            print(f"error: plan file not found: {exc}", file=sys.stderr)
            return 2
        plan_items = raw.get("plan") or raw.get("reservations") or []
        if not plan_items:
            print("error: plan file has no 'plan' or 'reservations' list",
                  file=sys.stderr)
            return 2
        if args.dry_run:
            active = log.active_all()
            held = False
            for raw_item in plan_items:
                dom = raw_item["domain"] if isinstance(raw_item, dict) else raw_item.domain
                syms = (raw_item.get("symbols") or ()) if isinstance(raw_item, dict) else raw_item.symbols
                if dom not in registry.domains:
                    print(f"error: unknown domain: {dom}", file=sys.stderr)
                    return 2
                holders = [e for e in active if e.domain == dom]
                for h in holders:
                    if h.is_whole_domain:
                        print(f"HELD: {dom} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                        held = True
                        break
                    if syms and set(syms).intersection(h.symbols):
                        overlap = sorted(set(syms).intersection(h.symbols))
                        print(f"HELD: {dom} symbols {overlap} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                        held = True
                        break
                    if not syms:
                        print(f"HELD: {dom} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                        held = True
                        break
                else:
                    sym_part = f" symbols={list(syms)}" if syms else ""
                    claim_label = f"PR#{args.pr}" if args.pr is not None else f"branch:{args.branch}"
                    print(f"WOULD-RESERVE: {dom}\t{claim_label}\t{args.agent}\t{args.branch}{sym_part}")
            return 1 if held else 0
        try:
            entries = reserve_plan(
                log, registry,
                pr=args.pr, agent=args.agent, branch=args.branch,
                plan=plan_items,
                intra_pr_exclusive=True if getattr(args, "intra_pr_exclusive", False) else None,
            )
        except DomainHeldError as exc:
            print(f"DENIED: {exc} (plan rolled back)", file=sys.stderr)
            return 1
        except UnknownPathError as exc:
            print(f"error: {exc} (plan rolled back)", file=sys.stderr)
            return 2
        for entry in entries:
            print(f"RESERVED: {_fmt_entry(entry)}")
        return 0

    if args.cmd == "release":
        log = LockLog(args.log)
        if args.pr is None and getattr(args, "branch", None) is None:
            print("error: release requires one of --pr or --branch", file=sys.stderr)
            return 2
        # The open-PR safety check only applies to PR-keyed releases; a
        # branch-keyed claim (no PR) has nothing to query via `gh pr view`.
        if args.pr is not None and not getattr(args, "force", False):
            repo_name = None
            try:
                res = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    capture_output=True, text=True, check=False,
                    cwd=Path(args.git_cwd) if args.git_cwd else None,
                )
                if res.returncode == 0 and res.stdout.strip():
                    repo_name = _get_repo_name(res.stdout.strip())
            except Exception:
                pass

            if not repo_name:
                print("warning: git remote 'origin' unavailable; proceeding without explicit repo flag", file=sys.stderr)

            gh_cmd = ["gh", "pr", "view", str(args.pr), "--json", "state,files"]
            if repo_name:
                gh_cmd.extend(["--repo", repo_name])

            gh_success = False
            pr_state = None
            pr_files = []
            try:
                proc = subprocess.run(gh_cmd, capture_output=True, text=True, check=False)
                if proc.returncode == 0:
                    try:
                        data = json.loads(proc.stdout)
                        pr_state = data.get("state")
                        pr_files = [f["path"] for f in data.get("files", [])]
                        gh_success = True
                    except Exception as e:
                        print(f"warning: failed to parse gh output: {e}", file=sys.stderr)
                else:
                    print(f"warning: gh pr view failed: {proc.stderr.strip()}", file=sys.stderr)
            except FileNotFoundError:
                print("warning: gh CLI not found; proceeding without PR state verification", file=sys.stderr)
            except Exception as e:
                print(f"warning: error running gh CLI: {e}", file=sys.stderr)

            if gh_success and pr_state == "OPEN":
                active_all = log.active_all()
                to_release_domains = {
                    e.domain for e in active_all
                    if e.pr == args.pr and (args.domain is None or e.domain == args.domain)
                }

                if to_release_domains:
                    try:
                        registry = load_registry(args.registry)
                    except FileNotFoundError as exc:
                        print(f"warning: registry not found, skipping overlap check: {exc}", file=sys.stderr)
                        registry = None

                    if registry is not None:
                        overlapping_domains = set()
                        for f in pr_files:
                            resolved_dom = registry.domain_for_path(f)
                            if resolved_dom in to_release_domains:
                                overlapping_domains.add(resolved_dom)

                        if overlapping_domains:
                            sorted_overlapping = sorted(list(overlapping_domains))
                            print(
                                f"DENIED: Refusing to release active lock for open PR #{args.pr} "
                                f"because it still modifies files in the locked domain(s): {sorted_overlapping}. "
                                f"Use --force to override.",
                                file=sys.stderr
                            )
                            return 1

        released = release(
            log, pr=args.pr,
            branch=getattr(args, "branch", None) if args.pr is None else None,
            domain=args.domain, note=args.note,
        )
        if not released:
            claim_label = f"PR #{args.pr}" if args.pr is not None else f"branch '{getattr(args, 'branch', None)}'"
            print(f"no active reservations for {claim_label}", file=sys.stderr)
            return 1
        for entry in released:
            print(f"RELEASED: {_fmt_entry(entry)}")
        return 0

    if args.cmd == "check":
        try:
            registry = load_registry(args.registry)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        log = LockLog(args.log)
        touched_map: Optional[dict[str, Optional[set[str]]]] = None
        diff_fallback: list[str] = []
        if getattr(args, "symbols", ""):
            syms = set(s.strip() for s in args.symbols.split(",") if s.strip())
            touched_map = {f: syms for f in args.files}
        elif args.diff_mode:
            from merge_train.symbols import resolve_touched_symbols
            cwd = Path(args.git_cwd) if args.git_cwd else None
            per_file, fallback = resolve_touched_symbols(args.files, cwd=cwd)
            touched_map = per_file
            diff_fallback = fallback
            for fb_path in fallback:
                touched_map[fb_path] = None
        result = check(
            log, registry,
            files=args.files, pr=args.pr,
            branch=getattr(args, "branch", None),
            agent=getattr(args, "agent", None),
            touched_symbols_by_path=touched_map,
            override=getattr(args, "override", ""),
        )
        if args.json:
            payload = {
                "ok": result.ok,
                "free_domains": result.free,
                "held": [
                    {"domain": d, "holder": dataclasses.asdict(e)}
                    for d, e in result.held
                ],
                "advisory_held": [
                    {"domain": d, "holder": dataclasses.asdict(e)}
                    for d, e in result.advisory_held
                ],
                "unmapped_files": result.unmapped,
                "touched_symbols": {
                    d: sorted(s) for d, s in result.touched_symbols.items()
                },
            }
            if diff_fallback:
                payload["fallback_files"] = diff_fallback
            print(json.dumps(payload, indent=2))
        else:
            if result.unmapped:
                print(f"⚠️  WARN: unmapped files (no domain): "
                      f"{', '.join(result.unmapped)}", file=sys.stderr)
            if diff_fallback:
                print(f"⚠️  WARN: symbol-resolution fallback (whole-domain): "
                      f"{', '.join(diff_fallback)}", file=sys.stderr)
            
            print(
                _format_check_report(
                    result, registry, args.files, touched_map,
                ),
                end="",
            )
        return 0 if result.ok else 1

    if args.cmd == "list":
        log = LockLog(args.log)
        entries = list_locks(log, status=args.status)
        if args.json:
            print(json.dumps([dataclasses.asdict(e) for e in entries], indent=2))
        else:
            if not entries:
                print(f"no {args.status} locks")
            for entry in entries:
                print(_fmt_entry(entry))
        return 0

    if args.cmd == "audit":
        try:
            registry = load_registry(args.registry)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        log = LockLog(args.log)
        print(json.dumps(audit(log, registry), indent=2))
        return 0

    if args.cmd == "recommend-domains":
        from merge_train.domain_recommender import recommend_domains, to_yaml_dict
        repo = Path(args.repo)
        suggestions = recommend_domains(repo=repo, since_days=args.since_days, top_n=args.top_n)
        payload = to_yaml_dict(suggestions)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(yaml.safe_dump(payload, sort_keys=False))
        return 0

    if args.cmd == "predict-conflicts":
        try:
            registry = load_registry(args.registry)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        from merge_train.predict import cli_predict_conflicts
        cwd = Path(args.git_cwd) if args.git_cwd else None
        plan_path = getattr(args, "plan", None)
        from_prs = getattr(args, "from_prs", None)
        if not plan_path and not from_prs:
            print("error: one of --plan or --from-prs is required",
                  file=sys.stderr)
            return 2
        return cli_predict_conflicts(
            plan_path=plan_path,
            registry=registry,
            include_textual=not args.no_textual,
            git_base=args.git_base,
            git_cwd=cwd,
            json_output=args.json,
            from_prs=from_prs,
            repo=getattr(args, "repo", None),
            enrich_symbols=getattr(args, "enrich_symbols", False),
        )

    return 2


if __name__ == "__main__":
    sys.exit(main())
