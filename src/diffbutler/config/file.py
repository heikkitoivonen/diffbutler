"""Read the user's configuration verbatim, creating defaults only when absent."""

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
