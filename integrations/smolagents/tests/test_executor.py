import base64
import gc
import importlib.metadata
import io
import json
import os
import queue
import random
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from textwrap import dedent
from unittest.mock import MagicMock, patch

import numpy as np
import PIL.Image
import pytest
from rich.console import Console
from smolagents.agents import CodeAgent
from smolagents.default_tools import FinalAnswerTool
from smolagents.local_python_executor import CodeOutput
from smolagents.monitoring import AgentLogger, LogLevel
from smolagents.remote_executors import RemotePythonExecutor
from smolagents.serialization import SafeSerializer, SerializationError
from smolagents.utils import AgentError

from smolagents_hyperlight import HyperlightExecutor
from smolagents_hyperlight.executor import _HYPERLIGHT_REPLY_MARKER, _HyperlightGuest


require_run_all = pytest.mark.skipif(not os.getenv("RUN_ALL"), reason="requires RUN_ALL environment variable")


class CommonExecutorTests:
    """The tests smolagents runs on each of its remote executors (tests/test_remote_executors.py there)."""

    @pytest.fixture(autouse=True)
    def set_executor(self, custom_executor):
        self.executor = custom_executor

    def test_state_persistence(self):
        """Test that variables and imports form one snippet persist in the next"""
        code_action = "import numpy as np; a = 2"
        self.executor(code_action)

        code_action = "print(np.sqrt(a))"
        code_output = self.executor(code_action)
        assert "1.41421" in code_output.logs

    def test_execute_output(self):
        """Test execution that returns a string"""
        self.executor.send_tools({"final_answer": FinalAnswerTool()})
        code_action = 'final_answer("This is the final answer")'
        code_output = self.executor(code_action)
        assert code_output.output == "This is the final answer", "Result should be 'This is the final answer'"

    def test_execute_multiline_output(self):
        """Test execution that returns a string"""
        self.executor.send_tools({"final_answer": FinalAnswerTool()})
        code_action = 'result = "This is the final answer"\nfinal_answer(result)'
        code_output = self.executor(code_action)
        assert code_output.output == "This is the final answer", "Result should be 'This is the final answer'"

    def test_execute_image_output(self):
        """Test execution that returns a base64 image"""
        self.executor.send_tools({"final_answer": FinalAnswerTool()})
        code_action = dedent("""
            import base64
            from PIL import Image
            from io import BytesIO
            image = Image.new("RGB", (10, 10), (255, 0, 0))
            final_answer(image)
        """)
        code_output = self.executor(code_action)
        assert isinstance(code_output.output, PIL.Image.Image), "Result should be a PIL Image"

    def test_syntax_error_handling(self):
        """Test handling of syntax errors"""
        code_action = 'print("Missing Parenthesis'  # Syntax error
        with pytest.raises(AgentError) as exception_info:
            self.executor(code_action)
        assert "SyntaxError" in str(exception_info.value), "Should raise a syntax error"

    @pytest.mark.parametrize(
        "code_action, expected_result",
        [
            (
                dedent('''
                    final_answer("""This is
                    a multiline
                    final answer""")
                '''),
                "This is\na multiline\nfinal answer",
            ),
            (
                dedent("""
                    text = '''Text containing
                    final_answer(5)
                    '''
                    final_answer(text)
                """),
                "Text containing\nfinal_answer(5)\n",
            ),
            (
                dedent("""
                    num = 2
                    if num == 1:
                        final_answer("One")
                    elif num == 2:
                        final_answer("Two")
                """),
                "Two",
            ),
        ],
    )
    def test_final_answer_patterns(self, code_action, expected_result):
        self.executor.send_tools({"final_answer": FinalAnswerTool()})
        code_output = self.executor(code_action)
        assert code_output.is_final_answer is True
        assert code_output.output == expected_result

    def test_custom_final_answer(self):
        class CustomFinalAnswerTool(FinalAnswerTool):
            def forward(self, answer: str) -> str:
                return "CUSTOM" + answer

        self.executor.send_tools({"final_answer": CustomFinalAnswerTool()})
        code_action = dedent("""
            final_answer(answer="_answer")
        """)
        code_output = self.executor(code_action)
        assert code_output.is_final_answer is True
        assert code_output.output == "CUSTOM_answer"

    def test_custom_final_answer_with_custom_inputs(self):
        class CustomFinalAnswerToolWithCustomInputs(FinalAnswerTool):
            inputs = {
                "answer1": {"type": "string", "description": "First part of the answer."},
                "answer2": {"type": "string", "description": "Second part of the answer."},
            }

            def forward(self, answer1: str, answer2: str) -> str:
                return answer1 + "CUSTOM" + answer2

        self.executor.send_tools({"final_answer": CustomFinalAnswerToolWithCustomInputs()})
        code_action = dedent("""
            final_answer(
                answer1="answer1_",
                answer2="_answer2"
            )
        """)
        code_output = self.executor(code_action)
        assert code_output.is_final_answer is True
        assert code_output.output == "answer1_CUSTOM_answer2"


def _png_base64(width, height, chunks=0, header_last=False):
    """A PNG of one color, which compresses to a few hundred KiB however large, with that many more empty chunks, and
    with header_last, a second header chunk before its end."""
    buffer = io.BytesIO()
    PIL.Image.new("1", (width, height)).save(buffer, format="PNG")
    png = buffer.getvalue()
    # After the signature and the header chunk: a private chunk, of no data, with its CRC
    empty = struct.pack(">I4s", 0, b"abCd") + struct.pack(">I", zlib.crc32(b"abCd"))
    png = png[:33] + empty * chunks + png[33:]
    if header_last:
        png = png[:-12] + png[8:33] + png[-12:]
    return base64.b64encode(png).decode()


def _image_base64(image_format):
    buffer = io.BytesIO()
    PIL.Image.new("RGB", (1, 1)).save(buffer, format=image_format)
    return base64.b64encode(buffer.getvalue()).decode()


def _hyperlight_result(output=None, logs="", error=None):
    return {"output": output, "logs": logs, "error": error}


class FakeHlukProcess:
    """A stand-in for the `hluk` process: real pipes for its output, and the fake guest behind its standard input."""

    def __init__(self, guest):
        self._guest = guest
        stdout_read, self._stdout = os.pipe()
        stderr_read, self._stderr = os.pipe()
        self.stdout, self.stderr = open(stdout_read, "rb"), open(stderr_read, "rb")
        self.stdin = MagicMock()
        self.stdin.write.side_effect = self._take
        self.returncode = None
        self.kill = MagicMock(side_effect=self.exit)
        self.wait = MagicMock(side_effect=lambda timeout=None: self.returncode)
        self.poll = MagicMock(side_effect=lambda: self.returncode)
        if guest.ready:
            self.reply({"id": None})

    def print(self, text, stream="stderr"):
        os.write(self._stdout if stream == "stdout" else self._stderr, text.encode())

    def reply(self, message):
        # On a line of its own, as the guest writes it
        self.print("\n" + _HYPERLIGHT_REPLY_MARKER + json.dumps(message) + "\n", stream="stdout")

    def close_stdout(self):
        if self._stdout is not None:
            os.close(self._stdout)
            self._stdout = None

    def exit(self, status=-9):
        if self.returncode is None:
            self.returncode = status
            self.close_stdout()
            os.close(self._stderr)

    def _take(self, line):
        # Called on the executor's writer thread, as the guest reads the step
        if self.returncode is not None:
            raise BrokenPipeError
        request = json.loads(line)
        self._guest.codes.append(request["code"])
        result = self._guest.next_result()
        if callable(result):
            result = result(self)
        if isinstance(result, dict):
            self.reply({"id": request["id"], **result})
        self._guest.taken.append(request["id"])


class FakeHyperlightGuest:
    """Answers the executor's steps as the guest does, on the console of a `FakeHlukProcess`.

    Each step takes the next item of `results`: a result to reply with, a function of the process to call for one
    (or for none: the guest stays silent), or an exception that waiting for the reply raises instead.
    """

    def __init__(self):
        self.results = []
        self.codes = []
        self.ready = True
        # The steps the guest has read, by request id
        self.taken = []
        self._raise_next = None

    def next_result(self):
        result = self.results.pop(0) if self.results else None
        if result is None:
            # Installing packages prints the list of those still missing
            return _hyperlight_result(logs="[]\n" if "_smolagents_install_packages" in self.codes[-1] else "")
        if isinstance(result, BaseException):
            self._raise_next = result
            return None
        return result

    def receive(self, guest, request_id, timeout):
        # The step goes to the guest on the executor's writer thread: wait until the guest has read it, or for a
        # moment if it never does, to learn whether waiting for the reply raises. A timeout is raised at once rather
        # than after it.
        deadline = time.monotonic() + 1
        while request_id is not None and request_id not in self.taken and time.monotonic() < deadline:
            time.sleep(0.001)
        if self._raise_next is not None:
            exception, self._raise_next = self._raise_next, None
            raise exception
        return _HyperlightGuest_receive(guest, request_id, timeout)


_HyperlightGuest_receive = _HyperlightGuest.receive


def _crash(status):
    def crash(process):
        process.print("guest aborted\n")
        process.exit(status)

    return crash


@pytest.fixture
def mock_hluk():
    """Mock the `hluk` process and the guest behind it, yielding the mocked `Popen` and the fake guest."""
    fake_guest = FakeHyperlightGuest()
    with (
        patch("shutil.which", return_value="/usr/bin/hluk"),
        patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="hluk 0.18.0\n")),
        patch("subprocess.Popen", side_effect=lambda *args, **kwargs: FakeHlukProcess(fake_guest)) as mock_popen,
        patch.object(_HyperlightGuest, "receive", autospec=True, side_effect=fake_guest.receive),
    ):
        yield mock_popen, fake_guest


class TestHyperlightExecutorUnit:
    def test_instantiation_without_hluk(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="Could not find the `hluk` binary"):
                HyperlightExecutor(additional_imports=[], logger=MagicMock())

    @pytest.mark.parametrize("version", ["hluk 0.17.0", "hluk 0.17.0+dev.44ee170", "hluk 0.18.0-rc1", ""])
    def test_old_hluk_is_refused(self, mock_hluk, version):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=version)):
            with pytest.raises(RuntimeError, match="needs hluk v0.18.0 or later"):
                HyperlightExecutor(additional_imports=[], logger=MagicMock())

    @pytest.mark.parametrize("version", ["hluk 0.18.0", "hluk 0.18.0+dev.1234567", "hluk 0.19.0-rc1", "hluk 1.0.0"])
    def test_recent_hluk_is_accepted(self, mock_hluk, version):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=version)):
            HyperlightExecutor(additional_imports=[], logger=MagicMock()).cleanup()

    def test_default_command_has_no_network(self, mock_hluk):
        mock_popen, _ = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())

        # No network flag at all: the guest can neither listen nor connect, and the executor uses its console
        command = mock_popen.call_args.args[0]
        assert command == ["/usr/bin/hluk", "run", "--runtime", "agent", executor._script_path]
        assert mock_popen.call_args.kwargs == {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        with open(executor._script_path) as f:
            script = f.read()
        assert f"MARKER = {_HYPERLIGHT_REPLY_MARKER!r}" in script
        assert f"REPLY_LIMIT = {HyperlightExecutor.MAX_REPLY_BYTES}" in script
        executor.cleanup()

    def test_custom_parameters(self, mock_hluk):
        mock_popen, _ = mock_hluk
        executor = HyperlightExecutor(
            additional_imports=[],
            logger=MagicMock(),
            hluk_path="/opt/hluk",
            initrd="/images/custom.cpio",
            scratch_mb=512,
            net_allow=["pypi.org", "files.pythonhosted.org"],
            hluk_args=["--mount", "/data:/data:ro"],
        )

        command = mock_popen.call_args.args[0]
        assert command[:5] == ["/opt/hluk", "run", "--initrd", "/images/custom.cpio", "--warm"]
        assert command[5:] == [
            "--net-allow",
            "pypi.org",
            "--net-allow",
            "files.pythonhosted.org",
            "--scratch-mb",
            "512",
            "--mount",
            "/data:/data:ro",
            executor._script_path,
        ]
        executor.cleanup()

    def test_hluk_exiting_before_ready_raises(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        fake_guest.ready = False

        def failing_hluk(*args, **kwargs):
            process = FakeHlukProcess(fake_guest)
            process.print("error: no hypervisor found\n")
            process.exit(1)
            return process

        mock_popen.side_effect = failing_hluk
        with pytest.raises(
            RuntimeError, match="hluk exited with status 1 before the guest was ready:\nerror: no hypervisor found"
        ):
            HyperlightExecutor(additional_imports=[], logger=MagicMock())

    def test_startup_times_out(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        fake_guest.ready = False
        processes = []
        mock_popen.side_effect = lambda *args, **kwargs: processes.append(FakeHlukProcess(fake_guest)) or processes[-1]
        with pytest.raises(RuntimeError, match="not ready after 0.2s"):
            HyperlightExecutor(additional_imports=[], logger=MagicMock(), startup_timeout=0.2)
        processes[0].kill.assert_called()

    def test_interrupted_startup_stops_hluk(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        fake_guest.ready = False
        processes = []
        mock_popen.side_effect = lambda *args, **kwargs: processes.append(FakeHlukProcess(fake_guest)) or processes[-1]
        workdirs = []
        make_workdir = tempfile.mkdtemp
        with (
            patch(
                "tempfile.mkdtemp",
                side_effect=lambda **kwargs: workdirs.append(make_workdir(**kwargs)) or workdirs[-1],
            ),
            patch.object(_HyperlightGuest, "receive", side_effect=KeyboardInterrupt),
        ):
            with pytest.raises(KeyboardInterrupt):
                HyperlightExecutor(additional_imports=[], logger=MagicMock())
        processes[0].kill.assert_called()
        assert len(workdirs) == 1 and not os.path.exists(workdirs[0])

    def test_run_code_output(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        fake_guest.results.append(_hyperlight_result(output="3", logs="hello\n"))

        code_output = executor.run_code_raise_errors("print('hello')\n1 + 2")

        assert code_output == CodeOutput(output="3", logs="hello\n", is_final_answer=False)
        assert fake_guest.codes[-1] == "print('hello')\n1 + 2"
        executor.cleanup()

    def test_steps_and_replies_are_single_lines_of_ascii(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        code = "print('\\r\\x04\\n')\nname = 'café 🚀'\r\n"

        executor.run_code_raise_errors(code)

        line = executor.process.stdin.write.call_args.args[0]
        assert line.endswith(b"\n") and line.count(b"\n") == 1
        assert all(0x20 <= byte < 0x7F for byte in line[:-1])
        assert fake_guest.codes[-1] == code
        executor.cleanup()

    def test_run_code_error(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        fake_guest.results.append(
            _hyperlight_result(
                logs="before\n",
                error={"name": "ZeroDivisionError", "value": "division by zero", "traceback": "Traceback..."},
            )
        )

        with pytest.raises(AgentError) as exception_info:
            executor.run_code_raise_errors("print('before'); 1 / 0")
        assert "before" in str(exception_info.value)
        assert "ZeroDivisionError" in str(exception_info.value)
        assert "Traceback..." in str(exception_info.value)
        executor.cleanup()

    def test_run_code_final_answer(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        fake_guest.results.append(
            _hyperlight_result(error={"name": "FinalAnswerException", "value": 'safe:{"answer": 42}', "traceback": ""})
        )

        code_output = executor.run_code_raise_errors("final_answer({'answer': 42})")

        assert code_output.is_final_answer is True
        assert code_output.output == {"answer": 42}
        executor.cleanup()

    @pytest.mark.parametrize(
        "payload, reason",
        [
            # Millions of values within the reply limit, which the host would make before anything else saw them
            ("[" + ",".join(["{}"] * 2_000_000) + "]", "more than 2000000 values"),
            ('{"__type__": "tuple", "data": [' + ",".join(["0"] * 2_000_000) + "]}", "more than 2000000 values"),
            # An array whose type asks for gigabytes in a few bytes
            ('{"__type__": "ndarray", "data": ["a"], "dtype": "U300000000"}', "array of"),
            ('{"__type__": "ndarray", "data": [[1]], "dtype": "(100000000,)f8"}', "array of"),
            ('{"__type__": "ndarray", "data": [1], "dtype": "no such type"}', "could not be read"),
            # Deeper than the host rebuilds
            ("[" * 100_000 + "]" * 100_000, "could not be read"),
            ("not json", "could not be read"),
            ('{"a": 1} {"b": 2}', "not one JSON value"),
            # Integers whose hashes all collide, which a set would take hours to build
            (
                '{"__type__": "set", "data": [' + ",".join(str(((1 << 61) - 1) * i) for i in range(10_001)) + "]}",
                "more than 10000 values",
            ),
            # Tuples, which hash as their items do, as members
            (
                '{"__type__": "set", "data": ['
                + ",".join('{"__type__": "tuple", "data": [1, 2, 3, %d]}' % i for i in range(2501))
                + "]}",
                "more than 10000 values",
            ),
            ('{"__type__": "set", "data": "abc"}', "without its items"),
            ('{"__type__": "tuple", "data": "' + "x" * 100_000 + '"}', "without its items"),
            # Long integers whose hashes collide, each comparison of which reads all their digits
            (
                '{"__type__": "set", "data": ['
                + ",".join(str(10**4000 * ((1 << 61) - 1) - i * ((1 << 61) - 1)) for i in range(1000))
                + "]}",
                "not hashed: int",
            ),
            # Decimals, which an integer compares with by converting itself, at a cost quadratic in its digits
            ('{"__type__": "set", "data": [{"__type__": "Decimal", "data": "1"}, 2]}', "not hashed: Decimal"),
            # Items rebuilt before numpy converts them, which numpy would expand
            (
                '{"__type__": "ndarray", "dtype": "int64", "data": [{"__type__": "Decimal", "data": "1e9999"}]}',
                "not numbers",
            ),
            (
                '{"__type__": "ndarray", "dtype": "uint8", "data": [{"__type__": "PIL.Image", "data": ""}]}',
                "not numbers",
            ),
            ('{"__type__": "ndarray", "dtype": "float64", "data": [' + "9" * 41 + "]}", "more than 40 digits"),
            ('{"__type__": "ndarray", "dtype": "object", "data": [1]}', "array of a type"),
            ('{"__type__": "ndarray", "dtype": "str", "data": [1]}', "array of a type"),
            # A Decimal whose exponent int() or format() would write out
            ('{"__type__": "Decimal", "data": "1e999999999"}', "exponent beyond 4300"),
            ('{"__type__": "Decimal", "data": "1e-999999999"}', "exponent beyond 4300"),
            ('{"__type__": "set", "data": [' + str(2**256) + "]}", "not hashed: int"),
            (
                '{"__type__": "ndarray", "dtype": "int8", "data": ' + "[" * 120 + "]" * 120 + "}",
                "nested more than 100",
            ),
            # Deeper than the host rebuilds, within what JSON reads
            ("[" * 150 + "]" * 150, "nested more than 100"),
            (
                '{"__type__": "dataclass", "class_name": ' + "[" * 500 + "]" * 500 + ', "module": "m", "data": {}}',
                "at most 256 characters",
            ),
            # Text that makes objects whose cost grows faster than its length
            ('{"__type__": "Path", "data": "' + "ab/" * 100_000 + '"}', "at most 4300 characters"),
            ('{"__type__": "Decimal", "data": "' + "9" * 100_000 + '"}', "at most 4300 characters"),
            # Images of many chunks, which Pillow reads one by one, or many images
            ('{"__type__": "PIL.Image", "data": "' + _png_base64(1, 1, chunks=5000) + '"}', "more than 4096 chunks"),
            (
                "["
                + ",".join(['{"__type__": "PIL.Image", "data": "' + _png_base64(1, 1, chunks=100) + '"}'] * 41)
                + "]",
                "more than 4096 chunks",
            ),
            (
                "[" + ",".join(['{"__type__": "PIL.Image", "data": "' + _png_base64(1, 1) + '"}'] * 257) + "]",
                "256 images",
            ),
            # A size the budget would read in one header chunk, and Pillow in another
            ('{"__type__": "PIL.Image", "data": "' + _png_base64(1, 1, header_last=True) + '"}', "first and only"),
            ('{"__type__": "dict_with_complex_keys", "data": [[1]]}', "not a key and a value"),
            ('{"__type__": "dict_with_complex_keys", "data": [[[], 1]]}', "could not be read"),
            # Formats other than PNG, some of which Pillow has other programs draw
            ('{"__type__": "PIL.Image", "data": "' + _image_base64("GIF") + '"}', "could not be read"),
            # Type strings that take numpy itself long to read
            ('{"__type__": "ndarray", "data": [], "dtype": "' + "i1," * 100_000 + 'i1"}', "array of a type"),
            ('{"__type__": "ndarray", "data": [], "dtype": "i' + "1" * 100_000 + '"}', "array of a type"),
            ('{"__type__": "ndarray", "data": [], "dtype": {"names": ["a"], "formats": ["i1"]}}', "array of a type"),
            # An integer that takes quadratic time to read, on a Python without a limit of its own
            ("9" * 5000, "more than 4300 digits"),
            # A small compressed image of a few hundred million pixels, for when it is shown
            ('{"__type__": "PIL.Image", "data": "' + _png_base64(8000, 8000) + '"}', "more than 25000000 pixels"),
        ],
        ids=lambda value: value[:24] + str(len(value)),
    )
    def test_unreadable_final_answer_is_an_error(self, mock_hluk, payload, reason):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        fake_guest.results.append(
            _hyperlight_result(
                logs="computing\n",
                error={"name": "FinalAnswerException", "value": "safe:" + payload, "traceback": ""},
            )
        )

        with pytest.raises(AgentError, match=reason) as exception_info:
            executor("final_answer(x)")
        assert "computing" in str(exception_info.value)
        # Not the payload again, in the error the agent and its logs get
        assert len(str(exception_info.value)) < 2000
        # The guest itself is fine
        assert executor.process.poll() is None
        executor.cleanup()

    def test_final_answer_within_bounds_is_rebuilt(self, mock_hluk):
        # The ones hashed: scalars of a bounded size, and tuples of them
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        answers = [
            "a, b" * 1_000_000,
            set(range(10_000)),
            {(i, str(i)): [i] * 100 for i in range(1000)},
            # A grid, by coordinates
            {(x, y): x * y for x in range(50) for y in range(50)},
            {(i, i + 1) for i in range(5000)},
            {frozenset({i, -i}) for i in range(1, 1000)},
            __import__("decimal").Decimal("3.14159"),
            __import__("pathlib").Path("/data/report.csv"),
            __import__("datetime").datetime(2026, 10, 19, 12, 30),
            {"a", b"b", 1.5, None, True, 2**256 - 1, __import__("datetime").date(2026, 1, 1)},
            [PIL.Image.new("RGB", (10, 10))] * 256,
            # A 0-d array, whose data is the number itself
            np.array(5),
            PIL.Image.new("RGB", (5000, 5000)),
            10**4000,
        ]
        fake_guest.results += [
            _hyperlight_result(
                error={"name": "FinalAnswerException", "value": SafeSerializer.dumps(answer), "traceback": ""}
            )
            for answer in answers
        ]

        for answer in answers:
            output = executor("final_answer(x)").output
            if isinstance(answer, PIL.Image.Image):
                assert output.size == answer.size
            elif isinstance(answer, list):
                assert [image.size for image in output] == [image.size for image in answer]
            else:
                assert output == answer
        executor.cleanup()

    def test_oversized_logs_and_errors_are_clipped(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        # As a guest that does not keep to the limits would send them
        fake_guest.results += [
            _hyperlight_result(logs="a" * 10_000_000),
            _hyperlight_result(error={"name": "E" * 10_000_000, "value": "v" * 10_000_000, "traceback": "t" * 100}),
        ]

        logs = executor("print(x)").logs
        assert len(logs) < HyperlightExecutor.MAX_LOG_BYTES + 200 and "characters truncated" in logs
        with pytest.raises(AgentError) as exception_info:
            executor("raise E")
        assert len(str(exception_info.value)) < 3 * HyperlightExecutor.MAX_ERROR_CHARS
        executor.cleanup()

    def test_final_answer_arrays_are_rebuilt(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        values = list(range(1_000_000))
        fake_guest.results += [
            _hyperlight_result(
                error={
                    "name": "FinalAnswerException",
                    "value": "safe:" + json.dumps({"__type__": "ndarray", "data": values, "dtype": dtype}),
                    "traceback": "",
                }
            )
            for dtype in ("int64", "bool", "complex128")
        ]

        for dtype in ("int64", "bool", "complex128"):
            output = executor("final_answer(x)").output
            assert isinstance(output, np.ndarray) and output.dtype == dtype and output.shape == (1_000_000,)
        executor.cleanup()

    def test_install_packages(self, mock_hluk):
        _, fake_guest = mock_hluk
        logger = MagicMock()
        executor = HyperlightExecutor(additional_imports=[], logger=logger)
        fake_guest.results.append(_hyperlight_result(logs='["wikipediaapi"]\n'))

        installed = executor.install_packages(["numpy.*", "*", "pandas", "wikipediaapi"])

        assert installed == ["numpy", "pandas"]
        assert "['numpy', 'pandas', 'wikipediaapi'], False" in fake_guest.codes[-1]
        assert "net_allow" in logger.log_error.call_args.args[0]
        executor.cleanup()

    def test_reply_to_another_step_is_never_taken(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())

        def forged_then_real(process):
            # As the step's code could write to the console: a reply to another step, then the step's own
            process.reply({"id": 0, **_hyperlight_result(output="'forged'")})
            process.print("not a reply\n", stream="stdout")
            return _hyperlight_result(output="'real'")

        fake_guest.results.append(forged_then_real)

        # One reply more than the steps sent: the guest broke off, whichever reply came first
        with pytest.raises(AgentError, match="the guest sent more replies than steps, so the sandbox was restarted"):
            executor.run_code_raise_errors("'real'")
        assert mock_popen.call_count == 2
        executor.cleanup()

    def test_silent_reply_to_another_step_is_skipped(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock(), timeout_seconds=0.5)
        # A reply to another step, and none to this one: the step times out rather than taking it
        fake_guest.results.append(lambda process: process.reply({"id": 0, **_hyperlight_result(output="'forged'")}))

        with pytest.raises(AgentError, match="exceeded the maximum execution time"):
            executor.run_code_raise_errors("'real'")
        executor.cleanup()

    @pytest.mark.parametrize(
        "reply, reason",
        [
            ("{not json\n", r"a reply that is not one \(Expecting property name"),
            ('{"id": 1, "logs": "\u00e9t\u00e9"}\n', r"a reply that is not one \('ascii' codec"),
            ('"just a string"\n', r"a reply that is not one \(a reply is an object\)"),
            # JSON whose containers would cost the host far more memory than its bytes do
            (
                '{"id": 1, "logs": ' + "[" * 400 + "]" * 400 + "}\n",
                r"a reply that is not one \(a reply has no arrays\)",
            ),
            ('{"a": {"b": {"c": {}}}}\n', r"a reply that is not one \(more objects than a reply has\)"),
            ('{"id": 1' + ', "a": 0' * 100 + "}\n", r"a reply that is not one \(more members than a reply has\)"),
            ("x" * 2000, "a reply longer than 1000 bytes"),
        ],
    )
    def test_unreadable_reply_restarts_guest(self, mock_hluk, reply, reason):
        mock_popen, fake_guest = mock_hluk
        with patch.object(HyperlightExecutor, "MAX_REPLY_BYTES", 1000):
            executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        broken_process = executor.process
        fake_guest.results.append(
            lambda process: process.print("\n" + _HYPERLIGHT_REPLY_MARKER + reply, stream="stdout")
        )

        with pytest.raises(
            AgentError, match=f"broke off the step: the guest sent {reason}.*, so the sandbox was restarted"
        ):
            executor.run_code_raise_errors("1")

        broken_process.kill.assert_called()
        assert mock_popen.call_count == 2
        executor.cleanup()

    def test_reply_of_the_largest_size_is_taken(self, mock_hluk):
        _, fake_guest = mock_hluk
        with patch.object(HyperlightExecutor, "MAX_REPLY_BYTES", 1000):
            executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())

        # The first step's, as the guest replies to it
        reply = {"id": 1, **_hyperlight_result(output="")}
        reply["output"] = "o" * (1000 - len(json.dumps(reply)))
        assert len(json.dumps(reply)) == 1000
        fake_guest.results.append(_hyperlight_result(output=reply["output"]))

        assert len(executor.run_code_raise_errors("1").output) > 900
        executor.cleanup()

    def test_console_lines_that_are_not_replies_are_kept_apart(self, mock_hluk):
        _, fake_guest = mock_hluk
        with patch.object(HyperlightExecutor, "MAX_REPLY_BYTES", 1000):
            executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())

        def console_writes_then_reply(process):
            # Longer than any reply, and a line the guest's code left without its end, as through /dev/stdout
            process.print("n" * 5000 + "\npartial", stream="stdout")
            process.print("kernel says hello\n")
            return _hyperlight_result(output="'fine'")

        fake_guest.results.append(console_writes_then_reply)

        assert executor.run_code_raise_errors("1").output == "'fine'"
        output = executor._live["guest"].output()
        assert "partial" in output and output.endswith("kernel says hello")
        executor.cleanup()

    @pytest.mark.parametrize("error", ["boom", {"name": "ValueError", "value": 1, "traceback": ""}, {"name": "X"}])
    def test_reply_with_malformed_error_restarts_guest(self, mock_hluk, error):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        fake_guest.results.append(_hyperlight_result(error=error))

        with pytest.raises(AgentError, match="reply without the step's result, so the sandbox was restarted"):
            executor.run_code_raise_errors("1")
        assert mock_popen.call_count == 2
        executor.cleanup()

    def test_failure_to_watch_hluk_stops_it(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        processes = []
        mock_popen.side_effect = lambda *args, **kwargs: processes.append(FakeHlukProcess(fake_guest)) or processes[-1]
        with patch("threading.Thread.start", side_effect=RuntimeError("can't start new thread")):
            with pytest.raises(RuntimeError, match="can't start new thread"):
                HyperlightExecutor(additional_imports=[], logger=MagicMock())
        processes[0].kill.assert_called()

    @pytest.mark.parametrize(
        "status, cause",
        [
            (0xC000013A if sys.platform == "win32" else -signal.SIGINT, "a Ctrl-C"),
            (3, r"the sandbox stopped \(guest aborted\)"),
        ],
    )
    def test_guest_stopped_between_steps_is_restarted_before_the_code_runs(self, mock_hluk, status, cause):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        executor.send_tools({"final_answer": FinalAnswerTool()})
        # A Ctrl-C in the terminal reaches hluk too, in the executor's process group, or the guest died after replying
        _crash(status)(executor.process)

        # The code never reached the stopped guest, so it runs in the fresh one, and the model hears why its state went
        code_output = executor("1 + 1")
        assert re.match(f"Note: the sandbox was restarted before this code ran, after {cause}:", code_output.logs)
        assert mock_popen.call_count == 2
        assert "final_answer = " in fake_guest.codes[-2] and fake_guest.codes[-1] == "1 + 1"
        executor.cleanup()

    def test_tools_sent_after_a_ctrl_c_between_runs_reach_a_fresh_guest(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        executor.process.exit(0xC000013A if sys.platform == "win32" else -signal.SIGINT)

        # As a new agent run starts: no error, and the first step tells the model
        executor.send_tools({"final_answer": FinalAnswerTool()})
        assert mock_popen.call_count == 2
        assert executor("1").logs.startswith("Note: the sandbox was restarted before this code ran, after a Ctrl-C")
        executor.cleanup()

    def test_cause_of_a_stop_outlives_a_failed_restart(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        executor.send_tools({"final_answer": FinalAnswerTool()})
        _crash(-signal.SIGTERM)(executor.process)
        # The fresh guest's tool definitions fail
        fake_guest.results.append(TimeoutError())

        with pytest.raises(RuntimeError, match="could not be set up again"):
            executor("1")
        # The model heard of the failure, but not yet why its state went
        assert executor("1").logs.startswith(
            "Note: the sandbox was restarted before this code ran, after the sandbox stopped (guest aborted)"
        )
        assert executor("1").logs == ""
        executor.cleanup()

    def test_explicit_reset_after_a_ctrl_c_owes_no_notice(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock(), timeout_seconds=1)
        executor.send_tools({"final_answer": FinalAnswerTool()})
        fake_guest.results.append(KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            executor("while True: pass")

        # As between tasks: the caller asked for the fresh state
        executor.reset()
        assert executor("1").logs == ""
        # Nor later, after a timeout whose failed reset the model hears of with the error
        fake_guest.results += [TimeoutError(), TimeoutError()]
        with pytest.raises(RuntimeError, match="could not be set up again"):
            executor("while True: pass")
        assert executor("1").logs == ""
        executor.cleanup()

    def test_notice_of_a_restart_while_sending_variables_reaches_the_next_step(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        executor.process.exit(-signal.SIGINT if sys.platform != "win32" else 0xC000013A)

        executor.send_variables({"task_input": 7})
        assert "task_input" in fake_guest.codes[-1]
        assert executor("1").logs.startswith("Note: the sandbox was restarted before this code ran, after a Ctrl-C")
        assert executor("1").logs == ""
        executor.cleanup()

    def test_ctrl_c_during_a_step_is_told_apart_from_a_crash(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        # The Ctrl-C reaches hluk while the step runs, but not the executor's thread, as when the agent runs on a worker
        fake_guest.results.append(
            lambda process: process.exit(0xC000013A if sys.platform == "win32" else -signal.SIGINT)
        )

        with pytest.raises(
            AgentError, match="stopped by a Ctrl-C while this code was sent or ran, so the sandbox was"
        ):
            executor.run_code_raise_errors("1")
        assert mock_popen.call_count == 2
        executor.cleanup()

    @pytest.mark.parametrize("chunk_size", [1, 2, 13, 64, 65536, "random"])
    def test_reader_finds_replies_wherever_chunks_cut(self, chunk_size):
        rng = random.Random(chunk_size if isinstance(chunk_size, int) else 7)
        marker = _HYPERLIGHT_REPLY_MARKER
        noise_pieces = ["", "noise", "\n", "partial \x1esmol", "\x1e", "\x1esmolagents" + "\n", "é\n", "line\n" * 50]
        stream, expected_console, expected = "", "", []
        for index in range(40):
            noise = "".join(rng.choice(noise_pieces) for _ in range(rng.randrange(4)))
            reply = {"id": index, "output": "o" * rng.randrange(300), "logs": "l\nl", "error": None}
            stream += noise + "\n" + marker + json.dumps(reply) + "\n"
            # The end of line before each reply is the console's
            expected_console += noise + "\n"
            expected.append(reply)
        stream += "trailing"
        expected_console += "trailing"
        data = stream.encode()
        chunks = []
        while data:
            size = rng.randrange(1, 5000) if chunk_size == "random" else chunk_size
            chunks.append(data[:size])
            data = data[size:]

        guest = object.__new__(_HyperlightGuest)
        guest.process = MagicMock()
        guest.process.stdout.read1.side_effect = chunks + [b""] * 2
        guest._console, guest._errors, guest._output_lock = bytearray(), bytearray(), threading.Lock()
        guest._replies, guest._sent = queue.Queue(), len(expected) - 1
        guest._read_replies(10_000)

        replies = [guest._replies.get_nowait() for _ in range(len(expected) + 1)]
        assert replies == expected + [None]
        assert guest._console.decode() == expected_console

    @pytest.mark.parametrize(
        "logs",
        [
            "",
            "not json\n",
            '{"a": 1}\n',
            "[1]\n",
            # A package that was not asked for
            '["numpy"]\n',
            # Longer than every package asked for: millions of values the host would otherwise make
            "[" + ",".join(["{}"] * 1_000_000) + "]\n",
        ],
        # Short: pytest puts a test's id in an environment variable, which Windows limits to 32767 characters
        ids=lambda logs: logs[:24] + str(len(logs)),
    )
    def test_unexpected_package_list_is_an_error(self, mock_hluk, logs):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        fake_guest.results.append(_hyperlight_result(logs=logs))

        with pytest.raises(RuntimeError, match="Could not tell which packages the Hyperlight guest has"):
            executor.install_packages(["wikipediaapi"])
        executor.cleanup()

    def test_reply_without_result_restarts_guest(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        fake_guest.results.append({"logs": ""})

        with pytest.raises(AgentError, match="reply without the step's result, so the sandbox was restarted"):
            executor.run_code_raise_errors("1")
        assert mock_popen.call_count == 2
        executor.cleanup()

    def test_crash_restarts_guest_and_defines_tools_and_variables_again(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        executor.send_tools({"final_answer": FinalAnswerTool()})
        executor.send_variables({"task_input": 7})
        crashed_process = executor.process
        fake_guest.results.append(_crash(3))

        # The reason is the crashed guest's, not the fresh one's
        with pytest.raises(AgentError, match=r"sandbox crashed while executing code \(guest aborted\)"):
            executor.run_code_raise_errors("crash()")

        assert mock_popen.call_count == 2
        assert executor.process is not crashed_process
        crashed_process.kill.assert_called()
        assert "final_answer = " in fake_guest.codes[-2]
        assert "task_input" in fake_guest.codes[-1]
        executor.cleanup()

    def test_crash_without_output_reports_exit_status(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        fake_guest.results.append(lambda process: process.exit(3))

        with pytest.raises(AgentError, match=r"sandbox crashed while executing code \(exit status 3\)"):
            executor.run_code_raise_errors("crash()")
        executor.cleanup()

    def test_console_closed_by_a_running_hluk_restarts_guest(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())

        def close_console(process):
            process.close_stdout()

            # Still running, until killed
            def wait(timeout=None):
                if process.returncode is None:
                    raise subprocess.TimeoutExpired("hluk", timeout)
                return process.returncode

            process.wait.side_effect = wait

        fake_guest.results.append(close_console)

        with pytest.raises(AgentError, match=r"crashed while executing code \(it stopped answering\)"):
            executor.run_code_raise_errors("1")
        assert mock_popen.call_count == 2
        executor.cleanup()

    def test_timeout_restarts_guest(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock(), timeout_seconds=5)
        stuck_process = executor.process
        fake_guest.results.append(TimeoutError())

        with pytest.raises(AgentError, match="exceeded the maximum execution time of 5 seconds"):
            executor.run_code_raise_errors("while True: pass")

        stuck_process.kill.assert_called()
        assert mock_popen.call_count == 2
        executor.cleanup()

    def test_silent_guest_times_out(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock(), timeout_seconds=0.2)
        # Neither a reply nor an exit, as a guest busy with the step
        fake_guest.results.append(lambda process: None)

        start = time.monotonic()
        with pytest.raises(AgentError, match="exceeded the maximum execution time of 0.2 seconds"):
            executor.run_code_raise_errors("while True: pass")
        assert time.monotonic() - start < 3
        assert mock_popen.call_count == 2
        executor.cleanup()

    def test_guest_that_stops_reading_cannot_hold_the_executor(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock(), timeout_seconds=0.2)
        unblock = threading.Event()
        executor.process.stdin.write.side_effect = lambda line: unblock.wait(10)

        start = time.monotonic()
        with pytest.raises(AgentError, match="exceeded the maximum execution time"):
            executor.run_code_raise_errors("x" * 1_000_000)
        assert time.monotonic() - start < 3
        unblock.set()
        executor.cleanup()

    def test_interrupted_step_stops_guest_and_next_step_restarts_it(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        busy_process = executor.process
        fake_guest.results.append(KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            executor.run_code_raise_errors("while True: pass")
        busy_process.kill.assert_called()
        assert executor.process is None

        # The model learns that the state of earlier steps is gone
        code_output = executor("1")
        assert code_output.logs.startswith("Note: the sandbox was restarted before this code ran")
        assert mock_popen.call_count == 2
        assert executor("1").logs == ""
        executor.cleanup()

    def test_repeated_timeouts_each_restart_guest(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock(), timeout_seconds=1)
        fake_guest.results += [TimeoutError(), TimeoutError()]

        for _ in range(2):
            with pytest.raises(AgentError, match="maximum execution time of 1 seconds, so the sandbox was restarted"):
                executor("while True: pass")

        assert mock_popen.call_count == 3
        executor.cleanup()

    @pytest.mark.parametrize("reset_failure", [TimeoutError(), _crash(3)])
    def test_failure_during_reset_is_reported_without_resetting_again(self, mock_hluk, reset_failure):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock(), timeout_seconds=1)
        executor.send_tools({"final_answer": FinalAnswerTool()})
        # The step times out, then so does (or crashes) the fresh guest's tool definitions
        fake_guest.results += [TimeoutError(), reset_failure]

        with pytest.raises(RuntimeError, match="could not be set up again"):
            executor("while True: pass")
        assert mock_popen.call_count == 2
        assert executor.process is None

        # The next step sets a fresh guest up, tools included; the model was already told, with the error
        code_output = executor("1")
        assert mock_popen.call_count == 3
        assert "final_answer = " in fake_guest.codes[-2]
        assert code_output.logs == ""
        executor.cleanup()

    def test_interrupted_reset_leaves_guest_to_reset(self, mock_hluk):
        mock_popen, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock(), timeout_seconds=1)
        executor.send_variables({"task_input": 7})
        # Ctrl-C lands while the timed-out step's reset defines the variables again
        fake_guest.results += [TimeoutError(), KeyboardInterrupt()]

        with pytest.raises(KeyboardInterrupt):
            executor("while True: pass")
        assert executor.process is None

        executor("task_input")
        assert mock_popen.call_count == 3
        assert "task_input" in fake_guest.codes[-2]
        executor.cleanup()

    def test_interruption_is_announced_after_a_failed_reset(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        executor.send_tools({"final_answer": FinalAnswerTool()})
        # Ctrl-C ends a step; the next step's reset then fails to define the tools
        fake_guest.results += [KeyboardInterrupt(), TimeoutError()]
        with pytest.raises(KeyboardInterrupt):
            executor("while True: pass")
        with pytest.raises(RuntimeError, match="could not be set up again"):
            executor("1")

        # That error said nothing of the lost state, so the step after it still hears of it
        assert executor("1").logs.startswith("Note: the sandbox was restarted before this code ran")
        assert executor("1").logs == ""
        executor.cleanup()

    def test_unserializable_variables_are_not_kept_for_resets(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        executor.send_variables({"task_input": 7})
        with pytest.raises(SerializationError):
            executor.send_variables({"task_input": 7, "handle": object()})

        executor.reset()
        assert "task_input" in fake_guest.codes[-1] and "handle" not in fake_guest.codes[-1]
        executor.cleanup()

    def test_reset_defines_the_last_variables_sent(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        executor.send_variables({"first_run_input": 1})
        executor.send_variables({"second_run_input": 2})

        executor.reset()
        assert "second_run_input" in fake_guest.codes[-1] and "first_run_input" not in fake_guest.codes[-1]
        executor.cleanup()

    def test_cleanup_during_startup_returns_promptly(self, mock_hluk):
        _, fake_guest = mock_hluk
        # The guest never answers, as while hluk pulls the image on a first run
        fake_guest.ready = False
        starting = []
        build_command = HyperlightExecutor._build_command

        def record_executor(executor):
            starting.append(executor)
            return build_command(executor)

        failures = []

        def start():
            try:
                HyperlightExecutor(additional_imports=[], logger=MagicMock())
            except RuntimeError as e:
                failures.append(e)

        with patch.object(HyperlightExecutor, "_build_command", record_executor):
            thread = threading.Thread(target=start)
            thread.start()
            while not starting:
                time.sleep(0.01)
            time.sleep(0.1)
            start_time = time.monotonic()
            starting[0].cleanup()
            thread.join(timeout=5)
        assert time.monotonic() - start_time < 2
        assert not thread.is_alive()
        assert "shut down while starting" in str(failures[0])

    def test_cleanup_during_step(self, mock_hluk):
        _, fake_guest = mock_hluk
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        # From another thread, while the step waits for its reply
        cleaning = threading.Thread(target=executor.cleanup)
        fake_guest.results.append(lambda process: cleaning.start())

        with pytest.raises(AgentError, match="shut down while executing code"):
            executor.run_code_raise_errors("1")
        cleaning.join()

    def test_code_agent_executor(self, mock_hluk):
        executor = HyperlightExecutor()
        agent = CodeAgent(tools=[], model=MagicMock(), executor=executor)

        assert agent.python_executor is executor
        assert isinstance(executor.logger, AgentLogger)
        agent.cleanup()
        assert executor.process is None

    def test_entry_point_creates_executor(self, mock_hluk):
        mock_popen, _ = mock_hluk
        # The first, as smolagents takes, should an editable install and a wheel both register it
        entry_point = next(iter(importlib.metadata.entry_points(group="smolagents.executors", name="hyperlight")))
        logger = AgentLogger(LogLevel.OFF)

        # As smolagents calls an executor's entry point
        executor = entry_point.load()(["numpy"], logger, scratch_mb=256)

        assert isinstance(executor, HyperlightExecutor)
        assert executor.logger is logger
        assert executor.installed_packages == ["numpy"]
        assert "--scratch-mb" in mock_popen.call_args.args[0]
        executor.cleanup()

    def test_code_agent_executor_type(self, mock_hluk):
        mock_popen, _ = mock_hluk
        try:
            agent = CodeAgent(
                tools=[], model=MagicMock(), executor_type="hyperlight", executor_kwargs={"scratch_mb": 256}
            )
        except ValueError as e:
            # A smolagents that loads executors from entry points lists the types it found when it finds none
            if "Unsupported executor type" not in str(e) or "Available executor types" in str(e):
                raise
            pytest.skip("this smolagents does not load executors from entry points")

        assert isinstance(agent.python_executor, HyperlightExecutor)
        assert "--scratch-mb" in mock_popen.call_args.args[0]
        agent.cleanup()
        assert agent.python_executor.process is None

    def test_cleanup(self, mock_hluk):
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        process = executor.process
        workdir = executor._workdir

        executor.cleanup()

        process.kill.assert_called()
        process.wait.assert_called()
        assert executor.process is None
        assert not os.path.exists(workdir)
        with pytest.raises(RuntimeError, match="shut down"):
            executor.run_code_raise_errors("1")

    def test_dropped_executor_stops_hluk(self, mock_hluk):
        executor = HyperlightExecutor(additional_imports=[], logger=MagicMock())
        process = executor.process
        workdir = executor._workdir

        del executor
        gc.collect()

        process.kill.assert_called_once()
        assert not os.path.exists(workdir)


def _is_running(pid):
    if sys.platform == "win32":
        # os.kill() would end the process there, whatever the signal
        tasks = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in tasks.stdout.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def local_hluk(tmp_path):
    """A stand-in for `hluk` that runs the guest server with the host's Python, to test the protocol without a VM."""
    fake_hluk = tmp_path / "hluk"
    fake_hluk.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('hluk 0.18.0')\n"
        "else:\n"
        "    runpy.run_path(sys.argv[-1])\n"
    )
    fake_hluk.chmod(0o755)
    return str(fake_hluk)


class HyperlightGuestTests(CommonExecutorTests):
    """Tests that need the guest server, run with the stand-in for `hluk` and with the real one."""

    hluk_path = None
    # Arguments that choose the image the guest runs
    image = {}

    @pytest.fixture
    def custom_executor(self):
        executor = self.make_executor(additional_imports=["pillow", "numpy"])
        yield executor
        executor.delete()

    def make_executor(self, additional_imports=(), **kwargs):
        return HyperlightExecutor(
            additional_imports=list(additional_imports),
            logger=AgentLogger(LogLevel.INFO, Console(force_terminal=False, file=io.StringIO())),
            hluk_path=self.hluk_path,
            **(self.image | kwargs),
        )

    def test_final_answer_array(self):
        self.executor.send_tools({"final_answer": FinalAnswerTool()})
        output = self.executor("import numpy as np\nfinal_answer(np.arange(6).reshape(2, 3) / 2)").output
        assert isinstance(output, np.ndarray)
        assert output.tolist() == [[0, 0.5, 1], [1.5, 2, 2.5]]

    def test_last_expression_is_output(self):
        code_output = self.executor("a = 20\nprint('computing')\na + 22")
        assert code_output.output == "42"
        assert code_output.logs == "computing\n"

    def test_logs_have_both_streams_and_subprocess_output_in_order(self):
        code_output = self.executor(
            dedent(
                """\
                import subprocess, sys, warnings
                print("out")
                print("err", file=sys.stderr)
                subprocess.run([sys.executable, "-c", "print('child')"])
                warnings.warn("careful")
                print("done")
                """
            )
        )
        lines = code_output.logs.splitlines()
        assert lines[:3] == ["out", "err", "child"]
        assert "UserWarning: careful" in lines[3]
        assert lines[-1] == "done"
        # Nothing leaks into the next step
        assert self.executor("1").logs == ""

    def test_output_is_capped_without_exhausting_the_guest(self):
        code_output = self.executor(
            "import os, sys\nprint('start')\nfor _ in range(64):\n    os.write(1, b'y' * 1_000_000)\nprint('\\nend')"
        )
        assert code_output.logs.startswith("start\nyyy")
        assert code_output.logs.endswith("yyy\nend\n")
        assert re.search(r"\[\.\.\. \d+ bytes of output truncated \.\.\.\]", code_output.logs)
        assert len(code_output.logs) < HyperlightExecutor.MAX_LOG_BYTES + 100
        assert self.executor("print('alive')").logs == "alive\n"

        # From Python too, in one write larger than the pipe takes at once: none of it is lost on the way
        logs = self.executor("print('x' * 8_000_000)").logs
        assert "[... 5902849 bytes of output truncated ...]" in logs and logs.endswith("xxx\n")

    def test_output_not_ending_a_line_belongs_to_its_step(self):
        code_output = self.executor(
            "import sys\nsys.stdout.buffer.write(b'raw ')\nsys.__stdout__.write('dunder ')\nprint('partial', end='')"
        )
        assert all(word in code_output.logs for word in ("raw", "dunder", "partial"))
        assert self.executor("print('next')").logs == "next\n"

    def test_character_across_the_kept_halves_stays_whole(self):
        half = HyperlightExecutor.MAX_LOG_BYTES // 2
        # Nothing to drop: one two-byte character straddles the beginning and the end that are kept. (Written through
        # a buffered writer: a single write to a pipe may take only part of it.)
        logs = self.executor(
            f"import sys\nsys.stdout.buffer.write(b'a' * ({half} - 1) + 'é'.encode() + b'c' * 10)"
        ).logs
        assert logs == "a" * (half - 1) + "é" + "c" * 10

    def test_truncation_cuts_between_characters(self):
        half = HyperlightExecutor.MAX_LOG_BYTES // 2
        # Characters across both cuts: the end of the kept beginning, and the start of the kept end
        logs = self.executor(
            f"import sys\nsys.stdout.buffer.write(b'a' * ({half} - 1) + 'é'.encode() + b'b' * 1000 + 'é'.encode() + b'c' * ({half} - 1))"
        ).logs
        assert "\ufffd" not in logs
        assert logs.startswith("a" * (half - 1) + "\n[... 1004 bytes of output truncated ...]\n")
        assert logs.endswith("\n" + "c" * (half - 1))

    def test_steps_end_while_a_background_process_keeps_writing(self):
        self.executor(
            "import subprocess, sys\nwriter = subprocess.Popen([sys.executable, '-c', "
            '\'import os, time\\nwhile True:\\n    os.write(1, b"q" * 4095 + b"\\\\n")\\n    time.sleep(0.001)\'])'
        )
        try:
            for step in range(20):
                start = time.monotonic()
                assert f"step {step}\n" in self.executor(f"print('step {step}')").logs
                assert time.monotonic() - start < 3
        finally:
            self.executor("writer.kill(); writer.wait()")

    def test_logging_handler_keeps_working_in_later_steps(self):
        self.executor("import logging\nlogging.basicConfig(format='%(message)s')\nlogging.warning('first')")
        assert self.executor("logging.warning('second')").logs == "second\n"

    def test_unserializable_variable_does_not_break_resets(self):
        self.executor.send_variables({"task_input": 7})
        with pytest.raises(SerializationError):
            self.executor.send_variables({"task_input": 7, "handle": object()})
        self.executor.reset()
        assert self.executor("task_input").output == "7"

    def test_send_variables(self):
        self.executor.send_variables({"counter": 1, "tags": ("a", "b")})
        assert self.executor("(counter, tags)").output == "(1, ('a', 'b'))"

    def test_system_exit_is_an_error(self):
        with pytest.raises(AgentError, match="SystemExit"):
            self.executor("exit(3)")
        assert self.executor("'still running'").output == "'still running'"

    def test_reset_discards_state_and_keeps_tools_and_variables(self):
        self.executor.send_tools({"final_answer": FinalAnswerTool()})
        self.executor.send_variables({"task_input": 7})
        self.executor("a = 2\ntask_input = 8")
        self.executor.reset()
        with pytest.raises(AgentError, match="NameError"):
            self.executor("a")
        assert self.executor("task_input").output == "7"
        assert self.executor("final_answer('after reset')").output == "after reset"

    def test_crash_reports_exit_status_and_restarts(self):
        with pytest.raises(AgentError, match=r"sandbox crashed while executing code \(.*status 3\)"):
            self.executor("import os; os._exit(3)")
        assert self.executor("1 + 1").output == "2"

    def test_step_timeout_restarts_guest(self):
        executor = self.make_executor(timeout_seconds=2)
        try:
            executor.send_variables({"task_input": 7})
            start = time.monotonic()
            with pytest.raises(AgentError, match="exceeded the maximum execution time of 2 seconds"):
                executor("while True: pass")
            assert time.monotonic() - start < 10
            assert executor("task_input").output == "7"
        finally:
            executor.cleanup()

    def test_repeated_timeouts_each_restart_guest(self):
        executor = self.make_executor(timeout_seconds=1)
        try:
            for _ in range(2):
                with pytest.raises(AgentError, match="maximum execution time of 1 seconds"):
                    executor("while True: pass")
            assert executor("1 + 1").output == "2"
        finally:
            executor.cleanup()

    @pytest.mark.parametrize("failing_definitions", ["import time; time.sleep(5)", "import os; os._exit(3)"])
    def test_failure_during_reset_is_reported_without_resetting_again(self, monkeypatch, failing_definitions):
        executor = self.make_executor(timeout_seconds=1)
        try:
            executor.send_variables({"task_input": 7})
            # Tools whose definitions a reset can send, so that only the patched ones fail
            executor.send_tools({"final_answer": FinalAnswerTool()})
            definitions_sent = []

            def definitions(tools):
                definitions_sent.append(tools)
                return failing_definitions

            with monkeypatch.context() as patched:
                # The fresh guest's tool definitions time out, or crash it, as the step did
                patched.setattr("smolagents_hyperlight.executor.get_tools_definition_code", definitions)
                start = time.monotonic()
                with pytest.raises(RuntimeError, match="could not be set up again"):
                    executor("while True: pass")
                assert time.monotonic() - start < 10
                assert definitions_sent
            code_output = executor("task_input")
            assert code_output.output == "7"
            # The model was already told, with the error
            assert code_output.logs == ""
            # And the fresh guest has the tools again
            assert executor("final_answer(task_input)").output == 7
        finally:
            executor.cleanup()

    def test_interrupted_reset_leaves_guest_to_reset(self, monkeypatch):
        executor = self.make_executor(timeout_seconds=1)
        try:
            executor.send_variables({"task_input": 7})

            def interrupted(self, variables):
                raise KeyboardInterrupt

            with monkeypatch.context() as patched:
                # Ctrl-C lands while the timed-out step's reset defines the variables again
                patched.setattr(RemotePythonExecutor, "send_variables", interrupted)
                with pytest.raises(KeyboardInterrupt):
                    executor("while True: pass")
            code_output = executor("task_input")
            assert code_output.output == "7"
            assert code_output.logs.startswith("Note: the sandbox was restarted before this code ran")
        finally:
            executor.cleanup()

    def test_standard_input_is_empty_for_code_and_subprocesses(self):
        code_output = self.executor(
            dedent(
                """\
                import subprocess, sys
                child = subprocess.run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], capture_output=True, text=True)
                (sys.stdin.read(), child.stdout)
                """
            )
        )
        assert code_output.output == "('', \"''\\n\")"
        assert self.executor("1 + 1").output == "2"

    def test_code_crosses_the_console_unchanged(self):
        # Characters a terminal acts on, where they would end what it reads, and characters beyond ASCII
        assert self.executor("x = '\u00e9t\u00e9 \U0001f680 \x04'\r\nx\r").output == "'\u00e9t\u00e9 \U0001f680 \\x04'"
        assert self.executor("x = '\x04'").output is None
        assert self.executor("len(x)").output == "1"

    def test_large_values_cross_the_console(self):
        self.executor.send_variables({"big": "b" * 5_000_000})
        assert self.executor("len(big)").output == "5000000"
        assert len(self.executor("'c' * 5_000_000").output) == 5_000_002
        assert self.executor("1 + 1").output == "2"

    def test_too_large_result_is_an_error_and_keeps_state(self):
        with patch.object(HyperlightExecutor, "MAX_REPLY_BYTES", 1_000_000):
            executor = self.make_executor()
        try:
            executor("kept = 7")
            with pytest.raises(AgentError, match="more than the 1000000 the sandbox returns"):
                executor("print('before')\n'd' * 2_000_000")
            assert executor("kept").output == "7"
        finally:
            executor.cleanup()

    def test_console_line_left_unended_does_not_hide_the_reply(self):
        # As code writing to /dev/stdout, the guest's console, would leave it
        code_output = self.executor(
            "import os, sys\n# The server's globals, whose function runs the step\nserver = sys._getframe(1).f_globals\nos.write(server['replies_fd'], b'unended')\n'reply'"
        )
        assert code_output.output == "'reply'"
        assert self.executor("1 + 1").output == "2"

    def test_guest_loop_failure_is_reported(self):
        self.executor("import os, sys\nos.close(sys._getframe(1).f_globals['requests_fd'])")
        with pytest.raises(
            AgentError, match=r"crashed while executing code \(OSError: \[Errno 9\] Bad file descriptor\)"
        ):
            self.executor("1")
        assert self.executor("1 + 1").output == "2"

    def test_exception_that_cannot_say_what_it_is_keeps_state(self):
        self.executor("kept = 7")
        with pytest.raises(AgentError, match=r"BadError\n<BadError object: str\(\) failed>"):
            self.executor(
                "class BadError(Exception):\n    def __str__(self):\n        raise RuntimeError\nraise BadError()"
            )
        assert self.executor("kept").output == "7"

    def test_console_flood_of_short_lines_stays_cheap(self):
        start = time.process_time()
        # Through the console itself, as code writing to /dev/stdout would: millions of lines, none of them replies
        self.executor("import os, sys\nos.write(sys._getframe(1).f_globals['replies_fd'], b'\\n' * 5_000_000)")
        assert time.process_time() - start < 3
        assert self.executor("1 + 1").output == "2"

    def test_exit_without_cleanup_stops_hluk(self):
        script = dedent(
            f"""\
            from smolagents.monitoring import AgentLogger, LogLevel
            from smolagents_hyperlight import HyperlightExecutor
            executor = HyperlightExecutor([], AgentLogger(LogLevel.OFF), hluk_path={self.hluk_path!r}, **{self.image!r})
            print(executor.process.pid, executor._workdir)
            """
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        pid, workdir = result.stdout.strip().split(maxsplit=1)
        assert not os.path.exists(workdir)
        assert not _is_running(int(pid))


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in for hluk is a script with a shebang")
class TestHyperlightExecutorLocalGuest(HyperlightGuestTests):
    @pytest.fixture(autouse=True)
    def set_hluk_path(self, local_hluk):
        self.hluk_path = local_hluk

    @pytest.fixture
    def custom_executor(self, set_hluk_path):
        executor = self.make_executor(additional_imports=["pillow", "numpy"])
        yield executor
        executor.delete()


# The `hluk` to test and the image it runs: by default the one on PATH, with its release's `agent` image, and in CI the
# repository's own build of both
HLUK_PATH = os.getenv("HLUK_PATH") or shutil.which("hluk")
HLUK_IMAGE = {}
if os.getenv("HLUK_INITRD"):
    HLUK_IMAGE["initrd"] = os.environ["HLUK_INITRD"]
if os.getenv("HLUK_SCRATCH_MB"):
    HLUK_IMAGE["scratch_mb"] = os.environ["HLUK_SCRATCH_MB"]


@require_run_all
@pytest.mark.skipif(HLUK_PATH is None, reason="requires hluk")
class TestHyperlightExecutorIntegration(HyperlightGuestTests):
    hluk_path = HLUK_PATH
    image = HLUK_IMAGE

    def test_no_egress_by_default(self):
        with pytest.raises(AgentError, match="PermissionError"):
            self.executor("import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)")

    def test_no_listening_by_default(self):
        with pytest.raises(AgentError, match="PermissionError"):
            self.executor("import socket\nsocket.socket().bind(('0.0.0.0', 8080))")

    def test_subprocesses(self):
        code_output = self.executor(
            dedent(
                """\
                import subprocess, sys, threading

                def run(results):
                    results.append(subprocess.run([sys.executable, "-c", "print(6 * 7)"], capture_output=True, text=True).stdout)

                results = []
                run(results)
                thread = threading.Thread(target=run, args=(results,))
                thread.start()
                thread.join()
                results
                """
            )
        )
        assert code_output.output == "['42\\n', '42\\n']"

    def test_subprocess_timeout_kills_child(self):
        code_output = self.executor(
            dedent(
                """\
                import subprocess, sys, time
                start = time.monotonic()
                try:
                    subprocess.run([sys.executable, "-c", "import time; time.sleep(60)"], timeout=1, start_new_session=True)
                except subprocess.TimeoutExpired:
                    pass
                round(time.monotonic() - start)
                """
            )
        )
        assert code_output.output == "1"
