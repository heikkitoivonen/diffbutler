"""Keep local configuration and credentials out of every test."""

import os
from pathlib import Path

import httpx
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-kernel-sandbox",
        action="store_true",
        help="Fail instead of skipping when Linux sandbox prerequisites are missing",
    )


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in os.environ:
        if key.startswith("DIFFBUTLER_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(_self: httpx.AsyncHTTPTransport, _request: httpx.Request) -> httpx.Response:
        pytest.fail("Tests must use a fake HTTP transport, never a real forge")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
