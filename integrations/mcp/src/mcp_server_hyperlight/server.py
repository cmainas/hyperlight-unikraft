"""An MCP server whose tools run Python in a local Hyperlight micro-VM, through smolagents-hyperlight's executor."""

import argparse
import contextlib
import functools
import importlib.metadata
import os
import re
import signal
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Annotated, Any

import anyio
import anyio.to_thread
from anyio.lowlevel import RunVar
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from rich.console import Console
from smolagents.local_python_executor import MAX_EXECUTION_TIME_SECONDS
from smolagents.monitoring import AgentLogger, LogLevel
from smolagents.serialization import SerializationError
from smolagents.utils import AgentError
from smolagents_hyperlight import HyperlightExecutor


# Not main(), the console script: it takes the process's signals and ends it with os._exit
__all__ = ["Sandbox", "build_server"]

try:
    __version__ = importlib.metadata.version("mcp-server-hyperlight")
# Run from a checkout, without the distribution's metadata
except importlib.metadata.PackageNotFoundError:
    __version__ = "0+unknown"

# What the published `agent` image ships, for the model to know what it can import without installing
_AGENT_IMAGE_PACKAGES = "CPython 3.12 with numpy, pandas, scipy, scikit-learn, matplotlib, pillow and requests"


# What the executor tells a smolagents agent of its state, in the words of an MCP server's calls: the server defines no
# tools or variables in the guest, so none are defined again
# Read with a fallback: a private attribute, which a release of the executor could rename without the server failing
# to import. A change to any of these words fails the newest CI leg, which runs this repository's executor.
_STATE_LOST = getattr(
    HyperlightExecutor,
    "_STATE_LOST",
    "variables and imports from previous steps are lost, while tools and the agent's variables are defined again.",
)
# A restart notice, which the executor puts before what the code printed
_NOTICE = (
    re.compile(
        r"(Note: the sandbox was restarted before this code ran, after .+: variables and imports defined by earlier "
        r"code are gone), while tools and the agent's variables are defined again\.\Z"
    ),
    r"\1.",
)
# A failed call's message, after a restart notice or none
_RECOVERIES = (
    (
        re.compile(r"(.+, so the sandbox was restarted: )" + re.escape(_STATE_LOST) + r"\Z"),
        r"\1" + "variables and imports from earlier calls are lost.",
    ),
    (
        re.compile(
            r"(.+) while packages, tools and variables were defined in a fresh sandbox; the next step will try again "
            r"with another one\.\Z"
        ),
        r"\1 while a fresh sandbox was set up; the next call will try again with another one.",
    ),
)


def _reworded(line: str, wordings: tuple[tuple[re.Pattern[str], str], ...]) -> str | None:
    for pattern, words in wordings:
        if pattern.match(line):
            return pattern.sub(words, line)
    return None


def _for_mcp(text: str, failed: bool = False) -> str:
    # Only the executor's own lines, whole: its notice first, and on a failure its message then; any other line, the
    # code's output among them, is left as it was
    lines = text.split("\n", 2)
    if (notice := _reworded(lines[0], (_NOTICE,))) is not None:
        lines[0] = notice
        if failed and len(lines) > 1 and (message := _reworded(lines[1], _RECOVERIES)) is not None:
            lines[1] = message
    elif failed and (message := _reworded(lines[0], _RECOVERIES)) is not None:
        lines[0] = message
    return "\n".join(lines)


# Calls queued behind a long one wait for their turn without a worker thread: the default limiter's threads are the
# stdio transport's too, which they would otherwise all hold. A call the client gives up on returns its token at once,
# and its thread runs on until its code ends; the next call's thread then waits on the guest's lock.
_LIMITER: RunVar[anyio.CapacityLimiter] = RunVar("mcp_server_hyperlight_limiter")


def _limiter() -> anyio.CapacityLimiter:
    # One for each event loop, which a limiter belongs to
    try:
        return _LIMITER.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(1)
        _LIMITER.set(limiter)
        return limiter


def _packages(additional_imports: list[str]) -> list[str]:
    """The packages that `--package` names, as the executor reads them: `numpy.*` installs numpy."""
    return sorted({name.split(".")[0] for name in additional_imports} - {"", "*"})


# The most of a call's result, or error, the model gets, its beginning and its end: the executor keeps 2 MiB of what
# code prints, for a smolagents agent, many times what an MCP client takes in one tool result
MAX_RESULT_CHARS = 100_000


def _clip(text: str, limit: int) -> str:
    """The beginning and the end of text longer than `limit` characters, as the guest keeps of what code prints."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters truncated ...]\n{text[-half:]}"


_SHUT_DOWN = "The Hyperlight sandbox has been shut down"


class _Call:
    """A tool call, and whether the client gave up on it."""

    __slots__ = ("cancelled",)

    def __init__(self):
        self.cancelled = False


class Sandbox:
    """The server's one guest: started on first use, or ahead of it by `warm()`, and used by one tool call at a time.

    A guest keeps its state from one call to the next, as a notebook kernel does, so calls that overlap would see each
    other's variables half set: they take turns instead. A call the client gives up on while it waits for its turn
    does not run; one that runs goes on until it ends or its time is up, as the executor has no way to stop a step
    short of its own timeout, which always bounds how long one call holds the guest.
    """

    def __init__(self, executor_factory: Callable[..., HyperlightExecutor] | None = None, **executor_kwargs):
        # By default, one that finds hluk where it is installed, too
        self._executor_factory = executor_factory or _make_executor
        self._executor_kwargs = executor_kwargs
        # On the standard error: the standard output is the MCP client's
        self._logger = AgentLogger(LogLevel.INFO, console=Console(stderr=True, highlight=False))
        self._executor: HyperlightExecutor | None = None
        # Taken for as long as a call uses the guest
        self._lock = threading.Lock()
        # Whether code ran in the guest since it started or was reset: a reset of one that ran none has nothing to
        # discard, and would start the same guest again
        self._used = False
        self._closed = False

    def _get(self) -> HyperlightExecutor:
        # Under the lock
        if self._closed:
            raise ToolError(_SHUT_DOWN)
        if self._executor is None:
            try:
                executor = self._executor_factory(logger=self._logger, **self._executor_kwargs)
            # Exception: hluk found nowhere (RuntimeError), hluk_path names no program (FileNotFoundError), or hluk
            # does not answer --version (TimeoutExpired), as well as a guest that would not start. The model hears why,
            # and the next call tries again.
            except Exception as e:
                # hluk's and the guest's last output comes with it, up to 128 KiB
                raise ToolError(_clip(str(e) or type(e).__name__, MAX_RESULT_CHARS)) from e
            self._executor = executor
            # Set before close() reads `_executor`, or read after it set it: one of the two stops the guest
            if self._closed:
                self._executor = None
                executor.cleanup()
                raise ToolError(_SHUT_DOWN)
        return self._executor

    def warm(self):
        """Start the guest ahead of the first call, which then finds it ready. A failure is left for that call."""
        with self._lock:
            try:
                self._get()
            except ToolError as e:
                if not self._closed:
                    self._logger.log_error(f"The Hyperlight sandbox did not start, the first call will try again: {e}")

    async def call(self, function: Callable[[_Call], str]) -> str:
        """Run one of the tools on a worker thread, which a call the client gives up on leaves behind."""
        call = _Call()
        try:
            # Abandoned when cancelled, rather than waited for: the client hears at once, and the thread skips the
            # code if it has yet to run it
            return await anyio.to_thread.run_sync(function, call, abandon_on_cancel=True, limiter=_limiter())
        except anyio.get_cancelled_exc_class():
            call.cancelled = True
            raise

    @staticmethod
    def _check_cancelled(call: _Call):
        # A call the client gave up on while it waited for its turn: nobody reads what it would do
        if call.cancelled:
            raise ToolError("The call was cancelled")

    @contextlib.contextmanager
    def _turn(self, call: _Call):
        """The guest's lock, for `call`, which a cancel takes out of the wait: a thread the client gave up on, and
        anyio left behind, does not wait out a long call for nothing."""
        while not self._lock.acquire(timeout=0.1):
            self._check_cancelled(call)
        try:
            yield
        finally:
            self._lock.release()

    def run(self, code: str, call: _Call | None = None) -> str:
        call = call or _Call()
        with self._turn(call):
            # Before the guest is started for it, and again after
            self._check_cancelled(call)
            executor = self._get()
            self._check_cancelled(call)
            self._used = True
            try:
                result = executor(code)
            # AgentError: the code's own error, with what it printed. RuntimeError: a guest that crashed or timed out
            # and could not be replaced. SerializationError: code that raised what reads as a final answer.
            except (AgentError, RuntimeError, SerializationError) as e:
                # Without the empty line the executor puts first when the code printed nothing
                message = (getattr(e, "message", None) or str(e)).lstrip("\n")
                raise ToolError(_clip(_for_mcp(message, failed=True), MAX_RESULT_CHARS)) from e
        parts = [_for_mcp(result.logs.rstrip("\n"))] if result.logs.strip() else []
        if result.output is not None or result.is_final_answer:
            # A trailing expression's value comes as its repr; a final answer, which code that raises what reads as
            # one makes, as the object rebuilt from it
            value = repr(result.output) if result.is_final_answer else result.output
            parts.append(f"Out: {value}")
        return _clip("\n".join(parts), MAX_RESULT_CHARS) or "(no output)"

    def reset(self, call: _Call | None = None) -> str:
        call = call or _Call()
        # After code still running, which its time limit ends
        with self._turn(call):
            self._check_cancelled(call)
            if self._closed:
                raise ToolError(_SHUT_DOWN)
            if self._executor is None:
                # The guest it starts is a fresh one
                self._get()
                # As for run(): one the client gave up on while the guest started reports to nobody
                self._check_cancelled(call)
            else:
                executor = self._executor
                requested = self._executor_kwargs.get("additional_imports") or []
                # A package that failed to install, which a reset tries again: the executor's own installs only the
                # ones that did
                # Only with the network to install them: without, a reset could not do better
                missing = self._executor_kwargs.get("net_allow") and (
                    set(_packages(requested)) - set(executor.installed_packages)
                )
                if self._used or missing or self._stopped(executor):
                    executor.installed_packages = list(requested)
                    try:
                        executor.reset()
                    except RuntimeError as e:
                        raise ToolError(_clip(_for_mcp(str(e), failed=True), MAX_RESULT_CHARS)) from e
                    self._used = False
        return "The sandbox was reset: variables and imports from earlier calls are gone."

    @staticmethod
    def _stopped(executor: HyperlightExecutor) -> bool:
        # A guest that exited unused, which a reset replaces all the same
        process = executor.process
        return process is None or process.poll() is not None

    def close(self):
        """Stop the guest. Not under the lock, which a running call holds: the call fails at once instead."""
        self._closed = True
        if (executor := self._executor) is not None:
            executor.cleanup()


def _describe_run_python(
    net_allow: list[str], timeout: int | None, image: str | None, packages: list[str], hluk_args: bool
) -> str:
    lines = [
        "Run Python code in a sandbox: a local Hyperlight micro-VM, isolated from this machine by a hypervisor.",
        "Variables, imports and functions persist from one call to the next, as in a notebook, until reset_sandbox. "
        "The result is what the code prints, then the value of a trailing expression after `Out:`.",
    ]
    if image:
        lines.append(f"The sandbox has {image}.")
    # Only with --net-allow, as the executor tries pip only then, whatever else the hluk arguments open
    if packages and net_allow:
        lines.append(f"It installs {', '.join(packages)} at start, where its image lacks them and its network allows.")
    elif packages:
        lines.append(f"It has {', '.join(packages)} only where its image does: without network, none is installed.")
    # More arguments for hluk can mount host directories or open the network: the sandbox is then not described as
    # closed
    if net_allow and hluk_args:
        lines.append(f"Network: connections to {', '.join(net_allow)}, and what else the server's settings allow.")
    elif net_allow:
        lines.append(f"Network: only connections to {', '.join(net_allow)}.")
    elif not hluk_args:
        lines.append("Network: none, every connection is refused.")
    if not hluk_args:
        lines.append("Files: the sandbox has its own filesystem, and none of this machine's files.")
    if timeout is None:
        # A Sandbox made without a time limit, which the command line refuses
        lines.append("A crash restarts the sandbox, and the variables of earlier calls are lost.")
    else:
        lines.append(
            f"A call that runs longer than {timeout} seconds is stopped. That, or a crash, restarts the sandbox, and "
            "the variables of earlier calls are lost."
        )
    return "\n".join(lines)


def _describe_reset(timeout: int | None, packages: list[str], net_allow: list[str]) -> str:
    restore = "in milliseconds"
    if packages and net_allow:
        restore += f", then {', '.join(packages)} installed again"
    each = f", each of which runs for at most {timeout} seconds, then for the restart one that ran out of time needs"
    return (
        f"Replace the sandbox with a fresh one, {restore}: variables and imports are gone. It first waits for calls "
        f"still running or sent before it{each if timeout is not None else ''}; longer while the sandbox first "
        "starts, its image pulled."
    )


def build_server(sandbox: Sandbox, warm: bool = True) -> MCPServer:
    """The MCP server, with `sandbox` behind its tools, which describe it to the model as it was made. With `warm`,
    the guest starts as the server does."""
    settings = sandbox._executor_kwargs
    net_allow = settings.get("net_allow") or []
    timeout = settings.get("timeout_seconds", MAX_EXECUTION_TIME_SECONDS)
    is_agent = settings.get("initrd") is None and settings.get("runtime", "agent") == "agent"
    image = _AGENT_IMAGE_PACKAGES if is_agent else None
    packages = _packages(settings.get("additional_imports") or [])
    # Whether `hluk run` takes more arguments, which can mount host directories or open the network
    hluk_args = bool(settings.get("hluk_args"))

    async def run_python(code: Annotated[str, Field(description="The Python code to run")]) -> str:
        return await sandbox.call(functools.partial(sandbox.run, code))

    async def reset_sandbox() -> str:
        return await sandbox.call(sandbox.reset)

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer):
        if warm:
            # So that the first call finds the guest ready, rather than wait for it, or for the image's pull
            threading.Thread(target=sandbox.warm, daemon=True).start()
        yield

    server = MCPServer(
        "hyperlight",
        version=__version__,
        instructions=(
            "Runs Python in a local, hardware-isolated micro-VM that keeps its state between calls. Use run_python "
            "for computation, data analysis and code that should not touch this machine."
        ),
        lifespan=lifespan,
    )
    server.add_tool(
        run_python,
        title="Run Python in a micro-VM",
        description=_describe_run_python(net_allow, timeout, image, packages, hluk_args),
        annotations=ToolAnnotations(
            read_only_hint=False,
            # A host directory mounted read-write can lose what the code deletes, and an allowed host what the
            # code sends it: the allowlist names destinations, not what is done there
            destructive_hint=bool(net_allow) or hluk_args,
            idempotent_hint=False,
            open_world_hint=bool(net_allow) or hluk_args,
        ),
        structured_output=False,
    )
    server.add_tool(
        reset_sandbox,
        title="Reset the sandbox",
        description=_describe_reset(timeout, packages, net_allow),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            # Installing the packages again reaches the package index
            open_world_hint=bool(packages and net_allow),
        ),
        structured_output=False,
    )
    return server


def _number(kind: type[int] | type[float], minimum: float, what: str) -> Callable[[str], int | float]:
    """An argparse type for a number of `kind` of at least `minimum`, refused at launch rather than by every call."""

    def parse(text: str) -> int | float:
        try:
            value = kind(text)
        except ValueError:
            value = minimum - 1
        # Not `value < minimum`, which a NaN passes
        if not value >= minimum:
            raise argparse.ArgumentTypeError(f"not {what}: {text!r}")
        return value

    return parse


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp-server-hyperlight",
        description="An MCP server, on stdio, that runs Python in a local Hyperlight micro-VM through hluk.",
    )
    image = parser.add_mutually_exclusive_group()
    # No default here, so that argparse tells an explicit `--runtime agent` from none and refuses it with --initrd
    image.add_argument("--runtime", help="published image to run, by name or reference (agent)")
    image.add_argument("--initrd", help="path to a rootfs CPIO of your own to run instead (it must have CPython 3)")
    parser.add_argument(
        "--scratch-mb",
        type=_number(int, 1, "a number of MiB"),
        metavar="MIB",
        help="guest memory in MiB (the image's)",
    )
    parser.add_argument(
        "--net-allow", action="append", default=[], metavar="HOST", help="host or IP the guest may connect to (none)"
    )
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        help="package to install in the guest at start and after each reset, where the image lacks it (needs "
        "--net-allow)",
    )
    parser.add_argument(
        "--timeout",
        # Not 0 for no limit: the time limit is all that ends code that never does, which would hold the guest for
        # good, and every call after it
        type=_number(int, 1, "a whole number of seconds above 0"),
        default=MAX_EXECUTION_TIME_SECONDS,
        metavar="SECONDS",
        help=f"longest a call may run, and a reset wait for one ({MAX_EXECUTION_TIME_SECONDS})",
    )
    parser.add_argument(
        "--startup-timeout",
        type=_number(float, 1e-3, "a number of seconds above 0"),
        default=300.0,
        metavar="SECONDS",
        help="longest to wait for the guest to start, its image's first pull included (300)",
    )
    parser.add_argument("--hluk-path", help="the hluk binary (hluk on PATH, or in the install scripts' directory)")
    parser.add_argument(
        "--hluk-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="more arguments for `hluk run`, one per flag: --hluk-arg=--mount --hluk-arg=/data:/data:ro",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def _flush():
    # Before os._exit, which leaves what the standard error buffers unwritten. Not the standard output, whose writer
    # the transport may hold, blocked on a client that stopped reading.
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.flush()


def _shut_down(sandbox: Sandbox, status: int):
    sandbox.close()
    _flush()
    os._exit(status)


def _hluk_path() -> str | None:
    """The hluk on PATH, or else where it is installed: `HLUK_INSTALL_DIR` or the install scripts' default, or Cargo's."""
    # Each directory on PATH as _hluk_in takes it, so that a relative entry such as `.`, or Windows' search of the
    # current directory first, cannot pick a hluk from wherever the client started the server
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if found := _hluk_in(directory):
            return found
    # A client started from a desktop's launcher, such as Claude Desktop on macOS, may run the server with a PATH
    # that lacks the install directory
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        defaults = [local and os.path.join(local, "hluk", "bin")]
    else:
        defaults = [os.path.join(home, ".local", "bin")]
    cargo = os.environ.get("CARGO_HOME") or os.path.join(home, ".cargo")
    # As a client's config sets it, where no shell expands a ~ or a variable
    chosen = os.environ.get("HLUK_INSTALL_DIR")
    for directory in [
        chosen and os.path.expanduser(os.path.expandvars(chosen)),
        *defaults,
        os.path.join(cargo, "bin"),
    ]:
        if installed := _hluk_in(directory):
            return installed
    # The executor then says how to install hluk
    return None


def _hluk_in(directory: str | None) -> str | None:
    # Never a directory that the current directory or drive picks, where any hluk could be: one directory, absolute,
    # with its drive on Windows. Not shutil.which, which splits a path on os.pathsep and, on Windows before Python
    # 3.12, looks in the current directory first.
    if (
        not directory
        or not os.path.isabs(directory)
        or (sys.platform == "win32" and not os.path.splitdrive(directory)[0])
    ):
        return None
    installed = os.path.join(directory, "hluk.exe" if sys.platform == "win32" else "hluk")
    # One the executor can run: not one an install cut short leaves without its mode
    return installed if os.path.isfile(installed) and os.access(installed, os.X_OK) else None


def _make_executor(*, hluk_path: str | None = None, **kwargs) -> HyperlightExecutor:
    # Looked up for each guest the server starts: a call after one that found no hluk finds one installed since
    # Expanded as HLUK_INSTALL_DIR is, as a client's config passes --hluk-path with no shell to expand a ~ or a
    # variable; unless it names a file as given, whose name may hold a $ or a %
    if hluk_path and not os.path.isfile(hluk_path):
        hluk_path = os.path.expanduser(os.path.expandvars(hluk_path))
    hluk_path = hluk_path or _hluk_path()
    if hluk_path is None:
        # In the server's words: the executor's names its own keyword argument, which a client cannot pass
        raise RuntimeError(
            "Could not find the `hluk` binary on PATH, in HLUK_INSTALL_DIR, or where the install scripts or Cargo put "
            "it: install it from https://github.com/hyperlight-dev/hyperlight-unikraft#install, or pass its location "
            "with --hluk-path."
        )
    return HyperlightExecutor(hluk_path=hluk_path, **kwargs)


def main(argv: list[str] | None = None):
    args = _parse_args(argv)
    timeout = args.timeout
    executor_kwargs: dict[str, Any] = {
        "additional_imports": args.package,
        "hluk_path": args.hluk_path,
        "runtime": args.runtime or "agent",
        "initrd": args.initrd,
        "scratch_mb": args.scratch_mb,
        "net_allow": args.net_allow,
        "hluk_args": args.hluk_arg,
        "startup_timeout": args.startup_timeout,
        "timeout_seconds": timeout,
    }
    sandbox = Sandbox(**executor_kwargs)
    server = build_server(sandbox)

    def stop(signum, _frame):
        # How a client stops a server that outlasts the end of its input. Python's default for the signal would leave
        # the guest running, and an exception would not end the server: the stdio transport waits on a thread that
        # reads the client's input until it ends. A guest still starting, which close() cannot reach yet, stops once
        # it boots and finds its console's input closed: after the image's pull, the first time. On a thread of its
        # own: the handler runs between two of the main thread's steps, maybe within a write to the standard error,
        # which closing the guest logs to.
        threading.Thread(target=_shut_down, args=(sandbox, 128 + signum), daemon=True).start()

    # SIGINT too: a KeyboardInterrupt cannot end the server any more than an exception from SIGTERM could
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    status = 0
    try:
        server.run("stdio")
    except Exception:
        traceback.print_exc()
        status = 1
    finally:
        sandbox.close()
    # Not a normal exit, which would wait for the worker threads: one can wait for a guest still starting, for as long
    # as an image's pull takes, with nobody left to read its result
    _flush()
    os._exit(status)
