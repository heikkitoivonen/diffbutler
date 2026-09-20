"""Explicit removal of local application directories without loading configuration."""

import asyncio
import shutil
from pathlib import Path

from diffbutler.config.diagnostics import state_path
from diffbutler.config.settings import ConfigError, config_path, data_path


def reset_paths() -> tuple[Path, ...]:
    return tuple(
        dict.fromkeys(
            (config_path().parent.absolute(), data_path().absolute(), state_path().absolute())
        )
    )


def remove_directory(path: Path) -> None:
    """Remove the directory tree, unlinking rather than following symbolic links."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        raise ConfigError(f"reset: could not remove {path}; check permissions and retry") from None


async def reset_directories(paths: tuple[Path, ...]) -> None:
    for path in dict.fromkeys(paths):
        await asyncio.to_thread(remove_directory, path)
