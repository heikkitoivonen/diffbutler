"""Read local Linux host/boot identities; never signal processes by a stale PID."""

import asyncio
import sys
from pathlib import Path
from uuid import UUID

from diffbutler.domain.process import HostBoot


class LinuxHostBoot:
    """For a local, nonshared database on a host with a unique machine-id.

    Unsupported/missing host identity fails closed. This is not a confinement probe
    or a same-boot descendant supervisor. No reviewer process is started here.
    """

    async def current(self) -> HostBoot:
        if sys.platform != "linux":
            raise RuntimeError("Host boot verification is unavailable")

        def read() -> HostBoot:
            try:
                host = UUID(Path("/etc/machine-id").read_text(encoding="ascii").strip())
                boot = UUID(
                    Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
                )
            except OSError, ValueError:
                raise RuntimeError("Host boot verification is unavailable") from None
            if host.int == 0 or boot.int == 0:
                raise RuntimeError("Host boot verification is unavailable")
            return HostBoot(host, boot)

        return await asyncio.to_thread(read)
