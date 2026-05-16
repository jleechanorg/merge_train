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
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import yaml


DEFAULT_REGISTRY = "file_domains.yaml"
DEFAULT_LOG = "pr_domain_locks.jsonl"


class DomainHeldError(Exception):
    """Raised when a reservation is requested for a domain already held."""


class UnknownPathError(Exception):
    """Raised when a file path is not mapped to any declared domain."""


@dataclass(frozen=True)
class Domain:
    name: str
    paths: tuple[str, ...]
    owners: tuple[str, ...] = ()


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
            domains[name] = Domain(name=name, paths=paths, owners=owners)
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


@dataclass(frozen=True)
class LockEntry:
    domain: str
    pr: int
    agent: str
    branch: str
    opened_at: str
    status: str  # "active" | "released"
    closed_at: Optional[str] = None
    note: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "LockEntry":
        data = json.loads(line)
        return cls(**data)


class LockLog:
    """Append-only JSONL lock log."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def append(self, entry: LockEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.to_json() + "\n")

    def entries(self) -> list[LockEntry]:
        if not self.path.exists():
            return []
        out: list[LockEntry] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                out.append(LockEntry.from_json(raw))
        return out

    def active(self) -> dict[str, LockEntry]:
        """Resolve append-only log to current active locks keyed by domain.

        A domain is active if its most recent entry has status="active".
        A later "released" entry clears it. Reserving an already-active
        domain is rejected by :func:`reserve`; we do not enforce it here.
        """
        latest: dict[str, LockEntry] = {}
        for entry in self.entries():
            latest[entry.domain] = entry
        return {d: e for d, e in latest.items() if e.status == "active"}


def load_registry(path: str | os.PathLike = DEFAULT_REGISTRY) -> Registry:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"registry file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Registry.from_dict(data)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reserve(
    log: LockLog,
    registry: Registry,
    *,
    domain: str,
    pr: int,
    agent: str,
    branch: str,
    now: Optional[str] = None,
) -> LockEntry:
    """Reserve *domain* for the given PR/agent.

    Raises DomainHeldError if another active reservation exists.
    """
    if domain not in registry.domains:
        raise UnknownPathError(f"unknown domain: {domain}")
    active = log.active()
    if domain in active:
        held = active[domain]
        raise DomainHeldError(
            f"domain '{domain}' is held by PR #{held.pr} (agent={held.agent}, "
            f"branch={held.branch}, opened_at={held.opened_at})"
        )
    entry = LockEntry(
        domain=domain,
        pr=pr,
        agent=agent,
        branch=branch,
        opened_at=now or _utcnow(),
        status="active",
    )
    log.append(entry)
    return entry


def release(
    log: LockLog,
    *,
    pr: int,
    domain: Optional[str] = None,
    note: Optional[str] = None,
    now: Optional[str] = None,
) -> list[LockEntry]:
    """Release all active reservations for *pr* (optionally filter to a domain).

    Returns the list of release entries written.
    """
    released: list[LockEntry] = []
    for d, entry in log.active().items():
        if entry.pr != pr:
            continue
        if domain and d != domain:
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
        )
        log.append(rel)
        released.append(rel)
    return released


@dataclass(frozen=True)
class CheckResult:
    free: list[str]
    held: list[tuple[str, LockEntry]]  # (domain, holder)
    unmapped: list[str]

    @property
    def ok(self) -> bool:
        return not self.held


def check(
    log: LockLog,
    registry: Registry,
    *,
    files: Iterable[str],
    pr: Optional[int] = None,
) -> CheckResult:
    """Check whether *files* collide with any active reservation.

    If *pr* is provided, reservations already held by that PR are
    treated as free (re-checks should not self-conflict).
    """
    grouped = registry.domains_for_paths(files)
    active = log.active()
    free: list[str] = []
    held: list[tuple[str, LockEntry]] = []
    unmapped = grouped.pop("__unmapped__", [])
    for domain in grouped:
        holder = active.get(domain)
        if holder is None or (pr is not None and holder.pr == pr):
            free.append(domain)
        else:
            held.append((domain, holder))
    return CheckResult(free=free, held=held, unmapped=unmapped)


def list_locks(log: LockLog, *, status: str = "active") -> list[LockEntry]:
    if status == "active":
        return list(log.active().values())
    if status == "all":
        return log.entries()
    return [e for e in log.entries() if e.status == status]


def audit(log: LockLog, registry: Registry) -> dict:
    """Return a structured audit report of registry + lock-log state."""
    active = log.active()
    return {
        "registry": {
            "domains": {
                name: {"paths": list(d.paths), "owners": list(d.owners)}
                for name, d in registry.domains.items()
            }
        },
        "active_locks": {
            d: dataclasses.asdict(e) for d, e in active.items()
        },
        "total_entries": len(log.entries()),
        "generated_at": _utcnow(),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


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
    sub = p.add_subparsers(dest="cmd", required=True)

    pr_re = sub.add_parser("reserve", help="reserve a domain for a PR/agent")
    pr_re.add_argument("--domain", required=True)
    pr_re.add_argument("--pr", type=int, required=True)
    pr_re.add_argument("--agent", required=True)
    pr_re.add_argument("--branch", required=True)

    pr_rl = sub.add_parser("release", help="release a PR's reservations")
    pr_rl.add_argument("--pr", type=int, required=True)
    pr_rl.add_argument("--domain", default=None)
    pr_rl.add_argument("--note", default=None)

    pr_ck = sub.add_parser("check", help="check files against active reservations")
    pr_ck.add_argument("--files", nargs="+", required=True)
    pr_ck.add_argument("--pr", type=int, default=None,
                       help="PR being checked; its own reservations don't self-conflict")
    pr_ck.add_argument("--json", action="store_true", help="JSON output")

    pr_ls = sub.add_parser("list", help="list locks")
    pr_ls.add_argument("--status", default="active",
                       choices=["active", "released", "all"])
    pr_ls.add_argument("--json", action="store_true")

    sub.add_parser("audit", help="dump full registry + lock-log audit JSON")
    return p


def _fmt_entry(e: LockEntry) -> str:
    base = f"{e.domain}\tPR#{e.pr}\t{e.agent}\t{e.branch}\t{e.opened_at}\t{e.status}"
    if e.closed_at:
        base += f"\tclosed={e.closed_at}"
    return base


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        registry = load_registry(args.registry)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    log = LockLog(args.log)

    if args.cmd == "reserve":
        try:
            entry = reserve(
                log, registry,
                domain=args.domain, pr=args.pr,
                agent=args.agent, branch=args.branch,
            )
        except DomainHeldError as exc:
            print(f"DENIED: {exc}", file=sys.stderr)
            return 1
        except UnknownPathError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"RESERVED: {_fmt_entry(entry)}")
        return 0

    if args.cmd == "release":
        released = release(log, pr=args.pr, domain=args.domain, note=args.note)
        if not released:
            print(f"no active reservations for PR #{args.pr}", file=sys.stderr)
            return 1
        for entry in released:
            print(f"RELEASED: {_fmt_entry(entry)}")
        return 0

    if args.cmd == "check":
        result = check(log, registry, files=args.files, pr=args.pr)
        if args.json:
            payload = {
                "ok": result.ok,
                "free_domains": result.free,
                "held": [
                    {"domain": d, "holder": dataclasses.asdict(e)}
                    for d, e in result.held
                ],
                "unmapped_files": result.unmapped,
            }
            print(json.dumps(payload, indent=2))
        else:
            if result.unmapped:
                print(f"WARN: unmapped files (no domain): "
                      f"{', '.join(result.unmapped)}", file=sys.stderr)
            if not result.held:
                print(f"FREE: {len(result.free)} domain(s) clear "
                      f"({', '.join(result.free) or 'none'})")
            else:
                for d, holder in result.held:
                    print(f"HELD: {d} by PR#{holder.pr} "
                          f"agent={holder.agent} branch={holder.branch}")
        return 0 if result.ok else 1

    if args.cmd == "list":
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
        print(json.dumps(audit(log, registry), indent=2))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
