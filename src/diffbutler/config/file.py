"""Read the user's configuration verbatim, creating defaults only when absent."""

import fcntl
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from diffbutler.config.settings import ConfigError, config_path


def show_config_file(path: Path | None = None) -> tuple[Path, str]:
    selected = (path or config_path()).expanduser().absolute()
    try:
        try:
            content = selected.read_text(encoding="utf-8")
        except FileNotFoundError:
            defaults = (
                files("diffbutler.config").joinpath("default.toml").read_text(encoding="utf-8")
            )
            selected.parent.mkdir(parents=True, exist_ok=True)
            try:
                # Exclusive creation preserves files created since the initial read.
                with selected.open("x", encoding="utf-8") as stream:
                    stream.write(defaults)
            except FileExistsError:
                pass
            content = selected.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        raise ConfigError(
            f"config: could not read or create configuration file: {selected}"
        ) from None
    return selected, content


@contextmanager
def config_lock(selected: Path) -> Generator[None]:
    """Serialize read-modify-write updates of one resolved config file across processes."""
    with selected.with_name(selected.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def publish_config(selected: Path, original: str | None, content: str) -> bool:
    """Atomically replace a file still holding original, keeping its permissions.

    Returns False if the file changed since original was read. With original None,
    the file is created without overwriting one created concurrently; that case
    raises FileExistsError.
    """
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=selected.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        if original is None:
            os.link(temporary, selected)
        else:
            if selected.read_text(encoding="utf-8") != original:
                return False
            temporary.chmod(selected.stat().st_mode & 0o777)
            os.replace(temporary, selected)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True
