"""Symbol-level resolution for merge_train.

Resolves which top-level Python symbols (functions, classes, methods) are
touched by a set of file modifications. Powers symbol-level reservations
in :mod:`merge_train.domain_lock` — two PRs editing the same file but
disjoint symbols both proceed; overlapping symbols collide.

Python files are parsed with the stdlib :mod:`ast` module. Non-Python
files raise :class:`UnsupportedLanguageError`; callers fall back to
file-level locking for those.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


class UnsupportedLanguageError(Exception):
    """Raised when symbol extraction is requested for an unsupported file type."""


class SymbolResolutionError(Exception):
    """Raised when symbol extraction fails — caller should fall back to file-level."""


@dataclass(frozen=True)
class Symbol:
    """A named code region with an inclusive line range.

    Methods are emitted as ``ClassName.method_name`` so two classes can
    have like-named methods without colliding in the reservation set.
    """

    name: str
    start: int  # inclusive, 1-based (matches ast.lineno)
    end: int    # inclusive

    def contains_line(self, line: int) -> bool:
        return self.start <= line <= self.end

    def overlaps(self, lo: int, hi: int) -> bool:
        return not (hi < self.start or lo > self.end)


def _node_range(node: ast.AST) -> Optional[tuple[int, int]]:
    """Return (start, end) line range for a node, or None if not located."""
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if start is None or end is None:
        return None
    # Include decorator lines so an edit to "@decorator" counts.
    decorators = getattr(node, "decorator_list", []) or []
    for dec in decorators:
        dec_start = getattr(dec, "lineno", None)
        if dec_start is not None and dec_start < start:
            start = dec_start
    return (start, end)


def extract_symbols(source: str) -> list[Symbol]:
    """Extract top-level symbols and methods from Python source.

    Emits:
      * top-level ``def`` / ``async def`` -> ``name``
      * top-level ``class`` -> ``ClassName``
      * methods inside a top-level class -> ``ClassName.method_name``

    Nested defs (function inside function) are NOT emitted separately —
    they're considered part of the enclosing symbol. This matches the
    granularity people actually reserve at.

    Raises :class:`SyntaxError` if the source can't be parsed.
    """
    tree = ast.parse(source)
    out: list[Symbol] = []

    for node in tree.body:
        rng = _node_range(node)
        if rng is None:
            continue
        start, end = rng

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(Symbol(name=node.name, start=start, end=end))
        elif isinstance(node, ast.ClassDef):
            out.append(Symbol(name=node.name, start=start, end=end))
            for child in node.body:
                child_rng = _node_range(child)
                if child_rng is None:
                    continue
                c_start, c_end = child_rng
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(Symbol(
                        name=f"{node.name}.{child.name}",
                        start=c_start, end=c_end,
                    ))

    return out


# --------------------------------------------------------------------------- #
# Diff hunk parser
# --------------------------------------------------------------------------- #

_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True)
class HunkRange:
    """An inclusive line range in the *new* file touched by a diff hunk."""
    start: int
    end: int


def parse_hunks(diff_text: str) -> list[HunkRange]:
    """Parse unified-diff hunk headers, return ranges in the new file.

    Pure-addition hunks (old_count=0) are reported with the line the
    additions land on. Pure-deletion hunks (new_count=0) are reported as
    a zero-width range at the deletion's adjacent line — we widen by 1
    so an "intersects" check still catches the surrounding symbol.

    The text is expected to be the output of ``git diff -U0`` (or a
    file slice thereof) — any other lines are ignored.
    """
    out: list[HunkRange] = []
    for line in diff_text.splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        new_start = int(m.group("new_start"))
        new_count_s = m.group("new_count")
        new_count = int(new_count_s) if new_count_s is not None else 1
        if new_count == 0:
            # Pure deletion: widen by 1 line so we catch adjacent symbol.
            start = max(1, new_start)
            end = new_start + 1
        else:
            start = new_start
            end = new_start + new_count - 1
        out.append(HunkRange(start=start, end=end))
    return out


def touched_symbols(
    *,
    new_source: str,
    diff_text: str,
) -> set[str]:
    """Return the set of symbol names whose line range intersects any hunk.

    *new_source* is the post-edit file content; *diff_text* is the
    unified-diff text (``git diff -U0`` style).

    Raises :class:`SymbolResolutionError` if the source cannot be parsed
    (SyntaxError) or if the file is non-empty but contains no top-level
    symbols — callers should fall back to file-level collision semantics.
    """
    try:
        symbols = extract_symbols(new_source)
    except SyntaxError as exc:
        raise SymbolResolutionError(
            f"cannot parse source: {exc}"
        ) from exc
    if new_source.strip() and not symbols:
        raise SymbolResolutionError(
            "non-empty source yielded no symbols — falling back to file-level"
        )
    hunks = parse_hunks(diff_text)
    if not hunks:
        return set()
    out: set[str] = set()
    for sym in symbols:
        for hunk in hunks:
            if sym.overlaps(hunk.start, hunk.end):
                out.add(sym.name)
                break
    return out


# --------------------------------------------------------------------------- #
# Git driver
# --------------------------------------------------------------------------- #


def is_python_path(path: str) -> bool:
    return path.endswith(".py")


def is_markdown_path(path: str) -> bool:
    return path.endswith(".md")


def extract_markdown_symbols(source: str, *, file_stem: str = "") -> list[Symbol]:
    """Extract Markdown headings as symbols.

    Each ``## Heading`` becomes a Symbol named ``md:<stem>.<heading_slug>``
    where the slug is the heading text lowercased with spaces replaced by
    underscores and non-alphanumeric chars stripped. The line range
    covers from the heading line to the line before the next same-or-higher
    level heading (or EOF).

    If *file_stem* is provided (e.g. ``"shared_plan"``), symbols are named
    ``md:shared_plan.slot_01``. Without it, ``md:slot_01``.

    Only ``##`` (h2) and deeper are extracted; the document title (``#``)
    is excluded since it's typically unique and not a useful lock unit.
    """
    lines = source.splitlines()
    headings: list[tuple[int, int, str, str]] = []
    for i, line in enumerate(lines, start=1):
        m = re.match(r"^(#{2,6})\s+(.+)$", line)
        if not m:
            continue
        level = len(m.group(1))
        title = m.group(2).strip()
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
        prefix = f"md:{file_stem}." if file_stem else "md:"
        headings.append((i, level, title, f"{prefix}{slug}"))

    out: list[Symbol] = []
    for idx, (start, level, _title, name) in enumerate(headings):
        end = len(lines)
        for j in range(idx + 1, len(headings)):
            if headings[j][1] <= level:
                end = headings[j][0] - 1
                break
        out.append(Symbol(name=name, start=start, end=end))
    return out


def _run_git(args: list[str], *, cwd: Optional[Path] = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd) if cwd is not None else None,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def staged_diff_for_file(path: str, *, cwd: Optional[Path] = None) -> str:
    """Return ``git diff --cached -U0`` for a single staged file."""
    return _run_git(
        ["diff", "--cached", "-U0", "--no-color", "--", path],
        cwd=cwd,
    )


def staged_content_for_file(path: str, *, cwd: Optional[Path] = None) -> str:
    """Return the staged (index) content for a file."""
    return _run_git(["show", f":{path}"], cwd=cwd)


def _find_merge_base(cwd: Optional[Path] = None) -> Optional[str]:
    """Find the common ancestor (merge-base) of HEAD and origin/main or main."""
    for base_ref in ("origin/main", "main"):
        try:
            proc = subprocess.run(
                ["git", "merge-base", base_ref, "HEAD"],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(cwd) if cwd is not None else None,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except Exception:
            pass
    return None


def _diff_and_source_for_file(
    path: str,
    *,
    cwd: Optional[Path] = None,
) -> tuple[str, str]:
    """Get the diff and the post-edit content for a file, supporting staged,
    unstaged, and committed (vs origin/main or main) changes.

    Returns (diff_text, new_source_text).
    Raises ValueError if there are no changes or if the source cannot be loaded.
    """
    # 1. Staged changes
    diff = staged_diff_for_file(path, cwd=cwd)
    if diff.strip():
        try:
            new_source = staged_content_for_file(path, cwd=cwd)
            return diff, new_source
        except Exception:
            pass

    # 2. Fallback to committed changes (vs origin/main or main) or unstaged changes
    try:
        base = _find_merge_base(cwd=cwd)
        if base:
            diff = _run_git(["diff", base, "-U0", "--no-color", "--", path], cwd=cwd)
        else:
            diff = _run_git(["diff", "HEAD", "-U0", "--no-color", "--", path], cwd=cwd)
    except Exception:
        raise ValueError("failed to get git diff")

    if diff.strip():
        file_path = Path(cwd) / path if cwd else Path(path)
        if file_path.exists():
            try:
                new_source = file_path.read_text(encoding="utf-8")
                return diff, new_source
            except Exception as exc:
                raise ValueError(f"failed to read file on disk: {exc}")
        else:
            raise ValueError("file does not exist on disk")
    else:
        raise ValueError("no changes detected in diff")


def touched_symbols_for_staged_file(
    path: str,
    *,
    cwd: Optional[Path] = None,
) -> set[str]:
    """Resolve touched symbols for a staged file via git plumbing.

    Supports Python (.py) and Markdown (.md). Raises
    :class:`UnsupportedLanguageError` for other file types — callers
    should treat that as a file-level collision.
    """
    if is_markdown_path(path):
        try:
            diff, new_source = _diff_and_source_for_file(path, cwd=cwd)
        except ValueError:
            return set()
        file_stem = Path(path).stem
        try:
            return _touched_markdown_symbols(new_source=new_source, diff_text=diff, file_stem=file_stem)
        except SymbolResolutionError as exc:
            raise UnsupportedLanguageError(
                f"markdown symbol resolution failed for {path}: {exc}"
            ) from exc

    if not is_python_path(path):
        raise UnsupportedLanguageError(f"symbol extraction supports .py and .md only: {path}")

    try:
        diff, new_source = _diff_and_source_for_file(path, cwd=cwd)
    except ValueError:
        return set()

    try:
        syms = touched_symbols(new_source=new_source, diff_text=diff)
        return {f"{path}:{s}" for s in syms}
    except SymbolResolutionError as exc:
        raise UnsupportedLanguageError(
            f"symbol resolution failed for {path}: {exc}"
        ) from exc


def resolve_touched_symbols(
    paths: Iterable[str],
    *,
    cwd: Optional[Path] = None,
) -> tuple[dict[str, set[str]], list[str]]:
    """Resolve every path to its set of touched symbols.

    Returns ``(per_file, file_level_fallback)`` where:
      * ``per_file`` maps path -> set of touched symbol names (Python/Markdown)
      * ``file_level_fallback`` lists paths that couldn't be symbol-resolved
        (unsupported type, missing, parse error) — callers fall back to
        whole-domain collision for these.
    """
    per_file: dict[str, set[str]] = {}
    fallback: list[str] = []
    for path in paths:
        try:
            per_file[path] = touched_symbols_for_staged_file(path, cwd=cwd)
        except Exception:
            fallback.append(path)
    return per_file, fallback


def _touched_markdown_symbols(
    *,
    new_source: str,
    diff_text: str,
    file_stem: str = "",
) -> set[str]:
    """Return the set of Markdown heading symbols whose range intersects any hunk."""
    symbols = extract_markdown_symbols(new_source, file_stem=file_stem)
    if new_source.strip() and not symbols:
        raise SymbolResolutionError(
            "non-empty markdown yielded no heading symbols — falling back to file-level"
        )
    hunks = parse_hunks(diff_text)
    if not hunks:
        return set()
    out: set[str] = set()
    for sym in symbols:
        for hunk in hunks:
            if sym.overlaps(hunk.start, hunk.end):
                out.add(sym.name)
                break
    return out
