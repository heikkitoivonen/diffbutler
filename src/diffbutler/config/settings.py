"""Validated local configuration; loading never starts reviews or contacts a forge."""

import os
import tomllib
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError
from pydantic_settings.sources import InitSettingsSource

from diffbutler.domain.forge import PRQuery


class ConfigError(ValueError):
    """A user-facing configuration error with no input values or secrets."""


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class General(Section):
    workspace_root: Path = Field(default_factory=lambda: data_path() / "reviews")
    poll_interval_s: int = Field(default=180, gt=0)
    max_concurrent_reviews: int = Field(default=2, gt=0)


def data_path() -> Path:
    return (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))).expanduser()
        / "diffbutler"
    )


class Database(Section):
    url: str = Field(default_factory=lambda: f"sqlite+aiosqlite:///{data_path() / 'diffbutler.db'}")
    auto_migrate: bool = True


class Forge(Section):
    kind: Literal["github", "gitlab"]
    base_url: str
    token_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    include_team_requests: bool = True
    login: str | None = None
    repositories: list[str] = Field(default_factory=list)

    @field_validator("repositories")
    @classmethod
    def valid_repositories(cls, values: list[str]) -> list[str]:
        for value in values:
            PRQuery(repository=value)
        return values


class Review(Section):
    default_reviewer: str | None = None
    findings_threshold: Literal["blocking", "important", "minor", "nit", "question"] = "minor"
    min_confidence: float = Field(default=0.5, ge=0, le=1)
    max_diff_bytes: int = Field(default=400_000, gt=0)
    exclude_globs: list[str] = Field(
        default_factory=lambda: ["**/*.lock", "**/vendor/**", "**/*.min.js", "**/generated/**"]
    )
    mode: Literal["source_only"] = "source_only"
    checkout: Literal["worktree", "mediated"] = "worktree"
    prompt: Path | None = None


class Reviewer(Section):
    id: str = Field(min_length=1)
    kind: Literal["cli"] = "cli"
    command: list[str] = Field(min_length=1)
    preset: Literal["codex", "claude"] | None = None
    max_output_bytes: int = Field(default=4_000_000, gt=0)
    timeout_s: int = Field(default=900, gt=0)
    parse: Literal["json_envelope", "jsonl_stream"] = "json_envelope"
    json_path: str | None = None
    stdin: Literal["prompt"] | None = None


class Attention(Section):
    blocking: int = 100
    important: int = 40
    age_pressure_per_day: int = 5
    size_penalty_per_100_lines: int = 2


class UI(Section):
    browser_command: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def known_columns(self) -> Self:
        from diffbutler.domain.dashboard import COLUMN_IDS

        if not self.columns or len(set(self.columns)) != len(self.columns):
            raise ValueError("ui.columns must be nonempty and unique")
        if any(column not in COLUMN_IDS for column in [*self.columns, self.sort_by]):
            raise ValueError("Unknown column; valid columns: " + ", ".join(COLUMN_IDS))
        return self

    columns: list[str] = Field(
        default_factory=lambda: [
            "number",
            "title",
            "author",
            "state",
        ]
    )
    sort_by: str = "attention"
    sort_desc: bool = True
    preview_pane: bool = True
    default_filter: Literal[
        "active", "requested", "following", "all", "triage", "posted", "snoozed", "history"
    ] = "active"
    attention: Attention = Field(default_factory=Attention)


class Repository(Section):
    enabled: bool = True
    path: Path | None = None
    default_reviewer: str | None = None
    prompt: Path | None = None
    checkout: Literal["worktree", "mediated"] | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DIFFBUTLER_", env_nested_delimiter="__", extra="forbid"
    )
    general: General = Field(default_factory=General)
    db: Database = Field(default_factory=Database)
    forges: list[Forge] = Field(default_factory=list[Forge])
    review: Review = Field(default_factory=Review)
    reviewers: list[Reviewer] = Field(default_factory=list[Reviewer])
    ui: UI = Field(default_factory=UI)
    keys: dict[str, list[str]] = Field(default_factory=lambda: {"open_in_browser": ["o", "ctrl+o"]})
    repos: dict[str, Repository] = Field(default_factory=dict)

    @field_validator("keys", mode="before")
    @classmethod
    def key_lists(cls, value: object) -> object:
        if isinstance(value, dict):
            return {
                key: [keys] if isinstance(keys, str) else keys
                for key, keys in cast(dict[str, object], value).items()
            }
        return value

    @model_validator(mode="after")
    def reviewer_references(self) -> Self:
        ids = [reviewer.id for reviewer in self.reviewers]
        if len(ids) != len(set(ids)):
            raise ValueError("reviewers.id must be unique")
        for name in [
            self.review.default_reviewer,
            *(repo.default_reviewer for repo in self.repos.values()),
        ]:
            if name is not None and name not in ids:
                raise ValueError(
                    "review.default_reviewer / repos.default_reviewer "
                    "must name a configured reviewers.id"
                )
        return self


def config_path() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
        / "diffbutler/config.toml"
    )


def load_settings(
    path: Path | None = None,
    *,
    repo: str | None = None,
    overrides: dict[str, Any] | None = None,
    missing_ok: bool = False,
) -> Settings:
    """CLI > environment > selected repo review options > TOML > defaults.

    Only the implicit default config may be missing unless startup opts in with
    missing_ok. No dotenv or repository code is loaded. Relative configured paths
    are relative to the config directory.
    """
    selected = (path or config_path()).expanduser().absolute()
    try:
        with selected.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        if path is not None and not missing_ok:
            raise ConfigError("config: explicitly selected file does not exist") from None
        document = {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"config: invalid TOML at line {exc.lineno}, column {exc.colno}"
        ) from None
    except OSError:
        raise ConfigError("config: cannot read configuration file") from None

    repo_values: dict[str, Any] = {}
    repositories = document.get("repos", {})
    if repo is not None and isinstance(repositories, dict):
        entry = cast(dict[str, Any], repositories).get(repo, {})
        if isinstance(entry, dict):
            repo_values = {
                "review": {
                    key: value
                    for key, value in cast(dict[str, Any], entry).items()
                    if key in {"default_reviewer", "prompt", "checkout"}
                }
            }

    class LoadedSettings(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (
                init_settings,
                env_settings,
                InitSettingsSource(settings_cls, repo_values),
                InitSettingsSource(settings_cls, document),
            )

    try:
        result = LoadedSettings(**(overrides or {}))
    except ValidationError as exc:
        errors = [
            f"{'.'.join(map(str, error['loc'])) or 'config'}: {error['msg']}"
            for error in exc.errors(include_input=False)
        ]
        raise ConfigError("; ".join(errors)) from None
    except SettingsError:
        raise ConfigError(
            "environment: invalid DIFFBUTLER_* value; use JSON for lists and objects"
        ) from None

    def resolve(value: Path) -> Path:
        value = value.expanduser()
        return value if value.is_absolute() else selected.parent / value

    result.general.workspace_root = resolve(result.general.workspace_root)
    if result.review.prompt is not None:
        result.review.prompt = resolve(result.review.prompt)
    for repository in result.repos.values():
        if repository.path is not None:
            repository.path = resolve(repository.path)
        if repository.prompt is not None:
            repository.prompt = resolve(repository.prompt)
    return result
