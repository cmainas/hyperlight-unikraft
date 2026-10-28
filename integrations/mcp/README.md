# mcp-server-hyperlight

<!-- mcp-name: io.github.hyperlight-dev/mcp-server-hyperlight -->

An [MCP](https://modelcontextprotocol.io) server that runs Python in a local Hyperlight micro-VM, for any MCP client: Claude Code, Claude Desktop, VS Code, Cursor and others.

The server boots a Unikraft unikernel in its own hardware-isolated micro-VM (KVM or MSHV on Linux, WHP on Windows, HVF on Apple silicon Macs) through the `hluk` CLI. It needs no account, no daemon and no API key, and the code and its data stay on your machine. The guest has no network access unless you allow hosts with `--net-allow`, and none of your files unless you mount them.

| | Pyodide servers | cloud sandboxes | **mcp-server-hyperlight** |
|---|---|---|---|
| Isolation | Wasm, in a Deno process | cloud VM or container | **micro-VM, its own kernel** |
| Python | Pyodide's packages only | full | **full CPython 3.12, native wheels** |
| Runs on your machine | yes | no | **yes** |
| Needs | Deno | account, API key | **`hluk` and a hypervisor** |
| Ready in | seconds | seconds | **~40 ms** (Linux, KVM) |

## Install

Install `hluk` v0.18.0 or later. On Linux or macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/hyperlight-dev/hyperlight-unikraft/main/install.sh | sh
```

On Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/hyperlight-dev/hyperlight-unikraft/main/install.ps1 | iex
```

Then add the server to your client. It runs with [uv](https://docs.astral.sh/uv/)'s `uvx`, or with `pip install mcp-server-hyperlight` and the `mcp-server-hyperlight` command.

Claude Code:

```bash
claude mcp add hyperlight -- uvx mcp-server-hyperlight
```

Claude Desktop, Cursor and other clients that take a JSON config:

```json
{
  "mcpServers": {
    "hyperlight": {
      "command": "uvx",
      "args": ["mcp-server-hyperlight"]
    }
  }
}
```

VS Code (`.vscode/mcp.json`):

```json
{
  "servers": {
    "hyperlight": {
      "command": "uvx",
      "args": ["mcp-server-hyperlight"]
    }
  }
}
```

Codex (`~/.codex/config.toml`), with time for `uvx` to install the server on its first start, and for the first call to wait for the guest image (`--startup-timeout`, 300 s) and then run (`--timeout`, 30 s), one call at a time:

```toml
[mcp_servers.hyperlight]
command = "uvx"
args = ["mcp-server-hyperlight"]
startup_timeout_sec = 60
tool_timeout_sec = 360
```

The OpenAI Agents SDK runs it with `MCPServerStdio(params={"command": "uvx", "args": ["mcp-server-hyperlight"]}, client_session_timeout_seconds=360)`, for the same first call; its default of 5 seconds is shorter than a call may run.

Claude Desktop also takes it as a Desktop Extension, which needs no config: download `mcp-server-hyperlight-0.1.1.mcpb` from [its release](https://github.com/hyperlight-dev/hyperlight-unikraft/releases/tag/mcp-server-hyperlight-v0.1.1) and open it. It installs the server from PyPI with uv. Its settings set `--timeout` and `--net-allow`.

The server starts its guest as soon as the client starts it. The first time, `hluk` pulls the `agent` image of its release (about 400 MB, a 130 MB download) and saves a warm snapshot of it booted (about 800 MB), which takes some seconds to a few minutes; after that the guest starts from the snapshot in milliseconds. The image ships CPython 3.12 with numpy, pandas, scipy, scikit-learn, matplotlib, pillow and requests. To have both ready before a client first starts the server, run `hluk run --runtime agent --exec 'print("ready")'`.

## Tools

| Tool | |
|---|---|
| `run_python` | Runs Python code. Variables, imports and functions stay for the next call, as in a notebook. Returns what the code printed, subprocesses included, and the value of a trailing expression after `Out:`. An error returns its traceback, as a tool error. |
| `reset_sandbox` | Replaces the guest with a fresh one from the warm snapshot, in about 60 ms on Linux with KVM, after code still running ends or times out. Packages passed with `--package` are installed again, with `--net-allow`. |

A call that runs longer than `--timeout` (30 s by default) is stopped, and the guest replaced with a fresh one; so is a call that crashes the guest. The model is told its variables are gone. A call the client cancels before its turn does not run; one already running goes on until it ends or times out, as the server has no way yet to stop code short of its time limit.

## Options

Pass them after the command, e.g. `uvx mcp-server-hyperlight --net-allow pypi.org --net-allow files.pythonhosted.org --package sympy`.

| Option | Default | |
|---|---|---|
| `--net-allow HOST` | none | A host or IP the guest may connect to; repeat for more, or separate them with commas. With none, every connection is refused. |
| `--package NAME` | none | A package to install in the guest at start and after each reset, if the image lacks it. Installing needs `--net-allow` for the package index. |
| `--timeout SECONDS` | 30 | Longest a call may run, and so the longest a reset or the next call waits for one. |
| `--runtime NAME` | `agent` | Published image to run, by name or full reference. |
| `--initrd PATH` | | A rootfs CPIO of your own to run instead (it must have CPython 3). |
| `--scratch-mb MIB` | the image's | Guest memory in MiB. |
| `--hluk-path PATH` | `hluk` on `PATH` | Else where it is installed: `$HLUK_INSTALL_DIR`, the install scripts' `~/.local/bin` (`%LOCALAPPDATA%\hluk\bin` on Windows), or Cargo's `$CARGO_HOME/bin` (`~/.cargo/bin`), for a client that starts the server with another `PATH`. |
| `--hluk-arg ARG` | | More arguments for `hluk run`, one per option: `--hluk-arg=--mount --hluk-arg=/data:/data:ro` mounts `/data` read-only. |
| `--startup-timeout SECONDS` | 300 | Longest to wait for the guest; the first start pulls the image. |

The tool descriptions tell the model what the guest has: its packages, which hosts it may reach, and whether it sees any of your files. With `--hluk-arg`, which can mount directories or open the network, they no longer say the guest has neither.

## How it works

The server is a thin layer over [`smolagents-hyperlight`](https://github.com/hyperlight-dev/hyperlight-unikraft/tree/main/integrations/smolagents)'s executor, which starts `hluk run` as a child process and talks to a small Python script in the guest over its console. The two packages share one guest protocol, its tests and its bounds on what the guest sends back.

- One guest serves the server's client, and tool calls take turns on it, so state stays consistent. A guest that fails to start is reported on the standard error, and to the model on its first call, which tries again.
- Between calls, the whole micro-VM is paused: a thread or subprocess a call leaves running only goes on while a call runs.
- The guest is not trusted: replies are bounded (64 MiB, 2 MiB of printed output) and checked before the server uses them. A tool result keeps at most 100,000 characters, its beginning and its end, as MCP clients take far less than an agent's executor does.
- The server speaks MCP on its standard input and output, and logs to its standard error. What `hluk` itself writes is kept apart, and comes with the error when a guest fails to start. It stops its guest as it exits, the input closed or on a `SIGTERM` or `SIGINT`.

## Known limits

- Python only, and text only: an image the code makes comes back as its `repr`. Save it to a mounted directory to keep it.
- Packages installed by the code itself, with pip, are gone after a reset; those passed with `--package` are installed again.
- One vCPU, and threads in the guest take turns rather than run at once.
- A guest still starting when the server exits, pulling its image or installing `--package` packages, is not stopped with it, and runs until that start ends: the executor gives the server no handle on it before then.
- With `hluk` v0.19.1 or earlier, each Python subprocess, `pip install` included, leaves about 15 MiB of guest memory in use until the guest is restarted (a guest kernel bug, fixed in the next release).
- CI tests the server on Linux (KVM) and Windows (WHP). `hluk` supports Apple silicon Macs too, but the server is not tested there yet.

## Development

```bash
pip install -e ../smolagents -e '.[test]'
pytest                    # unit tests, and the server end to end with a stand-in for hluk
RUN_ALL=1 pytest          # also the tests in a real micro-VM, with the hluk on PATH
```

As for `smolagents-hyperlight`, `HLUK_PATH`, `HLUK_INITRD` and `HLUK_SCRATCH_MB` test a local build of `hluk` and its agent rootfs instead of the `hluk` on `PATH` and its published image.
