"""Emit a tiny synthetic review; no source reads, descendants, or model calls."""

import json
import sys

print("synthetic diagnostic", file=sys.stderr)
print(
    json.dumps(
        {
            "orientation": {
                "intent": "Validate synthetic addition.",
                "shape": "One expression.",
                "look_here": [{"path": "sum.py", "line": 2, "why": "Check the operator."}],
                "low_risk": [],
                "unverified": ["Source-only review; code was not executed."],
            },
            "findings": [
                {
                    "path": "sum.py",
                    "line": 2,
                    "side": "RIGHT",
                    "severity": "important",
                    "category": "correctness",
                    "title": "Addition subtracts",
                    "body": "The expression subtracts b from a.",
                    "confidence": 0.9,
                }
            ],
        }
    )
)
