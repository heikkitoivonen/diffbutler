"""Synthetic native CLI contract: no provider requests or real authentication."""

import json
import os
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("codex-cli 0.155.1")
    raise SystemExit

root = Path(os.environ["CODEX_HOME"])
saved = root / "conversation.json"
identity = "01234567-89ab-cdef-0123-456789abcdef"
if "exec" not in sys.argv:
    assert "resume" in sys.argv and identity in sys.argv
    assert "danger-full-access" in sys.argv and "on-request" in sys.argv
    assert "read-only" not in sys.argv
    assert json.loads(saved.read_text())["marker"] == "remembered orientation"
    print("remembered orientation")
    raise SystemExit

assert "read-only" in sys.argv and "never" in sys.argv
assert "GH_TOKEN" not in os.environ and "GITHUB_TOKEN" not in os.environ
payload = sys.stdin.read()
assert "source-only review of the pinned base/head revisions." in payload
assert Path("head/changed.py").read_text() == "after\n"
assert Path("base/changed.py").read_text() == "before\n"
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
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(answer)}}
    )
)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
