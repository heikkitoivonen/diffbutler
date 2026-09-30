"""Run isolated tests in parallel, then timing-sensitive integration tests alone."""

import argparse
import subprocess
import sys


def run(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("-m", "--markexpr", default="")
    options, forwarded = parser.parse_known_args(arguments)
    codes: list[int] = []
    for serial in (False, True):
        marker = "serial" if serial else "not serial"
        if options.markexpr:
            marker = f"({options.markexpr}) and ({marker})"
        command = [sys.executable, "-m", "pytest", "-n", "auto", *forwarded, "-m", marker]
        if serial:
            # Override any caller-provided worker count for the serial track.
            command += ["-n", "0"]
        if options.coverage:
            command += ["--cov", "--cov-report="]
            if serial:
                command += ["--cov-append"]
        print(f"Running {'serial' if serial else 'parallel'} test track", flush=True)
        code = subprocess.run(command, check=False).returncode
        codes.append(code)
        if code not in {0, 1, 5}:
            return code
    if options.coverage and any(code != 5 for code in codes):
        for report in ("report", "html", "xml"):
            code = subprocess.run(
                [sys.executable, "-m", "coverage", report], check=False
            ).returncode
            if code:
                return code
    # A filtered selection can legitimately leave either track empty.
    return 1 if 1 in codes else 0 if 0 in codes else 5


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
