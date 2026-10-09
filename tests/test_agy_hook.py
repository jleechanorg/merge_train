"""Regression tests for the Antigravity (agy) PreToolUse hook contract.

agy sends ``{"toolCall": {"name", "args"}, "workspacePaths": [...]}``, runs the
hook with cwd set to the directory holding ``hooks.json``, and denies the tool
unless stdout carries a ``decision``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1] / "merge_train" / "hooks"
WRAPPER = HOOKS_DIR / "conflict-warn-pre-tool.sh"


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "conflict_check_helper", HOOKS_DIR / "conflict_check_helper.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return path


def _agy_payload(target: Path, workspace: Path) -> str:
    return json.dumps(
        {
            "toolCall": {
                "name": "write_to_file",
                "args": {"CodeContent": "hi", "TargetFile": str(target)},
            },
            "stepIdx": 3,
            "conversationId": "c",
            "workspacePaths": [str(workspace)],
        }
    )


def test_regression_agy_write_to_file_emits_decision_and_resolves_repo(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "agyrepo")
    hooks_cwd = tmp_path / "gemini_config"
    hooks_cwd.mkdir()
    log_root = tmp_path / "logs"

    proc = subprocess.run(
        ["bash", str(WRAPPER), "--runtime", "agy"],
        input=_agy_payload(repo / "hello.txt", repo),
        cwd=hooks_cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "MERGE_TRAIN_LOG_ROOT": str(log_root)},
    )

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "ask", out
    log = (log_root / "agyrepo" / "main").glob("hook-*.log")
    text = "".join(p.read_text() for p in log)
    assert "tool_name=write_to_file file_path=hello.txt" in text


def test_regression_agy_deny_payload_shape() -> None:
    helper = _load_helper()
    assert helper._decision_payload("deny", "conflict", "agy") == {
        "decision": "deny",
        "reason": "conflict",
    }
    assert helper._decision_payload("warn", "heads up", "agy") == {
        "decision": "ask",
        "reason": "heads up",
    }


def test_regression_agy_import_failure_still_emits_decision(
    monkeypatch, capsys
) -> None:
    import io
    import sys

    monkeypatch.setitem(sys.modules, "merge_train.symbol_discovery", None)
    helper = _load_helper()
    monkeypatch.setattr(sys, "stdin", io.StringIO(_agy_payload(Path("/tmp/x.py"), Path("/tmp"))))
    helper.main("agy")
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "ask", out
