"""First-run configuration with read-only forge validation; no database or AI operations."""

import asyncio
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from diffbutler.config.settings import ConfigError, Forge, Reviewer, Settings, load_settings
from diffbutler.config.setup import save_setup
from diffbutler.domain.forge import PRQuery, identity_part
from diffbutler.forges.github.setup import repository_is_public, validate_access
from diffbutler.reviewers.native import NativeError, native_preset, verify_version
from diffbutler.sources.remotes import discover_repository


async def startup_settings(
    path: Path | None,
    *,
    directory: Path,
    ask: Callable[[str, str], Awaitable[str]] | None,
) -> Settings:
    settings = await asyncio.to_thread(load_settings, path, missing_ok=True)

    async def question(label: str, default: str = "") -> str:
        if ask is None:
            raise ConfigError(
                "First-run setup requires more information; "
                "start diffbutler in an interactive terminal or edit the config"
            )
        return (await ask(label, default)).strip()

    if settings.forges:
        reviewer, default = await setup_reviewer(settings, directory, question)
        if default is not None:
            await asyncio.to_thread(save_setup, path, None, None, reviewer, default)
            return await asyncio.to_thread(load_settings, path)
        return settings
    detected = await discover_repository(directory)

    if detected is not None and detected.host == "github.com":
        host = detected.host
    else:
        kind = (await question("Forge (github or github-enterprise)", "github")).lower()
        if kind not in {"github", "github-enterprise"}:
            raise ConfigError("Only GitHub and GitHub Enterprise are currently supported")
        host = (
            "github.com"
            if kind == "github"
            else await question("GitHub Enterprise hostname", detected.host if detected else "")
        )
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
        raise ConfigError("Enter a forge hostname without a URL scheme or path")
    host = host.lower()
    matched = detected is not None and detected.host == host
    repository = (
        detected.repository
        if detected is not None and matched
        else await question("Repository to monitor (owner/name)")
    )
    try:
        PRQuery(repository=repository)
    except ValueError:
        raise ConfigError("Repository must be owner/name") from None
    base_url = "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"
    public = await repository_is_public(base_url, repository)
    login = await question("GitHub username for review requests", repository.split("/")[0])
    try:
        identity_part(login)
    except ValueError:
        raise ConfigError("Enter a valid GitHub username") from None
    token_env: str | None = None
    token: str | None = None
    if not public:
        token_env = await question("GitHub token environment variable name", "GITHUB_TOKEN")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise ConfigError("Enter an environment variable name, not a token value")
        token = os.environ.get(token_env)
        if not token:
            raise ConfigError("The selected token environment variable is unset or empty")
    login = await validate_access(base_url, repository, login, token)
    forge = Forge(
        kind="github",
        base_url=base_url,
        token_env=token_env,
        login=login,
        include_team_requests=not public,
        repositories=[repository],
    )
    reviewer, default = await setup_reviewer(settings, directory, question)
    await asyncio.to_thread(
        save_setup,
        path,
        forge,
        detected.path if detected and matched else None,
        reviewer,
        default,
    )
    return await asyncio.to_thread(load_settings, path)


async def check_reviewer(reviewer: Reviewer, directory: Path) -> None:
    if shutil.which("git") is None:
        raise ConfigError(
            "Git is required to prepare PR sources; install Git and restart DiffButler"
        )
    if shutil.which(reviewer.command[0]) is None:
        raise ConfigError("Reviewer executable was not found; install it or enter its full path")
    assert reviewer.preset is not None
    preset = native_preset(reviewer.preset)
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "XDG_CONFIG_HOME")
        if key in os.environ
    }
    try:
        await verify_version(tuple(reviewer.command), preset, cwd=directory, env=env)
    except NativeError, OSError, TimeoutError, UnicodeError:
        raise ConfigError(
            f"Reviewer check failed; this preset requires {preset.version}. "
            "Check the selected executable and restart DiffButler."
        ) from None


async def setup_reviewer(
    settings: Settings,
    directory: Path,
    question: Callable[[str, str], Awaitable[str]],
) -> tuple[Reviewer | None, str | None]:
    current = next(
        (r for r in settings.reviewers if r.id == settings.review.default_reviewer), None
    )
    if current is not None and current.preset is not None:
        return None, None
    native = [r for r in settings.reviewers if r.preset is not None]
    if native:
        choices = ", ".join(r.id for r in native)
        name = await question(f"Default AI reviewer ({choices})", native[0].id)
        chosen = next((r for r in native if r.id == name), None)
        if chosen is None:
            raise ConfigError("Choose one of the configured native reviewers")
        await check_reviewer(chosen, directory)
        return None, chosen.id
    name = (await question("AI reviewer (codex or claude)", "codex")).lower()
    if name not in {"codex", "claude"}:
        raise ConfigError("Choose codex or claude as the AI reviewer")
    executable = await question("Reviewer executable (name or full path)", name)
    resolved = shutil.which(executable)
    if resolved is None:
        raise ConfigError("Reviewer executable was not found; install it or enter its full path")
    identifier = name
    while any(r.id == identifier for r in settings.reviewers):
        identifier += "-native"
    reviewer = Reviewer(
        id=identifier,
        command=[str(Path(resolved).absolute())],
        preset="codex" if name == "codex" else "claude",
    )
    await check_reviewer(reviewer, directory)
    return reviewer, reviewer.id
