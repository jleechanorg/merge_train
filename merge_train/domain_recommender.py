from __future__ import annotations

import ast
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class DomainSuggestion:
    name: str
    files: tuple[str, ...]
    symbols: tuple[str, ...]
    reason: str


def _run_git(repo: Path, args: list[str]) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), check=False, capture_output=True, text=True)
    return proc.stdout


def _python_symbols(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
    return out


def recommend_domains(repo: Path, since_days: int = 30, top_n: int = 8) -> list[DomainSuggestion]:
    raw = _run_git(repo, ["log", f"--since={since_days}.days", "--name-only", "--pretty=format:__C__"])
    commits = [c for c in raw.split("__C__") if c.strip()]
    file_freq: Counter[str] = Counter()
    cochange: dict[tuple[str, str], int] = defaultdict(int)
    for chunk in commits:
        files = sorted({ln.strip() for ln in chunk.splitlines() if ln.strip()})
        py_files = [f for f in files if f.endswith(".py")]
        for f in py_files:
            file_freq[f] += 1
        for i, a in enumerate(py_files):
            for b in py_files[i + 1:]:
                cochange[(a, b)] += 1

    hot = [f for f, _ in file_freq.most_common(top_n)]
    suggestions: list[DomainSuggestion] = []
    for f in hot:
        pair_files = [f]
        for (a, b), n in cochange.items():
            if n < 2:
                continue
            if a == f:
                pair_files.append(b)
            elif b == f:
                pair_files.append(a)
        uniq_files = tuple(sorted(set(pair_files)))
        sym_union: set[str] = set()
        for rel in uniq_files:
            sym_union.update(_python_symbols(repo / rel))
        top_symbols = tuple(sorted(sym_union)[:20])
        name = f"hotspot_{Path(f).stem}"
        suggestions.append(DomainSuggestion(name=name, files=uniq_files, symbols=top_symbols, reason="recent co-change hotspot"))
    return suggestions


def to_yaml_dict(suggestions: Iterable[DomainSuggestion]) -> dict:
    domains: dict[str, dict] = {}
    symbol_groups: dict[str, dict] = {}
    for s in suggestions:
        domains[s.name] = {"paths": list(s.files), "owners": [], "reason": s.reason}
        symbol_groups[s.name] = {"symbols": list(s.symbols), "reason": "recommended symbol lock group"}
    return {"domains": domains, "symbol_groups": symbol_groups}
