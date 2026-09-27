"""Local and fake-forge doctor behavior and public CLI failure handling."""

from pathlib import Path

import httpx
import pytest

from diffbutler.config.settings import Forge
from diffbutler.core.doctor import run_doctor
from diffbutler.forges.base import ReadForge
from diffbutler.forges.github.adapter import GitHubForge
from diffbutler.forges.github.http import GitHubHTTP
from diffbutler.store.database import DatabaseStatus
from diffbutler.ui.cli import main


@pytest.mark.asyncio
async def test_doctor_passes_without_forge(tmp_path: Path) -> None:
    report = await run_doctor()
    assert report.passed
    assert (tmp_path / "data/diffbutler/diffbutler.db").exists()
    assert {check.name for check in report.checks if check.status == "SKIP"} == {
        "forges",
        "reviewers",
    }
    assert (await run_doctor()).passed


@pytest.mark.asyncio
async def test_invalid_config_does_not_open_database(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[general]\nmax_concurrent_reviews = -1")
    report = await run_doctor(path)
    assert not report.passed
    assert len(report.checks) == 1
    assert not (tmp_path / "data").exists()


@pytest.mark.asyncio
async def test_configured_reviewers_are_never_executed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("""
[[reviewers]]
id = "fake"
command = ["nonexistent-agent"]
[[forges]]
kind = "github"
base_url = "https://example.invalid"
token_env = "DO_NOT_READ"
[repos."acme/api"]
path = "missing"
""")
    report = await run_doctor(path, offline=True)
    assert not report.passed
    assert any(check.name == "repo:acme/api" and check.status == "FAIL" for check in report.checks)
    assert any(check.name == "reviewers" and check.status == "SKIP" for check in report.checks)


@pytest.mark.asyncio
async def test_store_protocol_and_failure_close() -> None:
    class FakeStore:
        closed = False

        async def initialize(self, *, auto_migrate: bool = True) -> DatabaseStatus:
            raise OSError("secret-connection-value")

        async def close(self) -> None:
            self.closed = True

    store = FakeStore()
    report = await run_doctor(store_factory=lambda _: store)
    assert store.closed and not report.passed
    assert "secret-connection-value" not in str(report)


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "sqlite+aiosqlite://user:secret@localhost:invalid/db",
        "postgresql+asyncpg://user:secret@localhost/db",
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///db?mode=ro",
    ],
)
def test_database_errors_have_nonzero_exit_without_secrets(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["doctor", "--db-url", url])
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert "FAIL" in output and "secret" not in output


def test_doctor_cli_overrides_and_check_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/cli.db"
    with pytest.raises(SystemExit) as error:
        main(["doctor", "--db-url", url, "--no-migrate"])
    assert error.value.code == 1
    assert "missing" in capsys.readouterr().out
    main(["doctor", "--db-url", url])
    assert "migrations applied" in capsys.readouterr().out
    main(["doctor", "--db-url", url, "--no-migrate"])
    assert "Schema 0010 current" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("login", "status", "passed"),
    [("reader", 200, True), ("wrong-user", 200, False), ("reader", 401, False)],
)
async def test_doctor_configured_forge_identity_and_closure(
    tmp_path: Path, login: str, status: int, passed: bool
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("""
[[forges]]
kind = "github"
base_url = "https://api.github.com"
token_env = "UNUSED"
login = "reader"
include_team_requests = false
""")
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.method == "GET" and request.url.path == "/user"
        return httpx.Response(
            status,
            json={"login": login},
            headers={
                "X-RateLimit-Resource": "core",
                "X-RateLimit-Remaining": "500",
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Reset": "2000000000",
            },
        )

    client = GitHubHTTP(
        "https://api.github.com", "synthetic-token", transport=httpx.MockTransport(handle)
    )

    async def factory(config: Forge) -> ReadForge:
        assert config.token_env == "UNUSED"
        return GitHubForge(client, include_team_requests=config.include_team_requests)

    report = await run_doctor(path, forge_factory=factory)
    assert report.passed is passed and client.client.is_closed
    assert calls == ["/user"]
    if status == 200:
        assert any(check.detail == "500/5000 requests remaining" for check in report.checks)


@pytest.mark.asyncio
async def test_offline_doctor_never_opens_configured_forge(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[[forges]]\nkind="github"\nbase_url="https://api.github.com"\ntoken_env="UNUSED"'
    )

    async def forbidden(_: Forge) -> ReadForge:
        pytest.fail("offline doctor must not resolve credentials or contact a forge")

    report = await run_doctor(path, forge_factory=forbidden, offline=True)
    assert report.passed
    assert any(check.name == "forges" and check.status == "SKIP" for check in report.checks)


@pytest.mark.asyncio
async def test_doctor_marks_incomplete_team_visibility_as_warning(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[[forges]]\nkind="github"\nbase_url="https://api.github.com"\ntoken_env="UNUSED"'
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, json={"login": "reader"})
            if request.url.path == "/user"
            else httpx.Response(403)
        )

    async def factory(_: Forge) -> ReadForge:
        return GitHubForge(
            GitHubHTTP(
                "https://api.github.com", "synthetic-token", transport=httpx.MockTransport(handle)
            )
        )

    report = await run_doctor(path, forge_factory=factory)
    assert report.passed
    assert any(check.name.endswith(":teams") and check.status == "WARN" for check in report.checks)
