import asyncio
import functools
import io
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import anyio
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError
from rich.console import Console
from smolagents.local_python_executor import CodeOutput
from smolagents.monitoring import AgentLogger, LogLevel
from smolagents.utils import AgentError
from smolagents_hyperlight import HyperlightExecutor

import mcp_server_hyperlight.server as server_module
from mcp_server_hyperlight import Sandbox, build_server
from mcp_server_hyperlight.server import MAX_RESULT_CHARS, _Call, _hluk_path, _parse_args


require_run_all = pytest.mark.skipif(not os.getenv("RUN_ALL"), reason="requires RUN_ALL environment variable")


class FakeProcess:
    """A guest's hluk that runs."""

    def poll(self):
        return None


class FakeExecutor:
    """Stands in for HyperlightExecutor: runs nothing, answers what each test has it answer."""

    instances: list["FakeExecutor"] = []

    def __init__(self, logger=None, **kwargs):
        self.logger = logger
        self.kwargs = kwargs
        self.codes: list[str] = []
        self.resets = 0
        self.installed_packages: list[str] = []
        self.cleaned_up = False
        self.answer = CodeOutput(output=None, logs="", is_final_answer=False)
        self.running = threading.Event()
        self.process = FakeProcess()
        FakeExecutor.instances.append(self)

    def __call__(self, code):
        self.codes.append(code)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer(code) if callable(self.answer) else self.answer

    def reset(self):
        self.resets += 1

    def cleanup(self):
        self.cleaned_up = True


@pytest.fixture(autouse=True)
def clear_instances():
    FakeExecutor.instances.clear()


def call(server, tool, arguments=None):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments or {})

    return asyncio.run(go())


def text(result):
    return "\n".join(item.text for item in result.content)


def list_tools(server):
    async def go():
        async with Client(server) as client:
            return {tool.name: tool for tool in (await client.list_tools()).tools}

    return asyncio.run(go())


class TestTools:
    def make(self, net_allow=None, timeout=None, image=True, packages=None, hluk_args=False):
        """A sandbox made with these settings, of a fake executor, and its server."""
        settings = {"runtime": "agent" if image else "python"}
        for key, value in (
            ("net_allow", net_allow),
            ("timeout_seconds", timeout),
            ("additional_imports", packages),
            ("hluk_args", ["--mount", "/d:/d"] if hluk_args else None),
        ):
            if value is not None:
                settings[key] = value
        sandbox = Sandbox(FakeExecutor, **settings)
        return sandbox, build_server(sandbox, warm=False)

    def test_lists_tools(self):
        _, server = self.make()
        tools = list_tools(server)
        assert set(tools) == {"run_python", "reset_sandbox"}
        assert tools["run_python"].input_schema["required"] == ["code"]
        assert tools["run_python"].annotations.destructive_hint is False
        assert tools["reset_sandbox"].annotations.destructive_hint is True

    def test_description_without_network(self):
        _, server = self.make()
        description = list_tools(server)["run_python"].description
        assert "Network: none" in description
        assert "numpy" in description
        assert "none of this machine's files" in description
        assert "30 seconds" in description
        assert list_tools(server)["run_python"].annotations.open_world_hint is False
        assert list_tools(server)["reset_sandbox"].annotations.open_world_hint is False

    def test_annotations_with_network_only(self):
        # An allowed host can still be sent what deletes or changes something there
        _, server = self.make(net_allow=["example.com"])
        tool = list_tools(server)["run_python"]
        assert tool.annotations.destructive_hint is True
        assert tool.annotations.open_world_hint is True
        assert list_tools(server)["reset_sandbox"].annotations.open_world_hint is False

    def test_reset_reaching_the_package_index(self):
        _, server = self.make(packages=["sympy"], net_allow=["pypi.org"])
        assert list_tools(server)["reset_sandbox"].annotations.open_world_hint is True
        # Without the network, nothing is installed
        _, server = self.make(packages=["sympy"])
        assert list_tools(server)["reset_sandbox"].annotations.open_world_hint is False

    def test_description_with_network_and_hluk_args(self):
        _, server = self.make(net_allow=["pypi.org"], timeout=5, image=False, hluk_args=True)
        tool = list_tools(server)["run_python"]
        assert "connections to pypi.org, and what else the server's settings allow" in tool.description
        assert tool.annotations.destructive_hint is True
        assert "numpy" not in tool.description
        assert "none of this machine's files" not in tool.description
        assert "longer than 5 seconds" in tool.description
        assert tool.annotations.open_world_hint is True

    def test_description_with_hluk_args_only(self):
        # Which may open the network, or mount host directories: the sandbox is not described as closed
        _, server = self.make(hluk_args=True)
        tool = list_tools(server)["run_python"]
        assert "Network" not in tool.description
        assert "Files" not in tool.description
        assert tool.annotations.open_world_hint is True

    def test_description_with_packages(self):
        _, server = self.make(packages=["sympy", "networkx"], net_allow=["pypi.org"])
        assert "installs networkx, sympy at start" in list_tools(server)["run_python"].description

    def test_description_follows_the_sandbox(self):
        # As a library caller makes one: the tools say what the guest can do, from how it was made
        sandbox = Sandbox(FakeExecutor, net_allow=["example.com"], hluk_args=["--mount", "/home:/home"])
        tool = list_tools(build_server(sandbox, warm=False))["run_python"]
        assert "connections to example.com, and what else" in tool.description
        assert "none of this machine's files" not in tool.description
        assert tool.annotations.destructive_hint is True
        assert tool.annotations.open_world_hint is True

    def test_description_with_packages_without_network(self):
        _, server = self.make(packages=["sympy"])
        assert "sympy only where its image does" in list_tools(server)["run_python"].description
        assert "installed again" not in list_tools(server)["reset_sandbox"].description
        _, server = self.make(packages=["sympy"], hluk_args=True)
        assert "sympy only where its image does" in list_tools(server)["run_python"].description

    def test_run_returns_logs_and_output(self):
        sandbox, server = self.make()
        result = call(server, "run_python", {"code": "x"})
        executor = FakeExecutor.instances[0]
        assert executor.codes == ["x"]
        assert executor.kwargs == {"runtime": "agent"}
        assert text(result) == "(no output)"
        executor.answer = CodeOutput(output="42", logs="hello\n", is_final_answer=False)
        result = call(server, "run_python", {"code": "print('hello'); 42"})
        assert not result.is_error
        assert text(result) == "hello\nOut: 42"
        # One guest for all the calls
        assert len(FakeExecutor.instances) == 1

    def test_run_output_without_logs(self):
        sandbox, server = self.make()
        sandbox._get().answer = CodeOutput(output="'a'", logs="  \n", is_final_answer=False)
        assert text(call(server, "run_python", {"code": "'a'"})) == "Out: 'a'"

    def test_logs_go_to_stderr(self):
        sandbox, _ = self.make()
        logger = sandbox._get().logger
        assert isinstance(logger, AgentLogger)
        assert logger.console.stderr

    @pytest.mark.parametrize(
        "error, expected",
        [
            (lambda logger: AgentError("printed\nExecuting code yielded an error:\nNameError", logger), "NameError"),
            (lambda logger: RuntimeError("could not be set up again"), "could not be set up again"),
        ],
    )
    def test_run_errors_reach_the_model(self, error, expected):
        sandbox, server = self.make()
        executor = sandbox._get()
        executor.answer = error(AgentLogger(LogLevel.OFF))
        result = call(server, "run_python", {"code": "y"})
        assert result.is_error
        assert expected in text(result)

    def test_startup_failure_reaches_the_model_and_is_retried(self):
        attempts = []

        def factory(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise RuntimeError("Could not find the `hluk` binary")
            return FakeExecutor(**kwargs)

        sandbox = Sandbox(factory)
        server = build_server(sandbox, warm=False)
        result = call(server, "run_python", {"code": "1"})
        assert result.is_error
        assert "Could not find the `hluk` binary" in text(result)
        result = call(server, "run_python", {"code": "1"})
        assert not result.is_error
        assert len(attempts) == 2

    def test_executor_wording_for_mcp(self):
        sandbox, server = self.make()
        executor = sandbox._get()
        executor.answer = AgentError(
            f"Code execution exceeded the maximum execution time of 30 seconds, so the sandbox was restarted: "
            f"{HyperlightExecutor._STATE_LOST}",
            AgentLogger(LogLevel.OFF),
        )
        message = text(call(server, "run_python", {"code": "1"}))
        assert message.endswith("so the sandbox was restarted: variables and imports from earlier calls are lost.")
        executor.answer = CodeOutput(
            output="1",
            logs="Note: the sandbox was restarted before this code ran, after a Ctrl-C: variables and imports defined "
            "by earlier code are gone, while tools and the agent's variables are defined again.\n",
            is_final_answer=False,
        )
        assert text(call(server, "run_python", {"code": "1"})) == (
            "Note: the sandbox was restarted before this code ran, after a Ctrl-C: variables and imports defined by "
            "earlier code are gone.\nOut: 1"
        )

    def test_wording_of_a_notice_then_a_recovery(self):
        # As the executor words a call that times out after it restarted a guest found stopped
        sandbox, server = self.make()
        sandbox._get().answer = AgentError(
            "Note: the sandbox was restarted before this code ran, after the sandbox stopped (killed): variables and "
            "imports defined by earlier code are gone, while tools and the agent's variables are defined again.\n"
            "Code execution exceeded the maximum execution time of 30 seconds, so the sandbox was restarted: "
            f"{HyperlightExecutor._STATE_LOST}",
            AgentLogger(LogLevel.OFF),
        )
        message = text(call(server, "run_python", {"code": "1"}))
        assert "agent" not in message
        assert message.endswith("variables and imports from earlier calls are lost.")

    def test_wording_leaves_the_code_output(self):
        sandbox, server = self.make()
        printed = "the agent said: , while tools and the agent's variables are defined again."
        logs = f"{printed}\nsecond\n{printed}\nthe agent's variables, mid-line, are defined again.\n"
        sandbox._get().answer = CodeOutput(output=None, logs=logs, is_final_answer=False)
        assert text(call(server, "run_python", {"code": "1"})) == logs.rstrip("\n")

    def test_wording_leaves_printed_executor_words(self):
        # Words the executor uses, printed by the code: only the executor's own whole sentences are reworded
        sandbox, server = self.make()
        executor = sandbox._get()
        printed = "the sandbox was restarted, while tools and the agent's variables are defined again."
        recovery = f"Something, so the sandbox was restarted: {HyperlightExecutor._STATE_LOST}"
        executor.answer = CodeOutput(output=None, logs=f"{printed}\n{recovery}\n", is_final_answer=False)
        assert text(call(server, "run_python", {"code": "1"})) == f"{printed}\n{recovery}"
        executor.answer = AgentError(
            f"{printed}\nExecuting code yielded an error:\nValueError\nboom", AgentLogger(LogLevel.OFF)
        )
        assert f": {printed}\nExecuting code" in text(call(server, "run_python", {"code": "1"}))

    def test_long_value_is_clipped(self):
        sandbox, server = self.make()
        limit = MAX_RESULT_CHARS
        sandbox._get().answer = CodeOutput(output="a" * (limit + 10) + "end", logs="", is_final_answer=False)
        result = text(call(server, "run_python", {"code": "1"}))
        assert len(result) < limit + 100
        assert "characters truncated" in result
        assert result.endswith("end")

    def test_final_answer_as_repr(self):
        sandbox, server = self.make()
        sandbox._get().answer = CodeOutput(output="a", logs="", is_final_answer=True)
        assert text(call(server, "run_python", {"code": "1"})) == "Out: 'a'"
        sandbox._get().answer = CodeOutput(output=None, logs="", is_final_answer=True)
        assert text(call(server, "run_python", {"code": "1"})) == "Out: None"

    def test_queued_calls_hold_no_worker_thread(self):
        sandbox, server = self.make()
        executor = sandbox._get()
        release = threading.Event()

        def answer(code):
            release.wait(10)
            return CodeOutput(output=code, logs="", is_final_answer=False)

        executor.answer = answer

        async def go():
            async with Client(server) as client:
                calls = [asyncio.ensure_future(client.call_tool("run_python", {"code": str(i)})) for i in range(50)]
                await asyncio.sleep(0.5)
                # More calls than the default limiter has threads, and the default limiter is free for the transport
                assert anyio.to_thread.current_default_thread_limiter().borrowed_tokens == 0
                release.set()
                return await asyncio.gather(*calls)

        results = asyncio.run(go())
        assert sorted(text(result) for result in results) == sorted(f"Out: {i}" for i in range(50))

    def test_startup_error_of_any_kind_reaches_the_model(self):
        def factory(**kwargs):
            # As subprocess.run raises for a hluk_path that names no program
            raise FileNotFoundError(2, "No such file or directory", "/nonexistent/hluk")

        server = build_server(Sandbox(factory), warm=False)
        result = call(server, "run_python", {"code": "1"})
        assert result.is_error
        assert "/nonexistent/hluk" in text(result)

    def test_reset(self):
        sandbox, server = self.make()
        # Before any guest, the one a reset starts is fresh
        assert "reset" in text(call(server, "reset_sandbox"))
        executor = FakeExecutor.instances[0]
        assert executor.resets == 0
        call(server, "run_python", {"code": "x = 1"})
        call(server, "reset_sandbox")
        assert executor.resets == 1

    def test_reset_of_an_unused_guest_starts_none(self):
        sandbox, server = self.make()
        executor = sandbox._get()
        call(server, "reset_sandbox")
        assert executor.resets == 0
        call(server, "run_python", {"code": "x = 1"})
        call(server, "reset_sandbox")
        call(server, "reset_sandbox")
        assert executor.resets == 1

    def test_reset_of_an_unused_guest_that_stopped(self):
        sandbox, server = self.make()
        executor = sandbox._get()

        class Exited:
            def poll(self):
                return -9

        executor.process = Exited()
        call(server, "reset_sandbox")
        assert executor.resets == 1

    def test_reset_without_network_leaves_packages_missing(self):
        sandbox = Sandbox(FakeExecutor, additional_imports=["sympy"])
        server = build_server(sandbox, warm=False)
        executor = sandbox._get()
        call(server, "reset_sandbox")
        # Nothing to try again without the network to install it: the unused guest stays
        assert executor.resets == 0

    def test_error_without_output_starts_with_it(self):
        sandbox, server = self.make()
        sandbox._get().answer = AgentError(
            "\nExecuting code yielded an error:\nZeroDivisionError", AgentLogger(LogLevel.OFF)
        )
        assert text(call(server, "run_python", {"code": "1/0"})).endswith(
            ": Executing code yielded an error:\nZeroDivisionError"
        )

    def test_reset_retries_packages_that_failed(self):
        sandbox = Sandbox(FakeExecutor, additional_imports=["sympy", "numpy.*"], net_allow=["pypi.org"])
        server = build_server(sandbox, warm=False)
        executor = sandbox._get()
        # numpy came with the image, sympy failed to install
        executor.installed_packages = ["numpy"]
        call(server, "reset_sandbox")
        assert executor.resets == 1
        # As the executor reinstalls them on a reset: all of those asked for
        assert executor.installed_packages == ["sympy", "numpy.*"]

    def test_reset_failure(self):
        sandbox, server = self.make()
        executor = sandbox._get()

        def fail():
            raise RuntimeError("The Hyperlight sandbox could not be set up again: boom")

        executor.reset = fail
        call(server, "run_python", {"code": "x = 1"})
        result = call(server, "reset_sandbox")
        assert result.is_error
        assert "boom" in text(result)

    def test_calls_take_turns(self):
        sandbox, server = self.make()
        running, overlapped = threading.Event(), []

        def answer(code):
            overlapped.append(running.is_set())
            running.set()
            time.sleep(0.2)
            running.clear()
            return CodeOutput(output=code, logs="", is_final_answer=False)

        sandbox._get().answer = answer

        async def go():
            async with Client(server) as client:
                return await asyncio.gather(*(client.call_tool("run_python", {"code": str(i)}) for i in range(3)))

        results = asyncio.run(go())
        assert sorted(text(result) for result in results) == ["Out: 0", "Out: 1", "Out: 2"]
        assert overlapped == [False, False, False]

    def test_warm_starts_the_guest(self):
        sandbox = Sandbox(FakeExecutor)
        server = build_server(sandbox)
        assert not call(server, "run_python", {"code": "1"}).is_error
        assert len(FakeExecutor.instances) == 1
        sandbox.close()
        assert FakeExecutor.instances[0].cleaned_up

    def test_warm_failure_is_logged_and_left_for_the_first_call(self):
        def factory(**kwargs):
            raise RuntimeError("no hypervisor")

        sandbox = Sandbox(factory)
        log = io.StringIO()
        sandbox._logger = AgentLogger(LogLevel.INFO, console=Console(file=log, width=200))
        sandbox.warm()
        assert "did not start, the first call will try again: no hypervisor" in log.getvalue()
        server = build_server(sandbox, warm=False)
        assert "no hypervisor" in text(call(server, "run_python", {"code": "1"}))

    def test_cancelled_waiting_call_does_not_run(self):
        sandbox, _ = self.make()
        executor = sandbox._get()
        release = threading.Event()

        def answer(code):
            executor.running.set()
            release.wait(10)
            return CodeOutput(output="1", logs="", is_final_answer=False)

        executor.answer = answer
        first = threading.Thread(target=sandbox.run, args=("first",))
        first.start()
        executor.running.wait(5)

        async def go():
            with anyio.move_on_after(0.3):
                await sandbox.call(functools.partial(sandbox.run, "second"))

        asyncio.run(go())
        # A thread left waiting for its turn, as anyio leaves a cancelled call's, gives up once cancelled
        waiting_call, outcome = _Call(), []

        def wait():
            try:
                sandbox.run("third", waiting_call)
            except ToolError as e:
                outcome.append(str(e))

        waiting = threading.Thread(target=wait)
        waiting.start()
        time.sleep(0.3)
        waiting_call.cancelled = True
        waiting.join(2)
        assert not waiting.is_alive()
        assert outcome == ["The call was cancelled"]
        release.set()
        first.join(5)
        # The cancelled call, which takes its turn once the first is done, runs nothing
        with sandbox._lock:
            pass
        time.sleep(0.2)
        assert executor.codes == ["first"]

    def test_cancelled_call_starts_no_guest(self):
        sandbox, _ = self.make()
        call = _Call()
        call.cancelled = True
        with pytest.raises(ToolError, match="cancelled"):
            sandbox.run("1", call)
        with pytest.raises(ToolError, match="cancelled"):
            sandbox.reset(call)
        assert not FakeExecutor.instances

    def test_cancelled_reset_keeps_state(self):
        sandbox, _ = self.make()
        executor = sandbox._get()
        call = _Call()
        call.cancelled = True
        with pytest.raises(ToolError):
            sandbox.reset(call)
        assert executor.resets == 0

    def test_reset_waits_for_running_code(self):
        sandbox, server = self.make()
        executor = sandbox._get()
        release = threading.Event()

        def answer(code):
            executor.running.set()
            release.wait(10)
            return CodeOutput(output="1", logs="", is_final_answer=False)

        executor.answer = answer

        async def go():
            async with Client(server) as client:
                running = asyncio.ensure_future(client.call_tool("run_python", {"code": "slow()"}))
                await asyncio.to_thread(executor.running.wait, 10)
                reset = asyncio.ensure_future(client.call_tool("reset_sandbox", {}))
                await asyncio.sleep(0.3)
                # Not before the code is done
                assert not reset.done()
                release.set()
                return await running, await reset

        running, reset = asyncio.run(go())
        assert text(running) == "Out: 1"
        assert not reset.is_error
        assert executor.resets == 1

    def test_cancelled_running_call_answers_at_once(self):
        sandbox, _ = self.make()
        executor = sandbox._get()
        release = threading.Event()

        def answer(code):
            release.wait(10)
            return CodeOutput(output="1", logs="", is_final_answer=False)

        executor.answer = answer

        async def go():
            with anyio.move_on_after(0.3):
                await sandbox.call(functools.partial(sandbox.run, "slow()"))

        start = time.monotonic()
        asyncio.run(go())
        assert time.monotonic() - start < 5
        # The code goes on until it ends: the next call takes its turn after it
        release.set()
        assert sandbox.run("1") == "Out: 1"
        assert executor.codes == ["slow()", "1"]

    def test_closed(self):
        sandbox, server = self.make()
        sandbox.close()
        result = call(server, "run_python", {"code": "1"})
        assert result.is_error
        assert "shut down" in text(result)
        assert not FakeExecutor.instances

    def test_reset_after_close(self):
        sandbox, server = self.make()
        executor = sandbox._get()
        sandbox.close()
        result = call(server, "reset_sandbox")
        assert result.is_error
        assert text(result).endswith("The Hyperlight sandbox has been shut down")
        assert executor.resets == 0

    def test_close_while_starting(self):
        started, release = threading.Event(), threading.Event()

        def factory(**kwargs):
            started.set()
            release.wait(5)
            return FakeExecutor(**kwargs)

        sandbox = Sandbox(factory)
        warm = threading.Thread(target=sandbox.warm)
        warm.start()
        started.wait(5)
        sandbox.close()
        release.set()
        warm.join(5)
        assert FakeExecutor.instances[0].cleaned_up
        assert sandbox._executor is None


class TestHlukLookup:
    @staticmethod
    def hluk_in(directory):
        # An executable hluk, as the server looks for one: hluk.exe on Windows
        directory.mkdir(parents=True, exist_ok=True)
        hluk = directory / ("hluk.exe" if sys.platform == "win32" else "hluk")
        hluk.touch()
        hluk.chmod(0o755)
        return hluk

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        # A machine with no hluk anywhere the server looks, as a client started from a desktop's launcher runs it:
        # without the install directory on PATH
        monkeypatch.setenv("PATH", "")
        for name in ("HOME", "USERPROFILE", "LOCALAPPDATA"):
            monkeypatch.setenv(name, str(tmp_path))
        for name in ("HLUK_INSTALL_DIR", "CARGO_HOME"):
            monkeypatch.delenv(name, raising=False)
        assert _hluk_path() is None
        return tmp_path

    def test_hluk_from_the_install_directory(self, home):
        installed = home / "hluk" / "bin" if sys.platform == "win32" else home / ".local" / "bin"
        hluk = self.hluk_in(installed)
        assert os.path.samefile(_hluk_path(), hluk)

    def test_hluk_from_cargo(self, home, monkeypatch):
        # As `cargo install hyperlight-unikraft` puts it, under CARGO_HOME or ~/.cargo
        hluk = self.hluk_in(home / ".cargo" / "bin")
        assert os.path.samefile(_hluk_path(), hluk)
        monkeypatch.setenv("CARGO_HOME", str(home / "cargo"))
        assert _hluk_path() is None
        hluk = self.hluk_in(home / "cargo" / "bin")
        assert os.path.samefile(_hluk_path(), hluk)

    def test_hluk_from_the_chosen_install_directory(self, home, monkeypatch):
        hluk = self.hluk_in(home / "bin")
        monkeypatch.setenv("HLUK_INSTALL_DIR", str(home / "bin"))
        assert os.path.samefile(_hluk_path(), hluk)
        # Unexpanded, as a client's config gives it
        monkeypatch.setenv("HLUK_INSTALL_DIR", os.path.join("~", "bin"))
        assert os.path.samefile(_hluk_path(), hluk)
        monkeypatch.setenv("HLUK_HOME_FOR_TEST", str(home))
        monkeypatch.setenv("HLUK_INSTALL_DIR", os.path.join("$HLUK_HOME_FOR_TEST", "bin"))
        assert os.path.samefile(_hluk_path(), hluk)
        if sys.platform != "win32":
            # One the executor could not run, as an install cut short leaves it: the executor says to install hluk
            hluk.chmod(0o644)
            assert _hluk_path() is None

    def test_hluk_from_path_only_where_absolute(self, home, monkeypatch):
        # A relative PATH entry names the current directory, which the client chose; Windows before Python 3.12 looks
        # there first, whatever PATH says
        monkeypatch.chdir(home)
        self.hluk_in(home)
        monkeypatch.setenv("PATH", os.pathsep.join([".", ""]))
        assert _hluk_path() is None
        hluk = self.hluk_in(home / "installed")
        monkeypatch.setenv("PATH", os.pathsep.join([".", str(home / "installed")]))
        assert os.path.samefile(_hluk_path(), hluk)

    def test_hluk_never_from_the_current_directory(self, home, monkeypatch):
        # A relative HLUK_INSTALL_DIR, or on Windows one without a drive or no LOCALAPPDATA: no path that the
        # current directory or drive decides
        monkeypatch.chdir(home)
        self.hluk_in(home / "hluk" / "bin")
        monkeypatch.setenv("HLUK_INSTALL_DIR", os.path.join("hluk", "bin"))
        if sys.platform == "win32":
            monkeypatch.delenv("LOCALAPPDATA")
            assert _hluk_path() is None
            monkeypatch.setenv("HLUK_INSTALL_DIR", "\\hluk\\bin")
        assert _hluk_path() is None

    def test_hluk_looked_up_for_each_guest(self, home, monkeypatch):
        # One installed after a guest failed to start for want of it is found by the next
        monkeypatch.setenv("HLUK_INSTALL_DIR", str(home))
        seen = []
        monkeypatch.setattr(server_module, "HyperlightExecutor", lambda **kwargs: seen.append(kwargs["hluk_path"]))
        # What a client's user can do about it, in the server's options
        with pytest.raises(RuntimeError, match="--hluk-path"):
            server_module._make_executor(runtime="agent")
        hluk = self.hluk_in(home)
        server_module._make_executor(runtime="agent")
        server_module._make_executor(hluk_path="/chosen/hluk", runtime="agent")
        server_module._make_executor(hluk_path=os.path.join("~", "hluk"), runtime="agent")
        assert os.path.samefile(seen[0], hluk)
        assert seen[1] == "/chosen/hluk"
        # As a client's config passes --hluk-path, unexpanded; but a file named as given is taken as it is
        assert seen[2] == os.path.join(str(home), "hluk")
        literal = home / "$NOT_A_VARIABLE"
        literal.touch()
        monkeypatch.setenv("NOT_A_VARIABLE", "elsewhere")
        server_module._make_executor(hluk_path=str(literal), runtime="agent")
        assert seen[3] == str(literal)


class TestArguments:
    def test_defaults(self):
        args = _parse_args([])
        assert args.runtime is None
        assert args.initrd is None
        assert args.net_allow == []
        assert args.timeout == 30

    def test_repeated(self):
        args = _parse_args(
            ["--net-allow", "a", "--net-allow", "b", "--package", "sympy", "--hluk-arg=--mount", "--hluk-arg=/d:/d:ro"]
        )
        assert args.net_allow == ["a", "b"]
        assert args.package == ["sympy"]
        assert args.hluk_arg == ["--mount", "/d:/d:ro"]

    def test_hosts_in_one_field(self):
        # As the Desktop Extension passes them: one setting, empty when the user lists none
        assert _parse_args(["--net-allow="]).net_allow == []
        assert _parse_args(["--net-allow=a, b,,c", "--net-allow", "d"]).net_allow == ["a", "b", "c", "d"]

    @pytest.mark.parametrize(("text", "seconds"), [("30", 30), ("30.0", 30), ("2.5", 2.5), ("1", 1)])
    def test_decimal_timeout(self, text, seconds):
        timeout = _parse_args(["--timeout", text]).timeout
        assert timeout == seconds
        assert type(timeout) is type(seconds)

    @pytest.mark.parametrize(
        "arguments",
        [
            ["--timeout", "0"],
            ["--timeout", "-1"],
            ["--timeout", "0.5"],
            ["--timeout", "inf"],
            ["--timeout", "nan"],
            ["--timeout", "thirty"],
            ["--startup-timeout", "inf"],
            ["--startup-timeout", "-5"],
            ["--startup-timeout", "0"],
            ["--startup-timeout", "nan"],
            ["--scratch-mb", "0"],
            ["--scratch-mb", "-1"],
        ],
    )
    def test_bad_numbers(self, arguments):
        with pytest.raises(SystemExit):
            _parse_args(arguments)

    def test_runtime_and_initrd_exclusive(self):
        with pytest.raises(SystemExit):
            _parse_args(["--runtime", "agent", "--initrd", "x.cpio"])


def stdio_session(*args, mode="auto"):
    """The server as a client starts it: its own process, speaking MCP on its standard input and output."""
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_server_hyperlight", *args], env=dict(os.environ)
    )
    # A transport rather than the parameters, which mcp's Client takes only from 2.3 on
    return Client(stdio_client(parameters), read_timeout_seconds=600, mode=mode)


class StdioTests:
    """The server end to end, over stdio, with the hluk and the image that `server_args` name."""

    server_args: list[str] = []

    def run(self, steps, *extra_args, mode="auto"):
        async def go():
            results = []
            async with stdio_session(*self.server_args, *extra_args, mode=mode) as client:
                for tool, arguments in steps:
                    results.append(await client.call_tool(tool, arguments))
            return results

        return asyncio.run(go())

    def test_state_persists_and_prints_stay_off_the_protocol(self):
        first, second = self.run(
            [
                (
                    "run_python",
                    {"code": "import sys\nx = 21\nprint('to stdout')\nprint('to stderr', file=sys.stderr)"},
                ),
                ("run_python", {"code": "x * 2"}),
            ]
        )
        assert not first.is_error, text(first)
        assert text(first) == "to stdout\nto stderr"
        assert text(second) == "Out: 42"

    def test_initialize_handshake(self):
        # As a client of a protocol version before 2026-07-28 connects, which most do
        (result,) = self.run([("run_python", {"code": "6 * 7"})], mode="legacy")
        assert text(result) == "Out: 42", text(result)

    def test_error_and_reset(self):
        error, reset, after = self.run(
            [
                ("run_python", {"code": "y = 1\n1 / 0"}),
                ("reset_sandbox", {}),
                ("run_python", {"code": "y"}),
            ]
        )
        assert error.is_error
        assert "ZeroDivisionError" in text(error)
        assert not reset.is_error, text(reset)
        assert after.is_error
        assert "NameError" in text(after)


def _is_running(pid):
    # /proc alone, where there is one: a kill(pid, 0) first leaves a window in which the process is reaped and the
    # read then fails with ESRCH. A zombie, exited but not reaped, is as good as gone
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split(")")[-1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        if os.path.isdir("/proc/self"):
            return False
    # Without /proc (macOS) there is no telling a zombie from a process that runs, so it runs: no test passes on a guess
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def local_hluk(tmp_path):
    """A stand-in for `hluk` that runs the guest server with the host's Python, to test the server without a VM."""
    fake_hluk = tmp_path / "hluk"
    fake_hluk.write_text(
        f"#!{sys.executable}\n"
        "import os, runpy, sys\n"
        "if os.environ.get('HLUK_PID_FILE') and sys.argv[1:] != ['--version']:\n"
        # Whole or not at all, for the test that polls for it
        "    open(os.environ['HLUK_PID_FILE'] + '.tmp', 'w').write(str(os.getpid()))\n"
        "    os.replace(os.environ['HLUK_PID_FILE'] + '.tmp', os.environ['HLUK_PID_FILE'])\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('hluk 0.18.0')\n"
        "else:\n"
        "    runpy.run_path(sys.argv[-1])\n"
    )
    fake_hluk.chmod(0o755)
    return str(fake_hluk)


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in for hluk is a script with a shebang")
class TestStdioLocalGuest(StdioTests):
    @pytest.fixture(autouse=True)
    def set_hluk(self, local_hluk):
        self.server_args = ["--hluk-path", local_hluk]

    @pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
    def test_signal_stops_the_guest(self, local_hluk, tmp_path, signum):
        pid_file = tmp_path / "hluk.pid"
        server = subprocess.Popen(
            [sys.executable, "-m", "mcp_server_hyperlight", "--hluk-path", local_hluk],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "HLUK_PID_FILE": str(pid_file)},
        )
        try:
            # The guest the server starts as it starts, without a call
            deadline = time.monotonic() + 30
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            hluk_pid = int(pid_file.read_text())
            server.send_signal(signum)
            assert server.wait(30) == 128 + signum
            deadline = time.monotonic() + 10
            while _is_running(hluk_pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not _is_running(hluk_pid)
        finally:
            server.kill()
            server.wait()


# The `hluk` to test and the image it runs: by default the one on PATH, with its release's `agent` image, and in CI the
# repository's own build of both
HLUK_PATH = os.getenv("HLUK_PATH") or shutil.which("hluk")
HLUK_IMAGE_ARGS = []
if os.getenv("HLUK_INITRD"):
    HLUK_IMAGE_ARGS += ["--initrd", os.environ["HLUK_INITRD"]]
if os.getenv("HLUK_SCRATCH_MB"):
    HLUK_IMAGE_ARGS += ["--scratch-mb", os.environ["HLUK_SCRATCH_MB"]]


@require_run_all
@pytest.mark.skipif(HLUK_PATH is None, reason="requires hluk")
class TestStdioIntegration(StdioTests):
    server_args = ["--hluk-path", str(HLUK_PATH), *HLUK_IMAGE_ARGS]

    def test_image_packages(self):
        (result,) = self.run([("run_python", {"code": "import numpy as np\nint(np.arange(10).sum())"})])
        assert text(result) == "Out: 45", text(result)

    def test_no_egress_by_default(self):
        (result,) = self.run(
            [("run_python", {"code": "import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)"})]
        )
        assert result.is_error
        assert "PermissionError" in text(result)

    def test_timeout_restarts_the_guest(self):
        slow, after = self.run(
            [
                ("run_python", {"code": "x = 1\nimport time\ntime.sleep(30)"}),
                ("run_python", {"code": "'x' in globals()"}),
            ],
            "--timeout",
            "2",
        )
        assert slow.is_error
        assert "maximum execution time of 2 seconds" in text(slow)
        # In the words of a server's calls, not of a smolagents agent's steps
        assert "agent" not in text(slow)
        assert "earlier calls are lost" in text(slow)
        # A fresh guest took the next call
        assert text(after) == "Out: False", text(after)

    def test_reset_waits_for_code_at_most_its_time(self):
        async def go():
            async with stdio_session(*self.server_args, "--timeout", "3") as client:
                await client.call_tool("run_python", {"code": "x = 1"})
                running = asyncio.ensure_future(client.call_tool("run_python", {"code": "while True: pass"}))
                await asyncio.sleep(1)
                start = time.monotonic()
                reset = await client.call_tool("reset_sandbox", {})
                waited = time.monotonic() - start
                after = await client.call_tool("run_python", {"code": "'x' in globals()"})
                return await running, reset, waited, after

        running, reset, waited, after = asyncio.run(go())
        assert running.is_error
        assert "maximum execution time of 3 seconds" in text(running)
        assert not reset.is_error, text(reset)
        assert waited < 30
        assert text(after) == "Out: False", text(after)
