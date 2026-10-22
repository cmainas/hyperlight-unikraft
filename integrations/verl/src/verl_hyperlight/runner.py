"""verl's Sandbox Fusion test cases run in local Hyperlight micro-VMs, through the `hluk` CLI.

With ``reward.sandbox_fusion.url=hyperlight://``, verl hands each test case of its code rewards to
:func:`run_code` instead of POSTing it to a Sandbox Fusion service. Each runs in its own micro-VM,
restored from a warm snapshot of a booted CPython, with its own kernel, no network and no view of the
host filesystem beyond its own input, so model code can neither reach the training node nor leave
state behind for the next test case. ``hyperlight://<runtime>`` picks another published runtime
image, e.g. ``hyperlight://agent`` for one with numpy, pandas and scipy, and
``hyperlight:///path/to/rootfs.cpio`` a rootfs of your own.

The result is shaped like Sandbox Fusion's ``/run_code`` reply, so verl scores it as the service's.
"""

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import IO, Any, Optional


logger = logging.getLogger(__name__)

URL_SCHEME = "hyperlight://"
DEFAULT_RUNTIME = "python"

# Earlier releases removed a guest's warm snapshot while saving it again, failing the runs restoring it.
MIN_HLUK_VERSION = (0, 19, 0)

# Output kept per stream; the rest is read and dropped so a flood cannot exhaust the worker.
MAX_OUTPUT_BYTES = 16 << 20

# Seconds allowed for the first run of a runtime on a node: it may pull the image and boot it cold.
PREPARE_TIMEOUT = 900

# Seconds a failed prepare is remembered, so test cases fail fast instead of each retrying it.
PREPARE_RETRY_AFTER = 60

# Imported before the warm snapshot is taken, so a solution's imports of them cost nothing: the
# modules Sandbox Fusion's call-based wrapper imports, and a few more solutions often use.
# Uncached, they are compiled on every run (the python image ships no bytecode for most of them).
WARM_IMPORTS = (
    "array bisect collections contextlib copy dataclasses datetime decimal enum fractions functools "
    "heapq io itertools json math operator random re statistics string sys threading traceback typing"
).split()

# Imported too where the image has them: slow to import, and used by solutions that need them.
WARM_IMPORTS_IF_PRESENT = ("numpy", "sympy")

# Where the solution and its stdin are mounted, read-only, in the guest.
_GUEST_DIR = "/verl"

# The runner writes this to stderr before the solution runs. Without it, a failure or timeout came
# from hluk itself (a missing image, no hypervisor), not from the solution.
_STARTED_MARKER = "__verl_hyperlight_started__"

# stdin is a mounted file, not a pipe: the guest console echoes piped input back to stdout.
# stdout and stderr are files in the guest's memory, copied to the console at the end: every
# write to the console leaves the VM, and it says it is a terminal, so Python would line-buffer
# it and each line of output would cost a VM exit, several times the host's time.
# The solution runs in a __main__ module that stays installed, as under `python solution.py`, so
# threads it starts still find their classes there. (str hashing keeps the seed the snapshot
# booted with, so set and dict orders that depend on it repeat across test cases.) The guest's
# interpreter is not finalized when the call ends, so the runner does what Python does at exit:
# threading's exit hooks, joining non-daemon threads, atexit handlers, flushing, and freeing the
# solution's module.
_RUNNER = f"""\
import atexit, gc, os, sys, threading, types, weakref
gc.freeze()
with open("{_GUEST_DIR}/solution.py", "rb") as _f:
    _source = _f.read()
# As Sandbox Fusion runs it: from a writable directory of its own, which is also the cwd.
os.makedirs("/tmp/verl", exist_ok=True)
_path = "/tmp/verl/solution.py"
with open(_path, "wb") as _f:
    _f.write(_source)
os.chdir("/tmp/verl")
_fd = os.open("{_GUEST_DIR}/stdin", os.O_RDONLY)
os.dup2(_fd, 0)
os.close(_fd)
# As CPython opens stdin on Linux: no newline translation.
# The driver's stdin owns fd 0 and would close it when freed, so it stays referenced; ours is
# sys.__stdin__ too, as Python's own is, so a solution rebinding sys.stdin does not free it.
_driver_stdin = sys.stdin
sys.stdin = sys.__stdin__ = open(0, closefd=False, newline="\\n")
# The inputs are in place: from here on, a failure is the solution's.
sys.stderr.write({_STARTED_MARKER!r} + "\\n")
sys.stderr.flush()

_console = (os.dup(1), os.dup(2))
_outputs = ("/tmp/.verl_stdout", "/tmp/.verl_stderr")
for _fd, _name in zip((1, 2), _outputs):
    _f = os.open(_name, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    os.dup2(_f, _fd)
    os.close(_f)
    # Nameless from here on, so nothing the solution does in /tmp touches it.
    os.unlink(_name)
# The runner's own handles on the files, sharing fds 1 and 2's offset, which hold even if the
# solution closes those (as a module-level open(1, "wb") does when it is freed at exit).
_captured = (os.dup(1), os.dup(2))
# Now a file, as it would be a pipe under Sandbox Fusion: block-buffered. stderr too, which
# Python line-buffers even on a pipe, but it is not graded. Each append to a file in the guest
# costs more the bigger the file, so text goes down in 1 MiB writes rather than 8 KiB ones.
for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(line_buffering=False)
    _s._CHUNK_SIZE = 1 << 20
_devnull = os.open(os.devnull, os.O_WRONLY)
_emit_lock = threading.Lock()
def _flush_streams():
    # Including the original streams, which a solution may have swapped for its own, as Python's
    # finalization flushes them; closed, detached or None ones are skipped, as it skips them.
    for _s in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        try:
            _s.flush()
        except Exception:
            pass
def _emit(flush=True):
    # Copy the output to the console. Then fd 2 goes back to it, for the traceback of an uncaught
    # exception, printed once this returns, and fd 1 to /dev/null: daemon threads still running
    # would write after the answer, where Python stops them at exit.
    with _emit_lock:
        if flush:
            _flush_streams()
        for _fd, _to, _own in zip((1, 2), _console, _captured):
            _end, _at = os.lseek(_own, 0, os.SEEK_CUR), 0
            while _at < _end and (_chunk := os.pread(_own, min(_end - _at, 1 << 20), _at)):
                _at += len(_chunk)
                while _chunk:
                    _chunk = _chunk[os.write(_to, _chunk):]
            # Copied once: a later emit (os._exit from a thread) must not repeat it.
            os.lseek(_own, 0, os.SEEK_SET)
            os.ftruncate(_own, 0)
            os.dup2(_to if _fd == 2 else _devnull, _fd)
# The guest's /dev has no stdin or stderr, and its stdout is the console, not fd 1: open them as
# Linux would, as the solution's own fds 0 to 2, so what is written through them keeps its order.
_std_paths = {{"/dev/stdin": 0, "/dev/stdout": 1, "/dev/stderr": 2,
               "/dev/fd/0": 0, "/dev/fd/1": 1, "/dev/fd/2": 2,
               "/proc/self/fd/0": 0, "/proc/self/fd/1": 1, "/proc/self/fd/2": 2}}
import builtins, io
_io_open, _os_open = io.open, os.open
def _std_fd(path):
    try:
        return _std_paths.get(os.fspath(path))
    except TypeError:
        return None
def _open(file, *args, **kwargs):
    if (_fd := _std_fd(file)) is not None:
        file = os.dup(_fd)
    return _io_open(file, *args, **kwargs)
def _os_open_std(path, *args, **kwargs):
    if (_fd := _std_fd(path)) is not None:
        return os.dup(_fd)
    return _os_open(path, *args, **kwargs)
builtins.open = io.open = _open
os.open = _os_open_std
# input() as the builtin behaves when stdin and stdout are not terminals, as here; the builtin's
# own check for a terminal and its flush of stderr cost microseconds a line in the guest.
def _input(prompt=""):
    if prompt:
        sys.stdout.write(str(prompt))
    try:
        sys.stdout.flush()
    except Exception:
        pass  # as the builtin, which ignores a failed flush
    _line = sys.stdin.readline()
    if not _line:
        raise EOFError("EOF when reading a line")
    _newline = b"\\n" if isinstance(_line, bytes) else "\\n"
    return _line[:-1] if _line[-1:] == _newline else _line
builtins.input = _input
_os_exit = os._exit
def _exit_now(status):
    # What reached fds 1 and 2; like the real os._exit, not what Python still buffers.
    _emit(flush=False)
    _os_exit(status)
os._exit = _exit_now

sys.argv = [_path]
sys.path.insert(0, "/tmp/verl")
# random, and numpy where the image has it loaded, were seeded once, before the snapshot: reseed
# them from this VM's own entropy, or every test case would see the same sequence.
import random
random.seed()
if "numpy" in sys.modules:
    sys.modules["numpy"].random.seed()
_main = types.ModuleType("__main__")
_main.__file__ = _path
_main.__builtins__ = __builtins__
_driver_main = sys.modules["__main__"]
sys.modules["__main__"] = _main

def _free_main():
    # As CPython 3.12 does at exit: drop the module and let the collector finalize it whole, so
    # __del__ methods still see the globals; clear only what survives that.
    global _main
    _ref = weakref.ref(_main)
    _main = None
    sys.modules["__main__"] = _driver_main
    gc.collect()
    _left = _ref()
    if _left is not None:
        _globals = _left.__dict__
        for _first in (True, False):
            for _name in list(_globals):
                if _name != "__builtins__" and _name.startswith("_") == _first:
                    _globals[_name] = None
        del _left, _globals
        gc.collect()

_status = None
try:
    try:
        exec(compile(_source, _path, "exec"), _main.__dict__)
    except SystemExit as _e:
        # Raised again at the end, without the solution's frames, which would keep its module
        # alive through the teardown.
        _status = (_e.code,)
        del _e
finally:
    try:
        for _hook in reversed(getattr(threading, "_threading_atexits", [])):
            _hook()
        while _pending := [t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon]:
            for _t in _pending:
                _t.join()
        atexit._run_exitfuncs()
        # As Python's exit: a sys.stdout or sys.stderr that is there, open and fails to flush
        # makes the exit status 120, whatever it would have been.
        for _s in (sys.stdout, sys.stderr):
            try:
                _closed = _s is None or bool(_s.closed)
            except Exception:
                _closed = False  # as Python takes a stream whose closed cannot be read
            if not _closed:
                try:
                    _s.flush()
                except Exception:
                    _status = (120,)
        _flush_streams()
        # Not with an uncaught exception, whose traceback Python prints before the teardown.
        if sys.exc_info()[1] is None:
            _free_main()
    finally:
        _emit()
if _status is not None:
    raise SystemExit(*_status)
"""

# Held for a whole prepare, so the test cases waiting on it do not each start one.
_prepare_lock = threading.Lock()
# Guests this process has prepared: (hluk, runtime, memory).
_ready: set[tuple[str, str, Optional[int]]] = set()
# Failed prepares: guest -> (time, message).
_prepare_failures: dict[tuple[str, str, Optional[int]], tuple[float, str]] = {}
# Consecutive test cases that hluk failed before the code ran, per guest. One is load or a
# transient host failure; a few in a row suggest the snapshot is gone from hluk's cache.
_early_failures: dict[tuple[str, str, Optional[int]], int] = {}
_early_failures_lock = threading.Lock()
EARLY_FAILURES_BEFORE_PREPARE = 3


def is_hyperlight_url(url: Optional[str]) -> bool:
    """Whether verl would send ``url``'s test cases here."""
    return bool(url) and url.strip().lower().startswith(URL_SCHEME)


_hluk_paths: dict[tuple[Optional[str], Optional[str]], str] = {}


def _hluk_path() -> Optional[str]:
    env = (os.environ.get("HLUK_PATH"), os.environ.get("PATH"))
    if env not in _hluk_paths:
        path = env[0] or shutil.which("hluk")
        if not path:
            return None  # not remembered: hluk may be installed later
        _hluk_paths[env] = path
    return _hluk_paths[env]


def _cache_home() -> str:
    return os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")


def _state_dir() -> str:
    path = os.path.join(_cache_home(), "verl", "hyperlight")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


# gc.freeze() keeps what the snapshot holds out of every later collection: the runner's at exit
# and the solution's own, which would otherwise walk all of it (over a second on the agent image).
_WARM_EXEC = (
    f"import {', '.join(WARM_IMPORTS)}\n"
    f"for _name in {WARM_IMPORTS_IF_PRESENT!r}:\n"
    "    try:\n"
    "        __import__(_name)\n"
    "    except ImportError:\n"
    "        pass\n"
    "import gc\n"
    "gc.freeze()"
)


def _guest_args(runtime: str, memory_limit_mb: Optional[int]) -> tuple[str, ...]:
    """The arguments that pick the guest, and with it the warm snapshot it restores: ``runtime`` is
    a published image's name, or the path of a rootfs."""
    image = ["--initrd", runtime, "--warm"] if runtime.startswith("/") else ["--runtime", runtime]
    args = [*image, "--warm-exec", _WARM_EXEC]
    # None keeps the memory size the runtime image is tested with.
    if memory_limit_mb is not None:
        args += ["--scratch-mb", str(_guest_memory_mb(memory_limit_mb))]
    return tuple(args)


# The least memory an image's guest boots and runs a solution in, where it is more than the
# formula below gives for small limits (the agent image unpacks 400 MB of rootfs into it).
MIN_GUEST_MEMORY_MB = {"agent": 1024}


# Rootfs files' floors, by path: worked out once a worker, so that a case's guest is the one it
# prepared, and only from a file that was there.
_rootfs_floors: dict[str, int] = {}


def _min_guest_memory_mb(runtime: str) -> int:
    """The least memory ``runtime``'s guest boots in: a known image's, by its name in a reference
    too (ghcr.io/.../agent:tag), and for a rootfs file, room to unpack it twice over."""
    if runtime.startswith("/"):
        if runtime not in _rootfs_floors:
            try:
                _rootfs_floors[runtime] = (os.path.getsize(runtime) >> 20) * 2 + 256
            except OSError:
                return 0
        return _rootfs_floors[runtime]
    name = runtime.rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]
    return MIN_GUEST_MEMORY_MB.get(name, 0)


def _guest_memory_mb(memory_limit_mb: int) -> int:
    """The micro-VM's memory for a solution that may use ``memory_limit_mb``, as Sandbox Fusion
    lets its process use: the guest's kernel and interpreter take about a quarter of the VM's
    memory (measured: 752 of 1024 MiB usable, 1504 of 2048). Pages are backed only once touched,
    so the headroom costs nothing until a solution uses it."""
    return memory_limit_mb * 4 // 3 + 64


def _prepare(hluk: str, runtime: str, memory_limit_mb: Optional[int]) -> Optional[str]:
    """Checks hluk's version and runs the guest once in this process, before its test cases, so
    its warm snapshot exists. Returns an error message, or None when ready.

    The first run of a guest on a node pulls the image and saves the snapshot, which takes far
    longer than a test case may, and concurrent first runs would each save one. A lock file beside
    hluk's cache serializes these runs across every reward worker that shares the cache, on this
    node or others; once the snapshot exists, each is a restore of a few tens of milliseconds.
    Running it per process, rather than trusting a record of an earlier run, keeps it right after
    hluk's cache is cleaned.
    """
    guest = (hluk, runtime, memory_limit_mb)
    if guest in _ready:
        return None
    with _prepare_lock:
        if guest in _ready:
            return None
        failed = _prepare_failures.get(guest)
        if failed and time.monotonic() - failed[0] < PREPARE_RETRY_AFTER:
            return failed[1]
        try:
            lock_path = _prepare_lock_path()
        except OSError as e:
            error = f"Hyperlight backend: cannot create the prepare lock: {e}"
        else:
            error = _prepare_once(hluk, runtime, memory_limit_mb, lock_path)
        if error:
            # Logged here, once per retry period: every test case fails until it is fixed.
            logger.error(error)
            _prepare_failures[guest] = (time.monotonic(), error)
        else:
            _ready.add(guest)
        return error


def _prepare_lock_path() -> str:
    """Beside hluk's cache, so the nodes that share the cache (an NFS home, say) share the lock
    that keeps them from saving a snapshot into it at once."""
    # As hluk resolves it: HLUK_CACHE_DIR when set at all, even empty (then the working directory).
    cache = os.environ.get("HLUK_CACHE_DIR")
    if cache is None:
        cache = os.path.join(_cache_home(), "hluk")
    if cache:
        os.makedirs(cache, exist_ok=True)
    return os.path.join(cache, "verl-prepare.lock")


def _forget_prepared(hluk: str, runtime: str, memory_limit_mb: Optional[int]) -> None:
    """Prepare the guest again before its next test case: hluk keeps failing before a solution
    runs, perhaps because its cache was cleaned."""
    _ready.discard((hluk, runtime, memory_limit_mb))


def _prepare_once(hluk: str, runtime: str, memory_limit_mb: Optional[int], lock_path: str) -> Optional[str]:
    from filelock import FileLock, Timeout

    try:
        out = subprocess.run([hluk, "--version"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"Hyperlight backend: cannot run {hluk}: {e}"
    match = re.search(r"(\d+)\.(\d+)\.(\d+)(-\S+)?", out)
    release = tuple(map(int, match.groups()[:3])) if match else None
    # A pre-release (`0.19.0-rc1`) comes before its release; build metadata (`+dev.1234567`) does not.
    if release is None or release < MIN_HLUK_VERSION or (release == MIN_HLUK_VERSION and match.group(4)):
        minimum = ".".join(map(str, MIN_HLUK_VERSION))
        return f"Hyperlight backend: needs hluk {minimum} or later, {hluk} reports {out.strip()!r}"
    # One deadline for waiting on the lock and running the guest together.
    deadline = time.monotonic() + PREPARE_TIMEOUT
    try:
        with FileLock(lock_path, timeout=PREPARE_TIMEOUT):
            command = [hluk, "run", *_guest_args(runtime, memory_limit_mb), "--exec", "pass"]
            proc = _spawn(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                _, err = proc.communicate(timeout=max(deadline - time.monotonic(), 1))
            except subprocess.TimeoutExpired:
                _kill(proc)
                raise
            if proc.returncode != 0:
                return f"Hyperlight backend: preparing runtime {runtime!r} failed: {_tail(err)}"
    except (OSError, subprocess.TimeoutExpired, Timeout) as e:
        return f"Hyperlight backend: preparing runtime {runtime!r} failed: {e}"
    except Exception as e:
        # filelock 3.x raises NotImplementedError where the filesystem has no flock.
        return f"Hyperlight backend: cannot lock {lock_path} to prepare runtime {runtime!r}: {e!r}"
    return None


def _spawn(command: list[str], **kwargs: Any) -> subprocess.Popen:
    """Starts hluk in its own process group, so a timeout kills everything it started, and on
    Linux ties it to the worker with util-linux's setpriv, so a worker that dies takes its VMs
    with it instead of leaving a solution's endless loop running. (setpriv execs hluk; a
    preexec_fn would make every spawn a full fork of the worker.)"""
    setpriv = _setpriv_path()
    if setpriv:
        command = [setpriv, "--pdeathsig", "KILL", *command]
    elif not _warned_no_setpriv.is_set():
        _warned_no_setpriv.set()
        logger.warning(
            "Hyperlight backend: util-linux setpriv not found, so a reward worker that dies leaves "
            "the micro-VMs it started running until their hluk exits"
        )
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True, **kwargs)


def _kill_group(proc: subprocess.Popen) -> None:
    """Kills hluk's process group; the caller has not reaped hluk yet."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()


def _kill(proc: subprocess.Popen) -> None:
    _kill_group(proc)
    proc.wait()


class _Watchdog:
    """Kills hluk's process group, and with it the micro-VM, once ``timeout`` seconds pass."""

    def __init__(self, proc: subprocess.Popen, timeout: float):
        self._proc = proc
        self._lock = threading.Lock()
        self._done = False
        self.fired = False
        self._timer = threading.Timer(timeout, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if self._done:
                return
            self.fired = True
            _kill_group(self._proc)

    def stop(self) -> bool:
        """Called once hluk has exited but before it is reaped, so a kill racing the exit hits a
        zombie, never a reused pid. Returns whether the timeout killed it."""
        with self._lock:
            self._done = True
        self._timer.cancel()
        return self.fired


_setpriv: Optional[str] = None
_warned_no_setpriv = threading.Event()


def _setpriv_path() -> Optional[str]:
    global _setpriv
    if _setpriv is None:
        _setpriv = (shutil.which("setpriv") or "") if sys.platform.startswith("linux") else ""
    return _setpriv or None


_cases: Optional[str] = None


def _cases_dir() -> Optional[str]:
    """Where test cases' files go: the node-local temp dir, unless its path has a ':', which
    hluk's --mount cannot take."""
    global _cases
    if _cases is None:
        path = tempfile.gettempdir()
        if ":" in path:
            path = os.path.join(_state_dir(), "cases")
            if ":" in path:
                raise OSError(f"{path} has a ':', which hluk's --mount cannot take")
            os.makedirs(path, exist_ok=True)
        _cases = path
    return _cases


def _wait_unreaped(proc: subprocess.Popen) -> None:
    """Waits for hluk to exit, leaving it to proc.wait() to reap."""
    if hasattr(os, "waitid"):
        try:
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
            return
        except ChildProcessError:
            pass
    proc.wait()


def _read_bounded(stream: IO[bytes], into: list[bytes]) -> None:
    kept = bytearray()
    truncated = False
    while chunk := stream.read(1 << 16):
        if len(kept) + len(chunk) > MAX_OUTPUT_BYTES:
            truncated = True
        if len(kept) < MAX_OUTPUT_BYTES:
            kept += chunk[: MAX_OUTPUT_BYTES - len(kept)]
    into += [bytes(kept), truncated]


def _tail(data: bytes) -> str:
    return data.decode(errors="replace").strip()[-500:]


def _sandbox_error(message: str) -> dict[str, Any]:
    return {"status": "SandboxError", "message": message, "compile_result": None, "run_result": None}


def run_code(
    sandbox_fusion_url: str,
    code: str,
    stdin: Optional[str],
    compile_timeout: int,
    run_timeout: int,
    memory_limit_mb: Optional[int],
    language: str = "python",
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Runs one test case in a micro-VM: verl's sandbox runner for ``hyperlight://`` URLs.

    Takes the arguments of verl's ``call_sandbox_api``, by position or name, and returns what it does, ``(response,
    error)``: the response shaped as Sandbox Fusion's ``/run_code`` reply, or an error when the case
    could not be run at all. ``compile_timeout`` goes unused, Python having no compile step;
    ``memory_limit_mb`` is what the solution may allocate, and the micro-VM gets more, for the
    guest's kernel and interpreter.
    """
    if language != "python":
        return None, f"Hyperlight backend: unsupported language {language!r} (supported: python)"
    if not is_hyperlight_url(sandbox_fusion_url):
        return None, f"Hyperlight backend: not a {URL_SCHEME} URL: {sandbox_fusion_url!r}"
    rest = sandbox_fusion_url.strip()[len(URL_SCHEME) :]
    # hyperlight:///path/to/rootfs.cpio names a rootfs, by its absolute path; hyperlight://<name> a
    # published image, by a name a registry keeps in lower case, or a full reference, whose tag keeps its
    # case; hyperlight:// and hyperlight:/// the default one
    target = rest.strip("/")
    if rest.startswith("/") and target:
        runtime = "/" + target
    elif target.endswith(".cpio"):
        # Each reward worker has a working directory of its own (Ray's, a job's)
        return (
            None,
            f"Hyperlight backend: give the rootfs by its absolute path (hyperlight:///abs/path), not {target!r}",
        )
    elif "/" in target:
        runtime = target
    elif ":" in target or "@" in target:
        return None, (
            f"Hyperlight backend: {target!r} is neither a published image's name nor a full reference: "
            "use hyperlight://<name> (the release's own) or hyperlight://<registry>/<repository>:<tag>"
        )
    else:
        runtime = target.lower() or DEFAULT_RUNTIME
    if memory_limit_mb is not None and _guest_memory_mb(memory_limit_mb) < _min_guest_memory_mb(runtime):
        # The guest kernel does not enforce a process's own limit (RLIMIT_AS), so a smaller limit than the
        # image boots with would be a larger one in effect
        least = -(-(_min_guest_memory_mb(runtime) - 64) * 3 // 4)
        return None, (
            f"Hyperlight backend: memory_limit_mb {memory_limit_mb} is below what {runtime!r} runs in; "
            f"use at least {least}, or a smaller image"
        )
    hluk = _hluk_path()
    if hluk is None:
        return None, "Hyperlight backend: hluk not found on PATH; install it or set HLUK_PATH"
    error = _prepare(hluk, runtime, memory_limit_mb)
    if error:
        return _sandbox_error(error), None

    response, error, retry = _run_case(hluk, runtime, code, stdin, run_timeout, memory_limit_mb)
    if retry:
        # hluk failed before the solution ran, so nothing of the solution's ran: once more, so a
        # transient host failure does not score a case.
        response, error, _ = _run_case(hluk, runtime, code, stdin, run_timeout, memory_limit_mb)
    return response, error


def _run_case(
    hluk: str, runtime: str, code: str, stdin: Optional[str], run_timeout: int, memory_limit_mb: Optional[int]
) -> tuple[Optional[dict[str, Any]], Optional[str], bool]:
    """One run of a test case: ``(response, error)`` as ``run_code`` returns them, and whether
    hluk failed before the solution ran, other than by timing out, so that it may be retried."""
    try:
        tmp = tempfile.TemporaryDirectory(prefix="verl-hyperlight-", dir=_cases_dir())
    except OSError as e:
        return None, f"Hyperlight backend: cannot write the test case's files: {e}", False
    with tmp:
        try:
            # A lone surrogate from a tokenizer decode would fail a strict encode; Sandbox Fusion's
            # JSON request escapes it, so it is replaced here rather than failing the case.
            for name, text in (("solution.py", code), ("stdin", stdin or "")):
                with open(os.path.join(tmp.name, name), "w", encoding="utf-8", errors="replace", newline="") as f:
                    f.write(text)
        except OSError as e:
            return None, f"Hyperlight backend: cannot write the test case's files: {e}", False
        command = [hluk, "run", *_guest_args(runtime, memory_limit_mb)]
        command += ["--mount", f"{tmp.name}:{_GUEST_DIR}:ro", "--exec", _RUNNER]

        start = time.monotonic()
        try:
            proc = _spawn(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            return None, f"Hyperlight backend: failed to start hluk: {e}", False
        stdout: list = []
        stderr: list = []
        watchdog = None
        try:
            readers = [
                threading.Thread(target=_read_bounded, args=(proc.stdout, stdout), daemon=True),
                threading.Thread(target=_read_bounded, args=(proc.stderr, stderr), daemon=True),
            ]
            for reader in readers:
                reader.start()
            # A blocking wait and a timer, not wait(timeout=), which polls with sleeps of up to
            # 50 ms and would add that to every test case.
            watchdog = _Watchdog(proc, run_timeout)
            _wait_unreaped(proc)
            timed_out = watchdog.stop()
            proc.wait()
        except BaseException:
            # No thread to start, say: do not leave the VM running unwatched.
            if watchdog:
                watchdog.stop()
            _kill(proc)
            raise
        elapsed = time.monotonic() - start
        for reader in readers:
            reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):
            # Something hluk started still holds its output open: its group outlives it, and the
            # group id cannot be reused while it does.
            _kill_group(proc)
            for reader in readers:
                reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):
            # Something outside hluk's process group still holds its output open.
            return _sandbox_error("hluk's output stayed open after it exited"), None, False

    out_bytes, out_truncated = stdout
    err_bytes, err_truncated = stderr
    out = out_bytes.decode(errors="replace")
    err = err_bytes.decode(errors="replace")
    guest = (hluk, runtime, memory_limit_mb)
    if _STARTED_MARKER not in err:
        # A few in a row suggest hluk's cache lost the snapshot: prepare again, under the lock,
        # rather than pull inside every test case.
        with _early_failures_lock:
            early = _early_failures.get(guest, 0) + 1
            _early_failures[guest] = 0 if early >= EARLY_FAILURES_BEFORE_PREPARE else early
        if early >= EARLY_FAILURES_BEFORE_PREPARE:
            _forget_prepared(*guest)
        how = "timed out" if timed_out else f"exited with {proc.returncode}"
        message = f"Hyperlight backend: hluk {how} before the code ran: {_tail(err_bytes)}"
        logger.warning(message)
        return _sandbox_error(message), None, not timed_out
    if _early_failures.get(guest):
        with _early_failures_lock:
            _early_failures[guest] = 0
    err = err.replace(_STARTED_MARKER + "\n", "", 1)
    for name, truncated in (("stdout", out_truncated), ("stderr", err_truncated)):
        if truncated:
            err += f"\n[verl hyperlight: {name} truncated at {MAX_OUTPUT_BYTES} bytes]\n"
    if timed_out:
        run_result = {"status": "TimeLimitExceeded", "return_code": None}
    elif out_truncated:
        # Not an answer: what was kept of it could match the expected output where the whole would not
        run_result = {"status": "Error", "return_code": proc.returncode}
    else:
        run_result = {"status": "Finished", "return_code": proc.returncode}
    run_result.update(execution_time=elapsed, stdout=out, stderr=err)
    return (
        {
            "status": "Success" if run_result["status"] == "Finished" and run_result["return_code"] == 0 else "Failed",
            "compile_result": None,
            "run_result": run_result,
        },
        None,
        False,
    )
