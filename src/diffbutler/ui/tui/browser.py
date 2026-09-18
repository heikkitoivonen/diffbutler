"""Browser dispatch stays at the UI boundary, with a copyable fallback URL."""

import asyncio
import os
import webbrowser
from urllib.parse import urlsplit


async def open_browser(url: str, command: list[str] | None = None) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        return False
    if command:
        process = await asyncio.create_subprocess_exec(
            *command,
            url,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(10):
                return await process.wait() == 0
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
    if os.environ.get("SSH_CONNECTION") or not (
        os.environ.get("BROWSER") or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False
    return await asyncio.to_thread(webbrowser.open, url)
