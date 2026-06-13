"""``merge_train`` multi-subcommand CLI entry point.

Routes the top-level ``merge_train`` command to one of the installed
subcommands:

- ``predict-conflicts`` (alias of the standalone ``predict-conflicts`` script)
- ``install-hooks`` (Phase C roadmap)
- ``test-hooks`` (Phase C roadmap)
- ``config`` (per-repo enforcement config at ``~/merge_train/config.json``)

Example::

    merge_train install-hooks --agent claude
    merge_train test-hooks --agent all
    merge_train config init
    merge_train config add /path/to/repo --enforce block
    merge_train config show
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merge_train",
        description=(
            "merge_train: symbol-level PR conflict prediction + per-agent hook installer. "
            "All hooks are warn-only after PR #18."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # install-hooks ---------------------------------------------------------
    p_install = sub.add_parser(
        "install-hooks",
        help="Install conflict-warn hooks for one or more agents (idempotent).",
    )
    p_install.add_argument(
        "--agent",
        required=True,
        choices=["claude", "opencode", "codex", "agy", "all"],
        help="Which agent to install hooks for.",
    )
    p_install.add_argument(
        "--target",
        default=None,
        help="Target repo root (default: cwd). Used by opencode for .opencode.json.",
    )

    # test-hooks ------------------------------------------------------------
    p_test = sub.add_parser(
        "test-hooks",
        help="Run synthetic-Edit test against installed hooks.",
    )
    p_test.add_argument(
        "--agent",
        required=True,
        choices=["claude", "opencode", "codex", "agy", "all"],
        help="Which agent to test hooks for.",
    )
    p_test.add_argument(
        "--target",
        default=None,
        help="Target repo root (default: cwd). Used by opencode.",
    )

    # config ----------------------------------------------------------------
    p_config = sub.add_parser(
        "config",
        help=(
            "Manage per-repo enforcement config at ~/merge_train/config.json. "
            "The conflict hook reads this to decide block vs warn-only per repo."
        ),
    )
    config_sub = p_config.add_subparsers(dest="config_cmd", required=True)

    p_cfg_init = config_sub.add_parser(
        "init",
        help="Create ~/merge_train/config.json with sensible defaults if missing.",
    )
    p_cfg_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing config file.",
    )

    p_cfg_show = config_sub.add_parser(
        "show",
        help="Print the current config (or a single repo's effective enforcement).",
    )
    p_cfg_show.add_argument(
        "repo",
        nargs="?",
        default=None,
        help="Optional repo path. If given, print only that repo's effective enforcement + alias.",
    )

    p_cfg_add = config_sub.add_parser(
        "add",
        help="Add (or update) a repo entry. Idempotent.",
    )
    p_cfg_add.add_argument(
        "repo",
        help="Absolute path to the git repo.",
    )
    p_cfg_add.add_argument(
        "--enforce",
        required=True,
        choices=["block", "warn", "allow"],
        help="Enforcement mode for this repo.",
    )
    p_cfg_add.add_argument(
        "--alias",
        default=None,
        help="Short alias for chat output (default: repo basename).",
    )

    p_cfg_remove = config_sub.add_parser(
        "remove",
        help="Remove a repo entry. No-op if absent.",
    )
    p_cfg_remove.add_argument(
        "repo",
        help="Absolute path to the git repo to remove.",
    )

    return parser


def _cmd_config(args: argparse.Namespace) -> int:
    """Dispatch ``merge_train config <subcmd>``."""
    from merge_train import config as mt_config

    if args.config_cmd == "init":
        path = mt_config.config_path()
        if path.exists() and not args.force:
            print(f"config: {path} already exists (use --force to overwrite)", file=sys.stderr)
            return 0
        cfg = mt_config.default_config()
        mt_config.save_config(cfg, path=path)
        print(json.dumps({"initialized": str(path), "config": cfg}, indent=2))
        return 0

    if args.config_cmd == "show":
        cfg = mt_config.load_config()
        if args.repo:
            abs_path = str(Path(args.repo).expanduser().resolve())
            mode = mt_config.lookup_enforcement(cfg, abs_path)
            alias = mt_config.get_repo_alias(cfg, abs_path)
            print(json.dumps({"repo": abs_path, "alias": alias, "enforcement": mode}, indent=2))
        else:
            print(json.dumps({"config_path": str(mt_config.config_path()), **cfg}, indent=2))
        return 0

    if args.config_cmd == "add":
        try:
            cfg = mt_config.add_repo(args.repo, enforcement=args.enforce, alias=args.alias)
        except ValueError as exc:
            print(f"config: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"config: failed to write config: {exc}", file=sys.stderr)
            return 1
        abs_path = str(Path(args.repo).expanduser().resolve())
        print(json.dumps({
            "added": abs_path,
            "enforcement": args.enforce,
            "alias": args.alias or Path(abs_path).name,
            "config": cfg,
        }, indent=2))
        return 0

    if args.config_cmd == "remove":
        try:
            cfg = mt_config.remove_repo(args.repo)
        except OSError as exc:
            print(f"config: failed to write config: {exc}", file=sys.stderr)
            return 1
        abs_path = str(Path(args.repo).expanduser().resolve())
        print(json.dumps({"removed": abs_path, "config": cfg}, indent=2))
        return 0

    print(f"config: unknown subcommand {args.config_cmd!r}", file=sys.stderr)
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for the ``merge_train`` console script."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "install-hooks":
        from merge_train.hook_install import install_hooks_for_agent

        target = Path(args.target) if args.target else None
        result = install_hooks_for_agent(args.agent, target=target)
        print(json.dumps(result, indent=2))
        if isinstance(result, list):
            return 0 if all(r.get("installed") for r in result) else 1
        return 0 if result.get("installed") else 1

    if args.cmd == "test-hooks":
        from merge_train.hook_install import test_hooks_for_agent

        target = Path(args.target) if args.target else None
        result = test_hooks_for_agent(args.agent, target=target)
        print(json.dumps(result, indent=2))
        if isinstance(result, list):
            return 0 if all(r.get("ok") for r in result) else 1
        return 0 if result.get("ok") else 1

    if args.cmd == "config":
        return _cmd_config(args)

    parser.error(f"unknown subcommand: {args.cmd}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
