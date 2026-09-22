"""First-run setup with fake visibility checks; no real network or reviewer calls."""

import asyncio
import sys
from pathlib import Path

import pytest
from tests.test_sources import git

from diffbutler.config.settings import ConfigError, config_path, load_settings
from diffbutler.core.setup import startup_settings
from diffbutler.sources.remotes import discover_repository, remote_identity
from diffbutler.ui.cli import main
from diffbutler.ui.tui.dashboard import Dashboard


@pytest.fixture(autouse=True)
def public_repository_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def public(base_url: str, repository: str) -> bool:
        return True

    async def validate(base_url: str, repository: str, login: str, token: str | None) -> str:
        assert token is None
        return login

    monkeypatch.setattr("diffbutler.core.setup.repository_is_public", public)
    monkeypatch.setattr("diffbutler.core.setup.validate_access", validate)

    async def checked(*args: object) -> None:
        pass

    monkeypatch.setattr("diffbutler.core.setup.check_reviewer", checked)


async def accept_default(label: str, default: str) -> str:
    if label == "GitHub access (token recommended, anonymous)":
        assert default == "token"
        return "anonymous"
    if label == "AI reviewer (codex or claude)":
        return "codex"
    if label == "Reviewer executable (name or full path)":
        return sys.executable
    assert label == "GitHub username for review requests"
    assert default == "acme"
    return default


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:acme/api.git", ("github.com", "acme/api")),
        ("https://user:secret@github.com/acme/api.git", ("github.com", "acme/api")),
        ("ssh://git@github.com/acme/api.git", ("github.com", "acme/api")),
        ("git://github.com/acme/api.git", ("github.com", "acme/api")),
        ("git@git.example.com:acme/api.git", ("git.example.com", "acme/api")),
        ("/local/repo", None),
        ("file:///local/repo", None),
        ("https://github.com/acme/api?token=secret", None),
        ("ext::arbitrary command", None),
    ],
)
def test_remote_identity(remote: str, expected: tuple[str, str] | None) -> None:
    assert remote_identity(remote) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", [False, True])
async def test_git_inference_asks_for_username_and_reviewer(tmp_path: Path, custom: bool) -> None:
    repo = tmp_path / "repository"
    repo.mkdir()
    await git(repo, "init", "--template=")
    await git(repo, "remote", "add", "origin", "https://user:secret@github.com/acme/api.git")
    await git(repo, "remote", "add", "upstream", "git@github.com:other/api.git")
    subdirectory = repo / "nested"
    subdirectory.mkdir()
    path = tmp_path / "custom" / "settings.toml" if custom else config_path()

    settings = await startup_settings(
        path if custom else None, directory=subdirectory, ask=accept_default
    )
    assert settings.forges[0].base_url == "https://api.github.com"
    assert settings.forges[0].repositories == ["acme/api"]
    assert settings.repos["acme/api"].path == repo
    assert "secret" not in path.read_text()
    assert not (tmp_path / "data").exists()
    assert settings.review.default_reviewer == "codex"
    assert settings.reviewers[0].preset == "codex"
    # Subsequent launches skip discovery and questions, even outside Git.
    again = await startup_settings(path, directory=tmp_path, ask=None)
    assert again.forges == settings.forges


@pytest.mark.asyncio
async def test_missing_remote_prompts_and_preserves_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text("# my settings\nforges = []\n[general]\npoll_interval_s = 42 # keep\n")
    monkeypatch.setenv("DIFFBUTLER_GENERAL__POLL_INTERVAL_S", "300")
    answers = iter(["github", "acme/api", "anonymous", "reader", "codex", sys.executable])
    questions: list[str] = []

    async def ask(label: str, default: str) -> str:
        questions.append(label)
        return next(answers)

    settings = await startup_settings(None, directory=tmp_path, ask=ask)
    assert len(questions) == 6
    assert settings.general.poll_interval_s == 300
    assert "poll_interval_s = 42 # keep" in path.read_text()
    assert "# my settings" in path.read_text()
    assert settings.forges[0].repositories == ["acme/api"]
    assert settings.repos == {}


@pytest.mark.asyncio
async def test_enterprise_remote_requires_provider_confirmation(tmp_path: Path) -> None:
    await git(tmp_path, "init", "--template=")
    await git(tmp_path, "remote", "add", "origin", "git@git.example.com:acme/api.git")
    answers = iter(
        ["github-enterprise", "git.example.com", "anonymous", "reader", "codex", sys.executable]
    )

    async def ask(label: str, default: str) -> str:
        return next(answers)

    settings = await startup_settings(None, directory=tmp_path, ask=ask)
    assert settings.forges[0].base_url == "https://git.example.com/api/v3"
    assert settings.forges[0].repositories == ["acme/api"]


@pytest.mark.asyncio
async def test_ambiguous_remotes_are_not_inferred(tmp_path: Path) -> None:
    await git(tmp_path, "init", "--template=")
    await git(tmp_path, "remote", "add", "one", "git@github.com:acme/api.git")
    assert (await discover_repository(tmp_path)) is not None
    await git(tmp_path, "remote", "add", "two", "git@github.com:other/api.git")
    assert await discover_repository(tmp_path) is None


@pytest.mark.asyncio
async def test_noninteractive_missing_information_does_not_write(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="interactive terminal"):
        await startup_settings(None, directory=tmp_path, ask=None)
    assert not config_path().exists()


@pytest.mark.asyncio
async def test_cancelled_questions_do_not_write(tmp_path: Path) -> None:
    async def cancel(label: str, default: str) -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await startup_settings(None, directory=tmp_path, ask=cancel)
    assert not config_path().exists()


@pytest.mark.asyncio
async def test_environment_forge_still_requires_reviewer_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "DIFFBUTLER_FORGES",
        '[{"kind":"github","base_url":"https://api.github.com","token_env":"TEST"}]',
    )
    with pytest.raises(ConfigError, match="interactive terminal"):
        await startup_settings(None, directory=tmp_path, ask=None)
    assert not config_path().exists()


def test_repository_filters_are_validated(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[[forges]]\nkind="github"\nbase_url="https://api.github.com"\n'
        'token_env="TEST"\nrepositories=["acme/api is:closed"]\n'
    )
    with pytest.raises(ConfigError, match="repositories"):
        load_settings(path)


@pytest.mark.parametrize("arguments", [[], ["dashboard"], ["dashboard", "--offline"]])
def test_dashboard_startup_runs_setup_before_rendering(
    arguments: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    answers = iter(["github", "acme/api", "anonymous", "reader", "codex", sys.executable])
    questions: list[str] = []
    rendered: list[bool] = []

    def prompt(label: str, *, default: str | None = None) -> str:
        questions.append(label)
        return next(answers)

    async def render(app: Dashboard) -> None:
        rendered.append(True)
        if "--offline" in arguments:
            assert not app.settings.forges
        else:
            assert app.settings.forges[0].repositories == ["acme/api"]
            assert load_settings().forges == app.settings.forges

    monkeypatch.setattr("diffbutler.ui.cli.typer.prompt", prompt)
    monkeypatch.setattr(Dashboard, "run_async", render)
    main(arguments)
    assert rendered == [True]
    assert len(questions) == (0 if "--offline" in arguments else 6)
    assert config_path().exists() == ("--offline" not in arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("inline", [False, True])
async def test_existing_repository_options_and_symlink_are_preserved(
    tmp_path: Path, inline: bool
) -> None:
    await git(tmp_path, "init", "--template=")
    await git(tmp_path, "remote", "add", "origin", "git@github.com:acme/api.git")
    target = tmp_path / "actual.toml"
    target.write_text(
        'repos = {"acme/api" = {enabled = false}}\n'
        if inline
        else '[repos."acme/api"]\nenabled = false\n'
    )
    link = tmp_path / "linked.toml"
    link.symlink_to(target)
    settings = await startup_settings(link, directory=tmp_path, ask=accept_default)
    assert link.is_symlink()
    assert settings.repos["acme/api"].enabled is False
    assert settings.repos["acme/api"].path == tmp_path


@pytest.mark.asyncio
async def test_invalid_answer_leaves_config_untouched(tmp_path: Path) -> None:
    answers = iter(["github", "not a repository"])

    async def ask(label: str, default: str) -> str:
        return next(answers)

    with pytest.raises(ConfigError, match="owner/name"):
        await startup_settings(None, directory=tmp_path, ask=ask)
    assert not config_path().exists()


@pytest.mark.asyncio
async def test_stalled_access_check_times_out_without_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_repository(directory: Path) -> None:
        return None

    async def stalled(*args: object) -> str:
        await asyncio.Event().wait()
        return "unreachable"

    monkeypatch.setattr("diffbutler.core.setup.discover_repository", no_repository)
    monkeypatch.setattr("diffbutler.core.setup.validate_access", stalled)
    monkeypatch.setattr("diffbutler.core.setup.SETUP_CHECK_TIMEOUT", 0.01)
    answers = iter(["github", "acme/api", "anonymous", "reader"])
    progress: list[str] = []

    async def ask(label: str, default: str) -> str:
        return next(answers)

    with pytest.raises(ConfigError, match="timed out"):
        await startup_settings(None, directory=tmp_path, ask=ask, progress=progress.append)
    assert "Checking GitHub username and PR access" in progress[-1]
    assert not config_path().exists()
