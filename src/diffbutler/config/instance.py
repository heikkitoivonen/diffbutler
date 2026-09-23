"""One CLI/TUI process per standard data home, using a kernel-held advisory lock."""

import fcntl
import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from diffbutler.config.settings import ConfigError, data_path


def instance_lock_path() -> Path:
    # Outside the reset target: unlinking a held flock file permits a second inode/owner.
    return data_path().parent / ".diffbutler-instance.lock"


@contextmanager
def single_instance() -> Generator[None]:
    """Keep a stable inode; a leftover PID is diagnostic only, never ownership proof."""
    path = instance_lock_path()
    fd: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ConfigError("Invalid DiffButler instance lock; check its ownership and file type")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pid = os.pread(fd, 32, 0).strip()
            owner = f" (PID {int(pid)})" if pid and pid.isdigit() else ""
            raise ConfigError(
                f"DiffButler is already running{owner}; close it before starting another command "
                "or resetting. Do not delete the instance lock file."
            ) from None
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except OSError:
        if fd is not None:
            os.close(fd)
        raise ConfigError(
            "Could not acquire the DiffButler instance lock; check permissions"
        ) from None
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    try:
        yield
    finally:
        # Closing releases the kernel lock. Never unlink, including after a clean exit.
        os.close(fd)
