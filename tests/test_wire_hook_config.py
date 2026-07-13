"""Regression coverage for the install.sh hook wiring helper."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


WIRE_HELPER = (
    Path(__file__).resolve().parents[1]
    / "merge_train"
    / "hooks"
    / "wire_hook_config.py"
)


def _run_wire(config: Path, *extra: str, style: str = "claude") -> dict:
    config.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            str(WIRE_HELPER),
            "--config",
            str(config),
            "--event",
            "PreToolUse",
            "--command",
            "bash /opt/merge_train/conflict-warn-pre-tool.sh",
            "--style",
            style,
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(config.read_text())


def test_codex_wiring_is_edit_only_and_has_no_routine_status(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "hooks.json"
    data = _run_wire(config, "--matcher", "^apply_patch$")

    group = data["hooks"]["PreToolUse"][0]
    assert group["matcher"] == "^apply_patch$"
    assert "statusMessage" not in group["hooks"][0]
    assert group["hooks"][0]["timeout"] == 15


def test_remove_only_preserves_unrelated_sibling_hook(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "hooks.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash /old/conflict-warn-pre-tool.sh",
                                },
                                {
                                    "type": "command",
                                    "command": "python3 /opt/policy.py",
                                },
                            ],
                        }
                    ]
                }
            }
        )
    )

    data = _run_wire(config, "--remove-only")

    assert data["hooks"]["PreToolUse"] == [
        {
            "matcher": "*",
            "hooks": [
                {"type": "command", "command": "python3 /opt/policy.py"}
            ],
        }
    ]


def test_cursor_wiring_can_filter_edit_tools(tmp_path: Path) -> None:
    config = tmp_path / ".cursor" / "hooks.json"
    data = _run_wire(
        config,
        "--matcher",
        "Edit|Write|StrReplace|Delete|EditNotebook",
        style="cursor",
    )

    assert data["version"] == 1
    assert data["hooks"]["PreToolUse"] == [
        {
            "command": "bash /opt/merge_train/conflict-warn-pre-tool.sh",
            "matcher": "Edit|Write|StrReplace|Delete|EditNotebook",
        }
    ]


def test_remove_only_does_not_create_a_missing_config(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "hooks.json"
    config.parent.mkdir(parents=True)

    subprocess.run(
        [
            sys.executable,
            str(WIRE_HELPER),
            "--config",
            str(config),
            "--event",
            "PreToolUse",
            "--command",
            "bash /opt/merge_train/conflict-warn-pre-tool.sh",
            "--style",
            "claude",
            "--remove-only",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert not config.exists()


def test_cursor_remove_only_cleans_legacy_subagent_banner(tmp_path: Path) -> None:
    config = tmp_path / ".cursor" / "hooks.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "subagentStart": [
                        {
                            "command": "bash -c 'echo merge_train: cursor subagent starting'"
                        }
                    ]
                },
            }
        )
    )

    subprocess.run(
        [
            sys.executable,
            str(WIRE_HELPER),
            "--config",
            str(config),
            "--event",
            "subagentStart",
            "--command",
            "unused",
            "--style",
            "cursor",
            "--remove-only",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    data = json.loads(config.read_text())
    assert data["hooks"]["subagentStart"] == []
