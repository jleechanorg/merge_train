"""Live Node checks for the OpenCode plugin payload adapter."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "merge_train"
    / "hooks"
    / "opencode-conflict-plugin.js"
)


def test_opencode_apply_patch_forwards_patch_text_without_startup_noise() -> None:
    patch_text = "*** Begin Patch\n*** Update File: src/example.py\n*** End Patch"
    script = f"""
      import {{ MergeTrainConflictPlugin }} from {json.dumps(PLUGIN.as_uri())};
      let captured = null;
      const shell = (parts, ...values) => {{
        captured = values[0];
        return {{
          quiet() {{ return this; }},
          async nothrow() {{ return {{ stdout: Buffer.from(\"\") }}; }},
        }};
      }};
      const hooks = await MergeTrainConflictPlugin({{ $: shell }});
      await hooks[\"tool.execute.before\"](
        {{ tool: \"apply_patch\" }},
        {{ args: {{ patchText: {json.dumps(patch_text)} }} }},
      );
      console.log(captured);
    """

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )

    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1, f"unexpected plugin startup output: {result.stdout!r}"
    payload = json.loads(lines[0])
    assert payload == {
        "tool_name": "apply_patch",
        "tool_input": {"command": patch_text},
    }
