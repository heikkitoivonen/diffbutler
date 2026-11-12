"""Internal POSIX launch gate: do not touch the terminal until ownership is granted."""

import os
import sys


def main() -> None:
    gate = int(sys.argv[1])
    try:
        granted = os.read(gate, 1) == b"1"
    finally:
        os.close(gate)
    if not granted:
        raise SystemExit(125)
    try:
        os.chdir(sys.argv[2])
        os.execvp(sys.argv[3], sys.argv[3:])
    except OSError as error:
        print(f"Could not launch agent: {error}", file=sys.stderr)
        raise SystemExit(127) from error


if __name__ == "__main__":
    main()
