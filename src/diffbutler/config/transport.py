"""Remember successful Git transports without rewriting unrelated configuration."""

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Literal, cast

import tomlkit
from tomlkit.exceptions import TOMLKitError

from diffbutler.config.file import config_lock, publish_config
from diffbutler.config.settings import ConfigError, repository_keys


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
            keys = repository_keys(repos, repository) or [repository.lower()]
            entries: list[MutableMapping[str, Any]] = []
            for key in keys:
                entry = repos.setdefault(key, tomlkit.table())
                if not isinstance(entry, MutableMapping):
                    raise ConfigError("Could not save Git transport: repository must be a table")
                entries.append(cast(MutableMapping[str, Any], entry))
            # Case variants of one repository must stay consistent; the lowercase key
            # wins when they disagree, matching how settings merge them.
            explicit = [
                (key, entry) for key, entry in zip(keys, entries, strict=True)
                if "git_transport" in entry
            ]  # fmt: skip
            current = next(
                (entry["git_transport"] for key, entry in explicit if key == repository.lower()),
                explicit[0][1]["git_transport"] if explicit else "auto",
            )
            if current != expected or current == transport:
                return
            for entry in [entry for _, entry in explicit] or entries[:1]:
                entry["git_transport"] = transport
            if not publish_config(selected, original, tomlkit.dumps(document)):
                raise ConfigError("Configuration changed while saving Git transport")
    except OSError, UnicodeError, TOMLKitError:
        raise ConfigError("Could not save the working Git transport to configuration") from None
