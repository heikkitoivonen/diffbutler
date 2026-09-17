"""Fake launcher tests are lifecycle evidence, not evidence of OS confinement."""

import asyncio
import json
import os
import select
import shutil
import signal
import socket
import sys
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import pytest

from diffbutler.reviewers.sandbox import OfflineSandbox, SandboxError, SandboxLimits


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    root = tmp_path / "image"
    root.mkdir()
    for name in ("tmp", "home", "work"):
        (root / name).mkdir()
    return root


def fake_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    script = tmp_path / "fake-bwrap"
    script.write_text(f"#!{sys.executable}\n" + body)
    script.chmod(0o700)

    def which(*_args: object, **_kwargs: object) -> str:
        return str(script)

    monkeypatch.setattr("diffbutler.reviewers.sandbox.shutil.which", which)
    monkeypatch.setattr("diffbutler.reviewers.sandbox.sys.platform", "linux")
    return script


@pytest.mark.asyncio
async def test_launch_policy_and_literal_input(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_launcher(
        tmp_path,
        monkeypatch,
        """
import json, os, sys
print(json.dumps(dict(argv=sys.argv[1:], env=dict(os.environ), cwd=os.getcwd(),
                     tty=[os.isatty(fd) for fd in range(3)], data=sys.stdin.read())))
""",
    )
    for key in ("GITHUB_TOKEN", "SSH_AUTH_SOCK", "HTTP_PROXY", "PYTHONPATH", "HOME"):
        monkeypatch.setenv(key, "synthetic-private-value")
    value = "$(touch escaped); --share-net"
    output = await OfflineSandbox().run(runtime, ("/probe", value), input_data=b"private prompt")
    result = json.loads(output.stdout)
    args = result["argv"]
    assert args[-3:] == ["--", "/probe", value]
    for flag in (
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
    ):
        assert flag in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--ro-bind") + 1 : args.index("--ro-bind") + 3] == [str(runtime), "/"]
    assert args.count("--ro-bind") == 1
    assert "--bind" not in args and "--share-net" not in args
    assert "--proc" not in args and "--dev" not in args
    assert [args[i + 1] for i, arg in enumerate(args) if arg == "--tmpfs"] == [
        "/tmp",
        "/home",
        "/work",
    ]
    assert "synthetic-private-value" not in str(result)
    assert result["cwd"] == "/" and result["tty"] == [False, False, False]
    assert result["data"] == "private prompt"
    assert not (tmp_path / "escaped").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,available", [("darwin", True), ("linux", False)])
async def test_unavailable_isolation_never_launches(
    runtime: Path, monkeypatch: pytest.MonkeyPatch, platform: str, available: bool
) -> None:
    monkeypatch.setattr("diffbutler.reviewers.sandbox.sys.platform", platform)

    def which(*_args: object, **_kwargs: object) -> str | None:
        return "/missing" if available else None

    monkeypatch.setattr("diffbutler.reviewers.sandbox.shutil.which", which)

    async def forbidden(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        pytest.fail("unavailable isolation must not spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(SandboxError, match=r"Linux|unavailable"):
        await OfflineSandbox().run(runtime, ("/probe",))


@pytest.mark.asyncio
async def test_failure_diagnostics_do_not_expose_output(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_launcher(
        tmp_path,
        monkeypatch,
        "import sys\nprint('synthetic secret', file=sys.stderr)\nsys.exit(23)\n",
    )
    with pytest.raises(SandboxError, match=r"^Isolated execution failed$"):
        await OfflineSandbox().run(runtime, ("/probe",))


@pytest.mark.asyncio
async def test_launcher_disappearing_fails_closed(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = fake_launcher(tmp_path, monkeypatch, "raise AssertionError\n")
    launcher.unlink()
    with pytest.raises(SandboxError, match="Could not start isolated execution"):
        await OfflineSandbox().run(runtime, ("/probe",))


@pytest.mark.asyncio
async def test_combined_output_budget(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_launcher(
        tmp_path, monkeypatch, "import os\nos.write(1, b'a' * 80)\nos.write(2, b'b' * 80)\n"
    )
    with pytest.raises(SandboxError, match="output exceeds"):
        await OfflineSandbox().run(runtime, ("/probe",), limits=SandboxLimits(max_output_bytes=100))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_reap_worker(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    marker = tmp_path / "pid"
    fake_launcher(
        tmp_path,
        monkeypatch,
        f"""
import os, pathlib, time
pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))
time.sleep(60)
""",
    )
    task = asyncio.create_task(
        OfflineSandbox().run(
            runtime,
            ("/probe",),
            input_data=b"x" * 500_000,
            limits=SandboxLimits(timeout_s=10 if cancel else 0.3),
        )
    )
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(0.01)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(SandboxError, match="timed out"):
            await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


@pytest.mark.asyncio
async def test_cancellation_during_spawn_reaps_late_process(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_launcher(tmp_path, monkeypatch, "import os\nos.write(1, b'x' * 2_000_000)\n")
    spawn = asyncio.create_subprocess_exec
    ready, release = asyncio.Event(), asyncio.Event()
    children: list[asyncio.subprocess.Process] = []

    async def delayed(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        # The wrapper retains the real call's signature through a local explicit call.
        child = await spawn(
            *args,
            cwd="/",
            env={"PATH": os.defpath},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        children.append(child)
        ready.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    task = asyncio.create_task(OfflineSandbox().run(runtime, ("/probe",)))
    await ready.wait()
    await asyncio.sleep(0.1)  # Fill and pause stdout before collectors have been created.
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    async with asyncio.timeout(5):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert children[0].returncode is not None


@pytest.mark.asyncio
async def test_bad_image_and_input_fail_before_launch(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_launcher(tmp_path, monkeypatch, "raise AssertionError('must not execute')\n")
    with pytest.raises(SandboxError, match="input exceeds"):
        await OfflineSandbox().run(
            runtime, ("/probe",), input_data=b"ab", limits=SandboxLimits(max_input_bytes=1)
        )
    for argv in ((), ("relative",), ("/../probe",), ("/probe", "\x00")):
        with pytest.raises(ValueError):
            await OfflineSandbox().run(runtime, argv)
    (runtime / "home").rmdir()
    (runtime / "home").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SandboxError, match="dedicated runtime"):
        await OfflineSandbox().run(runtime, ("/probe",))
    with pytest.raises(SandboxError, match="dedicated runtime"):
        await OfflineSandbox().run(Path("/"), ("/probe",))


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_limits(timeout: float) -> None:
    with pytest.raises(ValueError):
        SandboxLimits(timeout_s=timeout)


@pytest.fixture
def kernel_compiler(request: pytest.FixtureRequest) -> str:
    compiler = shutil.which("cc", path=os.defpath)
    missing: list[str] = []
    if sys.platform != "linux":
        missing.append("Linux")
    if shutil.which("bwrap", path=os.defpath) is None:
        missing.append("Bubblewrap")
    if compiler is None:
        missing.append("C compiler")
    if missing:
        reason = f"Kernel confinement unvalidated; missing {', '.join(missing)}"
        if request.config.getoption("--require-kernel-sandbox"):
            pytest.fail(reason)
        pytest.skip(reason)
    assert compiler is not None
    return compiler


async def compile_probe(compiler: str, runtime: Path, filename: str) -> None:
    child = await asyncio.create_subprocess_exec(
        compiler,
        "-static",
        "-Wall",
        "-Wextra",
        "-Werror",
        str(Path(__file__).parent / "fixtures" / filename),
        "-o",
        str(runtime / "probe"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, errors = await child.communicate()
    assert child.returncode == 0, errors.decode()


@pytest.mark.asyncio
async def test_real_kernel_confinement(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kernel_compiler: str
) -> None:
    await compile_probe(kernel_compiler, runtime, "sandbox_probe.c")
    private = tmp_path / "synthetic-credential"
    private.write_text("synthetic-private-value")
    (runtime / "escape").symlink_to(private)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-private-value")
    # Real connections are attempted only to disposable local test listeners.
    with socket.socket() as listener, socket.socket(socket.AF_UNIX) as agent:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        abstract = f"diffbutler-test-{os.getpid()}-{tmp_path.name}"
        agent.bind("\0" + abstract)
        agent.listen()
        with private.open() as inherited:
            os.set_inheritable(inherited.fileno(), True)
            for _ in range(2):
                output = await OfflineSandbox().run(
                    runtime,
                    (
                        "/probe",
                        str(private),
                        str(listener.getsockname()[1]),
                        abstract,
                        str(inherited.fileno()),
                    ),
                )
                assert output.stdout == b"isolated\n"
    assert private.read_text() == "synthetic-private-value"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "failure", "timeout", "cancel", "overflow"])
async def test_real_kernel_detached_descendant(
    runtime: Path, kernel_compiler: str, mode: str
) -> None:
    await compile_probe(kernel_compiler, runtime, "sandbox_descendant.c")
    # A unique kernel comm identifies our synthetic descendant in the host PID
    # namespace. pidfd then lets us observe death without races from PID reuse.
    name = "db-" + uuid4().hex[:12]
    task = asyncio.create_task(
        OfflineSandbox().run(
            runtime,
            ("/probe", name, mode),
            limits=SandboxLimits(timeout_s=3, max_output_bytes=1024),
        )
    )
    descriptor: int | None = None
    try:
        async with asyncio.timeout(10):
            while descriptor is None:
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        if (entry / "comm").read_text().strip() == name:
                            descriptor = os.pidfd_open(int(entry.name))
                            break
                    except FileNotFoundError, ProcessLookupError, PermissionError:
                        continue
                if descriptor is None:
                    if task.done():
                        await task  # Surface setup failures rather than a polling timeout.
                        pytest.fail("Probe exited before its detached descendant was observed")
                    await asyncio.sleep(0.01)
            assert not select.select([descriptor], [], [], 0)[0]
            if mode == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif mode == "success":
                assert (await task).exit_code == 0
            else:
                message = {
                    "failure": "execution failed",
                    "timeout": "timed out",
                    "overflow": "output exceeds",
                }[mode]
                with pytest.raises(SandboxError, match=message):
                    await task
            # The descendant closed all stdio and detached twice. EOF and initial
            # process-group exit alone cannot establish that it was terminated.
            while not select.select([descriptor], [], [], 0)[0]:
                await asyncio.sleep(0.01)
    finally:
        if descriptor is not None:
            if not select.select([descriptor], [], [], 0)[0]:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            os.close(descriptor)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
