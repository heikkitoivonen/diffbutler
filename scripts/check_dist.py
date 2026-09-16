"""Smoke-test the current wheel outside the source tree and development environment."""

import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    version = str(config["project"]["version"])
    wheel = root / "dist" / f"diffbutler-{version}-py3-none-any.whl"
    if not wheel.is_file():
        raise SystemExit("Build the wheel first with make build.")
    with tempfile.TemporaryDirectory(prefix="diffbutler-smoke-") as directory:
        env = Path(directory) / "venv"
        uv = os.environ.get("UV", "uv")
        subprocess.run([uv, "venv", "--python", sys.executable, str(env)], check=True)
        bin_dir = env / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        command = bin_dir / ("diffbutler.exe" if os.name == "nt" else "diffbutler")
        subprocess.run([uv, "pip", "install", "--python", str(python), str(wheel)], check=True)
        entry_points = (
            [str(command), "--version"],
            [str(python), "-I", "-m", "diffbutler", "--version"],
        )
        for args in entry_points:
            result = subprocess.run(args, cwd=directory, check=True, capture_output=True, text=True)
            if result.stdout.strip() != f"diffbutler {version}":
                raise SystemExit(f"Unexpected installed CLI output: {result.stdout!r}")
        subprocess.run([str(command), "--help"], cwd=directory, check=True)
        subprocess.run(
            [str(python), "-I", "-m", "diffbutler.ui.tui.handoff_probe", "--help"],
            cwd=directory,
            check=True,
            capture_output=True,
        )
    print("Wheel installation and both entry points passed outside the source tree.")


if __name__ == "__main__":
    main()
