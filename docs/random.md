# Random numbers

The guest has a cryptographically secure random source with the usual POSIX interfaces, seeded from the CPU (x86_64) or the host (arm64).

## Source

At boot the kernel keys a ChaCha20 generator (Unikraft's `ukrandom`) with 256 bits from the CPU's hardware random-number instructions (RDSEED/RDRAND).  On arm64 those instructions (RNDR) are optional and Apple's cores lack them, so the kernel asks the host instead: `GetRandomBytes`, from the host OS's generator.  `getrandom(2)`, `/dev/urandom` and `/dev/random` read from it and never block, and runtimes use them as on Linux: Python's `os.urandom`, `secrets` and `random` (which seeds itself from `os.urandom`), OpenSSL for TLS.  Each program also gets 16 bytes from it in its auxiliary vector (`AT_RANDOM`), which Go's runtime seeds from.

There is no periodic reseeding thread, and no way to seed the generator deterministically; reproducible randomness is an application concern (`random.Random(n)`).

## Snapshots

A snapshot captures memory, and with it every generator's state: two guests restored from one snapshot would draw the same numbers.  Each restore fixes that in two layers.

| Layer | What it does |
|---|---|
| Kernel | On the `resume` entry, before any guest code runs, the kernel reseeds its generator from the same source as at boot.  Everything that reads it afterwards is fresh in every clone: `getrandom`, `/dev/urandom`, the `AT_RANDOM` of a program started after the restore. |
| Kernel | It also counts restores: `HLCALL_IOC_RESTORES` ([driver.md](driver.md)). |
| Driver | A runtime that seeded generators of its own before the snapshot keeps their state.  Its driver reads the count at the top of each call, and on the first call after a restore reseeds those generators from the kernel's before the call's code runs.  A seed the code gave one of them before the snapshot (`random.seed(42)`, `RANDOM=42`) is replaced too. |

What each image covers:

| Image | Reseeded on the first call after a restore | Fresh by construction |
|---|---|---|
| python | `random`'s module-level functions, `tempfile`'s names, and NumPy's legacy `np.random.*` and the OpenSSL `cryptography` links in, where they are installed | `os.urandom`, `secrets`, `uuid4`, `random.Random()` made after the restore |
| python-shell, agent | as python, and OpenSSL (`ssl`, anything on libcrypto), SQLite's `random()`, `randomblob()` and temporary names | as python |
| node | OpenSSL (`crypto.randomBytes`, `randomUUID`, `randomInt`, Web Crypto) | `Math.random`, which the driver backs with the kernel's generator |
| java | `Math.random`, `StrictMath.random`, `Collections.shuffle`, `ThreadLocalRandom` on the threads calls run on, and the seeds of every new `Random`, `SplittableRandom`, `ThreadLocalRandom` thread and `RandomGenerator` algorithm (`getDefault()`, `of(...)`), `UUID.randomUUID`'s `SecureRandom` (a DRBG reseeded, NativePRNG's read-ahead dropped), and the seeders new DRBGs and SHA1PRNGs draw from | `SecureRandom`'s NativePRNG, the default, which mixes `/dev/urandom` into every draw |
| dotnet-jit | `Random.Shared` on the thread calls run on, OpenSSL (`RandomNumberGenerator`) | `new Random()`, `Guid.NewGuid()` |
| bash | `$RANDOM` | `$RANDOM` in a subshell or a new shell, which BusyBox seeds from the kernel |
| quickjs | | `Math.random`, reseeded on every call |
| wasmtime | | `wasi:random`, read from the kernel on every draw |
| powershell, c, go, rust, dotnet-aot | | each call runs a new process, which seeds from the kernel |

Not covered, in any image: a generator the code made and kept before the snapshot (`random.Random()`, NumPy's `default_rng()`, a Java `Random` or DRBG `SecureRandom` in a variable), `Random.Shared` and `ThreadLocalRandom` on threads other than the calls' own, node's `Math.random` in a context `vm` makes, before the restore or after it (V8 seeds it from a generator the snapshot holds), or in a worker thread started before the snapshot, node's DNS query IDs (c-ares keeps a generator of its own), secrets a runtime drew once at startup (Python's `multiprocessing` authkey), and hash seeds (Python's `str` hashes, .NET's `string.GetHashCode`, the iteration order of Java's `Set.of`), which stay the same in every clone.  The drivers reseed at the start of a call, so work that runs between calls keeps drawing the snapshot's numbers until the next one: a call in flight when the snapshot was taken (a server started with `--guest-exec`), and what a call left running (a thread, a timer, a shell's background job).  Make such generators after the restore, or seed them from `os.urandom`/`getrandom` in the first call.

The bash image's BusyBox is patched so that assigning `RANDOM` seeds `$RANDOM`, as in bash (`RANDOM=42` repeats a sequence), which the driver uses to reseed it, and so that a shell seeds it from the kernel rather than from its pid and the clock.
