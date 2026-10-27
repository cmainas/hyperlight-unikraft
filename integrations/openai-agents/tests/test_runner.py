"""The SDK's public Runner path with this package's client, against the stand-in for `hluk`.

These follow the SDK's provider guide (docs/sandbox/custom-providers.md): sessions the runner owns and sessions the
application owns, a run resumed from its saved state, and cleanup however a run ends.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


if sys.platform == "win32":
    pytest.skip("The Hyperlight sandbox backend is unavailable on Windows", allow_module_level=True)

from agents import ModelSettings, Runner, function_tool
from agents.exceptions import UserError
from agents.items import ToolCallOutputItem
from agents.run import RunConfig
from agents.run_state import RunState
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.errors import WorkspaceStartError
from agents.testing import ScriptedModel, assistant_message, function_call

from openai_agents_hyperlight import (
    HyperlightSandboxClient,
    HyperlightSandboxClientOptions,
    HyperlightSandboxSession,
    HyperlightSandboxSessionState,
)


def _shell(cmd: str, call_id: str, **args: object) -> list:
    return [function_call("exec_command", {"cmd": cmd, **args}, call_id=call_id)]


def _outputs(result) -> list[str]:
    return [str(item.output) for item in result.new_items if isinstance(item, ToolCallOutputItem)]


def _run_config(client: HyperlightSandboxClient, options: object = None) -> RunConfig:
    return RunConfig(
        tracing_disabled=True,
        sandbox=SandboxRunConfig(
            client=client, options=HyperlightSandboxClientOptions() if options is None else options
        ),
    )


def _workspaces(tmp_path: Path) -> set[str]:
    """The workspaces the guests ran in, from the stand-in's log of its arguments."""
    log = tmp_path / "hluk.log"
    if not log.exists():
        return set()
    roots = set()
    for line in log.read_text().splitlines():
        record = json.loads(line)
        if "spec" in record:
            roots.add(json.loads(record["spec"])["cwd"])
    return roots


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [None, {"type": "hyperlight"}], ids=["typed", "dict"])
async def test_a_session_the_runner_creates_is_removed_after_the_run(
    tmp_path: Path, fake_hluk: Path, options: object
) -> None:
    model = ScriptedModel([_shell("echo from-guest > out.txt; cat out.txt", "c1"), [assistant_message("done")]])
    agent = SandboxAgent(name="hyperlight", model=model, capabilities=[Shell()])
    result = await Runner.run(
        agent, "Write a file.", run_config=_run_config(HyperlightSandboxClient(hluk_path=str(fake_hluk)), options)
    )
    model.assert_complete()
    assert result.final_output == "done"
    assert any("from-guest" in output for output in _outputs(result))
    (workspace,) = _workspaces(tmp_path)
    assert not os.path.exists(workspace)


@pytest.mark.asyncio
async def test_a_session_the_application_owns_outlives_the_run(tmp_path: Path, fake_hluk: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk))
    session = await client.create(manifest=Manifest(root=str(workspace)))
    try:
        async with session:
            for call_id in ("c1", "c2"):
                model = ScriptedModel([_shell("echo run >> runs.txt", call_id), [assistant_message("done")]])
                agent = SandboxAgent(name="hyperlight", model=model, capabilities=[Shell()])
                await Runner.run(
                    agent,
                    "Append a line.",
                    run_config=RunConfig(tracing_disabled=True, sandbox=SandboxRunConfig(session=session)),
                )
            assert (await session.read(Path("runs.txt"))).read() == b"run\nrun\n"
            # Left running: the runner neither stopped nor closed a session it did not create.
            assert await session.running()
    finally:
        await client.delete(session)
    # The application's directory: the client removes only workspaces it made.
    assert workspace.is_dir()


@pytest.mark.asyncio
async def test_a_run_resumed_from_its_saved_state_keeps_the_workspace(tmp_path: Path, fake_hluk: Path) -> None:
    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    def agent(model: ScriptedModel) -> SandboxAgent:
        return SandboxAgent(
            name="hyperlight",
            model=model,
            tools=[approval_tool],
            capabilities=[Shell()],
            model_settings=ModelSettings(tool_choice="required"),
        )

    first_model = ScriptedModel(
        [
            _shell("echo kept > note.txt", "c1"),
            [function_call("approval_tool", {}, call_id="c2")],
        ]
    )
    first = await Runner.run(
        agent(first_model), "Write a note.", run_config=_run_config(HyperlightSandboxClient(hluk_path=str(fake_hluk)))
    )
    assert len(first.interruptions) == 1
    saved = first.to_state().to_json()
    assert saved["sandbox"]["backend_id"] == "hyperlight"

    # A new client, as a resume worker would have, picks up the session from the saved state alone.
    resumed_model = ScriptedModel([_shell("cat note.txt; stat -c %a .", "c3"), [assistant_message("done")]])
    resumed_agent = agent(resumed_model)
    state = await RunState.from_json(resumed_agent, json.loads(json.dumps(saved)))
    state.approve(state.get_interruptions()[0])
    resumed = await Runner.run(
        resumed_agent, state, run_config=_run_config(HyperlightSandboxClient(hluk_path=str(fake_hluk)))
    )
    resumed_model.assert_complete()
    assert resumed.final_output == "done"
    # Made again owner-only, as create() made it
    assert any("kept\n700\n" in output for output in _outputs(resumed))
    assert not any(os.path.exists(workspace) for workspace in _workspaces(tmp_path))


@pytest.mark.asyncio
async def test_a_failed_command_is_reported_and_the_session_removed(tmp_path: Path, fake_hluk: Path) -> None:
    model = ScriptedModel([_shell("echo oops >&2; exit 3", "c1"), [assistant_message("done")]])
    agent = SandboxAgent(name="hyperlight", model=model, capabilities=[Shell()])
    result = await Runner.run(
        agent, "Fail.", run_config=_run_config(HyperlightSandboxClient(hluk_path=str(fake_hluk)))
    )
    (output,) = _outputs(result)
    assert "oops" in output and "Process exited with code 3" in output
    (workspace,) = _workspaces(tmp_path)
    assert not os.path.exists(workspace)


@pytest.mark.asyncio
async def test_a_guest_that_fails_fails_the_run_and_the_session_is_removed(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel([_shell("true", "c1"), [assistant_message("done")]])
    agent = SandboxAgent(name="hyperlight", model=model, capabilities=[Shell()])
    # The stand-in starts, then crashes the command's guest: a tool error, which ends the run.
    monkeypatch.setenv("FAKE_HLUK_MODE", "crash")
    with pytest.raises(UserError, match="exec_command"):
        await Runner.run(agent, "Run.", run_config=_run_config(HyperlightSandboxClient(hluk_path=str(fake_hluk))))
    (workspace,) = _workspaces(tmp_path)
    assert not os.path.exists(workspace)


@pytest.mark.asyncio
async def test_a_command_that_times_out_stops_its_guest_and_the_session_is_removed(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HLUK_MODE", "sleep")
    model = ScriptedModel([_shell("true", "c1", yield_time_ms=5000), [assistant_message("done")]])
    agent = SandboxAgent(name="hyperlight", model=model, capabilities=[Shell()])
    result = await Runner.run(
        agent, "Wait.", run_config=_run_config(HyperlightSandboxClient(hluk_path=str(fake_hluk)))
    )
    assert any("timed out" in output for output in _outputs(result))
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "hluk.pid").read_text()), 0)
    (workspace,) = _workspaces(tmp_path)
    assert not os.path.exists(workspace)


@pytest.mark.asyncio
async def test_a_cancelled_run_stops_its_guest_and_the_session_is_removed(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HLUK_MODE", "sleep")
    model = ScriptedModel([_shell("true", "c1", yield_time_ms=60_000), [assistant_message("done")]])
    agent = SandboxAgent(name="hyperlight", model=model, capabilities=[Shell()])
    run = asyncio.create_task(
        Runner.run(agent, "Wait.", run_config=_run_config(HyperlightSandboxClient(hluk_path=str(fake_hluk))))
    )
    pid_file = tmp_path / "hluk.pid"
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    (workspace,) = _workspaces(tmp_path)
    assert not os.path.exists(workspace)


@pytest.mark.asyncio
async def test_a_session_that_cannot_start_is_removed(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HLUK_MODE", "no-guest")
    monkeypatch.setattr(
        "tempfile.tempdir", str(tmp_path / "tmp")
    )  # the workspaces the client makes, to find the one it made
    (tmp_path / "tmp").mkdir()
    model = ScriptedModel([_shell("true", "c1"), [assistant_message("done")]])
    agent = SandboxAgent(name="hyperlight", model=model, capabilities=[Shell()])
    with pytest.raises(WorkspaceStartError):
        await Runner.run(agent, "Run.", run_config=_run_config(HyperlightSandboxClient(hluk_path=str(fake_hluk))))
    # The start was tried, with a workspace made there, which is gone.
    (probe,) = [json.loads(line)["args"] for line in (tmp_path / "hluk.log").read_text().splitlines()]
    assert any(arg.startswith(f"--mount={tmp_path / 'tmp' / 'sandbox-hyperlight-'}") for arg in probe), probe
    assert list((tmp_path / "tmp").iterdir()) == []


def test_saved_values_parse_in_a_fresh_interpreter(tmp_path: Path) -> None:
    # Importing the package registers its types, in a process with nothing else imported, run outside the source tree.
    script = """
import json, sys
import openai_agents_hyperlight as hyperlight
from agents.sandbox import Manifest
from agents.sandbox.session import BaseSandboxClientOptions, SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot

options = BaseSandboxClientOptions.parse({"type": "hyperlight", "net_allow": ["pypi.org"], "scratch_mb": 512})
assert type(options) is hyperlight.HyperlightSandboxClientOptions, type(options)
assert options.net_allow == ("pypi.org",) and options.scratch_mb == 512
for bad in ({"type": "hyperlight", "net_hosts": ["pypi.org"]}, {"type": "hyperlight", "scratch_mb": 0},
            {"type": "hyperlight", "runtime": "agent", "initrd": "/rootfs.cpio"}):
    try:
        BaseSandboxClientOptions.parse(bad)
    except (TypeError, ValueError):
        pass
    else:
        sys.exit(f"parsed {bad}")

state = hyperlight.HyperlightSandboxSessionState(
    manifest=Manifest(root="/tmp/w"), snapshot=NoopSnapshot(id="s"), net_allow=("pypi.org",)
)
payload = json.loads(json.dumps(hyperlight.HyperlightSandboxClient().serialize_session_state(state)))
for parsed in (SandboxSessionState.parse(payload), hyperlight.HyperlightSandboxClient().deserialize_session_state(payload)):
    assert type(parsed) is hyperlight.HyperlightSandboxSessionState, type(parsed)
    assert parsed.net_allow == ("pypi.org",) and parsed.manifest.root == "/tmp/w"
try:
    hyperlight.HyperlightSandboxClient().deserialize_session_state({**payload, "scratch_mb": -1})
except ValueError:
    pass
else:
    sys.exit("deserialized a negative scratch_mb")
"""
    subprocess.run([sys.executable, "-I", "-c", script], cwd=tmp_path, check=True, timeout=120)


@pytest.mark.asyncio
async def test_a_state_saved_by_0_1_0_still_resumes() -> None:
    # The README promises saved states keep working across 0.1.x: this one was saved by 0.1.0.
    payload = json.loads((Path(__file__).parent / "data" / "session-state-0.1.0.json").read_text())
    client = HyperlightSandboxClient()
    state = client.deserialize_session_state(payload)
    assert isinstance(state, HyperlightSandboxSessionState)
    assert (state.runtime, state.initrd, state.scratch_mb, state.net_allow) == ("agent", None, 1024, ("pypi.org",))
    assert state.manifest.root == "/srv/agent-workspace" and not state.workspace_root_owned
    assert [(grant.path, grant.read_only) for grant in state.manifest.extra_path_grants] == [("/srv/data", True)]
    session = await client.resume(state)
    assert session.state is state


@pytest.mark.asyncio
async def test_a_workspace_too_large_to_snapshot_is_refused_and_removed(
    tmp_path: Path, fake_hluk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sparse: archived at its apparent size, which is what the limit counts.
    model = ScriptedModel([_shell("truncate -s 2G big", "c1"), [assistant_message("done")]])
    agent = SandboxAgent(name="hyperlight", model=model, capabilities=[Shell()])
    client = HyperlightSandboxClient(hluk_path=str(fake_hluk), max_workspace_bytes=1 << 20)
    # The runner logs the failed snapshot and goes on: the run ends, with no snapshot saved and
    # so no state to resume the sandbox from.
    refused: list[BaseException] = []
    persist = HyperlightSandboxSession.persist_workspace

    async def recorded(self):
        try:
            return await persist(self)
        except BaseException as error:
            refused.append(error)
            raise

    monkeypatch.setattr(HyperlightSandboxSession, "persist_workspace", recorded)
    result = await Runner.run(agent, "Write.", run_config=_run_config(client))
    assert result.final_output == "done"
    assert [error.context.get("reason") for error in refused] == ["workspace_too_large"]
    assert result._sandbox_resume_state is None
    (workspace,) = _workspaces(tmp_path)
    assert not os.path.exists(workspace)
