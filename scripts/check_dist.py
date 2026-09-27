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
            [str(command)],
            [str(python), "-I", "-m", "diffbutler"],
        )
        for args in entry_points:
            for flag in ("--version", "-v", "--help", "-h"):
                result = subprocess.run(
                    [*args, flag], cwd=directory, check=True, capture_output=True, text=True
                )
                if flag in {"--version", "-v"}:
                    if result.stdout.strip() != f"diffbutler {version}":
                        raise SystemExit(f"Unexpected installed CLI output: {result.stdout!r}")
                elif "--help" not in result.stdout:
                    raise SystemExit(f"Unexpected installed CLI help: {result.stdout!r}")
        doctor_env = {
            key: value for key, value in os.environ.items() if not key.startswith("DIFFBUTLER_")
        }
        doctor_env["XDG_CONFIG_HOME"] = str(Path(directory) / "config")
        doctor_env["XDG_DATA_HOME"] = str(Path(directory) / "data")
        config_path = Path(directory) / "config" / "diffbutler" / "config.toml"
        for args in entry_points:
            result = subprocess.run(
                [*args, "config"],
                cwd=directory,
                env=doctor_env,
                check=True,
                capture_output=True,
                text=True,
            )
            if result.stdout != config_path.read_text() + f"\nConfig file: {config_path}\n":
                raise SystemExit("Installed config command did not display its file and path")
        # Exercise packaged migration discovery, then reopen through the module entry point.
        for args in (
            [str(command), "doctor"],
            [str(python), "-I", "-m", "diffbutler", "doctor", "--no-migrate", "--offline"],
        ):
            result = subprocess.run(
                args, cwd=directory, env=doctor_env, check=True, capture_output=True, text=True
            )
            if "Schema 0010 current" not in result.stdout or "FAIL" in result.stdout:
                raise SystemExit(f"Unexpected installed doctor output: {result.stdout!r}")
        result = subprocess.run(
            [str(command), "ls", "--json"],
            cwd=directory,
            env=doctor_env,
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() != "[]":
            raise SystemExit("Installed offline queue should be empty")
        history = subprocess.run(
            [str(command), "history", "--json"],
            cwd=directory,
            env=doctor_env,
            check=True,
            capture_output=True,
            text=True,
        )
        if history.stdout.strip() != "[]":
            raise SystemExit("Installed offline history should be empty")
        for name in (
            "config",
            "reset",
            "sync",
            "ls",
            "follow",
            "review",
            "findings",
            "session",
            "approved",
            "cleanup",
            "dashboard",
            "history",
            "open",
            "waiting",
        ):
            for flag in ("--help", "-h"):
                subprocess.run(
                    [str(command), name, flag], cwd=directory, check=True, capture_output=True
                )
        for args in entry_points:
            subprocess.run(
                [*args, "reset", "--force"],
                cwd=directory,
                env=doctor_env,
                check=True,
                capture_output=True,
            )
        if config_path.parent.exists() or (Path(directory) / "data" / "diffbutler").exists():
            raise SystemExit("Installed reset command did not remove configuration and data")
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "from diffbutler.reviewers.native import native_preset; "
                "assert all(native_preset(name).background for name in ('codex', 'claude')); "
                "from diffbutler.ui.tui.dashboard import Dashboard",
            ],
            cwd=directory,
            check=True,
        )
        subprocess.run(
            [str(python), "-I", "-m", "diffbutler.ui.tui.handoff_probe", "--help"],
            cwd=directory,
            check=True,
            capture_output=True,
        )
    print("Wheel installation and both entry points passed outside the source tree.")


if __name__ == "__main__":
    main()
