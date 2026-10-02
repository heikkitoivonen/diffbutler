"""Owned native agents get time to release resources before forced termination."""

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from diffbutler.reviewers.native import CapturedProcess, stop_process_group


@pytest.mark.asyncio
@pytest.mark.serial
@pytest.mark.parametrize("cooperative", [True, False])
async def test_shutdown_allows_cleanup_but_bounds_unresponsive_agents(
    tmp_path: Path, cooperative: bool
) -> None:
    script = tmp_path / "agent.py"
    script.write_text(
        "import signal, time\n"
        "from pathlib import Path\n"
        "def stop(*args):\n"
        "    time.sleep(0.1)\n"
        "    Path('released').write_text('yes')\n"
        "    raise SystemExit(0)\n"
        f"signal.signal(signal.SIGTERM, {'stop' if cooperative else 'signal.SIG_IGN'})\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    execution = CapturedProcess()
    task = asyncio.create_task(
        execution.capture(
            (sys.executable, str(script)), cwd=tmp_path, env={"PATH": os.defpath}, input=b""
        )
    )
    try:
        async with asyncio.timeout(5):
            while b"ready" not in execution.stdout:
                await asyncio.sleep(0.01)
        assert execution.process is not None
        await asyncio.wait_for(execution.stop_and_wait(), 5)
        assert execution.process.returncode == (0 if cooperative else -signal.SIGKILL)
        assert (tmp_path / "released").exists() is cooperative
    finally:
        await execution.stop_and_wait()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.serial
async def test_shutdown_gives_descendant_time_after_launcher_exits(tmp_path: Path) -> None:
    script = tmp_path / "launcher.py"
    script.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "def stop(*args):\n"
        "    time.sleep(0.1)\n"
        "    Path('released').write_text('yes')\n"
        "    raise SystemExit(0)\n"
        "if os.fork() == 0:\n"
        "    signal.signal(signal.SIGTERM, stop)\n"
        "    Path('ready').touch()\n"
        "    while True: time.sleep(1)\n"
        "while not Path('ready').exists(): time.sleep(0.01)\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        cwd=tmp_path,
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), 5)
        await asyncio.wait_for(stop_process_group(process.pid), 5)
        assert (tmp_path / "released").read_text() == "yes"
    finally:
        await stop_process_group(process.pid, grace_s=0.01)
        await process.wait()
