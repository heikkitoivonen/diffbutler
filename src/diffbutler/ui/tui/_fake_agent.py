"""Offline interactive fixture for the handoff experiment, not a model backend."""

import argparse
import json
import os
import sys
import time
import tty
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path)
    parser.add_argument("--ticker", action="store_true")
    args = parser.parse_args()
    if args.ticker:
        while True:
            print("BACKGROUND OUTPUT MUST NOT REACH TERMINAL", flush=True)
            time.sleep(0.1)
    session = Path(str(args.session))
    data: dict[str, str | int] = json.loads(session.read_text())
    data["resumes"] = int(data["resumes"]) + 1
    session.write_text(json.dumps(data))
    print(f"FAKE_AGENT_READY session={data['id']} resumes={data['resumes']}", flush=True)
    print("Commands: exit, crash, raw-crash. Ctrl+C returns to the dashboard.", flush=True)
    try:
        while True:
            command = input("agent> ")
            if command == "exit":
                return
            if command == "crash":
                raise SystemExit(23)
            if command == "raw-crash":
                tty.setraw(sys.stdin.fileno())
                os._exit(24)
            print(f"remembered={data['seed']}; received={command}", flush=True)
    except KeyboardInterrupt, EOFError:
        print("\nFAKE_AGENT_INTERRUPTED", flush=True)


if __name__ == "__main__":
    main()
