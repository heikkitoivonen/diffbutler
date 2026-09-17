"""Synthetic session protocol for tests only; never imports or invokes an agent."""

import json
import sys
from pathlib import Path
from uuid import uuid4

state = Path("conversation.json")
if sys.argv[1:] == ["start"]:
    conversation = {"session_id": str(uuid4()), "turn": 1}
    with state.open("x") as output:
        json.dump(conversation, output)
elif len(sys.argv) == 3 and sys.argv[1] == "resume":
    conversation = json.loads(state.read_text())
    if conversation["session_id"] != sys.argv[2]:
        sys.exit(23)
    conversation["turn"] += 1
    state.write_text(json.dumps(conversation))
else:
    sys.exit(24)
print(json.dumps(conversation))
