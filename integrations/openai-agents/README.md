# openai-agents-hyperlight

A sandbox provider for the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) that runs a sandbox agent's shell commands in local Hyperlight micro-VMs.

Each command runs in a fresh micro-VM with its own kernel, restored from a warm snapshot of a Unikraft guest in tens of milliseconds, through the `hluk` CLI. It needs no account, no daemon and no API key beyond the model's. The guest has no network unless you allow hosts with `net_allow`, and sees nothing of the host but the sandbox's workspace and the directories you grant it.

| | `UnixLocalSandboxClient` | Docker | E2B, Modal, Daytona, … | **Hyperlight** |
|---|---|---|---|---|
| Commands run as | host processes | containers, shared host kernel | remote sandboxes | **micro-VMs, their own kernel** |
| Runs on your machine | yes | yes | no | **yes** |
| Needs | nothing | Docker daemon | account, API key | **`hluk` and KVM or MSHV** |
| Network | host's | open by default | provider's | **none unless allowed** |

## Install

Linux only, on a host with KVM or MSHV. Install `hluk` v0.19.0 or later:

```bash
curl -fsSL https://raw.githubusercontent.com/hyperlight-dev/hyperlight-unikraft/main/install.sh | sh
```

Then the package, which brings the SDK (`openai-agents`):

```bash
pip install openai-agents-hyperlight
```

Check both with the demo, which runs a command in a micro-VM through the SDK's `Runner` and a scripted model, so it needs no API key:

```bash
python -m openai_agents_hyperlight.demo   # prints "hyperlight provider works"
```

The first run pulls the `agent` image of your `hluk` release (`ghcr.io/hyperlight-dev/hyperlight-unikraft/agent:initrd-v<version>`) and saves a warm snapshot of it; every command after that starts from the snapshot. The image ships BusyBox and CPython 3.12 with numpy, pandas, scipy, scikit-learn, matplotlib, pillow and requests. Both stay in `hluk`'s cache (`$HLUK_CACHE_DIR`, else `~/.cache/hluk`); `hluk cache ls` lists them.

## Use

Pass the client to `SandboxRunConfig`:

```python
from agents import Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File

from openai_agents_hyperlight import HyperlightSandboxClient, HyperlightSandboxClientOptions

agent = SandboxAgent(
    name="analyst",
    instructions="Answer from the files in the workspace.",
    default_manifest=Manifest(entries={"data.csv": File(content=b"x,y\n1,2\n3,4\n")}),
    capabilities=[Shell()],
)
result = await Runner.run(
    agent,
    "What is the mean of y?",
    run_config=RunConfig(
        sandbox=SandboxRunConfig(
            client=HyperlightSandboxClient(),
            options=HyperlightSandboxClientOptions(net_allow=("pypi.org", "files.pythonhosted.org")),
        )
    ),
)
```

The runner then owns the session: it creates it, starts it, and removes the workspace the client made once the run ends, however it ends. Before removing it, the SDK saves a copy of the workspace as the session's snapshot, by default a tar under `$XDG_STATE_HOME/openai-agents-python/sandbox/snapshots/` (`~/.local/state/...`) that is kept; pass `SandboxRunConfig(snapshot=...)` to put it elsewhere, or `NoopSnapshotSpec()` to keep none. A session you create yourself, with `await client.create(...)` and `SandboxRunConfig(session=session)`, is yours: close it with `async with session:` or `await session.aclose()`, and remove it with `await client.delete(session)`, which removes only a workspace the client made, never a `Manifest(root=...)` of yours. A run interrupted for approval resumes from its saved state (`RunState.to_json()`) with any `HyperlightSandboxClient` in a process that imported this package, and the same temporary directory (`TMPDIR`) when the client made the workspace: it is made again there, owner-only, and refused elsewhere.

### Options

`HyperlightSandboxClientOptions`, or, in `SandboxRunConfig(options=...)`, `{"type": "hyperlight", ...}` as a dictionary. Unknown fields are refused.

| Option | Default | |
|---|---|---|
| `runtime` | `"agent"` | Published image to run, by name or full reference. It needs `hluk`'s Python driver and `/bin/sh` |
| `initrd` | | Path to a rootfs CPIO of your own to run instead |
| `scratch_mb` | the image's | Guest memory in MiB |
| `net_allow` | none | Hosts or IPs the guest may connect to. With none, every connection is refused |

`HyperlightSandboxClient` takes `hluk_path` (default: `hluk` on `PATH`), `startup_timeout_s` (300 s; the first run pulls the image) and `max_workspace_bytes` (1 GiB; `None` for no limit), the largest workspace archive it snapshots. They stay on the client, so a saved session state cannot choose which program the host runs. The guest's options do not: a saved state, and so a saved `RunState`, names its image, memory and `net_allow`, so resume only states you trust as you would the code that made them.

## What persists

- **The workspace.** It is a host directory, shared read-write into each guest at the same path, so files written there are there for the next command. Directories in `Manifest.extra_path_grants` are shared the same way, read-only when the grant is.
- **Nothing else.** Each command boots a fresh guest: packages a command installs outside the workspace, its environment changes, and processes it leaves running end with it. To keep a package, install it into the workspace: `HOME` is the workspace, so `python3 -m pip install --user <package>` (with `net_allow` naming the package index) puts it in its `.local`, where later commands import it from. The image has no `pip` command, only `python3 -m pip`.
- **No command outlives its wait.** With no PTY, the SDK's `exec_command` tool stops a command, and its guest, once its `yield_time_ms` (10 s by default) runs out, and the model is told it timed out. A model running a long command has to ask for a longer one: a large `pip install`, or any command importing heavy libraries, which every command does afresh in a new guest (scikit-learn about 7 s, scipy 4 s, pandas 2 s).
- **Snapshots.** Workspace snapshots (`LocalSnapshotSpec` and the others) work as they do for `UnixLocalSandboxClient`; the warm snapshot of the guest is `hluk`'s and holds no workspace data. Commands still running when the session stops are stopped first, so that no guest writes the workspace while it is archived. The SDK archives it in memory, so a workspace whose archive would be larger than `max_workspace_bytes` (sized as the SDK would make it, a sparse file at its apparent size, ephemeral entries left out) is not snapshotted. A session the runner owns logs it and goes on, its workspace removed as usual, and the result has no sandbox state to resume from. On a session of yours, `aclose()` raises `WorkspaceStopError` and leaves the session running: call `shutdown()` and `client.delete(session)` yourself. Commands are stopped, and the size checked with none running, only as the session stops: a direct `persist_workspace()` call, while commands run, archives what they leave as it goes.

## Not supported

Refused with an error, never run on the host instead: PTY sessions and `write_stdin`, exposed ports, sandbox users and groups, manifest mounts, Git repository entries, path grants with a `host_path`, and grants of single files.

The guest runs no script by its `#!` line (`./run.sh` fails with "Exec format error"), the console scripts pip installs included: run them with their interpreter (`sh run.sh`, `python3 -m <module>`).

## The trust boundary

The guest is treated as untrusted. File operations run on the host, with the SDK's UnixLocal descriptor-relative checks, against a directory the guest can also write:

- A shared path may not go through a symlink or overlap another, checked when the session starts and before each command, so a guest cannot swap a link to widen what the next one sees.
- A command's environment reaches the guest in an owner-only file, never on `hluk`'s command line.
- What the guest leaves for the host (exit status, output) is read without following links, from regular files only, and within bounds (16 MiB of output per stream).
- Removing a workspace never follows a link the guest left in it, and removes trees the guest made deep or unreadable.
- A command that stops its own guest (`kill -9 -1`) is a failed command, with the output it wrote and the status `hluk` reports the guest exited with, as is one with a memory fault in any of its processes, which aborts the guest's kernel and so ends the whole command, with 139; the next command gets a fresh guest. A failure of `hluk` or the hypervisor is still an error of the sandbox.
- A guest can see the names, not the contents, of the empty directories that were mount points when its warm snapshot was saved: the workspace and control directory of the session that first started it.

## Compatibility

The session builds on the SDK's `UnixLocalSandboxSession`, including some of its underscored hooks, so each SDK minor release is tested before the dependency range in `pyproject.toml` takes it in; CI tests its lowest and highest. Saved session states (`type: "hyperlight"`) keep working across `0.1.x`: a test resumes one saved by 0.1.0.

## Development

```bash
pip install -e '.[test]'
pytest            # the client, the Runner path and the guest's script, against a stand-in for hluk
RUN_ALL=1 pytest  # also in real micro-VMs, with the hluk on PATH
```

To test a local build, set `HLUK_PATH` to its `hluk`, `HLUK_INITRD` to its agent rootfs, and `HLUK_SCRATCH_MB` to the guest memory that rootfs needs. CI does this so each commit is tested against its own build.
