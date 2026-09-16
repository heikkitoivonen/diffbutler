"""Local-only doctor behavior and public CLI failure handling."""

from pathlib import Path

import pytest

from diffbutler.core.doctor import run_doctor
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
    report = await run_doctor(path)
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
    assert "Schema 0001 current" in capsys.readouterr().out
