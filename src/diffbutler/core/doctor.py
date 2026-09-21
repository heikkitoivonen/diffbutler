"""Shared local and configured-forge diagnostics; never launches AI reviewers."""

import asyncio
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy.exc import SQLAlchemyError

from diffbutler.config.settings import ConfigError, Forge, load_settings
from diffbutler.forges.base import ForgeError, ReadForge
from diffbutler.forges.registry import open_forge
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
    forge_factory: Callable[[Forge], Awaitable[ReadForge]] = open_forge,
    offline: bool = False,
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
    if not settings.forges or offline:
        checks.append(
            Check(
                "forges",
                "SKIP",
                "Offline mode; forge checks skipped" if offline else "No forge configured",
            )
        )
    else:
        for index, configured in enumerate(settings.forges, start=1):
            name = f"forge:{index}:{configured.kind}"
            forge: ReadForge | None = None
            if configured.kind != "github":
                checks.append(Check(name, "SKIP", "GitLab adapter is not implemented"))
                continue
            try:
                forge = await forge_factory(configured)
                health = await forge.health()
                matches = (
                    configured.login is None
                    or configured.login.lower() == health.account.login.lower()
                )
                checks.append(
                    Check(
                        name,
                        "PASS" if matches else "FAIL",
                        (
                            f"Authenticated as {health.account.login}"
                            if configured.token_env is not None
                            else f"Public access for review requests to {health.account.login}"
                        )
                        if matches
                        else "Authenticated login does not match configured login",
                    )
                )
                if not health.limits:
                    checks.append(Check(f"{name}:quota", "WARN", "Rate-limit headers unavailable"))
                for limit in health.limits:
                    checks.append(
                        Check(
                            f"{name}:{limit.resource}",
                            "PASS" if limit.remaining > 0 else "WARN",
                            f"{limit.remaining}/{limit.limit} requests remaining",
                        )
                    )
                if health.teams is not None:
                    checks.append(
                        Check(
                            f"{name}:teams",
                            "PASS" if health.teams.complete else "WARN",
                            f"{len(health.teams.teams)} teams visible"
                            + (
                                "; " + "; ".join(health.teams.warnings)
                                if health.teams.warnings
                                else ""
                            ),
                        )
                    )
            except ForgeError as exc:
                detail = str(exc)
                if exc.retry_after is not None:
                    detail += f"; retry in {exc.retry_after:.0f}s"
                checks.append(Check(name, "FAIL", detail))
            finally:
                if forge is not None:
                    await forge.close()
    checks.append(
        Check(
            "reviewers",
            "SKIP",
            "Reviewer execution, parsing, resume, and cleanup are not checked; no agent launched"
            if settings.reviewers
            else "No reviewer configured",
        )
    )
    return DoctorReport(tuple(checks))
