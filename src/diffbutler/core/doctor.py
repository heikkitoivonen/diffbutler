"""Local M0 diagnostics shared by interfaces; no forge or AI calls."""

import asyncio
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy.exc import SQLAlchemyError

from diffbutler.config.settings import ConfigError, load_settings
from diffbutler.store.database import Database, DatabaseError, DatabaseStatus


class DiagnosticStore(Protocol):
    async def initialize(self, *, auto_migrate: bool = True) -> DatabaseStatus: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["PASS", "FAIL", "SKIP", "WARN"]
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.status != "FAIL" for check in self.checks)


async def run_doctor(
    path: Path | None = None,
    *,
    repo: str | None = None,
    overrides: dict[str, Any] | None = None,
    store_factory: Callable[[str], DiagnosticStore] = Database,
) -> DoctorReport:
    try:
        settings = await asyncio.to_thread(load_settings, path, repo=repo, overrides=overrides)
    except ConfigError as exc:
        return DoctorReport((Check("config", "FAIL", str(exc)),))
    checks = [Check("config", "PASS", "Configuration loaded and validated")]
    store: DiagnosticStore | None = None
    try:
        store = store_factory(settings.db.url)
        status = await store.initialize(auto_migrate=settings.db.auto_migrate)
        detail = f"Schema {status.revision} current"
        if status.migrated:
            detail += "; migrations applied"
        if status.backup is not None:
            detail += "; pre-migration backup saved beside database"
        checks.append(Check("database", "PASS", detail))
    except DatabaseError as exc:
        checks.append(Check("database", "FAIL", str(exc)))
    except OSError, SQLAlchemyError:
        checks.append(
            Check(
                "database",
                "FAIL",
                "Cannot initialize database; check file permissions, free space, "
                "locks, and SQLite file integrity",
            )
        )
    finally:
        if store is not None:
            await store.close()

    for name, repository in settings.repos.items():
        if repository.path is not None:
            exists = await asyncio.to_thread(repository.path.is_dir)
            checks.append(
                Check(
                    f"repo:{name}",
                    "PASS" if exists else "FAIL",
                    "Repository directory exists"
                    if exists
                    else "Configured repository directory is missing or inaccessible",
                )
            )
        if repository.prompt is not None:
            exists = await asyncio.to_thread(repository.prompt.is_file)
            checks.append(
                Check(
                    f"prompt:{name}",
                    "PASS" if exists else "FAIL",
                    "Prompt file exists"
                    if exists
                    else "Configured prompt file is missing or inaccessible",
                )
            )
    if settings.review.prompt is not None:
        exists = await asyncio.to_thread(settings.review.prompt.is_file)
        checks.append(
            Check(
                "prompt",
                "PASS" if exists else "FAIL",
                "Prompt file exists"
                if exists
                else "Configured prompt file is missing or inaccessible",
            )
        )

    opener = "open" if sys.platform == "darwin" else "xdg-open"
    resolved = await asyncio.to_thread(shutil.which, opener)
    checks.append(
        Check(
            "browser",
            "PASS" if resolved else "WARN",
            f"{opener} resolves (launch not tested)"
            if resolved
            else f"{opener} is not on PATH; browser launch unavailable",
        )
    )
    checks.append(
        Check(
            "forges",
            "SKIP",
            "Authentication and rate-limit checks deferred to M1"
            if settings.forges
            else "No forge configured",
        )
    )
    checks.append(
        Check(
            "reviewers",
            "SKIP",
            "Execution, parsing, resume, and cleanup checks deferred to M2; no agent launched"
            if settings.reviewers
            else "No reviewer configured",
        )
    )
    return DoctorReport(tuple(checks))
