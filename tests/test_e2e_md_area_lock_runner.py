from __future__ import annotations

import hashlib
import importlib.util
import multiprocessing as mp
import subprocess
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "e2e_md_area_lock_runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("e2e_md_area_lock_runner", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_run_raises_when_checked_command_fails():
    runner = _load_runner()

    try:
        runner._run(["bash", "-lc", "printf fail >&2; exit 7"])
    except subprocess.CalledProcessError as exc:
        assert exc.returncode == 7
        assert exc.stderr == "fail"
    else:
        raise AssertionError("_run(check=True) returned instead of raising")


def _hash_file_in_process(path: str, queue: mp.Queue) -> None:
    runner = _load_runner()
    queue.put(runner._sha256_file(Path(path)))


def test_sha256_file_streams_to_eof(tmp_path: Path):
    payload = b"abc123" * 2000
    target = tmp_path / "payload.bin"
    target.write_bytes(payload)

    queue: mp.Queue = mp.Queue()
    proc = mp.Process(target=_hash_file_in_process, args=(str(target), queue))
    proc.start()
    proc.join(timeout=2)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=1)
        raise AssertionError("_sha256_file did not reach EOF")

    assert proc.exitcode == 0
    assert queue.get(timeout=1) == hashlib.sha256(payload).hexdigest()


def test_generated_plan_uses_markdown_heading_symbol_shape():
    runner = _load_runner()

    assert "md:shared_plan.slot_01" in runner._generate_plan_yaml(1)
    assert "md:e2e_shared_plan.slot_01" not in runner._generate_plan_yaml(1)
