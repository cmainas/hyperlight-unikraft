# clickhouse-hyperlight

ClickHouse user-defined functions in Python, run in Hyperlight micro-VMs.

```sql
CREATE FUNCTION py_words ARGUMENTS (s String) RETURNS UInt64
ENGINE = HyperlightPython AS 'return len(s.split())';

SELECT py_words(title) FROM articles;
```

`clickhouse-hyperlight` is a driver for ClickHouse's [driver-based executable UDFs](https://clickhouse.com/docs/sql-reference/functions/udf#driver-based-executable-user-defined-functions) (experimental, ClickHouse 26.6 and later). Any user allowed to run `CREATE FUNCTION` can write a function in Python, and the code never runs on the server itself:

- Each function runs in its own micro-VM, with its own kernel. It has no network, and no access to the server's files.
- Each block of rows starts from a fresh guest, restored from a snapshot in about 1.5 ms. Nothing one query leaves in the guest reaches the next one, even when another user runs it.
- A body that doesn't compile fails `CREATE FUNCTION`, with Python's error. A function that raises fails the query, with its traceback and the row it failed on.

| | executable UDF (Python) | `DockerC` / `GVisorC` drivers | **`HyperlightPython`** |
|---|---|---|---|
| Who writes the code | the operator, in the server's config | any user, in SQL, in C | **any user, in SQL, in Python** |
| Isolation | none: runs as the server's user | container | **micro-VM, its own kernel** |
| State between queries | kept for the pool process's lifetime | kept | **none: each block in a fresh guest** |
| Needs | Python on the server | Docker or gVisor | **KVM or MSHV** |

## Install

Linux only, on a host with KVM, or MSHV with the `mshv` feature.

1. Build the binary and put it on the server's `PATH`:

   ```bash
   cargo build --release --manifest-path integrations/clickhouse/Cargo.toml
   sudo install integrations/clickhouse/target/release/clickhouse-hyperlight /usr/local/bin/
   ```

2. Get the python rootfs. `hluk` pulls it into its cache, and `hluk cache ls` shows where (for a release, the path below; a dev build's is named by its commit):

   ```bash
   hluk run --runtime python --exec 'pass'
   sudo install -D -m 0644 ~/.cache/hluk/rootfs/ghcr.io/hyperlight-dev/hyperlight-unikraft/python/initrd-v$(hluk --version | cut -d' ' -f2).cpio /var/lib/clickhouse/hyperlight/python.cpio
   ```

   The `agent` image works the same way, with numpy, pandas and scikit-learn. It gets the 1536 MiB of guest memory it needs by default, and each function costs more: about 820 MB of disk instead of 75 MB, and up to a minute to create.

3. Let the server's user open `/dev/kvm`: `sudo usermod -aG kvm clickhouse`, then restart the server.

4. Enable drivers in the server's configuration, and copy [`config/`](config/)'s two files into the directory it names:

   ```xml
   <clickhouse>
       <allow_experimental_executable_udf_drivers>true</allow_experimental_executable_udf_drivers>
       <user_defined_executable_function_drivers_config>user_defined_executable_function_drivers_config.d/*_driver.xml</user_defined_executable_function_drivers_config>
   </clickhouse>
   ```

   `SYSTEM RELOAD CONFIG` picks up a new or changed driver.

### Settings

Set them in the driver's `<env>`. Each function takes the values in force when it is created.

| Variable | Default | |
|---|---|---|
| `CLICKHOUSE_HYPERLIGHT_INITRD` | required | The python rootfs, as an absolute path |
| `CLICKHOUSE_HYPERLIGHT_SCRATCH_MB` | the image's (256 for `python`) | Guest memory in MiB |
| `CLICKHOUSE_HYPERLIGHT_POOL_SIZE` | 16 | Guests per function, each in a process of its own |
| `CLICKHOUSE_HYPERLIGHT_MAX_EXECUTION_TIME` | 10 | Seconds a block may take |

## Write functions

The body is the body of a Python function whose parameters are the arguments, written flush left or indented as one block, and it can import modules:

```sql
CREATE FUNCTION py_domain ARGUMENTS (url String) RETURNS Nullable(String)
ENGINE = HyperlightPython AS $$
from urllib.parse import urlsplit
return urlsplit(url).hostname
$$;
```

| ClickHouse | Python |
|---|---|
| `Bool` | `bool` |
| `UInt8` to `UInt64`, `Int8` to `Int64` | `int` |
| `Float32`, `Float64` | `float`, NaN and infinities included |
| `String`, `LowCardinality(String)` | `str` (an argument that is not UTF-8 fails the query) |
| `Nullable(T)` | `None` or `T` |
| `Array(T)` | `list` |

A function may return numpy's scalars and arrays too. Other types, such as `Date` or `Decimal`, are refused when the function is created: convert them in SQL (`toString(d)`, `toFloat64(x)`).

## How it works

- `CREATE FUNCTION` runs the driver's `create` in the function's own directory. It boots a guest from the rootfs, defines the function in its CPython, imports the modules the body imports, and saves the guest's snapshot there, about 75 MB. It then hands ClickHouse an `executable_pool` UDF that runs `clickhouse-hyperlight serve` on that directory. `DROP FUNCTION` removes the directory.
- Each `serve` process restores the snapshot and reads the blocks ClickHouse sends, in RowBinary. Before each block it restores the guest again. The rows go to the guest, and the results come back, as JSON through host function calls, so a block of any size crosses in pieces.
- Each build of `clickhouse-hyperlight` keeps snapshots of its own, and so does each rootfs, told apart by its path (links followed), size and modification time. After an upgrade of either, the first `serve` of each function runs `clickhouse-hyperlight prepare` on its directory, which defines the function again from the saved body and replaces the old snapshot. That takes about a second with `python`. With `agent` it can outlast the first query's timeout, so that query fails, but `prepare` carries on and the next query restores its snapshot. To avoid the failure, run `clickhouse-hyperlight prepare` on each function's directory before the first query, as the server's user (`sudo -u clickhouse`), so that the files it writes stay the server's. When one fails, `serve` does not try again for five minutes, and restores the old snapshot meanwhile, if there is one. Running `prepare` by hand tries again at once.

A function costs about three times what an unsandboxed Python UDF doing the same work costs, mostly from converting each value to JSON and back. A word count, on an 8-core host with KVM and ClickHouse 26.9:

| | `HyperlightPython` | executable UDF (Python, TabSeparated) |
|---|---|---|
| 1 row | 5 ms | 2 ms |
| 1M rows, one stream | 1.6 s | 0.55 s |
| 10M rows, `numbers_mt` | 4.0 s | 1.5 s |

## Known limits

- One vCPU per guest. Parallelism comes from the pool: ClickHouse runs up to `CLICKHOUSE_HYPERLIGHT_POOL_SIZE` guests per function.
- Each of those processes can take the guest's memory (`CLICKHOUSE_HYPERLIGHT_SCRATCH_MB`) and up to about 1 GiB for a block's results (up to 256 MiB of JSON in a buffer that grows by doubling, and 256 MiB in RowBinary) from the host. Size the pool and the scratch for the memory the server can spare for every function at once.
- A block's rows and results are in the guest's memory at once. Raise `CLICKHOUSE_HYPERLIGHT_SCRATCH_MB` for wide rows, or lower `max_block_size` in the query.
- The guest has no network. Packages must be in the rootfs.
- Each block starts from the snapshot, so a random generator made when a module was imported starts each block in the same state. `random`'s and numpy's global ones are seeded again for each block; make others in the body, or seed them from `os.urandom`, which differs in every block.

## Development

```bash
cargo test --release                       # unit tests
CLICKHOUSE_HYPERLIGHT_INITRD=path/to/python-rootfs.cpio \
CLICKHOUSE_BINARY=path/to/clickhouse cargo test --release
```

Without `CLICKHOUSE_HYPERLIGHT_INITRD`, the tests that boot guests are skipped, and without `CLICKHOUSE_BINARY` the test that starts a ClickHouse server is. `just build-rootfs python` builds the rootfs from this repository, at `build-elfloader/python-rootfs.cpio`.
