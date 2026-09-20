"""Public access and token validation use only a synthetic HTTP transport."""

import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from tests.test_github import pull, search

from diffbutler.config.settings import ConfigError, config_path, load_settings
from diffbutler.core.setup import startup_settings
from diffbutler.forges.base import ForgeError
from diffbutler.forges.github.adapter import GitHubForge
from diffbutler.forges.github.http import GitHubHTTP
from diffbutler.forges.github.setup import repository_is_public
from diffbutler.forges.registry import open_forge
from diffbutler.ui.cli import main


def install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def http(base_url: str, token: str | None) -> GitHubHTTP:
        return GitHubHTTP(base_url, token, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("diffbutler.forges.github.setup.GitHubHTTP", http)
    monkeypatch.setattr("diffbutler.forges.registry.GitHubHTTP", http)


@pytest.mark.asyncio
async def test_public_setup_and_discovery_never_resolve_or_send_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        endpoint = request.url.path
        requests.append(endpoint)
        if endpoint == "/repos/acme/api":
            return httpx.Response(200, json={"full_name": "acme/api", "private": False})
        if endpoint == "/users/reader":
            return httpx.Response(200, json={"login": "reader"})
        if endpoint == "/repos/acme/api/pulls":
            return httpx.Response(200, json=[])
        if endpoint == "/search/issues":
            assert request.url.params["q"] == "is:pr is:open review-requested:reader repo:acme/api"
            return httpx.Response(200, json=search(1))
        if endpoint == "/repos/acme/api/pulls/1":
            return httpx.Response(200, json=pull())
        pytest.fail(f"Unexpected public endpoint: {endpoint}")

    async def forbidden(base_url: str, token_env: str) -> str:
        pytest.fail("Public access must not resolve credentials")

    install_transport(monkeypatch, handle)
    monkeypatch.setattr("diffbutler.forges.registry.resolve_token", forbidden)
    monkeypatch.setenv("GITHUB_TOKEN", "unused-secret")
    answers = iter(["github", "acme/api", "reader"])

    async def ask(label: str, default: str) -> str:
        if "username" in label:
            assert default == "acme"
        assert "token" not in label.lower()
        return next(answers)

    settings = await startup_settings(None, directory=tmp_path, ask=ask)
    config = settings.forges[0]
    assert config.token_env is None and config.login == "reader"
    assert config.include_team_requests is False
    assert "token_env" not in config_path().read_text()
    assert "unused-secret" not in config_path().read_text()
    assert load_settings().forges[0].token_env is None
    forge = await open_forge(config)
    try:
        discovery = await forge.list_review_requests()
        assert discovery.complete
        assert len(discovery.items) == 1
        assert discovery.items[0].sources == ("user:reader",)
    finally:
        await forge.close()
    assert "/user" not in requests and "/user/teams" not in requests


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "invalid", "wrong-user", "no-access", "no-pr-access"])
async def test_private_setup_validates_token_before_saving(
    failure: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "synthetic-private-token"

    def handle(request: httpx.Request) -> httpx.Response:
        if "authorization" not in request.headers:
            assert request.url.path == "/repos/acme/api"
            return httpx.Response(404)
        assert request.headers["authorization"] == f"Bearer {token}"
        if request.url.path == "/user":
            if failure == "invalid":
                return httpx.Response(401, json={"message": token})
            return httpx.Response(
                200, json={"login": "other" if failure == "wrong-user" else "reader"}
            )
        if request.url.path == "/repos/acme/api":
            if failure == "no-access":
                return httpx.Response(404)
            return httpx.Response(200, json={"full_name": "acme/api", "private": True})
        if request.url.path == "/repos/acme/api/pulls":
            return httpx.Response(403 if failure == "no-pr-access" else 200, json=[])
        pytest.fail("Unexpected authenticated endpoint")

    install_transport(monkeypatch, handle)
    monkeypatch.setenv("TEST_PRIVATE_TOKEN", token)
    answers = iter(["github", "acme/api", "reader", "TEST_PRIVATE_TOKEN"])

    async def ask(label: str, default: str) -> str:
        return next(answers)

    if failure:
        with pytest.raises(ForgeError) as error:
            await startup_settings(None, directory=tmp_path, ask=ask)
        assert token not in str(error.value)
        assert not config_path().exists()
        if failure == "invalid":
            assert "Invalid GitHub token" in str(error.value)
    else:
        settings = await startup_settings(None, directory=tmp_path, ask=ask)
        assert settings.forges[0].token_env == "TEST_PRIVATE_TOKEN"
        assert settings.forges[0].login == "reader"
        assert token not in config_path().read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "bad token", "bad\ntoken"])
async def test_private_setup_rejects_missing_or_malformed_environment_token(
    value: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(404)

    install_transport(monkeypatch, handle)
    monkeypatch.delenv("TEST_PRIVATE_TOKEN", raising=False)
    if value is not None:
        monkeypatch.setenv("TEST_PRIVATE_TOKEN", value)
    answers = iter(["github", "acme/api", "reader", "TEST_PRIVATE_TOKEN"])

    async def ask(label: str, default: str) -> str:
        return next(answers)

    with pytest.raises((ConfigError, ForgeError)):
        await startup_settings(None, directory=tmp_path, ask=ask)
    assert not config_path().exists()


@pytest.mark.asyncio
async def test_visibility_rate_limit_does_not_trigger_credential_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"Retry-After": "60"})

    install_transport(monkeypatch, handle)
    answers = iter(["github", "acme/api"])

    async def ask(label: str, default: str) -> str:
        return next(answers)

    with pytest.raises(ForgeError, match="rate limit"):
        await startup_settings(None, directory=tmp_path, ask=ask)
    assert not config_path().exists()


@pytest.mark.asyncio
async def test_public_mode_team_membership_is_explicitly_unavailable() -> None:
    forge = GitHubForge(GitHubHTTP("https://api.github.com", None), public_login="reader")
    try:
        teams = await forge.team_membership()
        assert not teams.complete and not teams.teams
        assert "authentication" in teams.warnings[0]
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_private_flag_is_not_mistaken_for_public(monkeypatch: pytest.MonkeyPatch) -> None:
    install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"full_name": "acme/api", "private": True}),
    )
    assert not await repository_is_public("https://api.github.com", "acme/api")


def test_invalid_token_exits_cli_without_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401 if "authorization" in request.headers else 404)

    install_transport(monkeypatch, handle)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TEST_PRIVATE_TOKEN", "synthetic-invalid-token")
    answers = iter(["github", "acme/api", "reader", "TEST_PRIVATE_TOKEN"])

    def prompt(label: str, *, default: str | None = None) -> str:
        return next(answers)

    monkeypatch.setattr("diffbutler.ui.cli.typer.prompt", prompt)
    with pytest.raises(SystemExit) as error:
        main(["dashboard"])
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "Invalid GitHub token" in output.err
    assert "synthetic-invalid-token" not in output.err + output.out
    assert not config_path().exists()
