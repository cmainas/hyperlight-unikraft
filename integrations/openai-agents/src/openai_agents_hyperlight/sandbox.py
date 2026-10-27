"""Hyperlight micro-VM sandbox backend, driven through the local `hluk` CLI.

Each command runs in a fresh Hyperlight micro-VM restored from a warm snapshot of a Unikraft
guest. The workspace stays a host directory, shared into every guest at the same path, so the
SDK's descriptor-relative UnixLocal file operations, snapshots, and persistence apply unchanged
while shell commands run behind a hypervisor boundary instead of as host processes.
"""

from __future__ import annotations

import sys


if sys.platform == "win32":  # pragma: no cover
    raise ImportError("The Hyperlight sandbox backend is not supported on Windows yet.")

import asyncio
import copy
import errno
import io
import json
import os
import re
import secrets
import shutil
import signal
import stat
import tarfile
import tempfile
import uuid
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from agents.sandbox.entries import GitRepo
from agents.sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceRootNotFoundError,
    WorkspaceStartError,
)
from agents.sandbox.manifest import Manifest
from agents.sandbox.sandboxes.unix_local import (
    _DEFAULT_MANIFEST_ROOT,
    UnixLocalSandboxSession,
    UnixLocalSandboxSessionState,
    _rebase_symlink_target,
    _symlink_target_stays_under,
)
from agents.sandbox.session import SandboxSession, SandboxSessionState
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.dependencies import Dependencies
from agents.sandbox.session.manager import Instrumentation
from agents.sandbox.session.pty_types import PtyExecUpdate
from agents.sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from agents.sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from agents.sandbox.types import ExecResult, ExposedPortEndpoint, User
from agents.sandbox.util.blocking_io import run_blocking_workspace_io
from agents.sandbox.util.tar_utils import should_skip_tar_member
from pydantic import ConfigDict, model_validator
from typing_extensions import Self


DEFAULT_HYPERLIGHT_RUNTIME = "agent"
DEFAULT_HYPERLIGHT_STARTUP_TIMEOUT_S = 300.0
# The SDK archives a workspace in memory to snapshot it, and a guest writes the workspace: the
# largest archive of one, in bytes, a sparse file in it counted at its apparent size.
DEFAULT_HYPERLIGHT_MAX_WORKSPACE_BYTES = 1 << 30

_WORKSPACE_PREFIX = "sandbox-hyperlight-"
_CONTROL_PREFIX = "openai-agents-hyperlight-"
# Directories deep that removing a tree by hand keeps open at once, and entries it visits.
_REMOVE_DEPTH_MAX = 64
_REMOVE_ENTRIES_MAX = 1_000_000
# A directory opened only to name entries relative to it: with O_PATH (Linux), which needs no
# read permission on it; elsewhere read-only.
_DIRECTORY_PATH_FLAGS = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY
# Bytes of a command's stdout or stderr kept, as the guest can make either any size.
_OUTPUT_MAX_BYTES = 16 << 20
_KILL_WAIT_S = 5.0
_STATUS_MAX_BYTES = 32
# The first release whose guests keep the kernel's messages out of a command's output, and whose
# concurrent first runs of a guest do not fail each other.
MIN_HLUK_VERSION = (0, 19, 0)
# What is kept of `hluk`'s own stdout and stderr: the guest's console, and its messages.
_HLUK_OUTPUT_TAIL_BYTES = 64 << 10
# `hluk`'s last line when its guest exits with a status of its own, and when the guest's kernel
# aborts, as it does for a program's memory fault rather than deliver it SIGSEGV
_GUEST_EXITED = re.compile(rb"error: the guest exited with status (\d{1,3})\s*\Z")
_GUEST_ABORTED = re.compile(rb"\nerror: Guest aborted: \d+\s*\Z")
_HLUK_NOT_FOUND = "the `hluk` CLI was not found; install it or pass hluk_path"

# Runs inside the guest. It reads the command from the control directory rather than the `hluk`
# command line, which other host users can read, and reports the exit status there so that a
# command's own exit status cannot be mistaken for a failure of `hluk` or the guest. Its output
# goes to files there too, which the host reads as they are, where `hluk`, which decodes the
# guest's console as UTF-8, would change bytes that are not. A program that is not found is
# reported as a shell would, without trying to run it.
_GUEST_EXEC_SCRIPT = """\
import json, os, shutil, subprocess

with open({spec_path!r}) as spec_file:
    spec = json.load(spec_file)
control = os.path.dirname(spec["status"])
out = open(os.path.join(control, "stdout"), "wb")
err = open(os.path.join(control, "stderr"), "wb")
env = dict(os.environ)
env.update(spec["env"])
argv = spec["argv"]
# In the command's directory first, against which a relative PATH entry is searched, as a shell
# would search it.
os.chdir(spec["cwd"])
program = argv[0] if "/" in argv[0] else shutil.which(argv[0], path=env.get("PATH"))
if program is None or not os.path.lexists(program):
    err.write(f"{{argv[0]}}: command not found\\n".encode())
    code = 127
else:
    # Once there, a guest that stops before the status is written stopped during the command,
    # which may have stopped it: `kill -9 -1` does.
    open(os.path.join(control, "started"), "w").close()
    try:
        code = subprocess.run(
            argv, executable=program, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err
        ).returncode
    except OSError as error:
        err.write(f"{{argv[0]}}: {{error.strerror}}\\n".encode())
        code = 126
    except ValueError as error:
        err.write(f"{{argv[0]}}: {{error}}\\n".encode())
        code = 126
if code < 0:
    code = 128 - code
out.close()
err.close()
with open(spec["status"], "w") as status:
    status.write(str(code))
"""


class HyperlightSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the Hyperlight sandbox backend.

    ``runtime`` names a published `hluk` runtime image, and ``initrd`` a local rootfs CPIO to
    boot instead; set at most one. The image must run Python through `hluk`'s Python driver and
    have ``/bin/sh``, as the default ``agent`` runtime and the ``python-shell`` image it builds
    on do. ``net_allow`` lists the hosts or IPs the guest may connect to;
    with none, the guest has no network access.
    """

    # A misspelled option is refused rather than left out: net_allow and the guest's image decide
    # what the guest can reach.
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True, extra="forbid")

    type: Literal["hyperlight"] = "hyperlight"
    runtime: str | None = None
    initrd: str | None = None
    scratch_mb: int | None = None
    net_allow: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_guest(self) -> Self:
        _validate_guest_configuration(self.runtime, self.initrd, self.scratch_mb)
        return self

    def __init__(
        self,
        runtime: str | None = None,
        initrd: str | None = None,
        scratch_mb: int | None = None,
        net_allow: tuple[str, ...] = (),
        *,
        type: Literal["hyperlight"] = "hyperlight",
    ) -> None:
        super().__init__(
            type=type,
            runtime=runtime,
            initrd=initrd,
            scratch_mb=scratch_mb,
            net_allow=net_allow,
        )


# Not a UnixLocalSandboxSessionState: UnixLocalSandboxClient.resume() accepts those, and would run
# the session's commands as host processes.
class HyperlightSandboxSessionState(SandboxSessionState):
    type: Literal["hyperlight"] = "hyperlight"
    workspace_root_owned: bool = False
    runtime: str | None = None
    initrd: str | None = None
    scratch_mb: int | None = None
    net_allow: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_guest(self) -> Self:
        _validate_guest_configuration(self.runtime, self.initrd, self.scratch_mb)
        return self


class HyperlightSandboxSession(UnixLocalSandboxSession):
    """Session that runs each command in a fresh Hyperlight micro-VM.

    The workspace is a host directory, shared read-write into each guest at the same path, and
    the SDK's file operations act on it from the host with the UnixLocal descriptor-relative
    checks. Same-path extra path grants for directories are shared into each guest too, read-only
    when the grant is. Apart from a private control directory for each command, nothing else on
    the host is visible to the guest.

    A guest lives for one command: processes a command leaves running stop with it, and PTY
    sessions, exposed ports, sandbox users, and manifest mounts are not supported.
    """

    state: HyperlightSandboxSessionState  # type: ignore[assignment]

    def __init__(
        self,
        *,
        state: HyperlightSandboxSessionState,
        hluk_path: str | None = None,
        startup_timeout_s: float = DEFAULT_HYPERLIGHT_STARTUP_TIMEOUT_S,
        max_workspace_bytes: int | None = DEFAULT_HYPERLIGHT_MAX_WORKSPACE_BYTES,
    ) -> None:
        super().__init__(state=cast(UnixLocalSandboxSessionState, state))
        self._hluk_path = _absolute_hluk_path(hluk_path)
        self._startup_timeout_s = startup_timeout_s
        self._max_workspace_bytes = max_workspace_bytes
        # The `hluk` processes running, each a guest; how many times they were all stopped; and
        # whether the session is stopping or shutting down. A command that starts its guest
        # after the stop it began before, or while the session stops, is stopped too.
        self._guests: set[asyncio.subprocess.Process] = set()
        # Those a stop killed, to tell from any other failure
        self._stopped_guests: set[asyncio.subprocess.Process] = set()
        self._guest_stops = 0
        self._stopping = False
        # The `hluk` that passed the version check and started a guest, and the temporary
        # directory resolved then.
        self._hluk: str | None = None
        self._temporary: Path | None = None

    @classmethod
    def from_state(  # type: ignore[override]
        cls,
        state: HyperlightSandboxSessionState,
        *,
        hluk_path: str | None = None,
        startup_timeout_s: float = DEFAULT_HYPERLIGHT_STARTUP_TIMEOUT_S,
        max_workspace_bytes: int | None = DEFAULT_HYPERLIGHT_MAX_WORKSPACE_BYTES,
    ) -> HyperlightSandboxSession:
        return cls(
            state=state,
            hluk_path=hluk_path,
            startup_timeout_s=startup_timeout_s,
            max_workspace_bytes=max_workspace_bytes,
        )

    async def _prepare_backend_workspace(self) -> None:
        # Once the workspace root exists, so that the start's guest mounts it as a command's
        # would, and `hluk` refuses here what it would refuse for every command.
        try:
            if self.state.workspace_root_owned:
                # A resumed session's workspace was removed when it stopped: made again here,
                # owner-only as create() made it, rather than by UnixLocal with the umask's mode.
                try:
                    await run_blocking_workspace_io(_remake_owned_workspace, self.state.manifest.root)
                except (OSError, ValueError) as e:
                    raise WorkspaceStartError(path=Path(self.state.manifest.root), message=str(e), cause=e) from e
            await super()._prepare_backend_workspace()
            hluk, temporary = await self._start_backend()
        except BaseException:
            # A restart whose checks fail leaves nothing of the last start to run commands with;
            # commands running meanwhile keep what the last start gave them.
            self._hluk = None
            self._temporary = None
            raise
        # Only an `hluk` that passed the version check and started a guest runs commands.
        self._hluk = hluk
        self._temporary = temporary

    async def _start_backend(self) -> tuple[str, Path]:
        # The first start of a runtime pulls its image and saves its warm snapshot. Doing it here,
        # within startup_timeout_s, keeps that out of the first command's timeout and reports a
        # missing `hluk` or hypervisor when the session starts.
        workspace = Path(self.state.manifest.root)
        try:
            temporary = await run_blocking_workspace_io(_resolved_temporary)
            shared = await run_blocking_workspace_io(self._shared_paths, temporary)
        except (OSError, ValueError) as e:
            raise WorkspaceStartError(path=workspace, message=str(e), cause=e) from e
        hluk = await run_blocking_workspace_io(_resolve_hluk, self._hluk_path)
        if hluk is None:
            raise WorkspaceStartError(
                path=workspace,
                message=_HLUK_NOT_FOUND,
            )
        # With the mounts a command gets, and a control directory like its own, so that `hluk`
        # refuses here what it would refuse for every command.
        control = os.path.join(temporary, _CONTROL_PREFIX + secrets.token_hex(8))
        try:
            try:
                await run_blocking_workspace_io(os.mkdir, control, 0o700)
                mounts = await run_blocking_workspace_io(_mount_args, shared)
            except OSError as e:
                raise WorkspaceStartError(
                    path=workspace,
                    message=f"cannot make a command's control directory in {temporary}: {e}",
                    cause=e,
                ) from e
            await self._probe_guest(hluk, workspace, mounts, control)
            return hluk, temporary
        finally:
            await run_blocking_workspace_io(_remove_tree, control)

    async def _probe_guest(self, hluk: str, workspace: Path, mounts: list[str], control: str) -> None:
        command = [
            *self._hluk_run_prefix(hluk),
            *mounts,
            f"--mount={control}:{control}",
            "--exec",
            "pass",
        ]
        try:
            # One limit for both: the version check and the guest.
            deadline = asyncio.get_running_loop().time() + self._startup_timeout_s
            version = await self._run_hluk([hluk, "--version"], timeout=self._startup_timeout_s)
            if version.exit_code != 0:
                raise WorkspaceStartError(
                    path=workspace,
                    message="`hluk --version` failed",
                    context={
                        "exit_code": version.exit_code,
                        "stderr": _decode_tail(version.stderr),
                    },
                )
            supported = _supported_hluk(version.stdout)
            if not supported:
                minimum = ".".join(map(str, MIN_HLUK_VERSION))
                raise WorkspaceStartError(
                    path=workspace,
                    message=(
                        "cannot read the version `hluk --version` printed"
                        if supported is None
                        else f"the Hyperlight backend needs hluk {minimum} or later"
                    ),
                    context={"version": _decode_tail(version.stdout + version.stderr, 200)},
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            result = await self._run_hluk(command, timeout=remaining)
        except FileNotFoundError as e:
            raise WorkspaceStartError(
                path=workspace,
                message=_HLUK_NOT_FOUND,
                cause=e,
            ) from e
        except asyncio.TimeoutError as e:
            raise WorkspaceStartError(
                path=workspace,
                message=f"the Hyperlight guest did not start within {self._startup_timeout_s} s",
                cause=e,
            ) from e
        except OSError as e:
            raise WorkspaceStartError(path=workspace, message="failed to start the Hyperlight guest", cause=e) from e
        if result.exit_code != 0:
            reason = _decode_tail(result.stderr).strip().splitlines()
            raise WorkspaceStartError(
                path=workspace,
                message="failed to start the Hyperlight guest" + (f": {reason[-1]}" if reason else ""),
                context={"exit_code": result.exit_code, "stderr": _decode_tail(result.stderr)},
            )

    def supports_pty(self) -> bool:
        return False

    async def pty_exec_start(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
        tty: bool = False,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        return await BaseSandboxSession.pty_exec_start(
            self,
            *command,
            timeout=timeout,
            shell=shell,
            user=user,
            tty=tty,
            yield_time_s=yield_time_s,
            max_output_tokens=max_output_tokens,
        )

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        return await BaseSandboxSession.pty_write_stdin(
            self,
            session_id=session_id,
            chars=chars,
            yield_time_s=yield_time_s,
            max_output_tokens=max_output_tokens,
        )

    async def pty_terminate_all(self) -> None:
        # No PTYs, but the SDK calls this before it snapshots the workspace and before it shuts
        # the session down: the guests still running stop first, so that none writes the
        # workspace meanwhile.
        self._guest_stops += 1
        guests = list(self._guests)
        for guest in guests:
            self._stopped_guests.add(guest)
            _kill_guest(guest)
        if guests:
            with suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*(guest.wait() for guest in guests)), timeout=_KILL_WAIT_S)

    # Until the workspace is archived, or the session is down, no command starts a guest.
    async def _before_stop(self) -> None:
        self._stopping = True
        await super()._before_stop()

    async def _after_stop(self) -> None:
        self._stopping = False
        await super()._after_stop()

    async def _before_shutdown(self) -> None:
        self._stopping = True
        await super()._before_shutdown()

    async def shutdown(self) -> None:
        try:
            await super().shutdown()
        finally:
            # However it ended, as the SDK runs _after_shutdown only if all of it succeeded:
            # commands need the session started again.
            self._hluk = None
            self._temporary = None
            self._stopping = False

    async def persist_workspace(self) -> io.IOBase:
        root = self.state.manifest.root
        # A missing root is the SDK's to report
        if self._max_workspace_bytes is not None and await run_blocking_workspace_io(os.path.lexists, root):
            try:
                too_large = await run_blocking_workspace_io(
                    _archive_too_large,
                    root,
                    self._persist_workspace_skip_relpaths(),
                    self._max_workspace_bytes,
                )
            except (tarfile.TarError, OSError, RecursionError) as e:
                raise WorkspaceArchiveReadError(path=Path(root), cause=e) from e
            if too_large:
                raise WorkspaceArchiveReadError(
                    path=Path(root),
                    context={
                        "reason": "workspace_too_large",
                        "limit_bytes": self._max_workspace_bytes,
                    },
                )
        return await super().persist_workspace()

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        raise ExposedPortUnavailableError(
            port=port,
            exposed_ports=self.state.exposed_ports,
            reason="backend_unavailable",
            context={"backend": type(self).__name__},
        )

    def _prepare_exec_command(
        self,
        *command: str | Path,
        shell: bool | list[str],
        user: str | User | None,
    ) -> list[str]:
        # UnixLocal runs user-scoped file operations through host sudo, which would act outside
        # the guest; the guest has no accounts to switch to either.
        if user:
            raise ValueError("HyperlightSandboxSession does not support sandbox users")
        return super()._prepare_exec_command(*command, shell=shell, user=None)

    async def _validate_manifest_application(
        self,
        *,
        only_ephemeral: bool = False,
        manifest: Manifest | None = None,
        session_running: bool | None = None,
    ) -> None:
        _assert_supported_manifest(manifest or self.state.manifest)
        await super()._validate_manifest_application(
            only_ephemeral=only_ephemeral, manifest=manifest, session_running=session_running
        )

    async def _exec_checked_nonzero(self, *command: str | Path) -> ExecResult:
        # Manifest entries set their permissions with chmod. They are set from the host, rather
        # than in a guest booted for each entry, and links are not followed out of the workspace,
        # as UnixLocal's own file operations do not follow them.
        parts = [str(part) for part in command]
        if len(parts) == 3 and parts[0] == "chmod":
            path = Path(parts[2])
            try:
                await run_blocking_workspace_io(self._chmod, int(parts[1], 8), path)
            except OSError as e:
                raise WorkspaceArchiveWriteError(path=path, cause=e) from e
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        return await super()._exec_checked_nonzero(*command)

    def _chmod(self, mode: int, path: Path) -> None:
        with self._files.parent(self.normalize_path(path), for_write=True) as (parent_fd, name):
            if _chmod_nofollow(name, parent_fd, mode, check=partial(_check_chmod_target, path=path)):
                return
            # Without /proc, through a descriptor opened on it, which needs read permission (an
            # entry the owner cannot read is refused there); checked before too, so that no
            # device swapped in is opened.
            _check_chmod_target(os.stat(name, dir_fd=parent_fd, follow_symlinks=False), path=path)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            try:
                _check_chmod_target(os.fstat(fd), path=path)
                os.fchmod(fd, mode)
            finally:
                os.close(fd)

    async def _exec_internal(self, *command: str | Path, timeout: float | None = None) -> ExecResult:
        # As the command begins: a stop while it is set up stops its guest too.
        stops = self._guest_stops
        hluk, temporary = self._hluk, self._temporary
        if hluk is None or temporary is None:
            raise ExecTransportError(
                command=command,
                context={"reason": "session_not_started"},
                message="HyperlightSandboxSession runs commands only once started",
            )
        try:
            shared = await run_blocking_workspace_io(self._shared_paths, temporary)
        except (OSError, ValueError) as e:
            raise ExecTransportError(command=command, message=str(e), cause=e) from e
        if not await run_blocking_workspace_io(_is_directory, shared[0][0]):
            raise WorkspaceRootNotFoundError(path=Path(self.state.manifest.root))
        env = await self.state.manifest.environment.resolve()
        env["HOME"] = self.state.manifest.root

        # Named before it is made, so that the cleanup owns it even if this is cancelled while it
        # is being made.
        control = os.path.join(temporary, _CONTROL_PREFIX + secrets.token_hex(8))
        try:
            try:
                await run_blocking_workspace_io(os.mkdir, control, 0o700)
            except OSError as e:
                raise ExecTransportError(command=command, cause=e) from e
            status_path = os.path.join(control, "status")
            spec = {
                "argv": [str(part) for part in command],
                "cwd": self.state.manifest.root,
                "env": env,
                "status": status_path,
            }
            spec_path = os.path.join(control, "spec.json")
            script_path = os.path.join(control, "exec.py")
            try:
                # The spec holds the command's environment, so it is written owner-only rather
                # than passed on the `hluk` command line, which other host users can read.
                await run_blocking_workspace_io(_write_private_file, spec_path, json.dumps(spec))
                await run_blocking_workspace_io(
                    _write_private_file,
                    script_path,
                    _GUEST_EXEC_SCRIPT.format(spec_path=spec_path),
                )
                mounts = await run_blocking_workspace_io(_mount_args, shared)
            except OSError as e:
                raise ExecTransportError(command=command, cause=e) from e
            hluk_command = [
                *self._hluk_run_prefix(hluk),
                *mounts,
                f"--mount={control}:{control}",
                script_path,
            ]
            try:
                result, stopped = await self._run_guest(hluk_command, timeout=timeout, stops=stops)
            except asyncio.TimeoutError as e:
                raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
            except OSError as e:
                raise ExecTransportError(command=command, cause=e) from e

            exit_code = await run_blocking_workspace_io(_read_status, status_path)
            # A guest that exited during the command, as `kill -9 -1` makes it, rather than one
            # that ended and left no status it could have, or an `hluk` or hypervisor failure.
            guest_status = _guest_exit_status(result) if exit_code is None else None
            stopped_during_command = guest_status is not None and await run_blocking_workspace_io(
                _is_regular_file, os.path.join(control, "started")
            )
            if stopped_during_command:
                # As a command killed by a signal would end on the host: a failed command, with
                # the output it wrote, rather than a failure of the sandbox that ends the run.
                exit_code = guest_status
            elif exit_code is None and stopped:
                raise ExecTransportError(
                    command=command,
                    context={
                        "reason": "session_stopping",
                        "hluk_exit_code": result.exit_code,
                        "stderr": _decode_tail(result.stderr),
                    },
                    message="the session stopped while the command ran, and stopped its guest",
                )
            # Killed by a stop once it had finished, the command's result stands.
            elif exit_code is None or (result.exit_code != 0 and not stopped):
                raise ExecTransportError(
                    command=command,
                    context={
                        "reason": "hyperlight_guest_failed",
                        "hluk_exit_code": result.exit_code,
                        # `hluk`'s own: the command's output stays out of the error, which logs
                        # and traces record, as it can hold the command's environment.
                        "stderr": _decode_tail(result.stderr),
                    },
                )
            try:
                stdout, stderr = (
                    await run_blocking_workspace_io(_read_output, os.path.join(control, "stdout")),
                    await run_blocking_workspace_io(_read_output, os.path.join(control, "stderr")),
                )
            except OSError as e:
                raise ExecTransportError(command=command, cause=e) from e
            if stopped_during_command:
                stderr += b"\n[the Hyperlight guest stopped before the command finished]\n"
            return ExecResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        finally:
            # It holds the command's environment; the removal finishes even if this is cancelled.
            await run_blocking_workspace_io(_remove_tree, control)

    def _hluk_run_prefix(self, hluk: str) -> list[str]:
        # Values are joined to their flags so that one starting with "-", say from a saved
        # state, cannot be read as another option.
        args = [hluk, "run"]
        if self.state.initrd is not None:
            # Restored from a warm snapshot, as a runtime is by default.
            args += [f"--initrd={self.state.initrd}", "--warm"]
        else:
            args.append(f"--runtime={self.state.runtime or DEFAULT_HYPERLIGHT_RUNTIME}")
        if self.state.scratch_mb is not None:
            args.append(f"--scratch-mb={self.state.scratch_mb}")
        args += [f"--net-allow={host}" for host in self.state.net_allow]
        return args

    def _shared_paths(self, temporary: Path) -> list[tuple[str, bool]]:
        """The workspace and the grants, each a host directory shared at the same guest path.

        `hluk` opens each by name for every command, so none may go through a link, which could
        be repointed between commands, or across a stop and a resume, to another host directory:
        each must be given as its resolved path, and is checked again before each command. What
        could still change is a directory inside a path a guest can write, so no shared path may
        lie in another: a guest could replace it with a link, or create it where it is missing
        and write there despite a read-only grant. Nor may the temporary directory, which holds
        each command's environment, lie in one.
        """
        manifest = self.state.manifest
        shared = [(manifest.root, False)]
        shared += [(grant.path, grant.read_only) for grant in manifest.extra_path_grants]
        for index, (path, _) in enumerate(shared):
            resolved = os.path.realpath(path)
            if resolved != path:
                reason = "must be given resolved" if resolved == os.path.normpath(path) else "cannot go through a link"
                raise ValueError(f"Hyperlight sandbox paths {reason}: give {path} as {resolved}")
            if index and os.path.lexists(path) and not _is_directory(path):
                raise ValueError("Hyperlight sandboxes support extra path grants for directories only")
            for other, _ in shared[index + 1 :]:
                if _nested(Path(path), Path(other)):
                    raise ValueError(
                        f"Hyperlight sandbox path grants cannot overlap the workspace or each other: {other}"
                    )
            if Path(path) == temporary or Path(path) in temporary.parents:
                raise ValueError(
                    f"the temporary directory {temporary} lies in a path the sandbox shares: "
                    "set TMPDIR to one outside it"
                )
        # `--mount` takes HOST:GUEST[:ro]; what else `hluk` refuses in a mount, it says when the
        # session starts, as that guest mounts all of them.
        for path in [*(path for path, _ in shared), str(temporary)]:
            if ":" in path:
                raise ValueError(f"Hyperlight sandbox paths cannot contain ':': {path}")
        return shared

    def _should_compute_snapshot_fingerprint_on_persist(self) -> bool:
        # The fingerprint helper is installed in the guest's own filesystem, which the next
        # command's guest does not have.
        return False

    async def _run_hluk(self, command: list[str], *, timeout: float | None) -> ExecResult:
        result, _ = await self._run_guest(command, timeout=timeout, stops=self._guest_stops)
        return result

    async def _run_guest(self, command: list[str], *, timeout: float | None, stops: int) -> tuple[ExecResult, bool]:
        """`hluk`'s result, and whether a stop of the session killed it."""
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # `hluk`'s own profile would otherwise be part of every command's stderr.
            env={name: value for name, value in os.environ.items() if name != "HLUK_PROFILE"},
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        self._guests.add(process)
        try:
            if self._stopping or self._guest_stops != stops:
                # Started while the session stops, or since the guests were stopped
                self._stopped_guests.add(process)
                _kill_guest(process)
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(_tail(process.stdout), _tail(process.stderr), process.wait()),
                timeout=timeout,
            )
        except BaseException:
            # A timeout or a cancelled caller: the guest lives in the `hluk` process, so killing
            # it stops the command too.
            _kill_guest(process)
            with suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=_KILL_WAIT_S)
            raise
        finally:
            self._guests.discard(process)
            stopped = process in self._stopped_guests and process.returncode == -signal.SIGKILL
            self._stopped_guests.discard(process)
        result = ExecResult(stdout=stdout or b"", stderr=stderr or b"", exit_code=process.returncode or 0)
        return result, stopped


class HyperlightSandboxClient(BaseSandboxClient[HyperlightSandboxClientOptions | None]):
    """Create sessions whose commands run in local Hyperlight micro-VMs through `hluk`.

    Requires the `hluk` CLI, v0.19.0 or later, and a hypervisor: KVM or MSHV on Linux. `hluk`
    also runs on Apple silicon macOS, where this backend is untested.
    """

    backend_id = "hyperlight"
    supports_default_options = True
    _instrumentation: Instrumentation

    def __init__(
        self,
        *,
        hluk_path: str | None = None,
        startup_timeout_s: float = DEFAULT_HYPERLIGHT_STARTUP_TIMEOUT_S,
        max_workspace_bytes: int | None = DEFAULT_HYPERLIGHT_MAX_WORKSPACE_BYTES,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        if startup_timeout_s <= 0:
            raise ValueError("startup_timeout_s must be positive")
        if max_workspace_bytes is not None and max_workspace_bytes < 0:
            raise ValueError("max_workspace_bytes cannot be negative")
        self._max_workspace_bytes = max_workspace_bytes
        # Kept on the trusted client rather than in session state, so a saved state cannot choose
        # which host program runs; a path is made absolute here, a bare name is looked up on PATH
        # when a session starts.
        self._hluk_path = _absolute_hluk_path(hluk_path)
        self._startup_timeout_s = startup_timeout_s
        self._instrumentation = instrumentation if instrumentation is not None else Instrumentation()
        self._dependencies = dependencies

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: HyperlightSandboxClientOptions | None = None,
    ) -> SandboxSession:
        resolved_options = options if options is not None else HyperlightSandboxClientOptions()
        if not isinstance(resolved_options, HyperlightSandboxClientOptions):
            raise TypeError(
                f"HyperlightSandboxClient takes HyperlightSandboxClientOptions, not {type(resolved_options).__name__}"
            )
        manifest = manifest if manifest is not None else Manifest()
        _assert_supported_manifest(manifest)
        self._validate_manifest_for_create(manifest)
        session_id = uuid.uuid4()
        resolved_snapshot = resolve_snapshot(snapshot, str(session_id))
        initrd = resolved_options.initrd
        if initrd is not None:
            # Absolute, so that the same file boots whatever the working directory later.
            initrd = await run_blocking_workspace_io(_absolute_path, initrd)
        # The workspace is made last, once nothing else can fail and leave it behind.
        workspace_root_owned = manifest.root == _DEFAULT_MANIFEST_ROOT
        if workspace_root_owned:
            # Named before it is made, so that it is removed even if this is cancelled while it
            # is being made.
            temporary = await run_blocking_workspace_io(_resolved_temporary)
            workspace_dir = str(temporary / (_WORKSPACE_PREFIX + secrets.token_hex(8)))
            try:
                await run_blocking_workspace_io(os.mkdir, workspace_dir, 0o700)
            except FileExistsError:
                # Not made here
                raise
            except BaseException:
                await run_blocking_workspace_io(_remove_owned_workspace, workspace_dir)
                raise
            manifest = manifest.model_copy(update={"root": workspace_dir}, deep=True)
        try:
            state = HyperlightSandboxSessionState(
                session_id=session_id,
                manifest=manifest,
                snapshot=resolved_snapshot,
                workspace_root_owned=workspace_root_owned,
                runtime=resolved_options.runtime,
                initrd=initrd,
                scratch_mb=resolved_options.scratch_mb,
                net_allow=resolved_options.net_allow,
            )
        except BaseException:
            if workspace_root_owned:
                await run_blocking_workspace_io(_remove_tree, manifest.root)
            raise
        return self._wrap_session(self._session(state), instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        """Best-effort cleanup of a workspace directory the client created."""
        inner = session._inner
        if not isinstance(inner, HyperlightSandboxSession):
            raise TypeError("HyperlightSandboxClient.delete expects a HyperlightSandboxSession")
        if inner.state.workspace_root_owned:
            with suppress(Exception):
                await run_blocking_workspace_io(_remove_owned_workspace, inner.state.manifest.root)
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, HyperlightSandboxSessionState):
            raise TypeError("HyperlightSandboxClient.resume expects a HyperlightSandboxSessionState")
        state.assert_path_grants_rebound()
        _assert_supported_manifest(state.manifest)
        return self._wrap_session(self._session(state), instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return self._deserialize_session_state_payload(payload, HyperlightSandboxSessionState)

    def _session(self, state: HyperlightSandboxSessionState) -> HyperlightSandboxSession:
        return HyperlightSandboxSession.from_state(
            state,
            hluk_path=self._hluk_path,
            startup_timeout_s=self._startup_timeout_s,
            max_workspace_bytes=self._max_workspace_bytes,
        )


def _validate_guest_configuration(runtime: str | None, initrd: str | None, scratch_mb: int | None) -> None:
    if runtime is not None and initrd is not None:
        raise ValueError("a Hyperlight sandbox takes runtime or initrd, not both")
    if runtime == "" or initrd == "":
        raise ValueError("a Hyperlight sandbox's runtime or initrd cannot be empty")
    if scratch_mb is not None and scratch_mb < 1:
        raise ValueError("scratch_mb must be at least 1")


def _assert_supported_manifest(manifest: Manifest) -> None:
    if any(grant.host_path is not None for grant in manifest.extra_path_grants):
        raise ValueError(
            "HyperlightSandboxClient does not support sandbox path grant host_path: a grant is "
            "shared into the guest at its own path"
        )
    if manifest.mount_targets():
        raise ValueError("HyperlightSandboxClient does not support manifest mounts")
    # Refused up front, as chgrp would otherwise fail part way through writing the workspace.
    if manifest.users or manifest.groups:
        raise ValueError("HyperlightSandboxClient does not support manifest users or groups")
    # Each command is a fresh guest, and a repository is cloned in one and copied in another.
    if any(isinstance(entry, GitRepo) for _, entry in manifest.iter_entries()):
        raise ValueError("HyperlightSandboxClient does not support Git repository entries")
    if any(entry.group is not None for _, entry in manifest.iter_entries()):
        raise ValueError("HyperlightSandboxClient does not support manifest entry groups")


def _supported_hluk(version_output: bytes) -> bool | None:
    """Whether the `hluk --version` output names a release this backend runs on; None if it
    names no version."""
    match = re.search(rb"(\d+)\.(\d+)\.(\d+)(-)?", version_output)
    if match is None:
        return None
    release = tuple(int(part) for part in match.groups()[:3])
    # A pre-release (`0.19.0-rc1`) comes before its release; build metadata (`+dev.1a2b3c4`)
    # does not.
    return release > MIN_HLUK_VERSION or (release == MIN_HLUK_VERSION and not match.group(4))


def _resolved_temporary() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def _remove_owned_workspace(root: str) -> None:
    # Only a directory create() could have made: a saved state names any root it likes.
    path = Path(root)
    if path.name.startswith(_WORKSPACE_PREFIX) and path.parent == _resolved_temporary():
        _remove_tree(root)


def _remake_owned_workspace(root: str) -> None:
    # Where delete() would remove it, as nowhere else
    path = Path(root)
    if not path.name.startswith(_WORKSPACE_PREFIX):
        raise ValueError(f"{root} is not a workspace HyperlightSandboxClient makes")
    if path.parent != _resolved_temporary():
        raise ValueError(
            f"a workspace the client made is resumed in the temporary directory it was made in: "
            f"{root} is not in {_resolved_temporary()}"
        )
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        # Still there, or made by another user at a name they could guess: only the former is
        # used, checked once open so that it cannot be swapped after the check.
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(fd).st_uid != os.getuid():
                raise ValueError(f"{root} belongs to another user") from None
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)


def _mount_args(shared: list[tuple[str, bool]]) -> list[str]:
    # Each share that is a directory now: a grant made after the session started is shared from
    # its next command, and a resumed session's workspace exists only once it is restored.
    return [
        f"--mount={path}:{path}" + (":ro" if read_only else "") for path, read_only in shared if _is_directory(path)
    ]


def _is_directory(path: str) -> bool:
    # Not through a link: a shared path is checked to be link-free, and stays so only until the
    # next check.
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _nested(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _check_chmod_target(entry: os.stat_result, *, path: Path) -> None:
    if not (stat.S_ISREG(entry.st_mode) or stat.S_ISDIR(entry.st_mode)):
        raise OSError(errno.EINVAL, "Not a regular file or directory", str(path))


def _absolute_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _absolute_hluk_path(hluk_path: str | None) -> str | None:
    # A path is made absolute; a bare name is left for a lookup on PATH.
    if hluk_path is None:
        return None
    expanded = os.path.expanduser(hluk_path)
    return os.path.abspath(expanded) if os.sep in expanded else expanded


def _resolve_hluk(hluk_path: str | None) -> str | None:
    # A bare name is looked up on PATH, never in the working directory (as an empty or relative
    # PATH entry would make it), and the result is absolute so that a later change of directory
    # does not change the program run.
    if hluk_path is not None and os.sep in hluk_path:
        return hluk_path
    path = os.environ.get("PATH", os.defpath).split(os.pathsep)
    found = shutil.which(hluk_path or "hluk", path=os.pathsep.join(entry for entry in path if os.path.isabs(entry)))
    return found


def _remove_tree(path: str) -> None:
    try:
        shutil.rmtree(path)
        return
    except FileNotFoundError:
        return
    except (OSError, RecursionError):
        pass
    # A guest can take the owner's permissions away from directories it was given, the control
    # directory included, which holds the command's environment, and nest them deeper than
    # shutil.rmtree recurses: remove the tree by hand.
    with suppress(OSError):
        _remove_tree_by_hand(path)


def _remove_tree_by_hand(root: str) -> None:
    # Without recursion, and by descriptor relative to each parent, so no path grows: each
    # directory is opened without following a link, given the owner's permissions back, and
    # read once. Past _REMOVE_DEPTH_MAX levels a directory is moved up to the root, so that few
    # descriptors are open at once, and no more than _REMOVE_ENTRIES_MAX directories are
    # visited, in case something still writes the tree. What cannot be removed is left, and the
    # rest still goes.
    parent, name = os.path.split(os.path.abspath(root))
    top = os.open(parent, _DIRECTORY_PATH_FLAGS)
    # Each directory open: its descriptor, its name in its parent, and the subdirectories left.
    stack: list[tuple[int, str, list[str]]] = []

    # Entries seen, files included, against _REMOVE_ENTRIES_MAX.
    visits = [0]

    def enter(child: str, dir_fd: int) -> None:
        # Pushed as soon as it is open, so that the descriptor is closed whatever follows.
        stack.append((_open_owned_directory(child, dir_fd), child, []))
        stack[-1][2].extend(_remove_files(stack[-1][0], visits))

    try:
        enter(name, top)
        while stack and visits[0] < _REMOVE_ENTRIES_MAX:
            fd, own_name, subdirectories = stack[-1]
            if not subdirectories:
                stack.pop()
                os.close(fd)
                with suppress(OSError):
                    os.rmdir(own_name, dir_fd=stack[-1][0] if stack else top)
                continue
            child = subdirectories.pop()
            visits[0] += 1
            if len(stack) < _REMOVE_DEPTH_MAX:
                with suppress(OSError):
                    enter(child, fd)
                continue
            # A move to another parent updates the directory's "..": it needs the permission.
            # What is no longer a directory is left where it is.
            try:
                _chmod_nofollow(child, fd, 0o700, check=_check_directory)
            except NotADirectoryError:
                continue
            except OSError:
                pass
            moved = secrets.token_hex(8)
            with suppress(OSError):
                os.rename(child, moved, src_dir_fd=fd, dst_dir_fd=stack[0][0])
                stack[0][2].append(moved)
    finally:
        for fd, _, _ in stack:
            os.close(fd)
        os.close(top)


def _remove_files(fd: int, visits: list[int]) -> list[str]:
    """Remove what the directory holds but directories, and return those, read in one pass."""
    subdirectories = []
    with suppress(OSError), os.scandir(fd) as scan:
        for entry in scan:
            visits[0] += 1
            if visits[0] >= _REMOVE_ENTRIES_MAX:
                break
            if entry.is_dir(follow_symlinks=False):
                subdirectories.append(entry.name)
            else:
                with suppress(OSError):
                    os.unlink(entry.name, dir_fd=fd)
    return subdirectories


def _open_owned_directory(name: str, dir_fd: int) -> int:
    """Open `name`, a directory and not a link, and give the owner its permissions on it back."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except PermissionError:
        # Not readable: changed without opening it, then opened.
        with suppress(OSError):
            _chmod_nofollow(name, dir_fd, 0o700, check=_check_directory)
        return os.open(name, flags, dir_fd=dir_fd)
    with suppress(OSError):
        os.fchmod(fd, 0o700)
    return fd


def _check_directory(entry: os.stat_result) -> None:
    if not stat.S_ISDIR(entry.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "Not a directory")


def _chmod_nofollow(
    name: str,
    dir_fd: int,
    mode: int,
    check: Callable[[os.stat_result], None] | None = None,
) -> bool:
    """Change `name`'s mode without following a link, after `check` of what it is.

    False when the host offers no way to without opening the entry, as Linux without /proc.
    """
    if os.chmod in os.supports_follow_symlinks:
        if check is not None:
            check(os.stat(name, dir_fd=dir_fd, follow_symlinks=False))
        os.chmod(name, mode, dir_fd=dir_fd, follow_symlinks=False)
        return True
    if not hasattr(os, "O_PATH"):
        return False
    # Linux has no chmod that refuses a link: through a path descriptor, which, unlike opening
    # the entry, needs no permission on it, and is checked once open, so the entry cannot be
    # swapped for another after the check.
    fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        if check is not None:
            check(os.fstat(fd))
        os.chmod(f"/proc/self/fd/{fd}", mode)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(fd)


def _write_private_file(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)


def _kill_guest(process: asyncio.subprocess.Process) -> None:
    # Only while it runs: once reaped, its process group id may belong to another.
    if process.returncode is None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)


class _ArchiveTooLarge(Exception):
    pass


class _ArchiveSizer(io.RawIOBase):
    """Counts what is written to it, and fails past `limit` bytes."""

    # What tarfile takes the archive's mode from
    mode = "wb"

    def __init__(self, limit: int) -> None:
        self.size = 0
        self.limit = limit

    def writable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.size

    def write(self, data) -> int:  # type: ignore[no-untyped-def, override]
        self.size += len(data)
        if self.size > self.limit:
            raise _ArchiveTooLarge
        return len(data)


def _archive_too_large(root: str, skip: set[Path], limit: int) -> bool:
    """Whether the tar the SDK would make of `root` is larger than `limit` bytes.

    Made as the SDK makes it, from the same members, with the same headers, but counted rather
    than kept, and given up on once past `limit`: the SDK keeps it in memory.
    """
    sizer = _ArchiveSizer(limit)
    roots = (Path(root), Path(root).resolve(strict=False))
    symlinks: list[tarfile.TarInfo] = []
    try:
        # Its end blocks, written as it closes, count too.
        with tarfile.open(fileobj=sizer, mode="w") as tar:

            def member(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
                # As the SDK does, so that each hard link is archived in full
                getattr(tar, "inodes").clear()  # noqa: B009
                if should_skip_tar_member(info.name, skip_rel_paths=skip, root_name=None):
                    return None
                if info.isfifo() or info.ischr() or info.isblk():
                    return None
                if info.issym():
                    symlinks.append(info)
                    return None
                return info

            tar.add(root, arcname=".", filter=member)
            # Links last, an absolute target under the workspace rewritten relative to the
            # link where the SDK proves it stays there, as UnixLocal's persist_workspace does
            members = {PurePosixPath(info.name).as_posix(): info for info in [*tar.getmembers(), *symlinks]}
            for link in symlinks:
                archived = copy.copy(link)
                if link.linkname.startswith("/"):
                    rebased = _rebase_symlink_target(link.linkname, link_name=link.name, roots=roots)
                    if rebased != link.linkname and _symlink_target_stays_under(
                        members, link_name=link.name, target=rebased
                    ):
                        archived.linkname = rebased
                tar.addfile(archived)
    except _ArchiveTooLarge:
        return True
    return False


def _guest_exit_status(result: ExecResult) -> int | None:
    """The status `hluk` reports its guest exited with, or None if it reports another failure."""
    if result.exit_code <= 0:
        # Exited, or `hluk` itself was killed
        return None
    match = _GUEST_EXITED.search(result.stderr)
    # `hluk` exits with the guest's status, so a line the guest printed last cannot pass for it
    if match is not None and int(match.group(1)) == result.exit_code:
        return result.exit_code
    if result.exit_code == 1 and _GUEST_ABORTED.search(result.stderr):
        return 128 + signal.SIGSEGV
    return None


def _is_regular_file(path: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _read_status(path: str) -> int | None:
    # The guest can write the control directory, so the status file is not followed if it was
    # replaced by a link, and only a bounded prefix of it is read.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return int(os.read(fd, _STATUS_MAX_BYTES))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


async def _tail(stream: asyncio.StreamReader) -> bytes:
    # The guest can print without end, so only the last of it is kept.
    kept = bytearray()
    while chunk := await stream.read(1 << 16):
        kept += chunk
        del kept[:-_HLUK_OUTPUT_TAIL_BYTES]
    return bytes(kept)


def _read_output(path: str) -> bytes:
    # The guest can write the control directory, so a file it replaced with a link, or with
    # anything but a regular file, is not read, and no more than _OUTPUT_MAX_BYTES of one is.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return b""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        with os.fdopen(fd, "rb", closefd=False) as file:
            data = file.read(_OUTPUT_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _OUTPUT_MAX_BYTES:
        return data[:_OUTPUT_MAX_BYTES] + b"\n[output truncated at %d bytes]\n" % _OUTPUT_MAX_BYTES
    return data


def _decode_tail(data: bytes, limit: int = 4096) -> str:
    return data[-limit:].decode("utf-8", errors="replace")
