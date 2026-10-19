"""The Hyperlight executor: smolagents' `CodeAgent` code run in a local micro-VM, through the `hluk` CLI."""

import base64
import datetime
import decimal
import itertools
import json
import os
import pathlib
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from io import BytesIO
from textwrap import dedent
from typing import Any, NoReturn

from smolagents.local_python_executor import MAX_EXECUTION_TIME_SECONDS, CodeOutput
from smolagents.monitoring import AgentLogger, LogLevel
from smolagents.remote_executors import RemotePythonExecutor
from smolagents.serialization import SafeSerializer, SerializationError
from smolagents.tools import Tool, get_tools_definition_code
from smolagents.utils import AgentError


__all__ = ["HyperlightExecutor"]


# Marks the guest's replies on its console, apart from anything else that might reach the host's end of it
_HYPERLIGHT_REPLY_MARKER = "\x1esmolagents "

# Runs inside the Hyperlight guest: a read-eval loop on its console, which `hluk` connects to its own standard input and
# output. A single long-lived guest keeps state across steps: it reads each step from its standard input, as a line of
# JSON, and answers on its standard output, both pipes that only the executor's process holds. Lines of ASCII-only
# JSON, as the guest's terminal reads a carriage return or a Ctrl-D that ends a chunk as a terminal would. The code
# runs on the main thread, one step at a time, as in a notebook kernel: `signal` handlers and alarms work for it.
# Waiting for the next step, the guest waits on the host, and the whole micro-VM with it: what a step leaves running,
# a thread or a subprocess, only goes on while a step runs.
_HYPERLIGHT_GUEST_SERVER_CODE = dedent(
    """\
    import ast
    import io
    import json
    import os
    import select
    import sys
    import termios
    import threading
    import time
    import traceback

    namespace = {"__name__": "__main__", "__builtins__": __builtins__}

    # The console, for the steps and their replies, and for this script's own errors, before the step's own streams
    # take its descriptors. Descriptors made by os.dup are not inherited, so subprocesses never hold it.
    requests_fd, replies_fd, errors_fd = os.dup(0), os.dup(1), os.dup(2)
    # The console echoes what it reads, as a terminal does. A guest that kept echoing would still work, as the executor
    # only takes the lines marked as replies, but every step would come back to it.
    try:
        attributes = termios.tcgetattr(requests_fd)
        attributes[3] &= ~termios.ECHO
        termios.tcsetattr(requests_fd, termios.TCSANOW, attributes)
    except termios.error:
        pass
    # What the agent's code and its subprocesses read from their standard input ends at once, rather than taking the
    # next step
    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)
    os.close(null_fd)


    # What is written to the standard output and error, at the descriptor level, as a notebook kernel hears it. Both
    # descriptors stay on one pipe, so subprocesses and C extensions are heard too, in the order written. A thread
    # drains it and keeps the beginning and the end of each step's share, LOG_LIMIT bytes in all: what lies between is
    # dropped as it arrives, so a step that prints without end costs the guest no memory.
    class StepOutput:

        def __init__(self):
            self._read_fd, self._write_fd = os.pipe()
            os.dup2(self._write_fd, 1)
            os.dup2(self._write_fd, 2)
            # Read only under the lock, by the thread or by end(), so that no chunk is ever on its way from the pipe
            # to the buffer while end() takes what the step wrote
            os.set_blocking(self._read_fd, False)
            self._lock = threading.Lock()
            self._clear()
            threading.Thread(target=self._drain, daemon=True).start()

        def _clear(self):
            self._head, self._tail, self._dropped = bytearray(), bytearray(), 0

        def _keep(self, data):
            half = LOG_LIMIT // 2
            taken = max(0, min(len(data), half - len(self._head)))
            self._head += data[:taken]
            self._tail += data[taken:]
            if len(self._tail) > half:
                self._dropped += len(self._tail) - half
                del self._tail[:-half]

        def _read(self):
            # Under the lock: False once the pipe is empty for now
            try:
                chunk = os.read(self._read_fd, 65536)
            except BlockingIOError:
                return False
            self._keep(chunk)
            return bool(chunk)

        def _drain(self):
            # Keeps the pipe from filling up, which would block whatever writes to it
            while True:
                select.select([self._read_fd], [], [])
                with self._lock:
                    self._read()

        def begin(self):
            with self._lock:
                # What came between steps, from something an earlier step left running, is no step's
                self._clear()

        def end(self):
            # The step's writes are all done, so what it wrote is in the pipe: take all of it, and no more than a
            # moment of what something it left running keeps writing. (Not a marker written after it: the guest's
            # pipe can split a short write, so a marker may never arrive whole.)
            with self._lock:
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and self._read():
                    pass
                if not self._dropped:
                    # One piece, so that a character across the two halves stays whole
                    return (self._head + self._tail).decode(errors="replace")
                # Cut where characters begin, rather than through one
                head, tail = complete_utf8(self._head), self._tail.lstrip(UTF8_CONTINUATION)
                dropped = self._dropped + len(self._head) - len(head) + len(self._tail) - len(tail)
                return (
                    head.decode(errors="replace")
                    + f"\\n[... {dropped} bytes of output truncated ...]\\n"
                    + tail.decode(errors="replace")
                )


    UTF8_CONTINUATION = bytes(range(0x80, 0xC0))


    def complete_utf8(data):
        # Without the character the end cuts short, if any
        lead = len(data.rstrip(UTF8_CONTINUATION)) - 1
        if lead < 0 or data[lead] < 0xC0:
            return data
        length = 2 if data[lead] < 0xE0 else 3 if data[lead] < 0xF0 else 4
        return data if len(data) - lead >= length else data[:lead]


    step_output = StepOutput()
    # Line buffered, as on a terminal: whole lines keep their order with what subprocesses write, at half the cost of
    # flushing every write, and the rest is flushed when the step ends. A buffered writer, as only one finishes a write
    # that the pipe takes in parts. They stay in place between steps: a logging handler made in one step keeps the
    # stream for the next.
    sys.stdout, sys.stderr = (
        io.TextIOWrapper(
            io.BufferedWriter(io.FileIO(fd, "w", closefd=False)), errors="backslashreplace", line_buffering=True
        )
        for fd in (1, 2)
    )


    def run_code(code):
        output, error = None, None
        step_output.begin()
        try:
            tree = ast.parse(code, filename="<code>", mode="exec")
            # Like a Jupyter cell, the value of a trailing expression is the output
            last_expression = None
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last_expression = ast.Expression(tree.body.pop().value)
            exec(compile(tree, "<code>", "exec"), namespace)
            if last_expression is not None:
                value = eval(compile(last_expression, "<code>", "eval"), namespace)
                if value is not None:
                    output = repr(value)
        # BaseException: FinalAnswerException and SystemExit must be reported, not take the server down
        except BaseException as e:
            if isinstance(e, SyntaxError):
                lines = traceback.format_exception_only(type(e), e)
            else:
                # Skip this function's frame so the traceback starts in the agent's code
                lines = traceback.format_exception(type(e), e, e.__traceback__.tb_next)
            try:
                value = str(e)
            # An exception of the agent's own making can fail to say what it is
            except BaseException:
                value = f"<{type(e).__name__} object: str() failed>"
            error = {"name": type(e).__name__, "value": value, "traceback": "".join(lines)}
        # What the step left in a buffer, without a newline or through a stream of its own making, is still its own
        for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
            try:
                stream.flush()
            except Exception:
                pass
        return {"output": output, "logs": step_output.end(), "error": error}


    def reply(message):
        data = json.dumps(message).encode()
        if len(data) > REPLY_LIMIT:
            # More than the executor takes: the step's value or final answer, as its logs are capped far below
            message = dict(message, output=None, error={
                "name": "ValueError",
                "value": f"The step's result takes {len(data)} bytes, more than the {REPLY_LIMIT} the sandbox returns",
                "traceback": "",
            })
            data = json.dumps(message).encode()
        # On a line of its own: whatever wrote to the console without ending its line, such as code that opened
        # /dev/stdout, the console itself, stays out of it
        view = memoryview(b"\\n" + MARKER.encode() + data + b"\\n")
        while view:
            view = view[os.write(replies_fd, view):]


    try:
        # Ready: the executor sends no step before this
        reply({"id": None})
        # Until the executor closes the console's input
        for line in io.open(requests_fd, "rb", closefd=False):
            request = json.loads(line)
            reply({"id": request["id"], **run_code(request["code"])})
    except BaseException:
        # The standard error is the step's: this script's last words go to the console's, where the executor hears them
        os.write(errors_fd, traceback.format_exc().encode(errors="backslashreplace"))
        raise
    """
)


class _HyperlightGuestStopped(Exception):
    """The guest stopped answering: `hluk` exited, or the guest sent what the executor cannot take as a reply."""


# Rebuilding a final answer, the guest not being trusted with the host's memory or time. smolagents' own decoding of the
# format (SafeSerializer.from_json_safe) builds whatever the payload names, so the executor rebuilds it itself, to the
# same objects, within budgets: values in all, in JSON, well past what an agent hands back (an array of a million numbers
# is one), and few enough that the JSON and what is built from it take the host some hundreds of MiB at most
_MAX_FINAL_ANSWER_VALUES = 2_000_000
# Values in what is hashed, members of sets and keys of dicts, which colliding hashes make quadratic: each compares in
# constant time, as only scalars of a bounded size are hashed (integers of up to _MAX_HASHED_INT_BITS, and no Decimal,
# which an integer converts to at a cost quadratic in its digits)
_MAX_FINAL_ANSWER_HASHED = 10_000
_MAX_HASHED_INT_BITS = 256
# Pixels of all the images, which a small compressed one can claim millions of, for when they are shown
_MAX_FINAL_ANSWER_PIXELS = 25_000_000
# Images, and chunks of all of them, which Pillow reads one by one in Python: a PNG Pillow writes has a few dozen
_MAX_FINAL_ANSWER_IMAGES = 256
_MAX_PNG_CHUNKS = 4096
_MAX_FINAL_ANSWER_DEPTH = 100
# Text that becomes an object whose cost grows faster than its length: a number's digits, a path's parts
_MAX_SCALAR_TEXT = 4300
# The arrays it may rebuild: booleans and numbers, whose items take a few bytes each, rather than strings, records or
# blocks of a size the payload names; named by a short type string such as "int64" or "<f8", and built from JSON
# booleans and numbers of up to _MAX_ARRAY_INT_DIGITS digits, as smolagents' own decoding builds them from the JSON
_FINAL_ANSWER_ARRAY_KINDS = "biufc"
_FINAL_ANSWER_DTYPE = re.compile(r"[<>=|]?[a-z]+[0-9]{0,3}")
_MAX_ARRAY_INT_DIGITS = 40
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _clip(text: str, limit: int) -> str:
    """Keep the beginning and the end of text longer than limit, as the guest does with a step's output."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters truncated ...]\n{text[-half:]}"


def _parse_int(literal: str) -> int:
    # What Python 3.11 on refuses to convert, which takes it quadratic time
    if len(literal) > _MAX_SCALAR_TEXT:
        raise ValueError(f"an integer of more than {_MAX_SCALAR_TEXT} digits")
    return int(literal)


def _png_chunks(data: bytes, budget: int) -> tuple[int, int]:
    """How many pixels a PNG claims and how many chunks it has, read as Pillow will: the size from its header chunk.
    Stops at more chunks than budget."""
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("an image that is not a PNG")
    offset, chunks, pixels = len(_PNG_SIGNATURE), 0, None
    while offset + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, offset)
        if (kind == b"IHDR") != (chunks == 0):
            raise ValueError("an image whose header chunk is not its first and only one")
        if kind == b"IHDR":
            if length < 8 or offset + 16 > len(data):
                raise ValueError("an image without its size")
            width, height = struct.unpack_from(">II", data, offset + 8)
            pixels = width * height
        chunks += 1
        if chunks > budget:
            raise ValueError(f"its images have more than {_MAX_PNG_CHUNKS} chunks")
        offset += 12 + length
        if kind == b"IEND":
            break
    if pixels is None:
        raise ValueError("an image without its size")
    return pixels, chunks


class _FinalAnswerBuilder:
    """SafeSerializer.from_json_safe within budgets: the same objects, from what the guest's SafeSerializer wrote."""

    def __init__(self):
        self.hashed = 0
        self.pixels = 0
        self.images = 0
        self.chunks = 0

    @classmethod
    def _hash_weight(cls, value: Any) -> int:
        """How many values a member or key is, refusing those whose comparisons take longer than a constant time."""
        if isinstance(value, (tuple, frozenset)):
            # n members of L items collide in n * n / 2 comparisons of up to L items: within the budget, as many as
            # members of one item each
            return max(1, sum(cls._hash_weight(item) for item in value))
        if (
            isinstance(value, bool)
            or value is None
            or isinstance(value, (float, complex, str, bytes, pathlib.PurePath))
        ):
            # Strings, bytes and paths hash with a key of the host's process: equal hashes are equal values
            return 1
        if isinstance(value, int) and value.bit_length() <= _MAX_HASHED_INT_BITS:
            return 1
        if isinstance(value, (datetime.date, datetime.time, datetime.timedelta)):
            return 1
        raise ValueError(f"a set member or dict key of a kind that is not hashed: {type(value).__name__}")

    def _hashable(self, value: Any) -> Any:
        self.hashed += self._hash_weight(value)
        if self.hashed > _MAX_FINAL_ANSWER_HASHED:
            raise ValueError(f"its sets and dict keys weigh more than {_MAX_FINAL_ANSWER_HASHED} values")
        return value

    def _items(self, value: Any, depth: int) -> list:
        if not isinstance(value, list):
            raise ValueError("a collection without its items")
        return [self.build(item, depth + 1) for item in value]

    @staticmethod
    def _numbers(value: Any, depth: int) -> list:
        """An array's items, as JSON has them: nested lists of booleans and numbers, which numpy converts cheaply."""
        pending = [(value, depth)]
        while pending:
            item, item_depth = pending.pop()
            if isinstance(item, list):
                if item_depth > _MAX_FINAL_ANSWER_DEPTH:
                    raise ValueError(f"it is nested more than {_MAX_FINAL_ANSWER_DEPTH} deep")
                pending.extend((member, item_depth + 1) for member in item)
            elif isinstance(item, int) and not isinstance(item, bool) and len(str(abs(item))) > _MAX_ARRAY_INT_DIGITS:
                raise ValueError(f"an array of integers of more than {_MAX_ARRAY_INT_DIGITS} digits")
            elif not isinstance(item, (bool, int, float)):
                raise ValueError("an array of items that are not numbers")
        return value

    @staticmethod
    def _text(value: Any, limit: int | None = _MAX_SCALAR_TEXT) -> str:
        if not isinstance(value, str) or (limit is not None and len(value) > limit):
            raise ValueError(f"a value whose text is not a string of at most {limit} characters")
        return value

    def build(self, value: Any, depth: int = 0) -> Any:
        if depth > _MAX_FINAL_ANSWER_DEPTH:
            raise ValueError(f"it is nested more than {_MAX_FINAL_ANSWER_DEPTH} deep")
        if isinstance(value, list):
            return self._items(value, depth)
        if not isinstance(value, dict):
            return value
        kind, data = value.get("__type__"), value.get("data")
        if kind == "tuple":
            return tuple(self._items(data, depth))
        if kind in ("set", "frozenset"):
            members = [self._hashable(item) for item in self._items(data, depth)]
            return set(members) if kind == "set" else frozenset(members)
        if kind == "dict_with_complex_keys":
            pairs = self._items(data, depth)
            if not all(isinstance(pair, list) and len(pair) == 2 for pair in pairs):
                raise ValueError("a dict entry that is not a key and a value")
            return {self._hashable(key): item for key, item in pairs}
        if kind == "bytes":
            return base64.b64decode(self._text(data, None))
        if kind == "PIL.Image":
            image = base64.b64decode(self._text(data, None))
            pixels, chunks = _png_chunks(image, _MAX_PNG_CHUNKS - self.chunks)
            self.images += 1
            self.pixels += pixels
            self.chunks += chunks
            if self.images > _MAX_FINAL_ANSWER_IMAGES:
                raise ValueError(f"it holds more than {_MAX_FINAL_ANSWER_IMAGES} images")
            if self.pixels > _MAX_FINAL_ANSWER_PIXELS:
                raise ValueError(f"its images hold more than {_MAX_FINAL_ANSWER_PIXELS} pixels")
            if self.chunks > _MAX_PNG_CHUNKS:
                raise ValueError(f"its images have more than {_MAX_PNG_CHUNKS} chunks")
            try:
                import PIL.Image
            except ImportError:
                return {"__type__": "PIL.Image", "data": data}
            # A PNG, as the guest writes them: none of the other formats Pillow reads, some of which have other
            # programs draw the image
            return PIL.Image.open(BytesIO(image), formats=["PNG"])
        if kind == "ndarray":
            dtype = value.get("dtype")
            if not isinstance(dtype, str) or not _FINAL_ANSWER_DTYPE.fullmatch(dtype):
                raise ValueError("an array of a type that is not rebuilt")
            items = self._numbers(data, depth)
            try:
                import numpy as np
            except ImportError:
                return items
            dtype = np.dtype(dtype)
            if dtype.kind not in _FINAL_ANSWER_ARRAY_KINDS or dtype.itemsize > 32:
                raise ValueError("an array of a type that is not rebuilt")
            return np.array(items, dtype=dtype)
        if kind == "complex":
            return complex(value["real"], value["imag"])
        if kind in ("datetime", "date", "time"):
            return getattr(datetime, kind).fromisoformat(self._text(data))
        if kind == "timedelta":
            return datetime.timedelta(seconds=value["total_seconds"])
        if kind == "Decimal":
            number = decimal.Decimal(self._text(data))
            # Whose digits, written out by int() or format(), would be as many as its exponent says
            if number.is_finite() and abs(number.adjusted()) > _MAX_SCALAR_TEXT:
                raise ValueError(f"a Decimal of an exponent beyond {_MAX_SCALAR_TEXT}")
            return number
        if kind == "Path":
            return pathlib.Path(self._text(data))
        if kind == "dataclass":
            if not isinstance(data, dict):
                raise ValueError("a dataclass without its fields")
            fields = {key: self.build(item, depth + 1) for key, item in data.items()}
            names = {
                "__dataclass__": self._text(value["class_name"], 256),
                "__module__": self._text(value["module"], 256),
            }
            return {**names, **fields}
        return {key: self.build(item, depth + 1) for key, item in value.items()}


def _decode_final_answer(encoded: str, allow_pickle: bool) -> Any:
    """Rebuild the final answer the guest serialized, within bounds: the guest is not trusted with the host."""
    if not encoded.startswith(SafeSerializer.SAFE_PREFIX):
        # A pickle, only taken with allow_pickle, which trusts the guest with the whole host
        return RemotePythonExecutor._deserialize_final_answer(encoded, allow_pickle)
    begin = len(SafeSerializer.SAFE_PREFIX)
    # Each value but the first follows a comma or opens an array or object: a bound on how many there are, counted
    # before any is made. Commas in strings count too, which only an answer of millions of them runs into.
    counted = encoded.count(",", begin) + encoded.count("[", begin) + encoded.count("{", begin)
    if counted >= _MAX_FINAL_ANSWER_VALUES:
        raise ValueError(f"it holds more than {_MAX_FINAL_ANSWER_VALUES} values, or commas")
    data, end = json.JSONDecoder(parse_int=_parse_int).raw_decode(encoded, begin)
    if encoded[end:].strip():
        raise ValueError("it is not one JSON value")
    return _FinalAnswerBuilder().build(data)


def _decode_hyperlight_reply(line: bytes, start: int) -> dict:
    """Decode the reply in a console line, from `start`: an object of strings and numbers, with at most one object
    within, and a handful of members in all.

    Anything else is refused before it is built: JSON's arrays and objects cost the host many times the bytes that
    describe them, and the guest is not trusted with the host's memory.
    """
    decoder = json.JSONDecoder()
    objects, members = 0, 0

    def parse_object(s_and_end, strict, scan_once, *args, **kwargs):
        nonlocal objects
        objects += 1
        if objects > 2:
            raise ValueError("more objects than a reply has")

        def scan_member(string, index):
            nonlocal members
            members += 1
            if members > 16:
                raise ValueError("more members than a reply has")
            return scan_once(string, index)

        return json.decoder.JSONObject(s_and_end, strict, scan_member, *args, **kwargs)

    def parse_array(*args, **kwargs):
        raise ValueError("a reply has no arrays")

    decoder.parse_object, decoder.parse_array = parse_object, parse_array
    # The scanner written in Python, as the one in C builds containers without asking these
    decoder.scan_once = json.scanner.py_make_scanner(decoder)
    # Decoded where it lies, rather than copied out of the line first
    text = line.decode("ascii")
    reply, end = decoder.raw_decode(text, start)
    if text[end:].strip():
        raise ValueError("a reply ends its line")
    if not isinstance(reply, dict):
        raise ValueError("a reply is an object")
    return reply


class _HyperlightGuest:
    """A running `hluk` process: the guest's console, and the end of what else `hluk` and the guest printed."""

    # Enough for the kernel's last words when the guest crashes, without growing for as long as the guest runs
    OUTPUT_LIMIT = 64 * 1024

    def __init__(self, command: list[str], reply_limit: int):
        # In the executor's process group: whatever stops it, from the terminal or from a supervisor, stops hluk too,
        # which no finalizer would otherwise do. A Ctrl-C between steps therefore stops the guest, which the executor
        # finds before it sends the next code (`interrupted`).
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # The steps sent, against which the replies are counted
        self._sent = 0
        # What the guest wrote to its console other than replies, and what hluk and the kernel wrote to its standard
        # error, apart, so that each keeps its lines whole
        self._console, self._errors = bytearray(), bytearray()
        self._output_lock = threading.Lock()
        self._replies: queue.Queue[dict | str | None] = queue.Queue()
        self._requests: queue.Queue[bytes | None] = queue.Queue()
        self._threads = [
            threading.Thread(target=target, args=args, daemon=True)
            for target, args in (
                (self._read_replies, (reply_limit,)),
                (self._drain, (self.process.stderr, self._errors)),
                (self._write_requests, ()),
            )
        ]
        try:
            for thread in self._threads:
                thread.start()
        # BaseException: hluk must not outlive a guest that nothing holds
        except BaseException:
            self.process.kill()
            self.process.wait()
            raise

    def _keep(self, data: bytes, kept: bytearray):
        with self._output_lock:
            kept += data
            del kept[: -self.OUTPUT_LIMIT]

    def _drain(self, stream, kept: bytearray):
        # Read until hluk exits: a pipe nobody drains would fill up and block the guest
        for chunk in iter(lambda: stream.read1(65536), b""):
            self._keep(chunk, kept)

    def _read_replies(self, reply_limit: int):
        # Each reply starts a line of its own, found with `bytes.find` in what arrives rather than line by line, so
        # that what else the guest writes to its console costs little however it is cut into lines
        start = b"\n" + _HYPERLIGHT_REPLY_MARKER.encode()
        # A reply, its marker and the end of its line, and no more: the guest is not trusted with the host's memory
        line_limit = len(start) - 1 + reply_limit + 1
        pending = bytearray()
        # Whether `pending` starts with a reply's marker, and how much of it is known to hold no end of line
        in_reply, searched = False, 0
        replies = 0
        try:
            for chunk in iter(lambda: self.process.stdout.read1(65536), b""):
                pending += chunk
                while True:
                    if not in_reply:
                        at = pending.find(start)
                        if at < 0:
                            # The guest's, all but what may begin a reply
                            keep = max(0, len(pending) - len(start) + 1)
                            self._keep(pending[:keep], self._console)
                            del pending[:keep]
                            break
                        self._keep(pending[: at + 1], self._console)
                        del pending[: at + 1]
                        in_reply, searched = True, 0
                    end = pending.find(b"\n", searched)
                    if end < 0 and len(pending) < line_limit:
                        searched = len(pending)
                        break
                    if end < 0 or end >= line_limit:
                        self._replies.put(f"a reply longer than {reply_limit} bytes")
                        return
                    line = bytes(pending[: end + 1])
                    del pending[: end + 1]
                    in_reply = False
                    # One for each step and one to say it is ready, and no more: decoding is the costly part
                    replies += 1
                    if replies > self._sent + 1:
                        self._replies.put("more replies than steps")
                        return
                    try:
                        self._replies.put(_decode_hyperlight_reply(line, len(start) - 1))
                    except ValueError as e:
                        self._replies.put(f"a reply that is not one ({e})")
            # A reply cut short as the guest stopped is left out: the end of its output tells why
            if not in_reply:
                self._keep(pending, self._console)
        finally:
            # hluk is done with its output, or the guest broke off: whoever waits for a reply, now or later, waits no
            # more. What it still writes is drained, so that hluk is not blocked.
            self._replies.put(None)
            self._drain(self.process.stdout, self._console)

    def _write_requests(self):
        # On a thread of its own, so that a guest that stops reading cannot hold the executor past a step's timeout
        for request in iter(self._requests.get, None):
            try:
                self.process.stdin.write(request)
                self.process.stdin.flush()
            except (OSError, ValueError):
                # The guest is gone, which the end of its output tells
                return

    def send(self, request: dict):
        self._sent += 1
        self._requests.put(json.dumps(request).encode() + b"\n")

    def receive(self, request_id: int | None, timeout: float | None) -> dict:
        """Wait for the reply to a request, for at most `timeout` seconds (`TimeoutError`).

        Raises `_HyperlightGuestStopped` if the guest can no longer reply. Replies to other requests are skipped, and
        more replies than steps stop the guest: only a step's code writing to the console sends them, which at worst
        fails that step, or the next.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            try:
                # In short waits: a Ctrl-C does not end a wait on a lock on Windows until it is over
                reply = self._replies.get(timeout=0.2 if remaining is None else min(remaining, 0.2))
            except queue.Empty:
                continue
            if reply is None:
                # Left for any later wait
                self._replies.put(None)
                raise _HyperlightGuestStopped()
            if isinstance(reply, str):
                raise _HyperlightGuestStopped(f"the guest sent {reply}")
            if isinstance(reply, dict) and reply.get("id") == request_id:
                return reply

    def output(self) -> str:
        """What the guest wrote to its console other than replies, then what hluk and the kernel wrote."""
        for thread in self._threads[:2]:
            thread.join(timeout=1)
        with self._output_lock:
            parts = (bytes(kept).decode(errors="replace").strip() for kept in (self._console, self._errors))
            return "\n".join(part for part in parts if part)

    def exit_reason(self) -> str:
        """Why the guest stopped: hluk's verdict, the last line of its standard error, or its exit status."""
        # A guest that aborts closes its console a moment before hluk exits, so give it a chance to
        try:
            status = self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return "it stopped answering"
        self._threads[1].join(timeout=1)
        # Not the console, where any step's code may write anything as it would print; a step's code reaches the
        # standard error only through the server's own descriptor, and hluk's verdict comes last. Only the last line:
        # the kernel's register dump before it means nothing to the model.
        with self._output_lock:
            errors = bytes(self._errors).decode(errors="replace").strip()
        return errors.splitlines()[-1] if errors else f"exit status {status}"

    def interrupted(self) -> bool:
        """Whether hluk was stopped by a Ctrl-C, which reaches it as it reaches the executor."""
        if sys.platform == "win32":
            return self.process.returncode == 0xC000013A  # STATUS_CONTROL_C_EXIT
        return self.process.returncode == -signal.SIGINT

    def stop(self):
        self.process.kill()
        self.process.wait()
        self._requests.put(None)
        for thread in self._threads:
            thread.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except OSError:
                pass


def _shut_down_hyperlight(live: dict, workdir: str):
    # Also run by the executor's finalizer, so it must not refer to the executor
    guest = live.pop("guest", None)
    if guest is not None:
        guest.stop()
    shutil.rmtree(workdir, ignore_errors=True)


class HyperlightExecutor(RemotePythonExecutor):
    """
    Remote Python code executor in a local Hyperlight micro-VM, using [hyperlight-unikraft](https://github.com/hyperlight-dev/hyperlight-unikraft).

    Pass it to an agent with `CodeAgent(..., executor=HyperlightExecutor())`. With a smolagents that loads executors
    from the `smolagents.executors` entry points, `CodeAgent(..., executor_type="hyperlight")` creates it too, with
    `executor_kwargs` as its arguments.

    Each executor boots a Unikraft unikernel in its own hardware-isolated micro-VM (KVM or MSHV on Linux, WHP on
    Windows, Hypervisor.framework on macOS) through the `hluk` CLI. No account or daemon is needed, and the guest is
    restored from a warm snapshot, so a fresh sandbox is ready in milliseconds. The executor talks to the guest over its
    console, so the guest has no network access at all unless hosts are explicitly allowed with `net_allow`.

    Args:
        additional_imports (`list[str]`, *optional*): Additional Python packages to install. Packages already present
            in the guest image are skipped; others are installed with pip, which requires `net_allow` to include the
            package index (e.g. `["pypi.org", "files.pythonhosted.org"]`). The agent's own
            `additional_authorized_imports` are only passed on when the agent creates the executor.
        logger (`AgentLogger`, *optional*): Logger to use for output and errors. Defaults to one that logs at the
            `INFO` level, as an agent's does; an agent that creates the executor passes its own.
        allow_pickle (`bool`, default `False`): Whether to allow pickle serialization for objects that cannot be safely serialized to JSON.
            - `False` (default, recommended): Only safe JSON serialization is used. Raises error if object cannot be safely serialized.
            - `True` (legacy mode): Tries safe JSON serialization first, falls back to pickle with warning if needed.

            **Security Warning:** Pickle deserialization can execute arbitrary code. Only set `allow_pickle=True`
            if you fully trust the execution environment and need backward compatibility with custom types.
        hluk_path (`str`, *optional*): Path to the `hluk` binary. Defaults to the `hluk` found on `PATH`.
        runtime (`str`, default `"agent"`): Published runtime image to run, by name or full image reference. A name
            pulls the image of the installed `hluk` release; the `agent` image ships CPython 3.12 with numpy, pandas,
            scipy, scikit-learn, matplotlib, pillow and requests. Ignored if `initrd` is set.
        initrd (`str`, *optional*): Path to a custom rootfs CPIO to run instead of a published runtime.
        scratch_mb (`int`, *optional*): Guest memory in MiB. Defaults to the size the runtime image is tested with.
        net_allow (`list[str]`, *optional*): Hosts or IPs the guest may connect to. Defaults to none: every connection
            is refused, and the guest cannot listen on any port.
        hluk_args (`list[str]`, *optional*): Additional arguments to pass to `hluk run`, e.g. `["--mount", "/data:/data:ro"]`.
        startup_timeout (`float`, default `300.0`): Seconds to wait for the guest to be ready. The first run pulls the
            runtime image and saves its warm snapshot, which can take a while; later runs start in milliseconds.
        timeout_seconds (`int`, *optional*, defaults to `MAX_EXECUTION_TIME_SECONDS`): Maximum time in seconds a code
            step may run. A step that runs longer is stopped by replacing the guest with a fresh one. Set to `None` to
            disable the timeout.

    What a step writes to its standard output and error, subprocesses included, makes its logs, in the order written;
    not what it writes to `/dev/stdout`, which is the guest's console.
    Logs longer than `MAX_LOG_BYTES` (2 MiB) keep their first and last MiB, with a note of how much was left out. A
    step's value or final answer is returned up to `MAX_REPLY_BYTES` (64 MiB) once serialized; a larger one is an
    error the agent sees.

    Between steps, the guest waits for the next one with its whole micro-VM paused: what a step leaves running, a
    thread or a subprocess, only goes on while a step runs.
    """

    # The first release whose console carries lines longer than 4 KiB whole, both ways, and where subprocesses work
    # from any thread
    MIN_HLUK_VERSION = (0, 18, 0)
    # What a step's output keeps, its beginning and its end, in bytes: the guest holds no more of it in memory
    MAX_LOG_BYTES = 2 * 1024 * 1024
    # The most a reply from the guest may take, serialized: the guest is not trusted with the host's memory, of which
    # a reply costs a few times this at most
    MAX_REPLY_BYTES = 64 * 1024 * 1024
    # What an error's name, message and traceback each keep, beginning and end
    MAX_ERROR_CHARS = 64 * 1024
    _STATE_LOST = (
        "variables and imports from previous steps are lost, while tools and the agent's variables are defined again."
    )

    def __init__(
        self,
        additional_imports: list[str] | None = None,
        logger: AgentLogger | None = None,
        allow_pickle: bool = False,
        hluk_path: str | None = None,
        runtime: str = "agent",
        initrd: str | None = None,
        scratch_mb: int | None = None,
        net_allow: list[str] | None = None,
        hluk_args: list[str] | None = None,
        startup_timeout: float = 300.0,
        timeout_seconds: int | None = MAX_EXECUTION_TIME_SECONDS,
    ):
        additional_imports = additional_imports or []
        super().__init__(additional_imports, logger or AgentLogger(LogLevel.INFO), allow_pickle)
        self.hluk_path = hluk_path or shutil.which("hluk")
        if self.hluk_path is None:
            raise RuntimeError(
                "Could not find the `hluk` binary: install it from https://github.com/hyperlight-dev/hyperlight-unikraft "
                "or pass its location with `hluk_path`."
            )
        self._check_hluk_version()
        self.runtime = runtime
        self.initrd = initrd
        self.scratch_mb = scratch_mb
        self.net_allow = net_allow or []
        # Without egress pip can only fail, after retrying for a while, so it is not even tried
        self._has_egress = bool(self.net_allow)
        self.hluk_args = hluk_args or []
        self.startup_timeout = startup_timeout
        self.timeout_seconds = timeout_seconds
        self._tools: dict[str, Tool] = {}
        self._variables: dict[str, Any] = {}
        # Taken to start or stop the guest, which a step's crash recovery and cleanup() from another thread may race to
        self._lock = threading.RLock()
        self._closed = False
        # Set from the moment a guest stops being usable as it is (a step was interrupted, or a reset was) until a
        # reset has fully set a fresh one up
        self._needs_reset = False
        # A failure while reset() sets a guest up is reported, rather than recovered from by another reset
        self._resetting = False
        # Whether the model has yet to hear about the coming reset: it does after an interruption, which ends the run
        # without telling it, or a guest found stopped before the next code, and not after a failed reset, whose error
        # it got as a step's observation
        self._announce_restart = False
        # What the model hears made the reset needed, when it is announced
        self._restart_cause = "an interruption"
        # Tells the model, with the next step's output, that a restart it did not see coming discarded its state
        self._restart_notice: str | None = None
        # Tells each step's reply from any other
        self._request_ids = itertools.count(1)

        self._workdir = tempfile.mkdtemp(prefix="smolagents-hyperlight-")
        # Stops hluk and removes the guest script even when the executor is dropped or the interpreter exits without
        # cleanup()
        self._live: dict[str, _HyperlightGuest] = {}
        self._finalizer = weakref.finalize(self, _shut_down_hyperlight, self._live, self._workdir)
        self._script_path = os.path.join(self._workdir, "server.py")
        with open(self._script_path, "w") as f:
            f.write(
                f"MARKER = {_HYPERLIGHT_REPLY_MARKER!r}\nLOG_LIMIT = {self.MAX_LOG_BYTES}\n"
                f"REPLY_LIMIT = {self.MAX_REPLY_BYTES}\n{_HYPERLIGHT_GUEST_SERVER_CODE}"
            )

        try:
            self._start_guest()
            self.installed_packages = self.install_packages(additional_imports)
        # BaseException: a KeyboardInterrupt while waiting for the guest must not leave it running
        except BaseException as e:
            self.cleanup()
            if isinstance(e, Exception):
                raise RuntimeError(f"Failed to initialize Hyperlight sandbox: {e}") from e
            raise

    def _check_hluk_version(self):
        # An older hluk fails in confusing ways (a reply longer than 4 KiB arrives cut short), so refuse it up front
        version = subprocess.run([self.hluk_path, "--version"], capture_output=True, text=True, timeout=30).stdout
        match = re.search(r"(\d+)\.(\d+)\.(\d+)(-\S+)?", version)
        if match is not None:
            release = tuple(map(int, match.groups()[:3]))
            # A pre-release (`0.18.0-rc1`) comes before its release; build metadata (`0.18.0+dev.1234567`) does not
            is_pre_release = match.group(4) is not None
        if match is None or release < self.MIN_HLUK_VERSION or (release == self.MIN_HLUK_VERSION and is_pre_release):
            minimum = ".".join(map(str, self.MIN_HLUK_VERSION))
            raise RuntimeError(
                f"HyperlightExecutor needs hluk v{minimum} or later, but {self.hluk_path} reports {version.strip()!r}: "
                "update it from https://github.com/hyperlight-dev/hyperlight-unikraft"
            )

    def _build_command(self) -> list[str]:
        command = [self.hluk_path, "run"]
        if self.initrd:
            command += ["--initrd", self.initrd, "--warm"]
        else:
            command += ["--runtime", self.runtime]
        for host in self.net_allow:
            command += ["--net-allow", host]
        if self.scratch_mb is not None:
            command += ["--scratch-mb", str(self.scratch_mb)]
        return command + self.hluk_args + [self._script_path]

    @property
    def process(self) -> subprocess.Popen | None:
        """The running `hluk` process, if any."""
        guest = self._live.get("guest")
        return guest.process if guest is not None else None

    def _start_guest(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("The Hyperlight sandbox has been shut down")
            start_time = time.time()
            self.logger.log("Starting Hyperlight sandbox...", level=LogLevel.INFO)
            self._live["guest"] = _HyperlightGuest(self._build_command(), self.MAX_REPLY_BYTES)
            if self._closed:
                # cleanup() came in since the check above, too early to see this guest: the wait below ends at once
                self._live["guest"].process.kill()
            self._wait_until_ready()
            self.logger.log(f"Hyperlight sandbox is running ({time.time() - start_time:.2f}s)", level=LogLevel.INFO)

    def _stop_guest(self):
        with self._lock:
            guest = self._live.pop("guest", None)
            if guest is not None:
                guest.stop()

    def _wait_until_ready(self):
        guest = self._live["guest"]
        try:
            guest.receive(None, self.startup_timeout)
        except TimeoutError:
            raise RuntimeError(f"Hyperlight sandbox was not ready after {self.startup_timeout}s") from None
        except _HyperlightGuestStopped as e:
            if self._closed:
                raise RuntimeError("The Hyperlight sandbox was shut down while starting") from None
            if e.args:
                raise RuntimeError(f"The Hyperlight guest failed to start: {e}") from None
            try:
                status = f"exited with status {guest.process.wait(timeout=5)}"
            except subprocess.TimeoutExpired:
                status = "closed the guest's console"
            raise RuntimeError(f"hluk {status} before the guest was ready:\n{guest.output()}") from None

    def run_code_raise_errors(self, code: str) -> CodeOutput:
        """
        Execute Python code in the Hyperlight sandbox and return the result.

        Args:
            code (`str`): Python code to execute.

        Returns:
            `CodeOutput`: Code output containing the result, logs, and whether it is the final answer.
        """
        return self._run_code(code, self.timeout_seconds)

    def __call__(self, code_action: str) -> CodeOutput:
        try:
            code_output = self.run_code_raise_errors(code_action)
        except AgentError as e:
            if notice := self._take_restart_notice():
                raise AgentError(f"{notice}\n{e.message}", self.logger) from e
            raise
        if notice := self._take_restart_notice():
            code_output.logs = f"{notice}\n{code_output.logs}"
        return code_output

    def _take_restart_notice(self) -> str | None:
        notice, self._restart_notice = self._restart_notice, None
        return notice

    def _run_code(self, code: str, timeout: float | None) -> CodeOutput:
        if self._closed:
            raise RuntimeError("The Hyperlight sandbox has been shut down")
        guest = self._live.get("guest")
        if not self._resetting and guest is not None and guest.process.poll() is not None:
            # The guest stopped since its last reply, before this code was sent: a Ctrl-C in the terminal, which
            # reaches hluk in the executor's process group, or another signal. Nothing of this code ran, so it can.
            if self._closed:
                raise AgentError("The Hyperlight sandbox was shut down while executing code.", self.logger)
            self._restart_cause = "a Ctrl-C" if guest.interrupted() else f"the sandbox stopped ({guest.exit_reason()})"
            self._needs_reset = True
            self._announce_restart = True
        if self._needs_reset and not self._resetting:
            # An interrupted step, a reset or a guest left no usable guest. Read before reset(), which clears them.
            announce, cause = self._announce_restart, self._restart_cause
            if announce:
                self.logger.log(f"Restarting the Hyperlight sandbox after {cause}...", level=LogLevel.INFO)
            else:
                self.logger.log("Setting the Hyperlight sandbox up again...", level=LogLevel.INFO)
            self.reset()
            if announce:
                self._restart_notice = (
                    f"Note: the sandbox was restarted before this code ran, after {cause}: variables and imports "
                    "defined by earlier code are gone, while tools and the agent's variables are defined again."
                )
        # Read once: cleanup() from another thread may stop the guest at any time
        guest = self._live.get("guest")
        if guest is None:
            raise AgentError("The Hyperlight sandbox was shut down while executing code.", self.logger)
        request_id = next(self._request_ids)
        try:
            guest.send({"id": request_id, "code": code})
            result = guest.receive(request_id, timeout)
        except TimeoutError as e:
            if self._closed:
                raise AgentError("The Hyperlight sandbox was shut down while executing code.", self.logger) from e
            # The guest is still busy with the step, and only a fresh one can take the next
            self._recover(f"Code execution exceeded the maximum execution time of {timeout} seconds", e)
        except _HyperlightGuestStopped as e:
            if self._closed:
                raise AgentError("The Hyperlight sandbox was shut down while executing code.", self.logger) from e
            if e.args:
                self._recover(f"The sandbox broke off the step: {e}", e)
            reason = guest.exit_reason()
            self.logger.log(f"Hyperlight guest output:\n{guest.output()}", level=LogLevel.DEBUG)
            if guest.interrupted():
                # Once the code was sent: it may have run in part
                self._recover("The sandbox was stopped by a Ctrl-C while this code was sent or ran", e)
            self._recover(f"The sandbox crashed while executing code ({reason})", e)
        except KeyboardInterrupt:
            # The guest would go on running the step and make the next one wait for it
            self._needs_reset = True
            self._announce_restart = True
            self._restart_cause = "an interruption"
            self._stop_guest()
            raise
        if not self._is_step_result(result):
            self._recover("The sandbox sent a reply without the step's result", ValueError("not a step's result"))
        # The guest keeps a step's logs within MAX_LOG_BYTES, and a note of what it left out; one that does not would
        # have the host log all of it
        logs = _clip(result["logs"], self.MAX_LOG_BYTES + 100)
        execution_logs, error, output = logs, result["error"], result["output"]

        if error:
            if error["name"] == RemotePythonExecutor.FINAL_ANSWER_EXCEPTION:
                try:
                    final_answer = _decode_final_answer(error["value"], self.allow_pickle)
                except SerializationError:
                    raise
                # RecursionError, from nesting deeper than the host rebuilds: any failure is the answer's, which the
                # agent's code made
                except Exception as e:
                    reason = _clip(str(e), 1000)
                    raise AgentError(
                        f"{execution_logs}\nThe final answer could not be read: {reason}", self.logger
                    ) from e
                return CodeOutput(output=final_answer, logs=execution_logs, is_final_answer=True)
            name, value, traceback = (
                _clip(error[key], self.MAX_ERROR_CHARS) for key in ("name", "value", "traceback")
            )
            error_message = f"{execution_logs}\nExecuting code yielded an error:\n{name}\n{value}\n{traceback}"
            raise AgentError(error_message, self.logger)

        return CodeOutput(output=output, logs=execution_logs, is_final_answer=False)

    @staticmethod
    def _is_step_result(reply: dict) -> bool:
        error = reply.get("error")
        return (
            reply.keys() >= {"logs", "output", "error"}
            and isinstance(reply.get("logs"), str)
            and isinstance(reply.get("output"), (str, type(None)))
            and (
                error is None
                or isinstance(error, dict)
                and all(isinstance(error.get(key), str) for key in ("name", "value", "traceback"))
            )
        )

    def _recover(self, failure: str, cause: Exception) -> NoReturn:
        """Bring a fresh guest up after the current one failed, so that the agent can keep going, and say so."""
        if self._resetting:
            # The failure is reset()'s, which reports it: resetting again from here could go on for good
            self._needs_reset = True
            self._stop_guest()
            raise AgentError(
                f"{failure} while packages, tools and variables were defined in a fresh sandbox; the next step will "
                "try again with another one.",
                self.logger,
            ) from cause
        try:
            self.reset()
        except Exception as reset_error:
            raise RuntimeError(f"{failure}. {reset_error}") from reset_error
        raise AgentError(f"{failure}, so the sandbox was restarted: {self._STATE_LOST}", self.logger) from cause

    def send_tools(self, tools: dict[str, Tool]):
        # Kept so that reset() can define them again in the fresh guest
        self._tools = tools
        super().send_tools(tools)

    def send_variables(self, variables: dict[str, Any]):
        super().send_variables(variables)
        # Kept so that reset() can define them again in the fresh guest, once they are known to serialize. Replaced
        # rather than merged: an agent sends all its state at the start of each run.
        self._variables = dict(variables)

    def install_packages(self, additional_imports: list[str]) -> list[str]:
        """Install packages missing from the guest image with pip, and return the ones available in the guest."""
        if not additional_imports:
            return []
        # Imports such as "numpy.*" or "*" are authorizations rather than packages; most of the time the package is
        # already in the image, so pip (and the network access it needs) is only used for the rest.
        packages = sorted({name.split(".")[0] for name in additional_imports if name.split(".")[0] not in ("", "*")})
        # Wrapped in a function so that none of these names leak into the agent's namespace. Not bound by the step
        # timeout: pip can take longer than an agent's step should.
        code_output = self._run_code(
            dedent(
                f"""\
                def _smolagents_install_packages(packages, has_egress):
                    import importlib.metadata
                    import importlib.util
                    import json
                    import subprocess
                    import sys

                    def is_installed(name):
                        try:
                            importlib.metadata.distribution(name)
                            return True
                        except importlib.metadata.PackageNotFoundError:
                            return importlib.util.find_spec(name) is not None

                    missing = [name for name in packages if not is_installed(name)]
                    if missing and has_egress:
                        pip = subprocess.run(
                            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                             "--root-user-action=ignore", *missing],
                            capture_output=True,
                            text=True,
                        )
                        if pip.returncode != 0:
                            print(pip.stdout + pip.stderr)
                        importlib.invalidate_caches()
                        missing = [name for name in missing if not is_installed(name)]
                    print(json.dumps(missing))

                _smolagents_install_packages({packages!r}, {self._has_egress!r})
                del _smolagents_install_packages
                """
            ),
            self.startup_timeout,
        )
        *pip_logs, missing_line = code_output.logs.strip().splitlines() or [""]
        missing = None
        # No longer than the list of every package asked for, which it is part of: a longer line, which a guest that
        # is not trusted could fill with millions of values, is not even parsed
        if len(missing_line) <= len(json.dumps(packages)):
            try:
                missing = json.loads(missing_line)
            except ValueError:
                pass
        if not isinstance(missing, list) or not all(name in packages for name in missing):
            # The step's code is the executor's, but a guest that answers it with something else is not trusted
            raise RuntimeError(f"Could not tell which packages the Hyperlight guest has: {code_output.logs[-1000:]!r}")
        if missing and not self._has_egress:
            self.logger.log_error(
                f"Packages {', '.join(missing)} are not in the Hyperlight guest image, and installing them needs "
                "network access to the package index: allow it with `net_allow`."
            )
        elif missing:
            self.logger.log_error(f"Failed to install packages {', '.join(missing)}:\n" + "\n".join(pip_logs))
        return [name for name in packages if name not in missing]

    def reset(self):
        """Replace the guest with a fresh one restored from the warm snapshot, which takes milliseconds.

        Variables defined by code are discarded; packages installed so far are installed again, and tools and the
        variables of the last `send_variables` call are defined again. If that fails, the fresh guest is stopped and a
        `RuntimeError` says why; the next step tries again with another one. A restart the model has yet to hear of,
        after an interruption, is no longer announced: the caller asked for the fresh state.
        """
        with self._lock:
            self._needs_reset = True
            self._resetting = True
            try:
                self._stop_guest()
                self._start_guest()
                self.installed_packages = self.install_packages(self.installed_packages)
                if self._tools:
                    self.run_code_raise_errors(get_tools_definition_code(self._tools))
                if self._variables:
                    super().send_variables(self._variables)
            # BaseException: a guest left half set up, by an error or an interruption, would run the next step without
            # its tools or variables, so it is stopped, and the next step tries again with a fresh one
            except BaseException as e:
                self._stop_guest()
                # Still owed to the model after an interruption, even when it hears of this failure
                if not isinstance(e, Exception):
                    self._announce_restart = True
                if isinstance(e, Exception):
                    raise RuntimeError(f"The Hyperlight sandbox could not be set up again: {e}") from e
                raise
            finally:
                self._resetting = False
            # A fresh guest is set up: nothing is owed to the model but what the caller tells it
            self._needs_reset = False
            self._announce_restart = False
            self._restart_cause = "an interruption"

    def cleanup(self):
        """Stop the Hyperlight sandbox and remove its temporary files."""
        self._closed = True
        try:
            # Killed before taking the lock, which a starting guest or a reset holds: the reply it waits for never
            # comes, which it learns at once, and it lets go
            if (guest := self._live.get("guest")) is not None:
                guest.process.kill()
            with self._lock:
                if self.process is not None:
                    self.logger.log("Shutting down Hyperlight sandbox...", level=LogLevel.INFO)
                self._finalizer()
        except Exception as e:
            self.logger.log_error(f"Error during cleanup: {e}")

    def delete(self):
        """Ensure cleanup on deletion."""
        self.cleanup()
