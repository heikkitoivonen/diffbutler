"""Restart recovery uses synthetic boot identities and never kills real processes."""

from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import pytest
from tests.test_review_claims import seed, service
from tests.test_review_results import captured
from tests.test_sync import PR, REF

from diffbutler.core.process import ProcessRecovery
from diffbutler.core.sync import DiffButler
from diffbutler.domain.process import HostBoot
from diffbutler.domain.review import ReviewConflict
from diffbutler.store.database import Database
from diffbutler.store.lifecycle import SQLiteLifecycleStore
from diffbutler.store.process import SQLiteProcessStore
from diffbutler.store.queue import SQLiteQueueStore
from diffbutler.store.review import SQLiteReviewStore

pytestmark = pytest.mark.asyncio


class Host:
    def __init__(self) -> None:
        self.identity = HostBoot(uuid4(), uuid4())

    async def current(self) -> HostBoot:
        return self.identity


@pytest.mark.parametrize("kind", ["background", "foreground", "cleanup"])
async def test_restart_retains_same_boot_then_recovers_after_local_reboot(
    tmp_path: Path,
    kind: str,
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/process.db"
    database = Database(url)
    await database.initialize()
    host = Host()
    try:
        forge, identity = await seed(database)
        reviews = service(database)
        run = await reviews.request_review(identity)
        claim = await reviews.claim_review(run.id)
        assert claim is not None
        lifecycle = SQLiteLifecycleStore(database)
        if kind == "background":
            token = claim.token
        else:
            await reviews.finalize_review(claim.token, captured())
            if kind == "foreground":
                token = (await lifecycle.acquire_foreground(run.session_id)).token
            else:
                forge.prs[REF] = replace(PR, state="closed")
                await DiffButler(SQLiteQueueStore(database), forge).sync()
                cleanup = await lifecycle.claim_cleanup()
                assert cleanup is not None
                token = cleanup.token
        processes = SQLiteProcessStore(database)
        recovery = ProcessRecovery(processes, SQLiteReviewStore(database), lifecycle, host)
        operations = await lifecycle.held_operations()
        assert len(operations) == 1
        launch_kind = cast(Literal["background", "foreground", "cleanup"], kind)
        await recovery.prepare_launch(token, launch_kind)
        with pytest.raises(ReviewConflict, match="already exists"):
            await recovery.prepare_launch(token, launch_kind)
        await processes.bind_launcher(token, 12345)
        await processes.bind_launcher(token, 12345)
        with pytest.raises(ReviewConflict, match="replaced"):
            await processes.bind_launcher(token, 54321)
    finally:
        await database.close()
    reopened = Database(url)
    try:
        processes = SQLiteProcessStore(reopened)
        lifecycle = SQLiteLifecycleStore(reopened)
        recovery = ProcessRecovery(processes, SQLiteReviewStore(reopened), lifecycle, host)
        assert (saved := await processes.get_launch(token)) is not None
        assert saved.launcher_pid == 12345
        assert not await recovery.recover_rebooted()  # Missing PID is not tree exit.
        original = host.identity
        host.identity = HostBoot(uuid4(), uuid4())
        assert not await recovery.recover_rebooted()  # A database copied to another host.
        host.identity = HostBoot(original.host_id, uuid4())
        assert await recovery.recover_rebooted() == (token,)
        assert not await lifecycle.held_operations()
        assert not await recovery.recover_rebooted()
        with pytest.raises(ReviewConflict):
            await processes.bind_launcher(token, 12345)
    finally:
        await reopened.close()


async def test_legacy_claims_without_launch_intent_stay_held(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/process.db")
    await database.initialize()
    try:
        _, identity = await seed(database)
        reviews = service(database)
        run = await reviews.request_review(identity)
        claim = await reviews.claim_review(run.id)
        assert claim is not None
        lifecycle = SQLiteLifecycleStore(database)
        recovery = ProcessRecovery(
            SQLiteProcessStore(database),
            SQLiteReviewStore(database),
            lifecycle,
            Host(),
        )
        assert not await recovery.recover_rebooted()
        assert len(await lifecycle.held_operations()) == 1
        with pytest.raises(ReviewConflict):
            await recovery.prepare_launch(uuid4(), "background")
    finally:
        await database.close()
