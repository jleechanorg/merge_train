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


def test_regression_agy_new_directory_resolves_target_workspace(
    tmp_path: Path,
) -> None:
    first = _git_repo(tmp_path / "first_ws")
    target_repo = _git_repo(tmp_path / "target_ws")
    hooks_cwd = tmp_path / "gemini_config"
    hooks_cwd.mkdir()
    log_root = tmp_path / "logs"
    payload = json.loads(_agy_payload(target_repo / "new" / "dir" / "f.py", first))
    payload["workspacePaths"] = [str(first), str(target_repo)]

    proc = subprocess.run(
        ["bash", str(WRAPPER), "--runtime", "agy"],
        input=json.dumps(payload),
        cwd=hooks_cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "MERGE_TRAIN_LOG_ROOT": str(log_root)},
    )

    assert proc.returncode == 0, proc.stderr
    assert list((log_root / "target_ws").glob("*/hook-*.log"))
    assert not (log_root / "first_ws").exists()


def test_regression_agy_conflict_denies_through_main(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import io
    import sys
    import time

    repo = _git_repo(tmp_path / f"agydeny_{os.getpid()}")
    target = repo / "shared.txt"
    target.write_text("x\n")
    cache = Path(f"/tmp/merge_train_cache_{repo.name}.json")
    cache.write_text(
        json.dumps(
            {
                "timestamp": time.time(),
                "prs": {"99": {"branch": "other", "files": ["shared.txt"]}},
            }
        )
    )
    try:
        helper = _load_helper()
        monkeypatch.setattr(helper, "_resolve_enforcement", lambda root: ("block", "t"))
        monkeypatch.chdir(repo)
        monkeypatch.setattr(sys, "stdin", io.StringIO(_agy_payload(target, repo)))
        helper.main("agy")
    finally:
        cache.unlink(missing_ok=True)

    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "deny", out
    assert "PR#99" in out["reason"]


def _run_wrapper_with_helper(tmp_path: Path, helper_source: str, runtime: str):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "conflict-warn-pre-tool.sh").write_text(WRAPPER.read_text())
    (hooks / "conflict_check_helper.py").write_text(helper_source)
    repo = _git_repo(tmp_path / "repo")
    return subprocess.run(
        ["bash", str(hooks / "conflict-warn-pre-tool.sh"), "--runtime", runtime],
        input=_agy_payload(repo / "f.txt", repo),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "MERGE_TRAIN_LOG_ROOT": str(tmp_path / "logs")},
    )


def test_regression_agy_helper_crash_defers_to_agy(tmp_path: Path) -> None:
    proc = _run_wrapper_with_helper(tmp_path, "raise RuntimeError('boom')\n", "agy")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "ask"


def test_regression_agy_helper_silent_exit_defers_to_agy(tmp_path: Path) -> None:
    proc = _run_wrapper_with_helper(tmp_path, "import sys\nsys.exit(1)\n", "agy")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "ask"


def test_helper_crash_keeps_claude_behavior(tmp_path: Path) -> None:
    proc = _run_wrapper_with_helper(tmp_path, "raise RuntimeError('boom')\n", "claude")
    assert proc.returncode != 0
    assert proc.stdout == ""


def test_regression_agy_partial_output_then_crash_emits_one_decision(
    tmp_path: Path,
) -> None:
    helper = "print('{\"decision\": \"al')\nraise RuntimeError('boom')\n"
    proc = _run_wrapper_with_helper(tmp_path, helper, "agy")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "decision": "ask",
        "reason": "merge_train: hook error; deferring to agy",
    }
