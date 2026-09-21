"""Host identity parsing fails closed without depending on real machine IDs."""

from pathlib import Path
from uuid import UUID

import pytest

from diffbutler.domain.process import HostBoot
from diffbutler.reviewers import process

pytestmark = pytest.mark.asyncio
HOST = "12345678-1234-5678-9abc-123456789abc"
BOOT = "abcdef01-1234-5678-9abc-123456789abc"


async def test_macos_identity_uses_platform_and_boot_session_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process.sys, "platform", "darwin")
    calls: list[tuple[str, ...]] = []

    async def read(*argv: str) -> str:
        calls.append(argv)
        return f'"IOPlatformUUID" = "{HOST}"' if len(calls) == 1 else BOOT + "\n"

    monkeypatch.setattr(process, "_read_command", read)
    assert await process.LocalHostBoot().current() == HostBoot(UUID(HOST), UUID(BOOT))
    assert calls == [
        ("/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"),
        ("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"),
    ]


@pytest.mark.parametrize(
    ("host", "boot"),
    [
        ("", BOOT),
        ('"IOPlatformUUID" = "invalid"', BOOT),
        (f'"IOPlatformUUID" = "{HOST}"\n"IOPlatformUUID" = "{HOST}"', BOOT),
        ('"IOPlatformUUID" = "00000000-0000-0000-0000-000000000000"', BOOT),
        (f'"IOPlatformUUID" = "{HOST}"', "invalid"),
        (f'"IOPlatformUUID" = "{HOST}"', "00000000-0000-0000-0000-000000000000"),
    ],
)
async def test_macos_missing_or_invalid_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch, host: str, boot: str
) -> None:
    monkeypatch.setattr(process.sys, "platform", "darwin")

    async def read(*argv: str) -> str:
        return host if argv[0].endswith("ioreg") else boot

    monkeypatch.setattr(process, "_read_command", read)
    with pytest.raises(process.HostBootError, match="Cannot verify"):
        await process.LocalHostBoot().current()


@pytest.mark.parametrize("error", [OSError("private"), TimeoutError("private")])
async def test_macos_command_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(process.sys, "platform", "darwin")

    async def read(*argv: str) -> str:
        raise error

    monkeypatch.setattr(process, "_read_command", read)
    with pytest.raises(process.HostBootError) as caught:
        await process.LocalHostBoot().current()
    assert "private" not in str(caught.value)


async def test_linux_identity_remains_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process.sys, "platform", "linux")

    def read(path: Path, **kwargs: object) -> str:
        return HOST if str(path) == "/etc/machine-id" else BOOT

    monkeypatch.setattr(Path, "read_text", read)
    assert await process.LocalHostBoot().current() == HostBoot(UUID(HOST), UUID(BOOT))


async def test_unsupported_platform_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process.sys, "platform", "unsupported")
    with pytest.raises(process.HostBootError):
        await process.LocalHostBoot().current()
