"""Compare the installed DiffButler with the latest release published on PyPI."""

import re
from dataclasses import dataclass
from importlib.metadata import version
from typing import cast

import httpx

PYPI_URL = "https://pypi.org/pypi/diffbutler/json"
CHECK_INTERVAL_S = 24 * 60 * 60
RELEASE = re.compile(r"\d+(?:\.\d+)*")
# PyPI reports the newest final release, which may be a post-release.
FINAL = re.compile(r"\d+(?:\.\d+)*(?:\.post\d+)?")


class UpdateCheckError(Exception):
    """PyPI could not be reached or returned no usable version."""


@dataclass(frozen=True)
class UpdateCheck:
    installed: str
    latest: str

    @property
    def available(self) -> bool:
        """Compare release numbers only, ignoring suffixes on either version.

        So a 0.9.0rc1 installation is not told about 0.9.0, nor 0.9.0 about 0.9.0.post1.
        """
        return numbers(self.latest) > numbers(self.installed)


def numbers(value: str) -> tuple[int, ...]:
    """Leading release numbers without trailing zeros, so 1.0 equals 1.0.0."""
    match = RELEASE.match(value)
    parts = [int(part) for part in match.group().split(".")] if match else []
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


async def latest_release(transport: httpx.AsyncBaseTransport | None = None) -> str:
    try:
        async with httpx.AsyncClient(
            timeout=10,
            headers={"Accept": "application/json", "User-Agent": "DiffButler"},
            transport=transport,
        ) as client:
            response = await client.get(PYPI_URL)
            response.raise_for_status()
            data: object = response.json()
    except httpx.HTTPStatusError as exc:
        raise UpdateCheckError(f"PyPI returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise UpdateCheckError(f"PyPI unreachable ({type(exc).__name__})") from exc
    except ValueError as exc:
        raise UpdateCheckError("PyPI returned invalid JSON") from exc
    info = cast("dict[str, object]", data).get("info") if isinstance(data, dict) else None
    latest = cast("dict[str, object]", info).get("version") if isinstance(info, dict) else None
    if not isinstance(latest, str) or not latest:
        raise UpdateCheckError("PyPI response has no version")
    if not FINAL.fullmatch(latest):
        raise UpdateCheckError("PyPI reported an unrecognized version")
    return latest


async def check_for_update(transport: httpx.AsyncBaseTransport | None = None) -> UpdateCheck:
    installed = version("diffbutler")
    if not RELEASE.match(installed):
        raise UpdateCheckError("Installed version is unrecognized")
    return UpdateCheck(installed=installed, latest=await latest_release(transport))
