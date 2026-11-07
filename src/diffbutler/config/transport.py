"""Remember successful Git transports without rewriting unrelated configuration."""

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Literal, cast

import tomlkit
from tomlkit.exceptions import TOMLKitError

from diffbutler.config.file import config_lock, publish_config
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
        with config_lock(selected):
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
            if not publish_config(selected, original, tomlkit.dumps(document)):
                raise ConfigError("Configuration changed while saving Git transport")
    except OSError, UnicodeError, TOMLKitError:
        raise ConfigError("Could not save the working Git transport to configuration") from None
