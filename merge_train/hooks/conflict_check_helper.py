#!/usr/bin/env python3
import sys
import json
import subprocess
import time
from pathlib import Path

# Add merge_train repos to path so we can import packages
for p in [Path.home() / "merge_train", Path("/Users/jleechan/projects/merge_train")]:
    if p.exists():
        sys.path.insert(0, str(p))

try:
    from merge_train.symbol_discovery import symbols_from_files_in_pr
    from merge_train.symbols import extract_symbols, is_python_path
except ImportError:
    # Fail-safe fallback if import fails
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
    sys.exit(0)

def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            # Empty input, just allow
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
            return
        
        payload = json.loads(raw_input)
    except Exception:
        # If parsing fails, fail-safe allow
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        return

    # Check tool name
    tool_name = payload.get("name") or payload.get("tool_name") or payload.get("tool") or ""
    if tool_name not in ["Edit", "Write", "replace_file_content", "multi_replace_file_content"]:
        # Only check file modification/creation
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        return

    # Extract target file path
    tool_input = payload.get("input") or payload.get("tool_input") or {}
    file_path = (
        tool_input.get("file_path")
        or tool_input.get("TargetFile")
        or payload.get("file_path")
        or payload.get("TargetFile")
        or ""
    )
    if not file_path:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        return

    # Extract start and end lines
    start_line = tool_input.get("StartLine") or tool_input.get("startLine") or tool_input.get("StartLineNumber")
    end_line = tool_input.get("EndLine") or tool_input.get("endLine") or tool_input.get("EndLineNumber")

    # Get git repository root
    try:
        repo_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
    except subprocess.CalledProcessError:
        # Not inside a git repository, just allow
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        return

    repo_path = Path(repo_root)
    repo_name = repo_path.name

    # Determine enforcement mode
    enforcement = False
    if repo_name == "merge_train":
        enforcement = True
    elif repo_name in ["jleechanclaw", "agent-orchestrator", "worldarchitect.ai"]:
        enforcement = False
    else:
        # Default warn-only
        enforcement = False

    # Normalize file path relative to repo root
    try:
        abs_path = Path(file_path).resolve()
        if abs_path.is_relative_to(repo_path):
            rel_path = abs_path.relative_to(repo_path).as_posix()
        else:
            rel_path = file_path
    except Exception:
        rel_path = file_path

    # Get current branch
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, check=False
    ).stdout.strip()

    # Detect remote repo OWNER/REPO
    repo_remote = ""
    try:
        remote_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False
        ).stdout.strip()
        if remote_url:
            if remote_url.endswith(".git"):
                remote_url = remote_url[:-4]
            import re
            m = re.search(r"github\.com[:/]([^/]+/[^/]+)$", remote_url)
            if m:
                repo_remote = m.group(1)
    except Exception:
        pass

    # Read from cache if valid (less than 45 seconds old)
    cache_file = Path(f"/tmp/merge_train_cache_{repo_name}.json")
    prs_data = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
            if time.time() - cache.get("timestamp", 0) < 45:
                prs_data = cache.get("prs", {})
        except Exception:
            pass

    if not prs_data:
        # Query open PRs via GitHub CLI
        try:
            cmd = ["gh", "pr", "list", "--state", "open", "--json", "number,headRefName"]
            if repo_remote:
                cmd += ["--repo", repo_remote]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode == 0:
                prs = json.loads(proc.stdout)
                for pr in prs:
                    pr_num = pr.get("number")
                    pr_branch = pr.get("headRefName")
                    if pr_branch == current_branch:
                        continue
                    
                    diff_cmd = ["gh", "pr", "diff", str(pr_num), "--name-only"]
                    if repo_remote:
                        diff_cmd += ["--repo", repo_remote]
                    diff_proc = subprocess.run(diff_cmd, capture_output=True, text=True, check=False)
                    if diff_proc.returncode == 0:
                        files = [f.strip() for f in diff_proc.stdout.splitlines() if f.strip()]
                        prs_data[str(pr_num)] = {
                            "branch": pr_branch,
                            "files": files,
                            "symbols": {}
                        }
                # Write to cache
                cache_file.write_text(json.dumps({
                    "timestamp": time.time(),
                    "prs": prs_data
                }))
        except Exception:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
            return

    if not prs_data:
        # No other open PRs, allow
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        return

    # Identify symbols we are editing
    our_symbols = set()
    is_python = is_python_path(rel_path)
    whole_file_lock = True

    if is_python and start_line is not None and end_line is not None:
        try:
            start_l = int(start_line)
            end_l = int(end_line)
            file_on_disk = repo_path / rel_path
            if file_on_disk.exists():
                source = file_on_disk.read_text()
                symbols = extract_symbols(source)
                for sym in symbols:
                    if sym.overlaps(start_l, end_l):
                        our_symbols.add(sym.name)
                if our_symbols:
                    whole_file_lock = False
        except Exception:
            pass

    # Check conflicts against other open PRs
    conflicts = []
    for pr_num, pr_info in prs_data.items():
        pr_files = pr_info.get("files", [])
        if rel_path not in pr_files:
            continue
        
        pr_branch = pr_info.get("branch", f"pr-{pr_num}")
        
        if whole_file_lock:
            conflicts.append((pr_num, pr_branch, f"whole-file '{rel_path}'"))
            continue
            
        pr_symbols = pr_info.get("symbols", {}).get(rel_path)
        if pr_symbols is None:
            try:
                sym_map = symbols_from_files_in_pr(int(pr_num), [rel_path], repo_remote or None)
                pr_symbols = list(sym_map.get(rel_path, []))
                pr_info.setdefault("symbols", {})[rel_path] = pr_symbols
                cache_file.write_text(json.dumps({
                    "timestamp": time.time(),
                    "prs": prs_data
                }))
            except Exception:
                pr_symbols = []

        if not pr_symbols:
            conflicts.append((pr_num, pr_branch, f"whole-file '{rel_path}'"))
        else:
            overlap = our_symbols.intersection(pr_symbols)
            if overlap:
                conflicts.append((pr_num, pr_branch, f"symbols: {', '.join(overlap)}"))

    if conflicts:
        conflict_details = []
        for pr_num, branch, detail in conflicts:
            conflict_details.append(f"PR#{pr_num} (branch '{branch}') is also modifying {detail}")
        
        msg = f"merge_train: Conflict detected in '{rel_path}'!\n  " + "\n  ".join(conflict_details)
        
        if enforcement:
            # Block
            print(json.dumps({"decision": "block", "reason": msg}))
            return
        else:
            # Warn-only
            print(msg, file=sys.stderr)
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
            return

    # No conflicts
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))

if __name__ == "__main__":
    main()
