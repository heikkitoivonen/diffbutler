"""Offline result persistence, ownership, and attention-race acceptance tests."""

import asyncio
import json
import sys
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import select
from tests.test_review_claims import seed, service
from tests.test_review_parsing import finding, payload
from tests.test_sync import ACCOUNT, NOW, PR, REF

from diffbutler.core.review import ReviewResult as ResultDTO
from diffbutler.core.sync import DiffButler
from diffbutler.domain.review import ReviewConflict, RunStatus
from diffbutler.domain.sync import RequestEvent, ReviewActivity
from diffbutler.reviewers.base import ReviewOutput
from diffbutler.store.base import SyncConflict
from diffbutler.store.database import Database, migration_config
from diffbutler.store.models import AIRun, ReviewResult, ReviewSession, RunClaim
from diffbutler.store.queue import SQLiteQueueStore

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncGenerator[Database]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/results.db")
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


def captured(*findings: dict[str, object], exit_code: int = 0) -> ReviewOutput:
    document = payload()
    document["findings"] = list(findings)
    return ReviewOutput(json.dumps(document).encode(), b"synthetic stderr", exit_code, "fake-id")


@pytest.mark.parametrize("has_findings", [False, True])
async def test_finalize_routes_and_survives_restart(database: Database, has_findings: bool) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    assert await reviews.get_result(run.id) is None
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    output = captured(finding()) if has_findings else captured()
    assert (await reviews.finalize_review(claim.token, output)).status == RunStatus.SUCCEEDED
    reopened = Database(str(database.url))
    try:
        await reopened.initialize(auto_migrate=False)
        restored = service(reopened)
        result = await restored.get_result(run.id)
        assert result is not None and result.document is not None
        assert result.stdout == output.stdout and result.stderr == output.stderr
        assert result.parse_tier == "json" and result.error is None
        assert result.document.orientation.intent == "Reject expired sessions."
        assert result.selected_indices == ((0,) if has_findings else ())
        assert (await restored.get_session(run.session_id)).backend_session_id == "fake-id"
        snapshot = await SQLiteQueueStore(reopened).snapshot(ACCOUNT)
        assert snapshot.items[0].state == (
            "FINDINGS_TRIAGE" if has_findings else "PENDING_HUMAN_REVIEW"
        )
        async with reopened.sessions() as db:
            assert await db.get(RunClaim, claim.token) is None
            stored = await db.get(AIRun, run.id)
            assert stored is not None and stored.parse_status == "succeeded"
        with pytest.raises(ValueError):
            result.document.orientation.intent = "changed"
        retry = await restored.request_review(identity)
        assert retry.attempt == 2 and retry.session_id == run.session_id
    finally:
        await reopened.close()


@pytest.mark.parametrize("kind", ["malformed", "nonzero", "encoding"])
async def test_failed_output_is_retained_and_waits_for_explicit_retry(
    database: Database, kind: str
) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    output = {
        "malformed": ReviewOutput(b'{"findings": []}', b"private diagnostic", 0),
        "nonzero": captured(exit_code=23),
        "encoding": ReviewOutput(b"\xff", b"", 0),
    }[kind]
    finished = await reviews.finalize_review(claim.token, output)
    assert finished.status == RunStatus.FAILED and finished.finished_at is not None
    result = await reviews.get_result(run.id)
    assert result is not None and result.document is None and result.error
    assert result.stdout == output.stdout and result.stderr == output.stderr
    assert result.exit_code == output.exit_code and result.selected_indices == ()
    assert ResultDTO.model_validate_json(result.model_dump_json()) == result
    state = (await SQLiteQueueStore(database).snapshot(ACCOUNT)).items[0].state
    assert state == "PENDING_AI_REVIEW"
    async with database.sessions() as db:
        stored = await db.get(AIRun, run.id)
        assert stored is not None and stored.parse_status == "failed"


async def test_thresholds_retain_all_findings_and_normalization_notes(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    await reviews.finalize_review(
        claim.token,
        captured(
            finding(severity="nit"),
            finding(confidence=0.49),
            finding(severity="unknown", category="unknown", confidence=0.5),
            finding(severity="question"),
        ),
    )
    result = await reviews.get_result(run.id)
    assert result is not None and result.document is not None
    assert len(result.document.findings) == 4 and result.selected_indices == (2,)
    assert len(result.notes) == 2
    assert result.min_confidence == 0.5 and result.findings_threshold == "minor"
    retry = await reviews.request_review(identity)
    second = await reviews.claim_review(retry.id)
    assert second is not None
    await reviews.finalize_review(second.token, captured(finding()), findings_threshold="blocking")
    state = (await SQLiteQueueStore(database).snapshot(ACCOUNT)).items[0].state
    assert state == "FINDINGS_TRIAGE"
    assert (await reviews.get_result(run.id)) == result


@pytest.mark.parametrize("change", ["push", "request", "closed", "cancel", "base"])
async def test_late_results_do_not_replace_new_attention(database: Database, change: str) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    if change == "cancel":
        await reviews.cancel_review(run.id)
    else:
        if change == "push":
            forge.prs[REF] = replace(PR, head_sha="new-head")
        elif change == "base":
            forge.prs[REF] = replace(PR, base_sha="new-base")
        elif change == "request":
            forge.activity = ReviewActivity(
                ACCOUNT, requests=(RequestEvent("renewed", NOW + timedelta(seconds=1)),)
            )
        else:
            forge.prs[REF] = replace(PR, state="closed")
        await DiffButler(SQLiteQueueStore(database), forge).sync()
    before = await reviews.get_run(run.id)
    finished = await reviews.finalize_review(claim.token, captured(finding()))
    expected = RunStatus.CANCELLED if change in {"closed", "cancel"} else RunStatus.STALE
    assert finished.status == expected
    if before.status != RunStatus.RUNNING:
        assert finished.finished_at == before.finished_at
    snapshot = await SQLiteQueueStore(database).snapshot(ACCOUNT)
    assert snapshot.items[0].state == ("DONE" if change == "closed" else "PENDING_AI_REVIEW")
    assert await reviews.get_result(run.id) is not None
    async with database.sessions() as db:
        assert await db.get(RunClaim, claim.token) is None


async def test_finalization_invalidates_old_sync_snapshot(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    queue = SQLiteQueueStore(database)
    old = await queue.snapshot(ACCOUNT)
    await reviews.finalize_review(claim.token, captured())
    with pytest.raises(SyncConflict):
        await queue.apply(ACCOUNT, old.version, old.items)


async def test_concurrent_finalization_has_one_winner(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    attempts = await asyncio.gather(
        reviews.finalize_review(claim.token, captured()),
        service(database).finalize_review(claim.token, captured(finding())),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ReviewConflict) for item in attempts) == 1
    result = await reviews.get_result(run.id)
    assert result is not None
    snapshot = await SQLiteQueueStore(database).snapshot(ACCOUNT)
    assert snapshot.items[0].state == (
        "FINDINGS_TRIAGE" if result.selected_indices else "PENDING_HUMAN_REVIEW"
    )
    with pytest.raises(ReviewConflict, match="claim"):
        await reviews.finalize_review(claim.token, captured())
    assert await reviews.get_result(run.id) == result


async def test_bad_token_and_released_claim_cannot_write(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    with pytest.raises(ReviewConflict, match="claim"):
        await reviews.finalize_review(uuid4(), captured())
    await reviews.release_review(claim.token)
    with pytest.raises(ReviewConflict, match="claim"):
        await reviews.finalize_review(claim.token, captured())
    assert await reviews.get_result(run.id) is None


async def test_binding_conflict_rolls_back_result_and_keeps_claim(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    await reviews.bind_backend_session(claim.token, "original")
    with pytest.raises(ReviewConflict, match="replaced"):
        await reviews.finalize_review(claim.token, captured())
    assert await reviews.get_result(run.id) is None
    assert (await reviews.get_run(run.id)).status == RunStatus.RUNNING
    async with database.sessions() as db:
        assert await db.get(RunClaim, claim.token) is not None
    await reviews.finalize_review(claim.token, replace(captured(), backend_session_id="original"))


async def test_cleaned_session_cannot_receive_output(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    async with database.sessions() as db:
        session = await db.get(ReviewSession, run.session_id)
        assert session is not None
        session.status = "cleaned"
        await db.commit()
    with pytest.raises(ReviewConflict, match="cleaned"):
        await reviews.finalize_review(claim.token, captured())
    assert await reviews.get_result(run.id) is None


async def test_invalid_policy_keeps_ownership(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    for confidence in (-0.1, 1.1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="confidence"):
            await reviews.finalize_review(claim.token, captured(), min_confidence=confidence)
    assert await reviews.get_result(run.id) is None
    async with database.sessions() as db:
        assert await db.get(RunClaim, claim.token) is not None
        stored = await db.get(AIRun, run.id)
        assert stored is not None and stored.stdout_blob is None


async def test_analysis_survives_raw_output_removal(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    await reviews.finalize_review(claim.token, captured(finding()))
    async with database.sessions() as db:
        stored = await db.get(AIRun, run.id)
        assert stored is not None
        stored.stdout_blob = stored.stderr_blob = None  # Simulate future raw-output cleanup.
        await db.commit()
    result = await reviews.get_result(run.id)
    assert result is not None and result.document is not None
    assert result.stdout is None and result.stderr is None
    assert len(result.document.findings) == 1


async def test_upgrade_0004_preserves_worker_claim(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    async with database.engine.begin() as connection:
        await connection.run_sync(lambda conn: command.downgrade(migration_config(conn), "0004"))
    status = await database.initialize()
    assert status.revision == "0009" and status.backup is not None
    await reviews.finalize_review(claim.token, captured())
    async with database.sessions() as db:
        assert (await db.scalars(select(ReviewResult.run_id))).all() == [run.id]


async def test_fake_executable_exit_then_finalize(database: Database, tmp_path: Path) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(Path(__file__).parent / "fixtures" / "fake_review_cli.py"),
        cwd=tmp_path,
        env={},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(5):
            stdout, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    assert process.returncode == 0
    await reviews.finalize_review(claim.token, ReviewOutput(stdout, stderr, process.returncode))
    result = await reviews.get_result(run.id)
    assert result is not None and result.document is not None
    assert result.document.findings[0].title == "Addition subtracts"
    assert result.stderr == b"synthetic diagnostic\n"
    assert (await reviews.get_run(run.id)).status == RunStatus.SUCCEEDED


async def test_old_cycle_result_cannot_overwrite_reopened_task(database: Database) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    queue = SQLiteQueueStore(database)
    sync = DiffButler(queue, forge)
    forge.prs[REF] = replace(PR, state="closed")
    await sync.sync()
    forge.prs[REF] = replace(PR, head_sha="new-head")
    await sync.follow_pr(REF)
    before = await queue.snapshot(ACCOUNT)
    assert before.items[0].task_id != identity.task_id
    assert before.items[0].state == "PENDING_AI_REVIEW"
    finished = await reviews.finalize_review(claim.token, captured(finding()))
    assert finished.status == RunStatus.CANCELLED
    after = await queue.snapshot(ACCOUNT)
    assert after == before
    assert await reviews.get_result(run.id) is not None


async def test_anchor_results_survive_restart_and_keep_demoted_findings(database: Database) -> None:
    from diffbutler.domain.diff import DiffIndex

    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    index = DiffIndex.parse(
        "diff --git a/src/auth.py b/src/auth.py\n--- a/src/auth.py\n+++ b/src/auth.py\n"
        "@@ -10,2 +10,2 @@\n context\n-old\n+new\n",
        base_sha=run.base_sha,
        head_sha=run.head_sha,
    )
    first = finding() | {"path": "src/auth.py", "line": 11, "start_line": 10}
    second = finding() | {"path": "src/auth.py", "line": 1000, "start_line": None}
    await reviews.finalize_review(claim.token, captured(first, second), diff_index=index)
    reopened = Database(str(database.url))
    try:
        await reopened.initialize(auto_migrate=False)
        result = await service(reopened).get_result(run.id)
        assert result is not None and result.document is not None and result.anchors is not None
        assert [item.status for item in result.anchors] == ["exact", "demoted"]
        assert result.anchors[1].original.line == 1000
        assert result.anchors[1].anchor is None
        assert len(result.document.findings) == 2 and result.selected_indices == (0, 1)
        assert ResultDTO.model_validate_json(result.model_dump_json()) == result
    finally:
        await reopened.close()


async def test_wrong_revision_anchor_index_preserves_claim(database: Database) -> None:
    from diffbutler.domain.diff import DiffIndex

    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    with pytest.raises(ReviewConflict, match="index revisions"):
        await reviews.finalize_review(claim.token, captured(), diff_index=DiffIndex.parse(""))
    assert await reviews.get_result(run.id) is None
    async with database.sessions() as db:
        assert await db.get(RunClaim, claim.token) is not None
    await reviews.finalize_review(claim.token, captured())
    result = await reviews.get_result(run.id)
    assert result is not None and result.anchors is None


async def test_envelope_finalization_retains_raw_bytes_and_session(database: Database) -> None:
    from diffbutler.reviewers.extraction import EnvelopeFormat

    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    raw = json.dumps({"result": json.dumps(payload()), "session_id": "envelope-id"}).encode()
    await reviews.finalize_review(
        claim.token, ReviewOutput(raw, b"", 0), output_format=EnvelopeFormat()
    )
    result = await reviews.get_result(run.id)
    assert result is not None and result.document is not None and result.stdout == raw
    assert (await reviews.get_session(run.session_id)).backend_session_id == "envelope-id"
