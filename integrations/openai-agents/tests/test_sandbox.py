from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest


if sys.platform == "win32":
    pytest.skip("The Hyperlight sandbox backend is unavailable on Windows", allow_module_level=True)

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxPathGrant, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import (
    File,
    GitRepo,
    InContainerMountStrategy,
    RcloneMountPattern,
    S3Mount,
)
from agents.sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    InvalidManifestPathError,
    WorkspaceStartError,
)
from agents.sandbox.manifest import Environment
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import Group, Permissions
from agents.testing import ScriptedModel, assistant_message, function_call

from openai_agents_hyperlight import (
    HyperlightSandboxClient,
    HyperlightSandboxClientOptions,
    HyperlightSandboxSession,
    HyperlightSandboxSessionState,
)
from openai_agents_hyperlight import sandbox as hyperlight_sandbox


def _runs(tmp_path: Path) -> list[dict[str, object]]:
    lines = (tmp_path / "hluk.log").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _command_runs(tmp_path: Path) -> list[dict[str, object]]:
    return [run for run in _runs(tmp_path) if "--exec" not in run["args"]]  # type: ignore[operator]


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return workspace


@pytest.mark.asyncio
async def test_exec_runs_in_the_guest_with_status_output_and_environment(tmp_path: Path, fake_hluk: Path) -> None:
    workspace = _workspace(tmp_path)
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    manifest = Manifest(root=str(workspace), environment=Environment(value={"TOKEN": "s3cr3t"}))
    session = await client.create(manifest=manifest)
    async with session:
        result = await session.exec('echo "$TOKEN $HOME $(pwd)"; echo oops >&2; exit 3')

    assert result.exit_code == 3
    assert result.stdout == f"s3cr3t {workspace} {workspace}\n".encode()
    assert result.stderr == b"oops\n"
    run = _command_runs(tmp_path)[0]
    args = run["args"]
    assert isinstance(args, list)
    # The environment reaches the guest in an owner-only file, never on the command line.
    assert not any("s3cr3t" in arg for arg in args)
    assert run["spec_mode"] == "0o600"
    assert "s3cr3t" in str(run["spec"])


@pytest.mark.asyncio
async def test_guest_arguments_follow_the_options(tmp_path: Path, fake_hluk: Path) -> None:
    workspace = _workspace(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    manifest = Manifest(
        root=str(workspace),
        extra_path_grants=(
            SandboxPathGrant(path=str(shared), read_only=True),
            SandboxPathGrant(path=str(tmp_path / "missing")),
        ),
    )
    options = HyperlightSandboxClientOptions(
        initrd="/images/rootfs.cpio", scratch_mb=512, net_allow=("pypi.org", "10.0.0.1")
    )
    session = await client.create(manifest=manifest, options=options)
    async with session:
        await session.exec("true")

    args = _command_runs(tmp_path)[0]["args"]
    assert isinstance(args, list)
    assert args[:6] == [
        "run",
        "--initrd=/images/rootfs.cpio",
        "--warm",
        "--scratch-mb=512",
        "--net-allow=pypi.org",
        "--net-allow=10.0.0.1",
    ]
    mounts = [arg.removeprefix("--mount=") for arg in args if arg.startswith("--mount=")]
    assert mounts[0] == f"{workspace}:{workspace}"
    assert mounts[1] == f"{shared}:{shared}:ro"
    # A grant whose path does not exist yet is not shared, and the control directory comes last.
    assert len(mounts) == 3
    control = mounts[2].split(":")[0]
    assert args[-1] == f"{control}/exec.py"
    assert not Path(control).exists()


@pytest.mark.asyncio
async def test_default_runtime_and_no_network(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        await session.exec("true")

    warm, command = _runs(tmp_path)
    workspace = _workspace(tmp_path)
    # The start's guest has the mounts a command gets, so that hluk checks them then.
    assert isinstance(warm["args"], list)
    assert warm["args"][:3] == ["run", "--runtime=agent", f"--mount={workspace}:{workspace}"]
    assert warm["args"][-2:] == ["--exec", "pass"]
    args = command["args"]
    assert isinstance(args, list)
    assert args[:2] == ["run", "--runtime=agent"]
    assert not any(arg.startswith("--net") for arg in args)


def test_exports_match_the_provider_package() -> None:
    import openai_agents_hyperlight as hyperlight

    assert hyperlight.HyperlightSandboxClient is HyperlightSandboxClient
    assert hyperlight.HyperlightSandboxClientOptions is HyperlightSandboxClientOptions
    assert hyperlight.HyperlightSandboxSession is HyperlightSandboxSession
    assert hyperlight.HyperlightSandboxSessionState is HyperlightSandboxSessionState
    assert hyperlight.DEFAULT_HYPERLIGHT_RUNTIME == "agent"


@pytest.mark.asyncio
async def test_another_backends_options_are_refused() -> None:
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClientOptions

    with pytest.raises(TypeError, match="HyperlightSandboxClientOptions"):
        await HyperlightSandboxClient().create(options=UnixLocalSandboxClientOptions())  # type: ignore[arg-type]


def test_options_reject_runtime_and_initrd_together() -> None:
    with pytest.raises(ValueError, match="runtime or initrd"):
        HyperlightSandboxClientOptions(runtime="agent", initrd="/rootfs.cpio")
    with pytest.raises(ValueError, match="scratch_mb"):
        HyperlightSandboxClientOptions(scratch_mb=0)
    # Options and states parsed from saved data are validated too.
    with pytest.raises(ValueError, match="runtime or initrd"):
        HyperlightSandboxClientOptions.parse({"type": "hyperlight", "runtime": "agent", "initrd": "/rootfs.cpio"})
    with pytest.raises(ValueError, match="startup_timeout_s"):
        HyperlightSandboxClient(startup_timeout_s=0)


@pytest.mark.asyncio
async def test_start_reports_a_guest_that_cannot_start(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HLUK_MODE", "no-guest")
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    with pytest.raises(WorkspaceStartError) as excinfo:
        await session.start()
    assert "No hypervisor was found" in str(excinfo.value.context["stderr"])


@pytest.mark.asyncio
async def test_start_reports_a_missing_hluk(tmp_path: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(tmp_path / "no-such-hluk"))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    with pytest.raises(WorkspaceStartError, match="hluk"):
        await session.start()


@pytest.mark.asyncio
async def test_guest_failure_is_a_transport_error(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        monkeypatch.setenv("FAKE_HLUK_MODE", "crash")
        with pytest.raises(ExecTransportError) as excinfo:
            await session.exec("exit 1")
    assert excinfo.value.context["reason"] == "hyperlight_guest_failed"
    assert "guest crashed" in str(excinfo.value.context["stderr"])


@pytest.mark.asyncio
async def test_a_guest_stopped_during_the_command_is_a_failed_command(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        monkeypatch.setenv("FAKE_HLUK_MODE", "killed")
        result = await session.exec("echo before")
        assert result.exit_code == 137
        assert result.stdout == b"before\n"
        assert b"stopped before the command finished" in result.stderr
        # The session goes on: the next command gets a fresh guest.
        monkeypatch.delenv("FAKE_HLUK_MODE")
        assert (await session.exec("echo after")).stdout == b"after\n"
        # A memory fault aborts the guest's kernel: a command killed by SIGSEGV.
        monkeypatch.setenv("FAKE_HLUK_MODE", "aborted")
        assert (await session.exec("echo before")).exit_code == 139
        # Not a failure of hluk's own during the command, which is still the sandbox's, even
        # with the guest's last line like hluk's.
        for mode in ("failed-during", "forged", "aborted-then-more"):
            monkeypatch.setenv("FAKE_HLUK_MODE", mode)
            with pytest.raises(ExecTransportError):
                await session.exec("echo before")
        # A guest that stopped before the command started, as here where it is not found: the
        # sandbox failed, not the command.
        for mode in ("killed", "aborted"):
            monkeypatch.setenv("FAKE_HLUK_MODE", mode)
            with pytest.raises(ExecTransportError):
                await session.exec("./missing", shell=False)


@pytest.mark.asyncio
async def test_a_resumed_workspace_is_made_again_owner_only_and_only_where_it_was_made(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temporary))
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create()
    root = Path(session.state.manifest.root)
    payload = client.serialize_session_state(session.state)
    await client.delete(session)
    assert not root.exists()
    resumed = await client.resume(client.deserialize_session_state(payload))
    async with resumed:
        assert root.stat().st_mode & 0o777 == 0o700
    await client.delete(resumed)

    # Something else at its name: a link is not followed.
    (tmp_path / "elsewhere").mkdir()
    root.symlink_to(tmp_path / "elsewhere")
    resumed = await client.resume(client.deserialize_session_state(payload))
    with pytest.raises(WorkspaceStartError):
        await resumed.start()
    root.unlink()

    # Resumed where the temporary directory is another: delete() would not remove it.
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(other))
    resumed = await client.resume(client.deserialize_session_state(payload))
    with pytest.raises(WorkspaceStartError, match="temporary directory it was made in"):
        await resumed.start()
    assert not root.exists()


@pytest.mark.asyncio
async def test_linked_status_file_is_not_followed(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        monkeypatch.setenv("FAKE_HLUK_MODE", "linked-status")
        with pytest.raises(ExecTransportError):
            await session.exec("exit 0")


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["hluk 0.18.0", "hluk 0.18.0+dev.44ee170", "hluk 0.19.0-rc1"])
async def test_start_refuses_an_old_hluk(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.setenv("FAKE_HLUK_VERSION", version)
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    with pytest.raises(WorkspaceStartError, match="needs hluk 0.19.0 or later"):
        await session.start()
    assert not (tmp_path / "hluk.log").exists()


@pytest.mark.asyncio
async def test_start_reports_an_unreadable_hluk_version(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HLUK_VERSION", "hluk")
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    with pytest.raises(WorkspaceStartError, match="cannot read the version"):
        await session.start()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["hluk 0.19.0", "hluk 0.19.0+dev.1234567", "hluk 1.0.0-rc1"])
async def test_start_accepts_a_recent_hluk(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.setenv("FAKE_HLUK_VERSION", version)
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        assert (await session.exec("true")).exit_code == 0


@pytest.mark.asyncio
async def test_timeout_kills_the_guest(tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        monkeypatch.setenv("FAKE_HLUK_MODE", "sleep")
        with pytest.raises(ExecTimeoutError):
            await session.exec("true", timeout=5)
    pid = int((tmp_path / "hluk.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_cancelled_exec_kills_the_guest(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        monkeypatch.setenv("FAKE_HLUK_MODE", "sleep")
        task = asyncio.create_task(session.exec("true"))
        pid_file = tmp_path / "hluk.pid"
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def _spy_on_persistence(monkeypatch: pytest.MonkeyPatch, on_persist=None) -> list[int]:
    """The guests running each time the workspace is archived."""
    running: list[int] = []
    persist = hyperlight_sandbox.UnixLocalSandboxSession.persist_workspace

    async def spy(self):
        if on_persist is not None:
            on_persist()
        running.append(len(self._guests))
        # Long enough for a guest started meanwhile to be seen
        await asyncio.sleep(0.5)
        running.append(len(self._guests))
        return await persist(self)

    monkeypatch.setattr(hyperlight_sandbox.UnixLocalSandboxSession, "persist_workspace", spy)
    return running


@pytest.mark.asyncio
@pytest.mark.parametrize("started", ["running", "being set up", "during the snapshot"])
async def test_closing_the_session_stops_its_commands_before_the_snapshot(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch, started: str
) -> None:
    from agents.sandbox import LocalSnapshotSpec

    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(
        manifest=Manifest(root=str(_workspace(tmp_path))),
        snapshot=LocalSnapshotSpec(base_path=tmp_path / "snapshots"),
    )
    await session.start()
    tasks: list[asyncio.Task] = []
    running = _spy_on_persistence(
        monkeypatch,
        (lambda: tasks.append(asyncio.create_task(session.exec("true"))))
        if started == "during the snapshot"
        else None,
    )
    monkeypatch.setenv("FAKE_HLUK_MODE", "sleep")
    if started != "during the snapshot":
        tasks.append(asyncio.create_task(session.exec("true")))
    pid_file = tmp_path / "hluk.pid"
    if started == "running":
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert pid_file.exists()
    elif started == "being set up":
        # Into the command's first await, before its guest starts
        await asyncio.sleep(0)
    await asyncio.wait_for(session.aclose(), timeout=30)
    assert running == [0, 0]
    (task,) = tasks
    with pytest.raises(ExecTransportError):
        await task
    if pid_file.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    # Nor does a command start once the session is down.
    with pytest.raises(ExecTransportError, match="once started"):
        await session.exec("true")


@pytest.mark.asyncio
async def test_a_guest_started_while_the_guests_are_stopped_is_stopped(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        monkeypatch.setenv("FAKE_HLUK_MODE", "sleep")
        spawn = asyncio.create_subprocess_exec

        async def spawn_as_the_guests_stop(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            await session.pty_terminate_all()
            return process

        monkeypatch.setattr(hyperlight_sandbox.asyncio, "create_subprocess_exec", spawn_as_the_guests_stop)
        with pytest.raises(ExecTransportError):
            await asyncio.wait_for(session.exec("true"), timeout=30)
        monkeypatch.setattr(hyperlight_sandbox.asyncio, "create_subprocess_exec", spawn)


@pytest.mark.asyncio
async def test_a_cancelled_create_leaves_no_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temporary))
    run = hyperlight_sandbox.run_blocking_workspace_io

    async def cancelled_once_made(func, *args):
        result = await run(func, *args)
        if any(temporary.iterdir()):
            # As when the caller is cancelled while the worker makes the workspace
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(hyperlight_sandbox, "run_blocking_workspace_io", cancelled_once_made)
    with pytest.raises(asyncio.CancelledError):
        await HyperlightSandboxClient().create()
    assert list(temporary.iterdir()) == []


@pytest.mark.asyncio
async def test_the_workspace_limit_is_the_size_of_the_archive_the_sdk_makes(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tarfile

    # Archives padded to a block rather than a 10 KiB record, so that a block's difference shows
    monkeypatch.setattr(tarfile, "RECORDSIZE", tarfile.BLOCKSIZE)
    # What makes tar's headers longer than its 512 bytes: long and non-ASCII names, long link
    # targets; and hard links, archived in full; but not what the manifest marks ephemeral.
    workspace = _workspace(tmp_path)
    (workspace / "f\u00e9").write_bytes(b"x" * 700)
    os.link(workspace / "f\u00e9", workspace / "hard")
    deep = workspace / ("d" * 120) / ("e" * 120)
    deep.mkdir(parents=True)
    (deep / "file").write_text("y")
    (workspace / "link").symlink_to("t" * 300)
    # Absolute targets in the workspace, which the SDK rewrites relative to each link: longer
    # when deep
    deeper = workspace.joinpath(*["a"] * 200)
    deeper.mkdir(parents=True)
    for index in range(8):
        (deeper / f"up{index}").symlink_to(workspace / "hard")
    manifest = Manifest(root=str(workspace), entries={"cache": File(content=b"z" * 100_000, ephemeral=True)})
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk), max_workspace_bytes=None)
    session = await client.create(manifest=manifest)
    async with session:
        archive = (await session.persist_workspace()).read()
        skip = session._inner._persist_workspace_skip_relpaths()
    assert b"z" * 1000 not in archive
    root = str(workspace)
    assert not hyperlight_sandbox._archive_too_large(root, skip, len(archive))
    assert hyperlight_sandbox._archive_too_large(root, skip, len(archive) - 1)


@pytest.mark.asyncio
async def test_a_command_that_finished_keeps_its_result_when_a_stop_kills_its_guest(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    await session.start()
    monkeypatch.setenv("FAKE_HLUK_MODE", "finished-then-sleep")
    task = asyncio.create_task(session.exec("echo done"))
    pid_file = tmp_path / "hluk.pid"
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    await asyncio.wait_for(session.aclose(), timeout=30)
    result = await task
    assert (result.exit_code, result.stdout) == (0, b"done\n")


@pytest.mark.asyncio
async def test_a_command_set_up_across_the_whole_close_does_not_start_its_guest(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    await session.start()
    monkeypatch.setenv("FAKE_HLUK_MODE", "sleep")
    entered, release = threading.Event(), threading.Event()
    mount_args = hyperlight_sandbox._mount_args

    def held(shared):
        entered.set()
        release.wait(30)
        return mount_args(shared)

    monkeypatch.setattr(hyperlight_sandbox, "_mount_args", held)
    task = asyncio.create_task(session.exec("true"))
    assert await asyncio.to_thread(entered.wait, 10)
    await asyncio.wait_for(session.aclose(), timeout=30)
    release.set()
    started = time.monotonic()
    with pytest.raises(ExecTransportError) as excinfo:
        await asyncio.wait_for(task, timeout=30)
    assert excinfo.value.context["reason"] == "session_stopping"
    # Stopped as it started rather than left to sleep
    assert time.monotonic() - started < 20


@pytest.mark.asyncio
async def test_a_session_whose_shutdown_failed_starts_again(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    await session.start()

    async def fails() -> None:
        raise RuntimeError("shutdown failed part way")

    # Before _after_shutdown, which the SDK then skips
    session._inner._shutdown_backend = fails  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await session.shutdown()
    del session._inner._shutdown_backend
    with pytest.raises(ExecTransportError, match="once started"):
        await session.exec("true")
    await session.start()
    assert (await session.exec("echo ok")).stdout == b"ok\n"


def test_max_workspace_bytes_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="max_workspace_bytes"):
        HyperlightSandboxClient(max_workspace_bytes=-1)


@pytest.mark.asyncio
async def test_unsupported_operations(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        assert not session.supports_pty()
        with pytest.raises(NotImplementedError):
            await session.pty_exec_start("sh")
        # A user-scoped operation would otherwise go through sudo on the host.
        with pytest.raises(ValueError, match="sandbox users"):
            await session.exec("id", user="nobody")
        with pytest.raises(ValueError, match="sandbox users"):
            await session.write(Path("a.txt"), io.BytesIO(b"x"), user="nobody")
        with pytest.raises(ExposedPortUnavailableError):
            await session.resolve_exposed_port(8080)


@pytest.mark.asyncio
async def test_manifest_permissions_are_set_from_the_host(tmp_path: Path, fake_hluk: Path) -> None:
    workspace = _workspace(tmp_path)
    entry = File(content=b"#!/bin/sh\n", permissions=Permissions(owner=7, group=5, other=0))
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(workspace), entries={"run.sh": entry}))
    async with session:
        assert (workspace / "run.sh").stat().st_mode & 0o777 == 0o750
    # No guest was started for it.
    assert _command_runs(tmp_path) == []


@pytest.mark.asyncio
async def test_host_chmod_does_not_follow_links(tmp_path: Path, fake_hluk: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    outside.chmod(0o644)
    (workspace / "link").symlink_to(outside)
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(workspace)))
    async with session:
        inner = session._inner
        assert isinstance(inner, HyperlightSandboxSession)
        with pytest.raises(InvalidManifestPathError):
            await inner._exec_checked_nonzero("chmod", "0777", str(workspace / "link"))
    assert outside.stat().st_mode & 0o777 == 0o644


@pytest.mark.asyncio
async def test_host_chmod_needs_no_permission_on_the_entry(tmp_path: Path, fake_hluk: Path) -> None:
    workspace = _workspace(tmp_path)
    locked = workspace / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(workspace)))
    try:
        async with session:
            inner = session._inner
            assert isinstance(inner, HyperlightSandboxSession)
            await inner._exec_checked_nonzero("chmod", "0311", str(locked))
        assert locked.stat().st_mode & 0o777 == 0o311
    finally:
        locked.chmod(0o755)


@pytest.mark.asyncio
async def test_manifest_entry_groups_are_rejected(tmp_path: Path, fake_hluk: Path) -> None:
    entry = File(content=b"x", group=Group(name="staff", users=[]))
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    manifest = Manifest(root=str(_workspace(tmp_path)), entries={"a.txt": entry})
    with pytest.raises(ValueError, match="groups"):
        await client.create(manifest=manifest)


@pytest.mark.asyncio
async def test_file_grants_are_not_shared(tmp_path: Path, fake_hluk: Path) -> None:
    granted = tmp_path / "notes.txt"
    granted.write_text("x")
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    manifest = Manifest(
        root=str(_workspace(tmp_path)),
        extra_path_grants=(SandboxPathGrant(path=str(granted)),),
    )
    session = await client.create(manifest=manifest)
    with pytest.raises(WorkspaceStartError, match="directories only"):
        await session.start()


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", ["workspace/sub", "shared/sub", ".", "shared/missing"])
async def test_overlapping_grants_are_refused(tmp_path: Path, fake_hluk: Path, nested: str) -> None:
    # A guest could replace a shared path inside a writable one with a link before the next
    # command, or create a missing one there.
    workspace = _workspace(tmp_path)
    (workspace / "sub").mkdir()
    (tmp_path / "shared" / "sub").mkdir(parents=True)
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    manifest = Manifest(
        root=str(workspace),
        extra_path_grants=(
            SandboxPathGrant(path=str(tmp_path / "shared")),
            SandboxPathGrant(path=str((tmp_path / nested).resolve()), read_only=True),
        ),
    )
    session = await client.create(manifest=manifest)
    with pytest.raises(WorkspaceStartError, match="cannot overlap"):
        await session.start()


@pytest.mark.asyncio
async def test_a_temporary_directory_in_a_shared_path_is_refused(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Other commands' guests would reach the command's control directory there.
    shared = tmp_path / "shared"
    shared.mkdir()
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    manifest = Manifest(root=str(_workspace(tmp_path)), extra_path_grants=(SandboxPathGrant(path=str(shared)),))
    monkeypatch.setattr(tempfile, "tempdir", str(shared))
    session = await client.create(manifest=manifest)
    with pytest.raises(WorkspaceStartError, match="set TMPDIR"):
        await session.start()
    assert list(shared.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permissions the guest takes away")
async def test_control_directory_is_removed_whatever_the_guest_did(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It holds the command's environment.
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        # Without the owner's permissions on its control directory, the guest cannot report the
        # command's status either.
        with pytest.raises(ExecTransportError):
            await session.exec(
                "d=$(dirname $(ls -d " + str(temp) + "/openai-agents-hyperlight-*/spec.json)); "
                "mkdir $d/sub; touch $d/sub/f; chmod 000 $d/sub $d"
            )
    assert list(temp.iterdir()) == []


@pytest.mark.asyncio
async def test_manifest_changes_are_checked_while_running(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        inner = session._inner
        assert isinstance(inner, HyperlightSandboxSession)
        changed = inner.state.manifest.model_copy(
            update={"entries": {"a.txt": File(content=b"x", group=Group(name="staff", users=[]))}}
        )
        with pytest.raises(ValueError, match="groups"):
            await inner._validate_manifest_application(manifest=changed)


@pytest.mark.asyncio
async def test_a_bare_hluk_name_is_not_taken_from_the_working_directory(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fake_hluk.parent)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    client = HyperlightSandboxClient(hluk_path="hluk")
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    with pytest.raises(WorkspaceStartError, match="was not found"):
        await session.start()
    assert not (tmp_path / "hluk.log").exists()


@pytest.mark.asyncio
async def test_a_failed_start_leaves_no_hluk_to_run_commands(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HLUK_VERSION", "hluk 0.18.0")
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    with pytest.raises(WorkspaceStartError):
        await session.start()
    with pytest.raises(ExecTransportError, match="once started"):
        await session._inner._exec_internal("true")
    assert not (tmp_path / "hluk.log").exists()


@pytest.mark.asyncio
async def test_a_relative_initrd_is_made_absolute(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    options = HyperlightSandboxClientOptions(initrd="rootfs.cpio")
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))), options=options)
    assert session.state.initrd == str(tmp_path / "rootfs.cpio")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_delete_removes_a_workspace_the_guest_locked(fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create()
    async with session:
        result = await session.exec("mkdir -p d/e && touch d/e/f && chmod 000 d")
        assert result.exit_code == 0
    root = Path(session.state.manifest.root)
    await client.delete(session)
    assert not root.exists()


@pytest.mark.asyncio
async def test_git_repository_entries_are_rejected(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    manifest = Manifest(root=str(_workspace(tmp_path)), entries={"repo": GitRepo(repo="org/x", ref="main")})
    with pytest.raises(ValueError, match="Git repository"):
        await client.create(manifest=manifest)


@pytest.mark.asyncio
async def test_hluk_is_not_found_through_a_relative_path_entry(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fake_hluk.parent)
    monkeypatch.setenv("PATH", f"{tmp_path / 'empty'}::.")
    client = HyperlightSandboxClient()
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    with pytest.raises(WorkspaceStartError, match="was not found"):
        await session.start()


def test_a_hluk_path_is_made_absolute_with_the_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert HyperlightSandboxClient(hluk_path="bin/hluk")._hluk_path == str(tmp_path / "bin/hluk")
    assert HyperlightSandboxClient(hluk_path="~/hluk")._hluk_path == str(tmp_path / "home/hluk")
    assert HyperlightSandboxClient(hluk_path="hluk-0.19")._hluk_path == "hluk-0.19"


@pytest.mark.asyncio
async def test_delete_removes_only_a_workspace_it_could_have_made(tmp_path: Path, fake_hluk: Path) -> None:
    # A saved state can claim any root as its own.
    project = _workspace(tmp_path)
    (project / "keep.txt").write_text("x")
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    state = HyperlightSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root=str(project)),
        snapshot=NoopSnapshot(id="s"),
        workspace_root_owned=True,
    )
    await client.delete(await client.resume(state))
    assert (project / "keep.txt").exists()


@pytest.mark.asyncio
async def test_relative_path_entries_are_searched_from_the_working_directory(tmp_path: Path, fake_hluk: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "bin").mkdir()
    tool = workspace / "bin" / "tool"
    tool.write_text("#!/bin/sh\necho tool ran\n")
    tool.chmod(0o755)
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    manifest = Manifest(root=str(workspace), environment=Environment(value={"PATH": "bin:/usr/bin:/bin"}))
    session = await client.create(manifest=manifest)
    async with session:
        result = await session.exec("tool", shell=False)
    assert (result.exit_code, result.stdout) == (0, b"tool ran\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", ["workspace", "grant"])
async def test_shared_paths_through_a_link_are_refused(tmp_path: Path, fake_hluk: Path, linked: str) -> None:
    # A link could be repointed between commands, or across a stop and a resume.
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    workspace = _workspace(tmp_path)
    grants: tuple[SandboxPathGrant, ...] = ()
    if linked == "workspace":
        workspace = tmp_path / "link"
    else:
        grants = (SandboxPathGrant(path=str(tmp_path / "link")),)
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(workspace), extra_path_grants=grants))
    with pytest.raises(WorkspaceStartError, match=f"give {tmp_path / 'link'} as {tmp_path / 'real'}"):
        await session.start()


@pytest.mark.asyncio
async def test_a_link_made_after_the_start_is_refused(tmp_path: Path, fake_hluk: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (tmp_path / "elsewhere").mkdir()
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    manifest = Manifest(root=str(_workspace(tmp_path)), extra_path_grants=(SandboxPathGrant(path=str(shared)),))
    session = await client.create(manifest=manifest)
    async with session:
        shared.rmdir()
        shared.symlink_to(tmp_path / "elsewhere")
        with pytest.raises(ExecTransportError, match="cannot go through a link"):
            await session.exec("true")


@pytest.mark.asyncio
async def test_an_unnormalized_path_is_asked_for_resolved(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=f"{_workspace(tmp_path)}/"))
    with pytest.raises(WorkspaceStartError, match="must be given resolved"):
        await session.start()


@pytest.mark.asyncio
async def test_output_comes_back_as_written(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        result = await session.exec("printf '\\377\\376ab'; printf '\\200' >&2")
    assert (result.stdout, result.stderr) == (b"\xff\xfeab", b"\x80")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["linked-stdout", "fifo-stdout"])
async def test_output_files_the_guest_replaced_are_not_read(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # A link would hand the host's file back as output, and a FIFO would hang the read.
    secret = tmp_path / "secret.txt"
    secret.write_text("host-only")
    monkeypatch.setenv("FAKE_HLUK_SECRET", str(secret))
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        monkeypatch.setenv("FAKE_HLUK_MODE", mode)
        result = await asyncio.wait_for(session.exec("echo hi"), timeout=10)
    assert result.stdout == b""


@pytest.mark.asyncio
async def test_output_is_bounded(tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hyperlight_sandbox, "_OUTPUT_MAX_BYTES", 1000)
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        result = await session.exec("head -c 5000 /dev/zero")
    assert result.stdout == b"\0" * 1000 + b"\n[output truncated at 1000 bytes]\n"


@pytest.mark.parametrize("field", ["runtime", "initrd"])
def test_an_empty_runtime_or_initrd_is_refused(field: str) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        if field == "runtime":
            HyperlightSandboxClientOptions(runtime="")
        else:
            HyperlightSandboxClientOptions(initrd="")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permissions taken away")
@pytest.mark.parametrize(
    ("depth", "name", "mode"),
    [
        (1500, "a", 0o500),
        (60, "d" * 40, 0o500),
        (60, "\U0001f600" * 63, 0o500),
        (100, "a", 0o000),
    ],
)
def test_a_deep_tree_the_guest_locked_is_removed(tmp_path: Path, depth: int, name: str, mode: int) -> None:
    # Deeper than shutil.rmtree recurses, or with paths longer than the walk keeps, with every
    # directory's write (or all) permission taken away. Made through descriptors: os.makedirs
    # recurses.
    root = tmp_path / "deep"
    root.mkdir()
    fds = [os.open(root, os.O_RDONLY | os.O_DIRECTORY)]
    for _ in range(depth):
        os.mkdir(name, dir_fd=fds[-1])
        fds.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fds[-1]))
        if len(fds) > 64:
            os.fchmod(fds[0], mode)
            os.close(fds.pop(0))
    os.close(os.open("f", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fds[-1]))
    for fd in fds:
        os.fchmod(fd, mode)
        os.close(fd)
    # In a process allowed few descriptors, as the walk should need, which is killed rather than
    # hanging the test if the walk never ends.
    script = (
        "import resource, sys\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))\n"
        "from openai_agents_hyperlight import sandbox\n"
        "sandbox._remove_tree(sys.argv[1])\n"
    )
    subprocess.run([sys.executable, "-c", script, str(root)], check=True, timeout=120)
    assert not root.exists()


def test_removing_a_tree_leaves_what_is_not_a_directory_alone(tmp_path: Path) -> None:
    # A saved state can name any root; one that is a file is neither removed nor changed.
    target = tmp_path / "file"
    target.write_text("x")
    target.chmod(0o600)
    hyperlight_sandbox._remove_tree(str(target))
    assert target.exists() and target.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_a_program_keeps_the_name_it_was_run_by(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        result = await session.exec("echo $0")
    assert result.stdout == b"sh\n"


@pytest.mark.asyncio
async def test_a_missing_program_given_by_path_is_not_found(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        result = await session.exec("./missing", shell=False)
    assert (result.exit_code, result.stderr) == (127, b"./missing: command not found\n")


def test_net_allow_takes_a_sequence_of_hosts() -> None:
    with pytest.raises(ValueError):
        HyperlightSandboxClientOptions(net_allow="pypi.org")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_manifest_mounts_and_host_path_grants_are_rejected(tmp_path: Path) -> None:
    client = HyperlightSandboxClient()
    mount = S3Mount(
        bucket="bucket",
        mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
    )
    with pytest.raises(ValueError, match="mounts"):
        await client.create(manifest=Manifest(root=str(tmp_path), entries={"data": mount}))
    grant = SandboxPathGrant(path="/data", host_path=str(tmp_path))
    with pytest.raises(ValueError, match="host_path"):
        await client.create(manifest=Manifest(root=str(tmp_path), extra_path_grants=(grant,)))


@pytest.mark.asyncio
async def test_session_state_round_trip_and_resume(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    options = HyperlightSandboxClientOptions(runtime="python-shell", net_allow=("pypi.org",))
    session = await client.create(options=options)
    workspace = Path(session.state.manifest.root)
    try:
        assert workspace.name.startswith("sandbox-hyperlight-")
        async with session:
            await session.write(Path("kept.txt"), io.BytesIO(b"kept\n"))
        payload = client.serialize_session_state(session.state)
        assert payload["type"] == "hyperlight"
        assert "hluk_path" not in json.dumps(payload)

        state = client.deserialize_session_state(payload)
        assert isinstance(state, HyperlightSandboxSessionState)
        assert state.runtime == "python-shell"
        assert state.net_allow == ("pypi.org",)
        resumed = await client.resume(state)
        assert isinstance(resumed._inner, HyperlightSandboxSession)
        async with resumed:
            result = await resumed.exec("cat kept.txt")
        assert result.stdout == b"kept\n"
        args = _command_runs(tmp_path)[-1]["args"]
        assert isinstance(args, list)
        assert args[:3] == ["run", "--runtime=python-shell", "--net-allow=pypi.org"]
    finally:
        await client.delete(session)
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_saved_values_cannot_add_hluk_options(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    options = HyperlightSandboxClientOptions(runtime="--cold", net_allow=("--mount=/:/host",))
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))), options=options)
    async with session:
        await session.exec("true")
    args = _command_runs(tmp_path)[0]["args"]
    assert isinstance(args, list)
    assert args[1:3] == ["--runtime=--cold", "--net-allow=--mount=/:/host"]
    assert "--mount=/:/host" not in args


@pytest.mark.asyncio
async def test_unix_local_client_does_not_resume_a_hyperlight_session(tmp_path: Path) -> None:
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

    client = HyperlightSandboxClient()
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    # Resuming through UnixLocal would run the session's commands as host processes.
    with pytest.raises(TypeError):
        await UnixLocalSandboxClient().resume(session.state)
    with pytest.raises(ValueError):
        UnixLocalSandboxClient().deserialize_session_state(client.serialize_session_state(session.state))


@pytest.mark.asyncio
async def test_delete_keeps_a_caller_workspace(tmp_path: Path, fake_hluk: Path) -> None:
    workspace = _workspace(tmp_path)
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(workspace)))
    await client.delete(session)
    assert workspace.exists()


@pytest.mark.asyncio
async def test_runner_shell_tool_runs_in_the_guest(tmp_path: Path, fake_hluk: Path) -> None:
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "echo from-guest > out.txt"}, call_id="c1")],
            [assistant_message("done", item_id="m1")],
        ]
    )
    agent = SandboxAgent(name="hyperlight", model=model, capabilities=[Shell()])
    session = await client.create(manifest=Manifest(root=str(_workspace(tmp_path))))
    async with session:
        result = await Runner.run(
            agent, "Write a file.", run_config=RunConfig(sandbox=SandboxRunConfig(session=session))
        )
        assert result.final_output == "done"
        assert (await session.read(Path("out.txt"))).read() == b"from-guest\n"
    model.assert_complete()
    assert any("echo from-guest" in str(run.get("spec")) for run in _command_runs(tmp_path))


def _real_client() -> HyperlightSandboxClient:
    return HyperlightSandboxClient(hluk_path=os.environ.get("HLUK_PATH"))


def _real_options(net_allow: tuple[str, ...] = ()) -> HyperlightSandboxClientOptions:
    # CI boots the agent image its commit built, rather than pulling its release's
    scratch_mb = os.environ.get("HLUK_SCRATCH_MB")
    return HyperlightSandboxClientOptions(
        initrd=os.environ.get("HLUK_INITRD"), scratch_mb=int(scratch_mb) if scratch_mb else None, net_allow=net_allow
    )


# Real micro-VMs: set RUN_ALL=1 on a host with a hypervisor and `hluk` on PATH or in HLUK_PATH; HLUK_INITRD boots a
# local image instead of the agent runtime's, at HLUK_SCRATCH_MB of memory. With RUN_ALL set they run, and fail
# without a fit hluk rather than skip.
requires_hluk = pytest.mark.skipif(
    not os.environ.get("RUN_ALL"), reason="set RUN_ALL=1 with `hluk` 0.19.0 or later for micro-VM tests"
)


@requires_hluk
@pytest.mark.asyncio
async def test_hluk_commands_share_the_workspace_and_nothing_else(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("host-only")
    client = _real_client()
    session = await client.create(options=_real_options())
    try:
        async with session:
            await session.write(Path("in.txt"), io.BytesIO(b"from-host\n"))
            result = await session.exec(
                "cat in.txt; uname -s; [ -t 1 ] || echo no-tty; echo from-guest > out.txt; exit 4"
            )
            assert result.exit_code == 4
            assert result.stdout == b"from-host\nUnikraft\nno-tty\n"
            assert (await session.read(Path("out.txt"))).read() == b"from-guest\n"

            result = await session.exec(f"cat {secret}")
            assert result.exit_code != 0
            assert b"host-only" not in result.stdout

            result = await session.exec("nosuchcommand")
            assert result.exit_code == 127
            assert result.stderr == b"sh: can't execute 'nosuchcommand': No such file or directory\n"
    finally:
        await client.delete(session)


@requires_hluk
@pytest.mark.asyncio
async def test_hluk_manifest_files_and_read_only_grants(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "data.txt").write_text("shared\n")
    manifest = Manifest(
        entries={"run.sh": File(content=b"echo ran\n", permissions=Permissions(owner=7))},
        extra_path_grants=(SandboxPathGrant(path=str(shared), read_only=True),),
    )
    client = _real_client()
    session = await client.create(manifest=manifest, options=_real_options())
    try:
        async with session:
            result = await session.exec(f"sh run.sh; cat {shared}/data.txt; echo x > {shared}/new")
        assert result.stdout == b"ran\nshared\n"
        assert b"Read-only file system" in result.stderr
        assert not (shared / "new").exists()
        assert (Path(session.state.manifest.root) / "run.sh").stat().st_mode & 0o777 == 0o700
    finally:
        await client.delete(session)


@requires_hluk
@pytest.mark.asyncio
async def test_hluk_chmod_does_not_reach_a_read_only_grant(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    target = shared / "data.txt"
    target.write_text("x")
    target.chmod(0o644)
    manifest = Manifest(extra_path_grants=(SandboxPathGrant(path=str(shared), read_only=True),))
    client = _real_client()
    session = await client.create(manifest=manifest, options=_real_options())
    try:
        async with session:
            (Path(session.state.manifest.root) / "link").symlink_to(target)
            results = [await session.exec(f"chmod 777 {path}") for path in (target, "link")]
        assert all(result.exit_code != 0 for result in results)
        assert target.stat().st_mode & 0o777 == 0o644
    finally:
        await client.delete(session)


@requires_hluk
@pytest.mark.asyncio
async def test_hluk_sessions_start_together_on_a_fresh_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every session saves the guest's warm snapshot when the cache has none. The pulled image is
    # shared with the usual cache, so only the snapshots are fresh.
    fresh = tmp_path / "cache"
    fresh.mkdir()
    if not os.environ.get("HLUK_INITRD"):
        cache_home = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
        cache = Path(os.environ.get("HLUK_CACHE_DIR") or Path(cache_home) / "hluk")
        if not (cache / "rootfs").is_dir():
            pytest.skip("needs the runtime image in hluk's cache: run another micro-VM test first")
        (fresh / "rootfs").symlink_to(cache / "rootfs")
    monkeypatch.setenv("HLUK_CACHE_DIR", str(fresh))
    client = _real_client()
    sessions = [await client.create(options=_real_options()) for _ in range(4)]
    try:
        starts = await asyncio.gather(*(session.start() for session in sessions), return_exceptions=True)
        assert [start for start in starts if isinstance(start, BaseException)] == []
        results = await asyncio.gather(*(session.exec("echo ok") for session in sessions))
        assert [result.stdout for result in results] == [b"ok\n"] * 4
    finally:
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.aclose()
            await client.delete(session)


@requires_hluk
@pytest.mark.asyncio
async def test_hluk_output_comes_back_as_written() -> None:
    # hluk decodes the guest's console as UTF-8; the command's output does not go through it.
    client = _real_client()
    session = await client.create(options=_real_options())
    try:
        async with session:
            result = await session.exec("printf '\\377\\376ab'; printf '\\200' >&2")
        assert (result.stdout, result.stderr) == (b"\xff\xfeab", b"\x80")
    finally:
        await client.delete(session)


@requires_hluk
@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["space", "too many", "too long"])
async def test_hluk_refuses_unfit_mounts_at_start(tmp_path: Path, problem: str) -> None:
    if problem == "space":
        grants = [tmp_path / "with space"]
    elif problem == "too many":
        grants = [tmp_path / f"grant{index}" for index in range(32)]
    else:
        # Within the count, past the bytes: UTF-8 takes three for each of these characters.
        (tmp_path / ("\u6587" * 80)).mkdir()
        grants = [tmp_path / ("\u6587" * 80) / f"g{index}" for index in range(12)]
    for grant in grants:
        grant.mkdir()
    client = _real_client()
    manifest = Manifest(
        root=str(_workspace(tmp_path)),
        extra_path_grants=tuple(SandboxPathGrant(path=str(grant)) for grant in grants),
    )
    session = await client.create(manifest=manifest, options=_real_options())
    with pytest.raises(WorkspaceStartError, match="failed to start the Hyperlight guest: .*mount"):
        await session.start()


@requires_hluk
@pytest.mark.asyncio
async def test_hluk_runs_a_large_command() -> None:
    client = _real_client()
    session = await client.create(options=_real_options())
    try:
        async with session:
            result = await session.exec(f"printf %s '{'x' * 300_000}' | wc -c")
        assert result.stdout.strip() == b"300000"
    finally:
        await client.delete(session)


@requires_hluk
@pytest.mark.asyncio
async def test_hluk_timeout_and_no_network() -> None:
    client = _real_client()
    session = await client.create(options=_real_options())
    try:
        async with session:
            with pytest.raises(ExecTimeoutError):
                await session.exec("sleep 30", timeout=2)
            # An address, not a name, so that a missing resolver cannot pass for no network
            result = await session.exec(_CONNECT)
            assert result.exit_code == 1 and b"PermissionError" in result.stderr, result.stderr
    finally:
        await client.delete(session)


_CONNECT = "python3 -c 'import socket; socket.create_connection((\"1.1.1.1\", 80), timeout=10)'"


@requires_hluk
@pytest.mark.asyncio
async def test_hluk_net_allow_lets_the_host_through() -> None:
    client = _real_client()
    session = await client.create(options=_real_options(net_allow=("1.1.1.1",)))
    try:
        async with session:
            result = await session.exec(_CONNECT)
            assert result.exit_code == 0, result.stderr
    finally:
        await client.delete(session)


@requires_hluk
@pytest.mark.asyncio
async def test_hluk_a_command_that_stops_its_guest_fails_and_the_session_goes_on() -> None:
    client = _real_client()
    session = await client.create(options=_real_options())
    try:
        async with session:
            result = await session.exec("echo before; sleep 5 & kill -9 -1")
            assert (result.exit_code, result.stdout) == (137, b"before\n")
            result = await session.exec("echo before; python3 -c 'import ctypes; ctypes.string_at(0)'")
            assert (result.exit_code, result.stdout) == (139, b"before\n")
            assert (await session.exec("echo after")).stdout == b"after\n"
    finally:
        await client.delete(session)


@requires_hluk
@pytest.mark.asyncio
async def test_hluk_workspace_persists_across_sessions(tmp_path: Path) -> None:
    from agents.sandbox import LocalSnapshotSpec

    client = _real_client()
    session = await client.create(snapshot=LocalSnapshotSpec(base_path=tmp_path), options=_real_options())
    async with session:
        await session.exec("echo saved > state.txt")
    payload = client.serialize_session_state(session.state)
    await client.delete(session)

    resumed = await client.resume(client.deserialize_session_state(payload))
    try:
        async with resumed:
            result = await resumed.exec("cat state.txt")
        assert result.stdout == b"saved\n"
    finally:
        await client.delete(resumed)


@requires_hluk
def test_hluk_demo_runs(tmp_path: Path) -> None:
    args = [sys.executable, "-m", "openai_agents_hyperlight.demo"]
    if os.environ.get("HLUK_PATH"):
        args += ["--hluk", os.environ["HLUK_PATH"]]
    if os.environ.get("HLUK_INITRD"):
        args += ["--initrd", os.environ["HLUK_INITRD"]]
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "hyperlight provider works\n"
