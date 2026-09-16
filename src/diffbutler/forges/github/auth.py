"""Noninteractive auth resolution; credentials never enter config, storage, or diagnostics."""

import asyncio
import os
from collections.abc import Awaitable, Callable

from diffbutler.forges.base import ForgeError
from diffbutler.forges.github.http import api_url


async def gh_token(argv: tuple[str, ...]) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd="/",
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        return stdout.decode("utf-8").strip() if process.returncode == 0 else None
    except TimeoutError, UnicodeDecodeError:
        return None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def resolve_token(
    base_url: str,
    token_env: str,
    *,
    run: Callable[[tuple[str, ...]], Awaitable[str | None]] = gh_token,
) -> str:
    url = api_url(base_url)
    token = os.environ.get(token_env)
    if not token:
        hostname = "github.com" if url.host == "api.github.com" else url.host
        token = await run(("gh", "auth", "token", "--hostname", hostname))
    if not token or not token.isascii() or any(character.isspace() for character in token):
        raise ForgeError(
            "GitHub credentials unavailable; set the configured token_env "
            "or log in with gh for this host"
        )
    return token
