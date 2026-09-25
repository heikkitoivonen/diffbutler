"""Public access and token validation use only a synthetic HTTP transport."""

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from tests.test_github import pull, search
from textual.widgets import DataTable, Static

from diffbutler.config.settings import ConfigError, config_path, load_settings
from diffbutler.core.setup import startup_settings
from diffbutler.forges.base import ForgeError
from diffbutler.forges.github.adapter import GitHubForge
from diffbutler.forges.github.http import GitHubHTTP
from diffbutler.forges.github.setup import detect_login, repository_is_public
from diffbutler.forges.registry import open_forge
from diffbutler.ui.cli import main
from diffbutler.ui.tui.dashboard import Dashboard


def install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def http(
        base_url: str, token: str | None, *, max_pacing_wait: float | None = None
    ) -> GitHubHTTP:
        return GitHubHTTP(
            base_url, token, transport=httpx.MockTransport(handler), max_pacing_wait=max_pacing_wait
        )

    monkeypatch.setattr("diffbutler.forges.github.setup.GitHubHTTP", http)
    monkeypatch.setattr("diffbutler.forges.registry.GitHubHTTP", http)


@pytest.fixture(autouse=True)
def synthetic_reviewer_check(monkeypatch: pytest.MonkeyPatch) -> None:
    async def checked(*args: object) -> None:
        pass

    monkeypatch.setattr("diffbutler.core.setup.check_reviewer", checked)


@pytest.mark.asyncio
@pytest.mark.parametrize("review_requested", [False, True])
async def test_public_queue_switch_uses_review_requests_not_assignments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_requested: bool
) -> None:
    from dataclasses import replace

    from diffbutler.core.application import local_queue, local_store, sync_configured
    from diffbutler.core.dashboard import toggle_monitoring_configured

    expected_rows = int(review_requested)
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        endpoint = request.url.path
        calls.append(endpoint)
        if endpoint == "/users/reader":
            return httpx.Response(200, json={"login": "reader"})
        if endpoint == "/search/issues":
            query = request.url.params["q"]
            assert "assignee:" not in query
            # Include stale hits to verify membership against the actual PR.
            return httpx.Response(200, json=search(1, 2, 3))
        if endpoint.rsplit("/", 1)[-1].isdigit():
            number = int(endpoint.rsplit("/", 1)[-1])
            data = pull(number, users=("reader",) if number == 2 and review_requested else ())
            data["assignees"] = [{"login": "READER"}] if number == 1 else []
            return httpx.Response(200, json=data)
        return httpx.Response(200, json=[])

    install_transport(monkeypatch, handle)
    path = tmp_path / "public.toml"
    path.write_text(
        f'[db]\nurl="sqlite+aiosqlite:///{tmp_path}/public.db"\n'
        '[[forges]]\nkind="github"\nbase_url="https://api.github.com"\n'
        'login="reader"\nrepositories=["acme/api"]\ninclude_team_requests=false\n'
    )
    await toggle_monitoring_configured(path)
    await sync_configured(path)
    requested = await local_queue(path, "requested", None)
    assert len(requested) == expected_rows
    if review_requested:
        assert requested[0].tracked.sources == ("user:reader",)
        assert requested[0].tracked.pull_request.ref.number == 2

    # Simulate an existing database written by the rejected assignment implementation.
    async with local_store(path) as (queue, _):
        account = (await queue.accounts())[0]
        snapshot = await queue.snapshot(account)
        await queue.apply(
            account,
            snapshot.version,
            tuple(
                replace(item, sources=("assignee:reader",))
                if item.pull_request.ref.number == 1
                else item
                for item in snapshot.items
            ),
        )
    assert len(await local_queue(path, "requested", None)) == expected_rows

    refreshes = 0

    async def failing_refresh(_: Path | None) -> None:
        nonlocal refreshes
        refreshes += 1
        raise ForgeError("GitHub rate limit is exhausted", retry_after=60)

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.poll_configured", failing_refresh)
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.query_one(DataTable).row_count == 2
        # Enable live actions without starting the periodic poller in this test.
        app.offline = False
        before = len(calls)
        await pilot.press("a")
        # No periodic poller is running, so wait for the action worker and
        # its final reload rather than only the Textual message queue.
        await asyncio.wait_for(
            asyncio.gather(*(worker.wait() for worker in app.workers)), timeout=5
        )
        await pilot.pause()
        assert app.view == "active"
        assert app.query_one(DataTable).row_count == 2
        assert refreshes == 0 and len(calls) == before
        await pilot.press("R")
        await asyncio.wait_for(
            asyncio.gather(*(worker.wait() for worker in app.workers)), timeout=5
        )
        await pilot.pause()
        assert refreshes == 1
        assert "GitHub rate limit is exhausted" in str(app.query_one("#status", Static).content)
        assert app.query_one(DataTable).row_count == 2

    restarted = Dashboard(path, load_settings(path), offline=True)
    async with restarted.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert restarted.view == "active"
        assert restarted.query_one(DataTable).row_count == 2
        assert not restarted.review_tasks
        await pilot.press("h", "h")
        await pilot.pause()
        assert restarted.view == "active"
        assert restarted.query_one(DataTable).row_count == 2
        await pilot.press("f")
        await pilot.pause()
        assert restarted.view == "requested"
        assert restarted.query_one(DataTable).row_count == expected_rows

    # A stale search hit must not keep request membership after removal.
    review_requested = False
    await sync_configured(path)
    assert not await local_queue(path, "requested", None)
    assert len(await local_queue(path, "following", None)) == 2
    path.write_text(path.read_text() + "\n[ui]\ninclude_assigned_prs = true\n")
    assert len(await local_queue(path, "following", None)) == 3


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
    answers = iter(["github", "acme/api", "anonymous", "reader", "codex", sys.executable])

    async def ask(label: str, default: str) -> str:
        if "username" in label:
            assert default == "acme"
        assert label != "GitHub token environment variable name"
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
@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize(
    "failure", [None, "lookup-unavailable", "invalid", "wrong-user", "no-access", "no-pr-access"]
)
async def test_token_setup_validates_access_before_saving(
    failure: str | None, public: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "synthetic-private-token"
    identity_calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        if "authorization" not in request.headers:
            assert request.url.path == "/repos/acme/api"
            return (
                httpx.Response(200, json={"full_name": "acme/api", "private": False})
                if public
                else httpx.Response(404)
            )
        assert request.headers["authorization"] == f"Bearer {token}"
        if request.url.path == "/user":
            nonlocal identity_calls
            identity_calls += 1
            if failure in {"wrong-user", "lookup-unavailable"} and identity_calls == 1:
                return httpx.Response(403)
            if failure == "invalid":
                return httpx.Response(401, json={"message": token})
            return httpx.Response(
                200, json={"login": "other" if failure == "wrong-user" else "reader"}
            )
        if request.url.path == "/repos/acme/api":
            if failure == "no-access":
                return httpx.Response(404)
            return httpx.Response(200, json={"full_name": "acme/api", "private": not public})
        if request.url.path == "/repos/acme/api/pulls":
            return httpx.Response(403 if failure == "no-pr-access" else 200, json=[])
        pytest.fail("Unexpected authenticated endpoint")

    install_transport(monkeypatch, handle)
    monkeypatch.setenv("TEST_PRIVATE_TOKEN", token)
    answers = iter(["github", "acme/api", "TEST_PRIVATE_TOKEN", "codex", sys.executable])

    async def ask(label: str, default: str) -> str:
        if "username" in label:
            assert failure in {"invalid", "wrong-user", "lookup-unavailable"}
            assert default == "acme"
            return "reader"
        if label == "GitHub access (token recommended, anonymous)":
            assert default == "token"
            return default
        return next(answers)

    if failure and failure != "lookup-unavailable":
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
    answers = iter(["github", "acme/api", "TEST_PRIVATE_TOKEN", "codex", sys.executable])

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

    with pytest.raises(ForgeError, match="GitHub requires a wait of 60 seconds"):
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
    answers = iter(["github", "acme/api", "TEST_PRIVATE_TOKEN", "codex", sys.executable])

    def prompt(label: str, *, default: str | None = None) -> str:
        if "username" in label:
            return "reader"
        return next(answers)

    monkeypatch.setattr("diffbutler.ui.cli.typer.prompt", prompt)
    with pytest.raises(SystemExit) as error:
        main(["dashboard"])
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "Invalid GitHub token" in output.err
    assert "synthetic-invalid-token" not in output.err + output.out
    assert not config_path().exists()


@pytest.mark.asyncio
async def test_low_quota_setup_returns_retry_without_long_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    monkeypatch.setattr("diffbutler.forges.github.http.GitHubHTTP.DEFAULT_REQUEST_INTERVAL", 1.0)
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        body = (
            {"login": "reader"}
            if request.url.path == "/users/reader"
            else {"full_name": "acme/api", "private": False}
        )
        return httpx.Response(
            200,
            json=body,
            headers={
                "X-RateLimit-Resource": "core",
                "X-RateLimit-Limit": "60",
                "X-RateLimit-Remaining": "6",
                "X-RateLimit-Reset": str(int(time.time()) + 1200),
            },
        )

    install_transport(monkeypatch, handle)
    answers = iter(["github", "acme/api", "anonymous", "reader"])
    progress: list[str] = []

    async def ask(label: str, default: str) -> str:
        return next(answers)

    with pytest.raises(ForgeError, match="GitHub requires a wait") as error:
        await startup_settings(None, directory=tmp_path, ask=ask, progress=progress.append)
    assert error.value.retry_after is not None and error.value.retry_after > 1190
    assert requests == ["/repos/acme/api", "/users/reader"]
    assert "Checking GitHub username and PR access" in progress[-1]
    assert not config_path().exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["success", "malformed", "network", "timeout", "cancelled"])
async def test_username_detection_is_bounded_and_host_specific(
    result: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://github.example.com/api/v3/user"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        if result == "network":
            raise httpx.ConnectError("offline")
        if result == "timeout":
            raise TimeoutError
        if result == "cancelled":
            raise asyncio.CancelledError
        return httpx.Response(
            200, json={"login": "bad/name" if result == "malformed" else "Reader"}
        )

    install_transport(monkeypatch, handle)
    if result == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await detect_login("https://github.example.com/api/v3", "synthetic-token")
    else:
        login = await detect_login("https://github.example.com/api/v3", "synthetic-token")
        assert login == ("Reader" if result == "success" else None)
