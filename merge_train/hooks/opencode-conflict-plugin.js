// merge_train: OpenCode conflict-warning plugin.
//
// OpenCode has no native `hooks.json`; coordination hooks are JS plugins that
// live in ~/.config/opencode/plugins/ and are auto-loaded. This plugin mirrors
// the per-edit conflict check the other CLI runtimes (Claude / Cursor / Gemini /
// Codex) get via `conflict-warn-pre-tool.sh`, so OpenCode fanout agents are
// covered too.
//
// On every `edit` / `write` / `patch` tool call it builds a Claude-shaped hook
// payload ({tool_name, tool_input.file_path}) and pipes it to the SAME wrapper
// the other runtimes use. The wrapper logs to /tmp/merge_train/<repo>/<branch>/
// (the runtime-agnostic proof record) and returns a decision envelope. This
// plugin is WARN-ONLY: it surfaces the reason but never throws / blocks, matching
// merge_train's design for every repo except merge_train itself.
//
// Source of truth: merge_train/hooks/opencode-conflict-plugin.js (installed to
// ~/.config/opencode/plugins/merge-train-conflict.js by install.sh).

const WRAP = `${process.env.HOME}/.local/bin/conflict-warn-pre-tool.sh`;

// OpenCode edit-family tools whose args carry a target file path.
const EDIT_TOOLS = new Set(["edit", "write", "multiedit", "patch", "apply_patch"]);

export const MergeTrainConflictPlugin = async ({ $ }) => {
  console.log("[merge_train] OpenCode conflict plugin loaded (warn-only)");

  return {
    "tool.execute.before": async (input, output) => {
      try {
        const tool = (input?.tool || "").toLowerCase();
        if (!EDIT_TOOLS.has(tool)) return;

        const args = output?.args || {};
        const filePath =
          args.filePath || args.file_path || args.path || args.TargetFile || "";
        if (!filePath) return;

        // Build a Claude-shaped payload so the shared wrapper/helper recognize it.
        const payload = JSON.stringify({
          tool_name: tool,
          tool_input: { file_path: filePath },
        });

        // Pipe to the shared wrapper. Bun's `$` safely escapes ${payload}.
        // .quiet() suppresses passthrough; .nothrow() so a non-zero exit
        // (the wrapper never denies in warn-only repos, but be defensive)
        // can't crash the plugin.
        const res = await $`printf %s ${payload} | bash ${WRAP}`
          .quiet()
          .nothrow();
        const stdout = res?.stdout?.toString?.() ?? "";

        let reason = "";
        try {
          const decision = JSON.parse(stdout);
          reason =
            decision?.hookSpecificOutput?.permissionDecisionReason ||
            decision?.systemMessage ||
            "";
        } catch {
          /* non-JSON output — ignore, warn-only */
        }

        // Surface only real conflicts to keep the log readable; the wrapper
        // already records every check to the /tmp log regardless.
        if (reason && /conflict/i.test(reason)) {
          console.warn(`[merge_train] ${reason}`);
        }
      } catch (err) {
        // Warn-only: never let the conflict check break the edit.
        console.warn(`[merge_train] conflict check skipped: ${err?.message || err}`);
      }
    },
  };
};
