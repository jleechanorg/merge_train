"""Per-repo conflict enforcement config for the merge_train Claude hook.

The conflict hook (``~/.local/bin/conflict-warn-pre-tool.sh`` →
``conflict_check_helper.py``) runs on every Edit/Write. It needs to
decide, per-repo, whether to **block**, **warn-only**, or **allow**
conflicts. This module centralises that policy in a single JSON file
at ``~/merge_train/config.json`` so adding a new repo to enforce
becomes a config edit, not a Python change.

Schema::

    {
      "default_enforcement": "warn",   // "block" | "warn" | "allow"
      "repos": {
        "/abs/path/to/repo": {
          "alias": "short_name",        // optional, used in chat output
          "enforcement": "block"
        },
        ...
      }
    }

If the file is missing or malformed, ``load_config()`` returns the
default config — never raises. This keeps the hook fail-safe.

Typical use::

    from merge_train.config import load_config, lookup_enforcement

    cfg = load_config()
    mode = lookup_enforcement(cfg, "/Users/jleechan/projects/merge_train")
    # mode in {"block", "warn", "allow"}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

#: Canonical location of the user-scope config file.
#: Resolved lazily via :func:`config_path` so tests can monkeypatch
#: ``Path.home`` or ``HOME``.
CONFIG_FILENAME: str = "config.json"
CONFIG_DIR_PARTS: tuple[str, ...] = ("merge_train",)


def config_path() -> Path:
    """Return the absolute path of the user-scope config file.

    Default: ``$HOME/merge_train/config.json``. Resolved lazily so
    test fixtures can override ``Path.home`` before the first call.
    """
    return Path.home().joinpath(*CONFIG_DIR_PARTS, CONFIG_FILENAME)


#: Enforcement modes accepted in the config.
VALID_ENFORCEMENT: tuple[str, ...] = ("block", "warn", "allow")


def default_config() -> dict:
    """Return the in-memory default config used when no file exists.

    Every repo defaults to ``warn``. The previous default of ``block``
    for the merge_train repo itself was misleading: Claude Code's
    PreToolUse protocol does not actually prevent the Edit tool from
    running when the hook returns ``decision: block``, so a "blocked"
    edit silently completed and was committed. The honest default is
    warn-only for every repo — the TUI banner is the visible signal.
    """
    return {
        "default_enforcement": "warn",
        "repos": {
            "/Users/jleechan/projects/merge_train": {
                "alias": "merge_train",
                "enforcement": "warn",
            },
        },
    }


def load_config(path: Optional[Path] = None) -> dict:
    """Load the config file, falling back to :func:`default_config`.

    Args:
        path: Override the config location. Defaults to
            :func:`config_path` (i.e. ``~/merge_train/config.json``).

    Returns:
        The parsed config dict. Never raises — missing file,
        malformed JSON, or wrong shape all return the default config.
    """
    if path is None:
        path = config_path()
    try:
        if not path.exists():
            return default_config()
        raw = path.read_text()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return default_config()

    if not isinstance(data, dict):
        return default_config()

    # Normalise shape: ensure "repos" is a dict, "default_enforcement" is valid.
    repos = data.get("repos")
    if not isinstance(repos, dict):
        data["repos"] = {}
    default = data.get("default_enforcement")
    if default not in VALID_ENFORCEMENT:
        data["default_enforcement"] = "warn"
    return data


def save_config(cfg: dict, path: Optional[Path] = None) -> Path:
    """Persist *cfg* as JSON. Creates the parent directory if missing.

    Returns:
        The path the config was written to.

    Raises:
        OSError: if the file cannot be written (e.g. permission denied).
    """
    if path is None:
        path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    return path


def add_repo(
    repo_path: str,
    enforcement: str = "warn",
    alias: Optional[str] = None,
    path: Optional[Path] = None,
) -> dict:
    """Add (or update) a repo entry in the config file. Idempotent.

    Args:
        repo_path: Absolute path to the git repo (used as the key).
        enforcement: One of ``"block"``, ``"warn"``, ``"allow"``.
        alias: Optional short name for chat output. Defaults to the
            basename of *repo_path*.
        path: Override the config file location (for tests).

    Returns:
        The updated config dict (also persisted to disk).

    Raises:
        ValueError: if *enforcement* is not a valid mode.
        OSError: if the file cannot be written.
    """
    if enforcement not in VALID_ENFORCEMENT:
        raise ValueError(
            f"enforcement must be one of {VALID_ENFORCEMENT}, got {enforcement!r}"
        )
    cfg = load_config(path=path)
    abs_path = str(Path(repo_path).expanduser().resolve())
    entry: dict = {"enforcement": enforcement}
    if alias:
        entry["alias"] = alias
    else:
        entry.setdefault("alias", Path(abs_path).name)
    cfg.setdefault("repos", {})[abs_path] = entry
    save_config(cfg, path=path)
    return cfg


def remove_repo(repo_path: str, path: Optional[Path] = None) -> dict:
    """Remove a repo entry. Truly a no-op if absent (no file write).

    Returns the (possibly unchanged) config dict. This matters for fresh
    installs: ``merge_train config remove /typo/path`` should not create
    the config file just to record the absent removal.
    """
    cfg = load_config(path=path)
    abs_path = str(Path(repo_path).expanduser().resolve())
    repos = cfg.setdefault("repos", {})
    if abs_path not in repos:
        return cfg
    repos.pop(abs_path)
    save_config(cfg, path=path)
    return cfg


def lookup_enforcement(cfg: dict, repo_path: str) -> str:
    """Resolve the effective enforcement for *repo_path* against *cfg*.

    Order:
        1. ``cfg["repos"][repo_path]["enforcement"]`` if present
        2. ``cfg["default_enforcement"]`` (always present in a valid cfg)
        3. Literal ``"warn"`` as a last-resort fallback

    Returns:
        One of ``"block"``, ``"warn"``, ``"allow"``.
    """
    if not isinstance(cfg, dict):
        return "warn"
    repos = cfg.get("repos")
    if isinstance(repos, dict):
        entry = repos.get(repo_path)
        if isinstance(entry, dict):
            mode = entry.get("enforcement")
            if mode in VALID_ENFORCEMENT:
                return mode
    default = cfg.get("default_enforcement")
    if default in VALID_ENFORCEMENT:
        return default
    return "warn"


def get_repo_alias(cfg: dict, repo_path: str) -> str:
    """Return a short alias for chat output, or the repo basename."""
    if isinstance(cfg, dict):
        repos = cfg.get("repos")
        if isinstance(repos, dict):
            entry = repos.get(repo_path)
            if isinstance(entry, dict):
                alias = entry.get("alias")
                if isinstance(alias, str) and alias:
                    return alias
    return Path(repo_path).name


__all__ = (
    "CONFIG_FILENAME",
    "VALID_ENFORCEMENT",
    "config_path",
    "default_config",
    "load_config",
    "save_config",
    "add_repo",
    "remove_repo",
    "lookup_enforcement",
    "get_repo_alias",
)
