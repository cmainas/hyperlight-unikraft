# smolagents-hyperlight

Run the code of a [smolagents](https://github.com/huggingface/smolagents) `CodeAgent` in a local Hyperlight micro-VM.

Each executor boots a Unikraft unikernel in its own hardware-isolated micro-VM (KVM or MSHV on Linux, WHP on Windows, Hypervisor.framework on Apple silicon Macs) through the `hluk` CLI. It needs no account, no daemon and no API key, and the code and its data stay on your machine. The guest has no network access at all unless you allow hosts with `net_allow`.

| | local | docker | e2b, modal, blaxel | **hyperlight** |
|---|---|---|---|---|
| Isolation | AST interpreter in your process | container, shared host kernel | cloud VM or sandbox | **micro-VM, its own kernel** |
| Runs on your machine | yes | yes | no | **yes** |
| Needs | nothing | Docker daemon | account, API key | **`hluk` and a hypervisor** |
| Ready in | instant | seconds | seconds | **~40 ms** (Linux, KVM) |
| Network | host's | open by default | provider's | **none unless allowed** |

## Install

Install `hluk` v0.18.0 or later. On Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/hyperlight-dev/hyperlight-unikraft/main/install.sh | sh
```

On Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/hyperlight-dev/hyperlight-unikraft/main/install.ps1 | iex
```

Then the package:

```bash
pip install smolagents-hyperlight
```

The first run pulls the `agent` image of your `hluk` release (`ghcr.io/hyperlight-dev/hyperlight-unikraft/agent:initrd-v<version>`, about 400 MB) and saves its warm snapshot; later sandboxes start in milliseconds. The image ships CPython 3.12 with numpy, pandas, scipy, scikit-learn, matplotlib, pillow and requests.

## Use

```python
from smolagents import CodeAgent, InferenceClientModel
from smolagents_hyperlight import HyperlightExecutor

with CodeAgent(model=InferenceClientModel(), tools=[], executor=HyperlightExecutor()) as agent:
    agent.run("Use numpy to compute the 20th Fibonacci number via matrix power")
```

The package also registers the executor under the `smolagents.executors` entry points, for smolagents [#2724](https://github.com/huggingface/smolagents/pull/2724), which is not in a release yet. A smolagents with it creates the executor by name, with `executor_kwargs` as its arguments and the agent's `additional_authorized_imports` installed in the guest:

```python
CodeAgent(model=model, tools=[], executor_type="hyperlight", executor_kwargs={"net_allow": ["pypi.org"]})
```

With `executor=`, pass the packages to install yourself: `HyperlightExecutor(additional_imports=["sympy"], net_allow=["pypi.org", "files.pythonhosted.org"])`. Packages already in the image are not installed again, and need no network.

### Arguments

| Argument | Default | |
|---|---|---|
| `additional_imports` | none | Packages to install in the guest, with pip for those not in the image (which needs `net_allow` to include the package index) |
| `logger` | an `AgentLogger` at `INFO` | |
| `net_allow` | none | Hosts or IPs the guest may connect to. With none, every connection is refused and the guest cannot listen on a port |
| `runtime` | `"agent"` | Published image to run, by name or full reference |
| `initrd` | | Path to a rootfs CPIO of your own to run instead (it must have CPython 3) |
| `scratch_mb` | the image's | Guest memory in MiB |
| `hluk_path` | `hluk` on `PATH` | |
| `hluk_args` | | More arguments for `hluk run`, e.g. `["--mount", "/data:/data:ro"]` |
| `timeout_seconds` | smolagents' `MAX_EXECUTION_TIME_SECONDS` | A step that runs longer is stopped, and the next one gets a fresh guest |
| `startup_timeout` | 300 | Seconds to wait for the guest; the first run pulls the image |
| `allow_pickle` | `False` | As for smolagents' other remote executors |

## How it works

The executor starts `hluk run` as a child process, with a small Python script that `hluk` runs in the guest's CPython. The script reads each step from the guest's console, which `hluk` connects to its own standard input, as a line of JSON, runs it with the state of the steps before, and writes the result back on its standard output, after a marker that tells it from anything else on the console. What the step prints, subprocesses included, comes back as its logs.

- Between steps, the guest waits for the next one with its whole micro-VM paused: a thread or subprocess a step leaves running only goes on while a step runs.
- `reset()` replaces the guest with a fresh one from the warm snapshot, in about 60 ms on Linux with KVM; a step that times out or crashes the guest does the same, and the agent is told that its variables are gone.
- A Ctrl-C reaches `hluk` too, in the executor's process group, and stops the guest; the next code starts a fresh one.
- The guest is not trusted: replies are bounded (64 MiB) and checked before the host uses them.

## Known limits

- CI runs the tests in micro-VMs on Linux (KVM) and Windows (WHP). `hluk` supports Apple silicon Macs too, but the executor is not tested there yet.
- One vCPU, and threads in the guest take turns rather than run at once.
- The guest kernel keeps the memory of exited processes: a session that starts many heavy subprocesses runs out of memory after a while.
- Managed agents (`CodeAgent(managed_agents=...)`) are not supported, as with smolagents' other remote executors.

## Development

```bash
pip install -e '.[test]'
pytest                    # unit tests, and the guest's script run by a stand-in for hluk
RUN_ALL=1 pytest          # also the tests in a real micro-VM, with the hluk on PATH
```

`HLUK_PATH`, `HLUK_INITRD` and `HLUK_SCRATCH_MB` point the micro-VM tests at another `hluk` and image, as CI does with the repository's own build.
