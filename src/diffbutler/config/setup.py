"""Persist first-run choices while preserving unrelated TOML and comments."""

import os
import tempfile
from collections.abc import MutableMapping, Sequence
from pathlib import Path
from typing import Any, cast

import tomlkit
from tomlkit.exceptions import TOMLKitError

from diffbutler.config.settings import NATIVE_PRESETS, ConfigError, Forge, Reviewer, config_path


def save_setup(
    path: Path | None,
    forge: Forge | None,
    local_repository: tuple[str, Path] | None,
    reviewers: Sequence[Reviewer] = (),
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
        if reviewers:
            entries = document.get("reviewers", [])
            for reviewer in reviewers:
                if any(entry["id"] == reviewer.id for entry in entries):
                    raise ConfigError(
                        "Reviewer configuration changed during setup; restart DiffButler"
                    )
                entries.append(reviewer.model_dump(exclude_none=True))
            document["reviewers"] = entries
        if default_reviewer is not None:
            if "review" not in document:
                document["review"] = tomlkit.table()
            document["review"]["default_reviewer"] = default_reviewer
        if local_repository is not None and forge is not None:
            repository, repository_path = local_repository
            repos = document.get("repos")
            if repos is None:
                repos = tomlkit.table()
                document["repos"] = repos
            if not isinstance(repos, MutableMapping):
                raise ConfigError("Use a [repos] table to save local repository paths")
            repos = cast(MutableMapping[str, Any], repos)
            if repository not in repos:
                repos[repository] = {"path": str(repository_path)}
            else:
                entry = cast(MutableMapping[str, Any], repos[repository])
                if "path" not in entry:
                    entry["path"] = str(repository_path)
        content = tomlkit.dumps(document)
        if original is None:
            for preset in NATIVE_PRESETS:
                if any(entry.get("preset") == preset for entry in document.get("reviewers", [])):
                    continue
                example = tomlkit.dumps(
                    {"reviewers": [{"id": preset, "preset": preset, "command": [preset]}]}
                )
                content += (
                    f"\n# {preset} was not found in PATH; install it and uncomment to enable.\n"
                    + "\n".join(f"# {line}" for line in example.splitlines())
                    + "\n"
                )
        selected.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=selected.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
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
