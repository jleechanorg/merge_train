"""``merge_train`` multi-subcommand CLI entry point.

Routes the top-level ``merge_train`` command to one of the installed
subcommands:

- ``predict-conflicts`` (alias of the standalone ``predict-conflicts`` script)
- ``install-hooks`` (Phase C roadmap)
- ``test-hooks`` (Phase C roadmap)

Example::

    merge_train install-hooks --agent claude
    merge_train test-hooks --agent all
"""

from __future__ import annotations

import argparse
import sys
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
        "--agent", required=True,
        choices=["claude", "opencode", "codex", "all"],
        help="Which agent to install hooks for.",
    )
    p_install.add_argument(
        "--target", default=None,
        help="Target repo root (default: cwd). Used by opencode for .opencode.json.",
    )

    # test-hooks ------------------------------------------------------------
    p_test = sub.add_parser(
        "test-hooks",
        help="Run synthetic-Edit test against installed hooks.",
    )
    p_test.add_argument(
        "--agent", required=True,
        choices=["claude", "opencode", "codex", "all"],
        help="Which agent to test hooks for.",
    )
    p_test.add_argument(
        "--target", default=None,
        help="Target repo root (default: cwd). Used by opencode.",
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for the ``merge_train`` console script."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "install-hooks":
        from merge_train.hook_install import install_hooks_for_agent
        from pathlib import Path
        target = Path(args.target) if args.target else None
        result = install_hooks_for_agent(args.agent, target=target)
        import json
        print(json.dumps(result, indent=2))
        if isinstance(result, list):
            return 0 if all(r.get("installed") for r in result) else 1
        return 0 if result.get("installed") else 1

    if args.cmd == "test-hooks":
        from merge_train.hook_install import test_hooks_for_agent
        from pathlib import Path
        target = Path(args.target) if args.target else None
        result = test_hooks_for_agent(args.agent, target=target)
        import json
        print(json.dumps(result, indent=2))
        if isinstance(result, list):
            return 0 if all(r.get("ok") for r in result) else 1
        return 0 if result.get("ok") else 1

    parser.error(f"unknown subcommand: {args.cmd}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
