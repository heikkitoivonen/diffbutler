"""Synthetic native CLI contract: no provider requests or real authentication."""

import json
import os
import subprocess
import sys
from pathlib import Path

assert "--version" not in sys.argv, "Native launches must not probe CLI versions"
assert sys.argv[sys.argv.index("--name") + 1] == "diffbutler-pr-1"

root = Path(os.environ["CLAUDE_CONFIG_DIR"])
root.mkdir(parents=True, exist_ok=True)
saved = root / "conversation.json"
identity = "01234567-89ab-cdef-0123-456789abcdef"
if "--print" not in sys.argv:
    assert "--resume" in sys.argv and identity in sys.argv
    assert sys.argv[sys.argv.index("--permission-mode") + 1] == "auto"
    assert "--safe-mode" not in sys.argv
    assert "--permission-prompts" not in sys.argv
    assert "--restricted" not in sys.argv and "--strict-mcp-config" not in sys.argv
    assert sys.argv[sys.argv.index("--tools") + 1] == "default"
    assert json.loads(saved.read_text())["marker"] == "remembered orientation"
    print("remembered orientation")
    raise SystemExit

assert "-C" not in sys.argv
assert not {"--restricted", "--safe-mode", "--strict-mcp-config", "--mcp-config"}.intersection(
    sys.argv
)
assert sys.argv[sys.argv.index("--tools") + 1] == "Read,Glob,Grep,Bash"
assert sys.argv[sys.argv.index("--allowedTools") + 1] == "Read,Glob,Grep"
assert sys.argv[sys.argv.index("--permission-mode") + 1] == "auto"
assert sys.argv[sys.argv.index("--permission-prompts") + 1] == "none"
payload = sys.stdin.read()
assert "source-only review of PR" in payload
assert "readable Markdown, not JSON" in payload
assert "output_schema" not in payload
assert Path("changed.py").read_text() == ("before\n" if "--image-only" in sys.argv else "after\n")
assert subprocess.check_output(["git", "show", "diffbutler-base:changed.py"]) == b"before\n"
if "--resume" in sys.argv:
    assert identity in sys.argv and saved.exists()
saved.write_text(json.dumps({"id": identity, "marker": "remembered orientation"}))
print(json.dumps({"type": "system", "subtype": "init", "session_id": identity}), flush=True)
# Tool results cannot replace the final assistant answer.
print(
    json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Read"}]},
        }
    )
)
answer = (
    "## Change behavior\n\nOne source file changes.\n\n"
    "### Preserve the existing response\n\nLocation: `changed.py:1` (head).\n\n"
    "Draft comment:\nChanging `before` to `after` breaks callers that expect the existing "
    "response. Please preserve the response or update its callers together.\n\n"
    "Limitations: Project checks were not executed.\n"
)
print(
    json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": identity,
            "result": answer,
        }
    )
)
