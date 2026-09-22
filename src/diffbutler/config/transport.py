"""Remember successful Git transports without rewriting unrelated configuration."""

import fcntl
import os
import tempfile
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Literal, cast

import tomlkit
from tomlkit.exceptions import TOMLKitError

from diffbutler.config.settings import ConfigError


def save_git_transport(
    path: Path,
    repository: str,
    transport: Literal["https", "ssh"],
    *,
    expected: Literal["auto", "https", "ssh"],
) -> None:
    """Merge under a cross-process lock; preserve preferences edited during a fetch."""
    selected = path.expanduser().resolve()
    try:
        selected.parent.mkdir(parents=True, exist_ok=True)
        with selected.with_name(selected.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            original = selected.read_text(encoding="utf-8") if selected.exists() else None
            document = cast(MutableMapping[str, Any], tomlkit.parse(original or ""))
            if "repos" not in document:
                document["repos"] = tomlkit.table()
            repos = document["repos"]
            if not isinstance(repos, MutableMapping):
                raise ConfigError("Could not save Git transport: repos must be a table")
            repos = cast(MutableMapping[str, Any], repos)
            if repository not in repos:
                repos[repository] = tomlkit.table()
            entry = repos[repository]
            if not isinstance(entry, MutableMapping):
                raise ConfigError("Could not save Git transport: repository must be a table")
            entry = cast(MutableMapping[str, Any], entry)
            current = entry.get("git_transport", "auto")
            if current != expected or current == transport:
                return
            entry["git_transport"] = transport
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=selected.parent, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(tomlkit.dumps(document))
                if original is None:
                    os.link(temporary, selected)
                else:
                    if selected.read_text(encoding="utf-8") != original:
                        raise ConfigError("Configuration changed while saving Git transport")
                    temporary.chmod(selected.stat().st_mode & 0o777)
                    os.replace(temporary, selected)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    except OSError, UnicodeError, TOMLKitError:
        raise ConfigError("Could not save the working Git transport to configuration") from None
