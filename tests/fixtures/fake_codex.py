"""Synthetic native CLI contract: no provider requests or real authentication."""

import json
import os
import subprocess
import sys
from pathlib import Path

assert "--version" not in sys.argv, "Native launches must not probe CLI versions"
assert "--name" not in sys.argv, "Codex has no session naming launch option"

root = Path(os.environ["CODEX_HOME"])
root.mkdir(parents=True, exist_ok=True)
saved = root / "conversation.json"
identity = "01234567-89ab-cdef-0123-456789abcdef"
if "exec" not in sys.argv:
    assert "resume" in sys.argv and identity in sys.argv
    assert "on-request" in sys.argv
    assert "-s" not in sys.argv and "--sandbox" not in sys.argv
    assert "danger-full-access" not in sys.argv
    assert json.loads(saved.read_text())["marker"] == "remembered orientation"
    print("remembered orientation")
    raise SystemExit

assert "read-only" in sys.argv and "never" in sys.argv
payload = sys.stdin.read()
assert "source-only review of PR" in payload
assert "readable Markdown, not JSON" in payload
assert "output_schema" not in payload
assert Path("changed.py").read_text() == "after\n"
assert subprocess.check_output(["git", "show", "diffbutler-base:changed.py"]) == b"before\n"
if "resume" in sys.argv:
    assert identity in sys.argv and saved.exists()
saved.write_text(json.dumps({"id": identity, "marker": "remembered orientation"}))
print(json.dumps({"type": "thread.started", "thread_id": identity}), flush=True)
# Tool results cannot replace the final assistant answer.
print(
    json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "text": "untrusted tool output"},
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
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
