"""Persist first-run choices while preserving unrelated TOML and comments."""

import os
import tempfile
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, cast

import tomlkit
from tomlkit.exceptions import TOMLKitError

from diffbutler.config.settings import ConfigError, Forge, Reviewer, config_path


def save_setup(
    path: Path | None,
    forge: Forge | None,
    repository_path: Path | None,
    reviewer: Reviewer | None = None,
    default_reviewer: str | None = None,
) -> Path:
    selected = (path or config_path()).expanduser().resolve()
    try:
        original = selected.read_text(encoding="utf-8") if selected.exists() else None
        document = cast(MutableMapping[str, Any], tomlkit.parse(original or ""))
        if forge is not None:
            if document.get("forges"):
                raise ConfigError("Forge configuration changed during setup; restart DiffButler")
            document["forges"] = [forge.model_dump(exclude_none=True)]
        if reviewer is not None:
            entries = document.get("reviewers", [])
            if any(entry["id"] == reviewer.id for entry in entries):
                raise ConfigError("Reviewer configuration changed during setup; restart DiffButler")
            entries.append(reviewer.model_dump(exclude_none=True))
            document["reviewers"] = entries
        if default_reviewer is not None:
            if "review" not in document:
                document["review"] = tomlkit.table()
            document["review"]["default_reviewer"] = default_reviewer
        if repository_path is not None and forge is not None:
            repos = document.get("repos")
            if repos is None:
                repos = tomlkit.table()
                document["repos"] = repos
            if not isinstance(repos, MutableMapping):
                raise ConfigError("Use a [repos] table to save local repository paths")
            repos = cast(MutableMapping[str, Any], repos)
            repository = forge.repositories[0]
            if repository not in repos:
                repos[repository] = {"path": str(repository_path)}
            else:
                entry = cast(MutableMapping[str, Any], repos[repository])
                if "path" not in entry:
                    entry["path"] = str(repository_path)
        selected.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=selected.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(tomlkit.dumps(document))
            if original is not None:
                if selected.read_text(encoding="utf-8") != original:
                    raise ConfigError("Configuration changed during setup; restart DiffButler")
                temporary.chmod(selected.stat().st_mode & 0o777)
                os.replace(temporary, selected)
            else:
                # Atomic publication without overwriting a concurrently created file.
                os.link(temporary, selected)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except OSError, UnicodeError, TOMLKitError:
        raise ConfigError(f"Could not save startup configuration: {selected}") from None
    return selected
