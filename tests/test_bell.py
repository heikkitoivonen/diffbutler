"""The dashboard rings the terminal bell when a PR enters a ui.bell_on state."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from tests.test_sync import ACCOUNT, PR, REF, FakeForge
from textual.widgets import DataTable, Input

from diffbutler.config.settings import ConfigError, load_settings
from diffbutler.core.dashboard import DashboardRow, dashboard_rows
from diffbutler.core.sync import DiffButler
from diffbutler.domain.forge import Discovery, RequestedPR
from diffbutler.store.database import Database
from diffbutler.store.queue import SQLiteQueueStore
from diffbutler.ui.tui.dashboard import Dashboard

SECOND = replace(PR, ref=replace(REF, number=2), title="Second", url=PR.url[:-1] + "2")


@pytest_asyncio.fixture
async def ledger(tmp_path: Path) -> AsyncGenerator[tuple[SQLiteQueueStore, FakeForge, Path]]:
    path = tmp_path / "bell.toml"
    path.write_text(f'[db]\nurl = "sqlite+aiosqlite:///{tmp_path}/bell.db"\n')
    database = Database(load_settings(path).db.url)
    await database.initialize()
    queue = SQLiteQueueStore(database)
    forge = FakeForge()
    forge.prs[SECOND.ref] = SECOND
    forge.discovery = Discovery(
        tuple(RequestedPR(pr, ("user:reader",)) for pr in (PR, SECOND)), True
    )
    await DiffButler(queue, forge).sync()
    try:
        yield queue, forge, path
    finally:
        await database.close()


def staged(
    monkeypatch: pytest.MonkeyPatch, states: dict[int, str], hidden: set[int] | None = None
) -> None:
    """Serve saved rows with the workflow state given per PR number."""

    async def rows(
        path: Path | None, view: str = "active", text: str = ""
    ) -> tuple[DashboardRow, ...]:
        result: list[DashboardRow] = []
        for row in await dashboard_rows(path, view, text):
            number = row.row.tracked.pull_request.ref.number
            if hidden and number in hidden:
                continue
            tracked = replace(row.row.tracked, state=states.get(number, row.row.tracked.state))
            result.append(
                row.model_copy(update={"row": row.row.model_copy(update={"tracked": tracked})})
            )
        return tuple(result)

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.dashboard_rows", rows)


def bells(app: Dashboard, monkeypatch: pytest.MonkeyPatch) -> list[None]:
    rung: list[None] = []
    monkeypatch.setattr(app, "bell", lambda: rung.append(None))
    return rung


@pytest.mark.serial
@pytest.mark.asyncio
async def test_default_rings_once_per_reload_entering_check(
    ledger: tuple[SQLiteQueueStore, FakeForge, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, path = ledger
    states = {1: "FINDINGS_TRIAGE", 2: "PENDING_AI_REVIEW"}
    staged(monkeypatch, states)
    app = Dashboard(path, load_settings(path), offline=True)
    rung = bells(app, monkeypatch)
    async with app.run_test():
        await app.reload()
        assert rung == []  # Rows already in check at startup stay silent.
        states.update({1: "PENDING_AI_REVIEW", 2: "AI_RUNNING"})
        await app.reload()
        assert rung == []
        states.update({1: "FINDINGS_TRIAGE", 2: "FINDINGS_TRIAGE"})
        await app.reload()
        assert len(rung) == 1  # Two PRs entering check in one refresh ring once.
        await app.reload()
        assert len(rung) == 1
        states[1] = "FINDINGS_TRIAGE_MERGED"
        await app.reload()
        assert len(rung) == 2
        states.update({1: "PENDING_HUMAN_REVIEW", 2: "PENDING_AUTHOR_CHANGES"})
        await app.reload()
        states[2] = "PENDING_AI_REVIEW"
        await app.reload()
        assert len(rung) == 2  # approve and pending are not in the default list.


@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("[]", []),
        ('["pending", "approve"]', ["pending", "approve"]),
        ('["check", "check+", "pending", "approve"]', ["pending", "approve", "check"]),
    ],
)
async def test_configured_states_ring(
    ledger: tuple[SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    expected: list[str],
) -> None:
    _, _, path = ledger
    path.write_text(path.read_text() + f"[ui]\nbell_on = {setting}\n")
    states = {1: "AI_RUNNING"}
    hidden = {2}
    staged(monkeypatch, states, hidden)
    app = Dashboard(path, load_settings(path), offline=True)
    rung = bells(app, monkeypatch)
    async with app.run_test():
        await app.reload()
        events: list[str] = []
        for label, change in [
            ("pending", lambda: hidden.clear()),  # A new review request arrives as pending.
            ("approve", lambda: states.update({1: "PENDING_HUMAN_REVIEW"})),
            ("check", lambda: states.update({1: "FINDINGS_TRIAGE"})),
        ]:
            before = len(rung)
            change()
            await app.reload()
            if len(rung) > before:
                events.append(label)
        assert events == expected


@pytest.mark.serial
@pytest.mark.asyncio
async def test_changed_view_or_filter_and_hidden_dashboard_stay_silent(
    ledger: tuple[SQLiteQueueStore, FakeForge, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, path = ledger
    states = {1: "AI_RUNNING", 2: "FINDINGS_TRIAGE"}
    hidden = {2}
    staged(monkeypatch, states, hidden)
    app = Dashboard(path, load_settings(path), offline=True)
    rung = bells(app, monkeypatch)
    async with app.run_test() as pilot:
        await app.reload()
        # A filter that shows a check row for the first time is not a state change.
        hidden.clear()
        app.query_one(Input).value = "Second"
        await pilot.pause()
        await app.reload()
        assert rung == []
        app.query_one(Input).value = ""
        await pilot.pause()
        await app.reload()
        assert rung == []
        # Nothing is written while an agent session owns the terminal; the
        # dashboard rings once it is shown again.
        app.attached = True
        states[1] = "FINDINGS_TRIAGE"
        await app.reload()
        assert rung == []
        app.attached = False
        await app.reload()
        assert len(rung) == 1
        # Visiting history and returning is a view change too.
        states[1] = "AI_RUNNING"
        await app.reload()
        await pilot.press("h")
        await pilot.pause()
        states[1] = "FINDINGS_TRIAGE"
        await pilot.press("h")
        await pilot.pause()
        await app.reload()
        assert app.view == "active" and len(rung) == 1


@pytest.mark.serial
@pytest.mark.asyncio
async def test_states_chosen_in_the_dashboard_stay_silent(
    ledger: tuple[SQLiteQueueStore, FakeForge, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, forge, path = ledger
    path.write_text(path.read_text() + '[ui]\nbell_on = ["pending", "approve"]\n')
    app = Dashboard(path, load_settings(path), offline=True)
    rung = bells(app, monkeypatch)
    async with app.run_test() as pilot:
        await app.reload()
        await pilot.press("a")
        await asyncio.wait_for(
            asyncio.gather(*(worker.wait() for worker in app.workers)), timeout=5
        )
        await app.reload()
        row = app.selected()
        assert row is not None and row.row.tracked.state == "PENDING_HUMAN_REVIEW"
        assert rung == [] and app.chosen_states == {}

        # A PR followed or selected here appears as pending without a bell;
        # one discovered by a refresh rings.
        third = replace(PR, ref=replace(REF, number=3), url=PR.url[:-1] + "3")
        fourth = replace(PR, ref=replace(REF, number=4), url=PR.url[:-1] + "4")
        forge.prs.update({third.ref: third, fourth.ref: fourth})
        forge.discovery = Discovery(
            tuple(RequestedPR(pr, ("user:reader",)) for pr in (PR, SECOND, third)), True
        )
        await DiffButler(queue, forge).sync()
        snapshot = await queue.snapshot(ACCOUNT)
        key = next(
            str(item.task_id) for item in snapshot.items if item.pull_request.ref.number == 3
        )
        app.chosen_saved(key, "pending")
        await app.reload()
        assert rung == [] and app.chosen_states == {}
        forge.discovery = Discovery(
            tuple(RequestedPR(pr, ("user:reader",)) for pr in (PR, SECOND, third, fourth)), True
        )
        await DiffButler(queue, forge).sync()
        await app.reload()
        assert len(rung) == 1


def test_bell_on_validates_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    assert load_settings(path, missing_ok=True).ui.bell_on == ["check", "check+"]
    path.write_text('[ui]\nbell_on = ["queued"]\n')
    with pytest.raises(ConfigError, match=r"ui\.bell_on.*valid states: pending, approve"):
        load_settings(path)
    path.write_text('[ui]\nbell_on = ["approve"]\n')
    monkeypatch.setenv("DIFFBUTLER_UI__BELL_ON", "[]")
    assert load_settings(path).ui.bell_on == []


@pytest.mark.serial
@pytest.mark.asyncio
async def test_cancel_and_expired_choices(
    ledger: tuple[SQLiteQueueStore, FakeForge, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, path = ledger
    path.write_text(path.read_text() + '[ui]\nbell_on = ["pending", "approve"]\n')
    states = {1: "AI_RUNNING", 2: "AI_RUNNING"}
    staged(monkeypatch, states)
    app = Dashboard(path, load_settings(path), offline=True)
    rung = bells(app, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.reload()
        # Ctrl+C returns the selected PR to pending without a bell.
        row = app.selected()
        assert row is not None
        number = row.row.tracked.pull_request.ref.number

        async def cancel(path: Path | None, task_id: object) -> None:
            states[number] = "PENDING_AI_REVIEW"

        monkeypatch.setattr("diffbutler.ui.tui.dashboard.cancel_task_configured", cancel)
        await pilot.press("ctrl+c")
        await asyncio.wait_for(
            asyncio.gather(*(worker.wait() for worker in app.workers)), timeout=5
        )
        await app.reload()
        assert rung == [] and app.chosen_states == {}
        # A choice the next read does not show is settled; it silences nothing later.
        other = next(key for key in app.rows if key != app.selected_key())
        app.chosen_saved(other, "approve")
        await app.reload()
        assert app.chosen_states == {}
        states[3 - number] = "PENDING_HUMAN_REVIEW"
        await app.reload()
        assert len(rung) == 1


@pytest.mark.serial
@pytest.mark.asyncio
async def test_pr_selected_with_R_stays_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_pr_lookup import LookupForge, config, install_forge

    from diffbutler.domain.review import ReviewConflict

    forge = LookupForge()
    forge.prs[REF] = PR
    install_forge(monkeypatch, forge)
    path = config(tmp_path, ["acme/api"])
    path.write_text(path.read_text() + '[ui]\nbell_on = ["pending"]\n')

    async def review(*args: object) -> None:
        raise ReviewConflict("Synthetic review stopped")

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.review_configured", review)
    app = Dashboard(path, load_settings(path), offline=True)
    rung = bells(app, monkeypatch)
    async with app.run_test() as pilot:
        await app.reload()
        assert not app.rows and app.bell_scope == ("active", "")
        await pilot.press("R", *PR.url, "enter")
        async with asyncio.timeout(5):
            while not app.rows or app.review_tasks:
                await pilot.pause()
        await app.reload()
        assert rung == [] and app.chosen_states == {}


@pytest.mark.serial
@pytest.mark.asyncio
async def test_pr_followed_with_b_stays_silent(
    ledger: tuple[SQLiteQueueStore, FakeForge, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_pr_lookup import LookupForge

    from diffbutler.config.settings import Forge
    from diffbutler.ui.tui.browse import BrowseScreen

    _, forge, path = ledger
    third = replace(PR, ref=replace(REF, number=3), title="Third", url=PR.url[:-1] + "3")
    forge.prs[third.ref] = third
    path.write_text(
        path.read_text()
        + '[ui]\nbell_on = ["pending"]\n'
        + '[[forges]]\nkind="github"\nbase_url="https://api.github.com"\ntoken_env="UNUSED"\n'
    )

    lookup = LookupForge()  # Resolves the followed PR's URL.
    lookup.prs, lookup.discovery = forge.prs, forge.discovery

    async def open_fake(_: Forge) -> FakeForge:
        return lookup

    monkeypatch.setattr("diffbutler.core.application.open_forge", open_fake)
    app = Dashboard(path, load_settings(path), offline=True)
    rung = bells(app, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.reload()
        assert len(app.rows) == 2
        await pilot.press("b")
        await pilot.pause()
        browse = app.screen
        assert isinstance(browse, BrowseScreen)
        await pilot.press("enter")  # Search.
        async with asyncio.timeout(5):
            while browse.query_one(DataTable).row_count != 3:
                await pilot.pause()
        await pilot.press("down", "down", "enter")
        async with asyncio.timeout(5):
            while app.screen is browse or len(app.rows) != 3:
                await pilot.pause()
        await app.reload()
        assert rung == [] and app.chosen_states == {}


@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.parametrize("saved_first", [True, False])
async def test_choice_saved_during_a_read_stays_silent(
    ledger: tuple[SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
    saved_first: bool,
) -> None:
    """A read that began before the choice was saved shows the old state; it settles nothing."""
    _, _, path = ledger
    path.write_text(path.read_text() + '[ui]\nbell_on = ["pending"]\n')
    states = {1: "AI_RUNNING", 2: "AI_RUNNING"}
    staged(monkeypatch, states)
    app = Dashboard(path, load_settings(path), offline=True)
    rung = bells(app, monkeypatch)
    async with app.run_test():
        await app.reload()
        staged_rows = app.rows
        reading, release = asyncio.Event(), asyncio.Event()

        async def stale(
            path: Path | None, view: str = "active", text: str = ""
        ) -> tuple[DashboardRow, ...]:
            reading.set()
            await release.wait()
            return tuple(staged_rows.values())  # Read before the choice was saved.

        monkeypatch.setattr("diffbutler.ui.tui.dashboard.dashboard_rows", stale)
        read = asyncio.create_task(app.reload())
        await reading.wait()
        key = next(iter(app.rows))
        number = app.rows[key].row.tracked.pull_request.ref.number

        saving = asyncio.Event()

        async def cancel() -> None:
            await saving.wait()
            states[number] = "PENDING_AI_REVIEW"

        choice = asyncio.create_task(app.choose_state(key, "pending", cancel()))
        first, second = (saving, release) if saved_first else (release, saving)
        first.set()
        await (choice if saved_first else read)
        second.set()
        await asyncio.gather(read, choice)
        assert rung == [] and key in app.chosen_states
        staged(monkeypatch, states)
        await app.reload()
        assert rung == [] and app.chosen_states == {}
