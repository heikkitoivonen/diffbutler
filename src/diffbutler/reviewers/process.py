"""Read local host/boot identities; never signal processes by a stale PID."""

import asyncio
import re
import sys
from contextlib import suppress
from pathlib import Path
from uuid import UUID

from diffbutler.domain.process import HostBoot


class HostBootError(RuntimeError):
    """A safe user-facing error without command output or machine identifiers."""


async def _read_command(*argv: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(5):
            output, _ = await process.communicate()
        if process.returncode != 0:
            raise ValueError("Identity command failed")
        return output.decode("ascii")
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()


class LocalHostBoot:
    """For a local, nonshared database on a host with a unique machine identity.

    Unsupported/missing host identity fails closed. This is not a confinement probe
    or a same-boot descendant supervisor. No reviewer process is started here.
    """

    async def current(self) -> HostBoot:
        try:
            if sys.platform == "darwin":
                platform = await _read_command(
                    "/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"
                )
                identities = re.findall(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', platform)
                if len(identities) != 1:
                    raise ValueError("Missing or ambiguous platform identity")
                host = UUID(identities[0])
                boot = UUID(
                    (await _read_command("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid")).strip()
                )
            elif sys.platform == "linux":
                return await asyncio.to_thread(self._linux)
            else:
                raise ValueError("Unsupported platform")
            return self._identity(host, boot)
        except OSError, ValueError, TimeoutError:
            raise HostBootError(
                "Cannot verify this computer's host and boot identity; "
                "managed launch and cleanup are unavailable."
            ) from None

    @staticmethod
    def _linux() -> HostBoot:
        host = UUID(Path("/etc/machine-id").read_text(encoding="ascii").strip())
        boot = UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip())
        return LocalHostBoot._identity(host, boot)

    @staticmethod
    def _identity(host: UUID, boot: UUID) -> HostBoot:
        if host.int == 0 or boot.int == 0:
            raise ValueError("Empty host or boot identity")
        return HostBoot(host, boot)
