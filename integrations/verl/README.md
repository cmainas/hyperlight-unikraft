# verl-hyperlight

Run the test cases of [verl](https://github.com/verl-project/verl)'s code rewards in local Hyperlight micro-VMs, instead of in the reward worker's own process or on a Sandbox Fusion service.

Each test case runs in its own micro-VM, restored from a warm snapshot of a booted CPython in a few tens of milliseconds, through the `hluk` CLI: with its own kernel, no network and no view of the host's files or environment, so a model's code can neither reach the training node nor change the result of another test case. Results are shaped as Sandbox Fusion's, so verl scores them as it scores the service's.

| | `prime_code` (verl's default) | Sandbox Fusion | **hyperlight** |
|---|---|---|---|
| Runs the code | `exec()` in a process forked from the grader | in a service, over HTTP | **in a micro-VM on the training node** |
| Isolation | none: a solution can rewrite its own result | the service's | **micro-VM, its own kernel** |
| Needs | nothing | a Sandbox Fusion deployment | **`hluk` and `/dev/kvm`** |
| Grading | its own comparison | reference | **Sandbox Fusion's** |

## Install

Linux only, as verl, on a host with KVM; the user running training needs access to `/dev/kvm`. Install `hluk` v0.19.0 or later:

```bash
curl -fsSL https://raw.githubusercontent.com/hyperlight-dev/hyperlight-unikraft/main/install.sh | sh
```

Then the package, beside verl:

```bash
pip install verl-hyperlight
```

Before training, pull the image once per node, so that the first reward batch does not wait for it: `hluk run --runtime python --exec pass` (or `--runtime agent`). The first test case of each guest then boots it and saves its warm snapshot, in seconds; every one after restores it.

## Use

Point verl's Sandbox Fusion reward at it:

```bash
python -m verl.trainer.main_ppo ... reward.sandbox_fusion.url=hyperlight://
```

It applies to the data sources verl scores through Sandbox Fusion (`codecontests`, `apps`, `codeforces`, `taco`, and a custom reward function that calls its `compute_score`), for Python only.

| URL | Guest |
|---|---|
| `hyperlight://` | The `python` image of the `hluk` release: CPython 3.12 and its standard library, without `ctypes`, `sqlite3`, `bz2`, `lzma`, `ssl`, `readline` and `curses` |
| `hyperlight://agent` | The `agent` image: also numpy, pandas, scipy and sympy (a 400 MB image) |
| `hyperlight://<name>` | Another published image with CPython 3 |
| `hyperlight:///path/to/rootfs.cpio` | A rootfs of your own, by its absolute path, built on hluk's python image (it needs hluk's runtime driver, and CPython 3.8 or later) |

verl's other settings mean here:

- `reward.sandbox_fusion.memory_limit_mb`: what a solution may allocate, as Sandbox Fusion lets its process; each micro-VM gets about a third more, for the guest's kernel and interpreter. The guest kernel does not enforce a limit of the process's own, so one below what the image boots in is refused (the agent image needs at least 720).
- `reward.sandbox_fusion.max_concurrent`: the micro-VMs running at once, per reward worker, each on a CPU of the node. Keep `max_concurrent` × `reward.num_workers` within the CPUs the reward workers can have, and that times the guest's memory within the node's free memory: the test case timeout counts wall-clock time.

### How verl finds it

verl (0.9.0 and later) imports the packages registered as its plugins, the `verl.plugins` entry points, at its own import, in every process. This package's registers `run_code` as the sandbox runner of the `hyperlight` scheme, in a verl with sandbox runners; an earlier one only knows Sandbox Fusion's HTTP service, and the plugin wraps its `call_sandbox_api` instead, so that `hyperlight://` URLs come here and every other one goes on as before. `VERL_USE_EXTERNAL_PLUGINS` must allow it: unset or `auto` does, `none` turns verl's plugins off, this one included, and a list of names must name `hyperlight`. Install the package wherever verl's reward workers run.

## How it works

`run_code` takes `call_sandbox_api`'s arguments. It writes the solution and its input to a temporary directory, which it mounts read-only in the guest, and runs `hluk run` with a small Python runner that makes the guest run the solution as `python solution.py` would under Sandbox Fusion:

- The solution runs from a writable directory of its own, which is its working directory and first on `sys.path`, as `__main__`; `random`, and numpy where the image has it, are reseeded in each micro-VM.
- Its standard input is the case's input, as a file; `/dev/stdin`, `/dev/stdout` and `/dev/stderr` open its own descriptors.
- Its output goes to files in the guest's memory, copied out at the end, before `os._exit`, and before an uncaught exception's traceback: the guest's console says it is a terminal, and each write to it leaves the micro-VM.
- At its end come threading's exit hooks, non-daemon threads joined until none is left, atexit handlers, a flush (status 120 when it fails, as Python's) and the module's teardown, as at interpreter exit.

The stdlib modules solutions commonly import, and numpy and sympy where the image has them, are imported before the snapshot is taken, so they cost nothing per case. Each reward worker runs its guest once before its first case, serialized by a lock beside `hluk`'s cache, so that the image pull and the snapshot save are not charged to a case's timeout. A failure of the backend itself (no `/dev/kvm`, an old `hluk`, an unknown image) is logged, as it zeroes every code reward; one before the solution ran is retried once. `hluk` runs in its own process group, killed whole on a timeout, and dies with the worker that started it. Output is capped at 16 MiB a stream.

## Known limits

- Python only.
- The guest has one CPU and does not preempt threads: a thread spinning on a flag another thread sets never yields to it. `time.process_time()` does not advance (use `time.perf_counter()`), and timer signals (`signal.alarm`, `setitimer`) never fire.
- No `os.fork()`, and so no `multiprocessing` pools, and no shell: `os.system` and shell commands fail, `subprocess` with `sys.executable` works.
- Solutions run somewhat slower than on the host, allocation- and I/O-heavy ones up to a few times slower, so one close to the test case timeout may exceed it where Sandbox Fusion would not.
- Standard input is a file, as on Codeforces, where Sandbox Fusion's is a pipe: `os.fstat(0).st_size` gives its size here.

## Development

```bash
pip install -e '.[test]'
pytest                    # unit tests, against a stand-in for hluk
RUN_ALL=1 pytest          # also the tests in real micro-VMs, with the hluk on PATH
```

`HLUK_PATH` names the `hluk` to run instead of the one on `PATH`; a wrapper script must `exec` it, so that a reward worker that dies takes its micro-VMs with it.

By default the micro-VM tests use the `hluk` on `PATH` and its published `python` image. To test a local build instead, set `HLUK_PATH` to its `hluk` and `HLUK_INITRD` to its python rootfs; CI does this so each commit is tested against its own build. `VERL_HYPERLIGHT_TEST_AGENT=1` adds the test that needs the `agent` image. With verl installed, `tests/test_with_verl.py` also runs cases through verl's own scoring, as CI does on its newest leg.
