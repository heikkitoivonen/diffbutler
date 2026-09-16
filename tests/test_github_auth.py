"""Credential resolution uses only fake providers in tests."""

import asyncio

import pytest

from diffbutler.forges.base import ForgeError
from diffbutler.forges.github.auth import gh_token, resolve_token


@pytest.mark.asyncio
async def test_configured_environment_precedes_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_FORGE_TOKEN", "synthetic-token")

    async def forbidden(_: tuple[str, ...]) -> str | None:
        pytest.fail("gh must not run when environment token is present")

    assert (
        await resolve_token("https://api.github.com", "TEST_FORGE_TOKEN", run=forbidden)
        == "synthetic-token"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base", "host"),
    [
        ("https://api.github.com", "github.com"),
        ("https://git.example.invalid/api/v3", "git.example.invalid"),
    ],
)
async def test_gh_auth_is_scoped_to_host(
    base: str, host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_FORGE_TOKEN", raising=False)

    async def run(argv: tuple[str, ...]) -> str:
        assert argv == ("gh", "auth", "token", "--hostname", host)
        return "synthetic-gh-token"

    assert await resolve_token(base, "TEST_FORGE_TOKEN", run=run) == "synthetic-gh-token"


@pytest.mark.asyncio
async def test_missing_auth_is_actionable_and_never_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_FORGE_TOKEN", raising=False)

    async def absent(_: tuple[str, ...]) -> None:
        return None

    with pytest.raises(ForgeError, match="token_env"):
        await resolve_token("https://api.github.com", "TEST_FORGE_TOKEN", run=absent)


@pytest.mark.asyncio
async def test_invalid_origin_does_not_resolve_credentials() -> None:
    async def forbidden(_: tuple[str, ...]) -> str | None:
        pytest.fail("credentials must not be resolved for an invalid origin")

    with pytest.raises(ForgeError):
        await resolve_token("http://example.invalid", "TEST_FORGE_TOKEN", run=forbidden)


@pytest.mark.asyncio
async def test_cancelled_gh_process_is_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()

    class Process:
        returncode: int | None = None
        killed = False
        waited = False

        async def communicate(self) -> tuple[bytes, bytes]:
            entered.set()
            await asyncio.Future[None]()
            return b"", b""

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.waited = True
            self.returncode = -9
            return -9

    process = Process()

    async def create(*_args: object, **_kwargs: object) -> Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(gh_token(("gh", "auth", "token")))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.waited
