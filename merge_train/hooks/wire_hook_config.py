#!/usr/bin/env python3
"""Idempotently wire the merge_train conflict-warn hook into a CLI runtime config.

Every fanout runtime stores per-tool hooks under ``<root>.hooks.<EVENT>`` but the
event name and the per-entry shape differ:

    claude / codex / gemini : <EVENT> = [ {matcher, hooks:[{type,command,...}]} ]
    cursor                  : <EVENT> = [ {command} ]

This helper upserts the canonical conflict-warn entry for one (config, event,
style) triple. It is genuinely idempotent AND upgrading: it first strips any prior
merge_train-owned entry for that event — conflict-warn, the legacy
``predict-spawn-check`` mis-wiring, or a leftover ``mt_capture`` probe — then
inserts the current canonical entry. The original install.sh only ``grep``-skipped
when *any* matching string was present, so it never upgraded stale wiring; this
helper fixes that (see the repo's install.sh idempotency bug note).

Top-level keys outside ``hooks`` (e.g. gemini's mcpServers/tools/security) are
preserved untouched.

Usage:
    wire_hook_config.py --config PATH --event EVENT --command CMD \
        --style {claude|cursor} [--status MSG] [--timeout-sec N]

Exit 0 on success (prints a one-line status), 0 with a SKIP note if the parent
dir is missing (nothing to wire), non-zero only on a real write error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Substrings that mark an entry as merge_train-owned (any generation), so a
# re-run replaces it instead of stacking duplicates.
_OWNED_MARKERS = ("conflict-warn-pre-tool", "predict-spawn-check", "mt_capture")


def _entry_is_owned_claude(entry: dict) -> bool:
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if any(m in cmd for m in _OWNED_MARKERS):
            return True
    return False


def _entry_is_owned_cursor(entry: dict) -> bool:
    cmd = entry.get("command", "")
    return any(m in cmd for m in _OWNED_MARKERS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--event", required=True)
    ap.add_argument("--command", required=True)
    ap.add_argument("--style", required=True, choices=["claude", "cursor"])
    ap.add_argument("--status", default="merge_train: checking conflicts...")
    ap.add_argument("--timeout-sec", type=int, default=15)
    args = ap.parse_args()

    path = Path(args.config).expanduser()

    # Only wire into an EXISTING config (don't create configs for tools the user
    # doesn't have). The parent dir existing is our signal the runtime is set up.
    if not path.parent.exists():
        print(f"  SKIP: {path.parent} not present; {args.event} not wired.")
        return 0

    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text()) or {}
        except Exception as e:  # noqa: BLE001
            print(f"  WARN: {path} is not valid JSON ({e}); leaving untouched.")
            return 0

    hooks = data.setdefault("hooks", {})
    event_list = hooks.setdefault(args.event, [])

    if args.style == "claude":
        event_list[:] = [e for e in event_list if not _entry_is_owned_claude(e)]
        event_list.append({
            "matcher": "*",
            "hooks": [{
                "type": "command",
                "command": args.command,
                "timeoutSec": args.timeout_sec,
                "statusMessage": args.status,
            }],
        })
    else:  # cursor
        # Cursor SILENTLY IGNORES a hooks.json that lacks a top-level "version"
        # field — the hooks simply never fire (proven empirically: identical
        # config with vs without "version":1 → fires Δ=12 vs Δ=0). The global
        # ~/.cursor/hooks.json ships with it; configs we author must set it too.
        data.setdefault("version", 1)
        event_list[:] = [e for e in event_list if not _entry_is_owned_cursor(e)]
        event_list.append({"command": args.command})

    try:
        path.write_text(json.dumps(data, indent=2) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: failed to write {path}: {e}")
        return 1

    print(f"  ok: wired {args.event} -> conflict-warn in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
