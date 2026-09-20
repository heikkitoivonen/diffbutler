"""Synthetic native CLI contract: no provider requests or real authentication."""

import json
import os
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("2.1.278 (Claude Code)")
    raise SystemExit

root = Path(os.environ["CLAUDE_CONFIG_DIR"])
saved = root / "conversation.json"
identity = "01234567-89ab-cdef-0123-456789abcdef"
if "--print" not in sys.argv:
    assert "--resume" in sys.argv and identity in sys.argv
    assert "manual" in sys.argv and "--safe-mode" not in sys.argv
    assert "--restricted" not in sys.argv and "--strict-mcp-config" not in sys.argv
    assert sys.argv[sys.argv.index("--tools") + 1] == "default"
    assert json.loads(saved.read_text())["marker"] == "remembered orientation"
    print("remembered orientation")
    raise SystemExit

assert "-C" not in sys.argv
assert "--restricted" in sys.argv and "--safe-mode" in sys.argv
assert "--strict-mcp-config" in sys.argv
assert sys.argv[sys.argv.index("--tools") + 1] == "Read,Glob,Grep"
assert sys.argv[sys.argv.index("--permission-mode") + 1] == "dontAsk"
assert sys.argv[sys.argv.index("--permission-prompts") + 1] == "none"
assert "CODEX_API_KEY" not in os.environ
assert "GH_TOKEN" not in os.environ and "GITHUB_TOKEN" not in os.environ
payload = sys.stdin.read()
assert "source-only review of the pinned base/head revisions." in payload
assert Path("head/changed.py").read_text() == "after\n"
assert Path("base/changed.py").read_text() == "before\n"
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
answer: dict[str, object] = {
    "orientation": {
        "intent": "Change behavior",
        "shape": "One source file",
        "look_here": [],
        "low_risk": [],
        "unverified": ["Project checks were not executed."],
    },
    "findings": [],
}
print(
    json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": identity,
            "result": json.dumps(answer),
        }
    )
)
