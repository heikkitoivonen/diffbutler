"""Construct configured read adapters without exposing credentials to the core."""

from diffbutler.config.settings import Forge
from diffbutler.forges.base import ForgeError, ReadForge
from diffbutler.forges.github.adapter import GitHubForge
from diffbutler.forges.github.auth import resolve_token
from diffbutler.forges.github.http import GitHubHTTP


async def open_forge(config: Forge) -> ReadForge:
    if config.kind != "github":
        raise ForgeError("GitLab adapter is not implemented")
    token = await resolve_token(config.base_url, config.token_env)
    return GitHubForge(
        GitHubHTTP(config.base_url, token), include_team_requests=config.include_team_requests
    )
