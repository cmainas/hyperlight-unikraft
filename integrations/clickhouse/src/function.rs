// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Hyperlight Authors.

//! A function as created, and the guest that runs it: booted once from
//! the rootfs to define it, then restored from that guest's snapshot for
//! every block.

use std::fs;
use std::io::Write;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};

use hyperlight_unikraft::{AppSandbox, SandboxBuilder, Snapshot};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::types::{Type, parse_signature};
use crate::{Error, setting};

/// The guest's half: the conversions and the block's call, in Python.
const GUEST: &str = include_str!("guest.py");

/// The guest function call that runs a block.
const BLOCK: &str = "_clickhouse_block";

/// How much of a block's rows each `clickhouse.input` call hands the
/// guest, quoted as the JSON string it returns: within the 69,619 bytes a
/// host function's result may take (docs/calls.md, "Limits").
const PIECE_BYTES: usize = 64 * 1024;

/// The most a block's results may take, as JSON and again in RowBinary:
/// the guest is not trusted with the host's memory.
pub const MAX_RESULTS_BYTES: usize = 256 * 1024 * 1024;

/// What the host keeps of what the guest prints, between blocks.
const OUTPUT_LIMIT: usize = 64 * 1024;

/// The longest a function may take to define, past which `prepare` gives
/// up, so that a define that hangs (an import that never returns) does not
/// hold the lock the next one waits for.
const PREPARE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(600);

/// A function, as `CREATE FUNCTION` described it: saved beside its
/// snapshot, so that a guest can be set up again without it.
#[derive(Debug, Serialize, Deserialize)]
pub struct Function {
    pub name: String,
    /// Names, and types as ClickHouse names them (an alias such as BIGINT
    /// resolved, LowCardinality dropped, as RowBinary carries it plain).
    pub args: Vec<(String, String)>,
    /// As written: ClickHouse takes its aliases in the UDF's configuration.
    pub returns: String,
    pub body: String,
    pub initrd: PathBuf,
    pub scratch_mb: Option<usize>,
}

impl Function {
    pub const FILE: &str = "function.json";

    /// Where this build keeps the function's snapshot in `dir`: a
    /// directory named by what the snapshot holds, so that one of another
    /// build, or of another rootfs at the same path, is never restored.
    pub fn snapshot_dir(&self, dir: &Path) -> Result<PathBuf, Error> {
        // The rootfs as hluk's own snapshot cache tells one from another: by size and time of change
        let rootfs = fs::metadata(&self.initrd)
            .map_err(|e| Error::Function(format!("the rootfs {}: {e}", self.initrd.display())))?;
        let changed = rootfs
            .modified()?
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default();
        // And where it is, its links followed, as hluk's own does: a link pointed at another file of the same
        // size and time names another rootfs
        let resolved = fs::canonicalize(&self.initrd)
            .map_err(|e| Error::Function(format!("the rootfs {}: {e}", self.initrd.display())))?;
        let rootfs = hex(Sha256::new()
            .chain_update(resolved.as_os_str().as_encoded_bytes())
            .chain_update(rootfs.len().to_le_bytes())
            .chain_update(changed.as_nanos().to_le_bytes())
            .finalize()
            .as_slice());
        Ok(dir.join(format!("snapshot-{}-{rootfs}", build_id())))
    }

    /// The snapshot this build saved of whatever rootfs it was: what to
    /// restore while the rootfs cannot be read, and so not checked.
    fn any_snapshot_of_this_build(dir: &Path) -> Result<Option<PathBuf>, Error> {
        let prefix = format!("snapshot-{}-", build_id());
        let mut newest = None;
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            // A directory: a failed prepare's marker has the same prefix
            if entry.file_name().to_string_lossy().starts_with(&prefix)
                && entry.file_type()?.is_dir()
            {
                // The latest saved, where a cleanup that failed left more than one
                let saved = entry.metadata()?.modified()?;
                if newest.as_ref().is_none_or(|(when, _)| saved > *when) {
                    newest = Some((saved, entry.path()));
                }
            }
        }
        Ok(newest.map(|(_, path)| path))
    }

    /// Define the function in a fresh guest and save its snapshot in
    /// `dir`, unless another process did while this one waited its turn
    /// (or, unless `retry`, failed to lately).  Written aside, then renamed
    /// into place, so a process stopped halfway leaves no snapshot rather
    /// than half of one; other snapshots are removed.  A failure leaves a
    /// marker saying why, for the `serve` processes to come.
    pub fn prepare(&self, dir: &Path, retry: bool) -> Result<(), Error> {
        let lock = lock_file(dir)?;
        // One process at a time: the others would only do the same work
        lock.lock()?;
        let path = self.snapshot_dir(dir)?;
        if path.is_dir() || (!retry && failed_lately(&path)) {
            return Ok(());
        }
        self.save_snapshot(dir, &path).inspect_err(|e| {
            // The failure is what to report, whether or not the marker can be written
            fs::write(failed_marker(&path), e.to_string()).ok();
        })
    }

    /// [`prepare`](Self::prepare)'s work, under its lock.
    fn save_snapshot(&self, dir: &Path, path: &Path) -> Result<(), Error> {
        let marker = failed_marker(path);
        let saved = path.to_path_buf();
        // Its sender goes when this returns, which ends the watchdog's wait early
        let (_running, finished) = std::sync::mpsc::channel::<()>();
        std::thread::spawn(move || {
            // Nor once the snapshot is in place, in the moment between its rename and the sender's drop
            if finished.recv_timeout(PREPARE_TIMEOUT)
                != Err(std::sync::mpsc::RecvTimeoutError::Timeout)
                || saved.is_dir()
            {
                return;
            }
            let why = format!(
                "defining the function took more than {} s",
                PREPARE_TIMEOUT.as_secs()
            );
            // Not tried again for a while, as for any failure
            fs::write(&marker, &why).ok();
            // Not eprintln!, which panics where standard error is a closed pipe (the serve that ran this was
            // stopped): the exit must come, as the lock goes with the process
            let _ = writeln!(std::io::stderr(), "clickhouse-hyperlight: {why}");
            std::process::exit(1);
        });
        // What a stopped process left half written
        let aside = dir.join("snapshot.partial");
        if aside.exists() {
            fs::remove_dir_all(&aside)?;
        }
        self.define()?.snapshot_to(&aside)?;
        // On disk before it is named: a crash after the rename must not leave a snapshot torn, which no
        // prepare would look at again, the directory being there
        sync_tree(&aside)?;
        fs::rename(&aside, path)?;
        // Saved: the watchdog has nothing left to stop, past here
        drop(_running);
        fs::File::open(dir)?.sync_all()?;
        // Then the other snapshots, of other builds or rootfs, which this build never restores, and their
        // failures; not before, so that a define that fails leaves the old one to restore. A process already
        // running from one has it loaded. As far as it goes: the snapshot is saved, so this prepare succeeded.
        for entry in fs::read_dir(dir)?.flatten() {
            if !entry.file_name().to_string_lossy().starts_with("snapshot-")
                || entry.path() == *path
            {
                continue;
            }
            if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                fs::remove_dir_all(entry.path()).ok();
            } else if entry.path().extension().is_some_and(|e| e == "failed") {
                fs::remove_file(entry.path()).ok();
            }
        }
        Ok(())
    }

    /// The types of its arguments and result, checked.
    pub fn types(&self) -> Result<(Vec<Type>, Type), Error> {
        let args = self
            .args
            .iter()
            .map(|(_, ty)| Type::parse(ty))
            .collect::<Result<_, _>>()
            .map_err(Error::Function)?;
        Ok((args, Type::parse(&self.returns).map_err(Error::Function)?))
    }

    pub fn new(
        name: String,
        signature: &str,
        returns: String,
        body: String,
    ) -> Result<Self, Error> {
        let args: Vec<(String, String)> = parse_signature(signature)
            .map_err(Error::Function)?
            .into_iter()
            .map(|(name, ty)| (name, ty.to_string()))
            .collect();
        // Which XML, where the UDF's configuration names them, cannot carry even escaped
        for text in [&name, &returns]
            .into_iter()
            .chain(args.iter().map(|(name, _)| name))
        {
            if text
                .chars()
                .any(|c| c.is_control() || c == '\u{fffe}' || c == '\u{ffff}')
            {
                return Err(Error::Function(format!(
                    "{text:?} has a control character, which the UDF's configuration cannot hold"
                )));
            }
        }
        let initrd = std::env::var_os("CLICKHOUSE_HYPERLIGHT_INITRD").map(PathBuf::from).ok_or_else(|| {
            Error::Function(
                "CLICKHOUSE_HYPERLIGHT_INITRD is not set: set it, in the driver's configuration, to a python rootfs \
                 of hluk's"
                    .into(),
            )
        })?;
        // Absolute: `create` runs in the function's directory and `prepare` where `serve` does, so a relative path
        // would name a different file in each. Kept with its symbolic links as they are, so that a link an upgrade
        // points elsewhere still names the rootfs.
        if !initrd.is_absolute() {
            return Err(Error::Function(format!(
                "CLICKHOUSE_HYPERLIGHT_INITRD is not an absolute path: {}",
                initrd.display()
            )));
        }
        match fs::metadata(&initrd) {
            Ok(file) if file.is_file() => {}
            Ok(_) => {
                return Err(Error::Function(format!(
                    "CLICKHOUSE_HYPERLIGHT_INITRD {} is not a file",
                    initrd.display()
                )));
            }
            Err(e) => {
                return Err(Error::Function(format!(
                    "CLICKHOUSE_HYPERLIGHT_INITRD {}: {e}",
                    initrd.display()
                )));
            }
        }
        let scratch_mb = setting("CLICKHOUSE_HYPERLIGHT_SCRATCH_MB")?;
        let function = Function {
            name,
            args,
            returns,
            body,
            initrd,
            scratch_mb,
        };
        function.types()?;
        Ok(function)
    }

    pub fn load(dir: &Path) -> Result<Self, Error> {
        let file = dir.join(Self::FILE);
        let text = fs::read_to_string(&file)
            .map_err(|e| Error::Function(format!("{}: {e}", file.display())))?;
        serde_json::from_str(&text).map_err(|e| Error::Function(format!("{}: {e}", Self::FILE)))
    }

    pub fn save(&self, dir: &Path) -> Result<(), Error> {
        let text =
            serde_json::to_string_pretty(self).map_err(|e| Error::Function(e.to_string()))?;
        fs::write(dir.join(Self::FILE), text)?;
        Ok(())
    }

    /// Boot a guest from the rootfs and define the function in it: how a
    /// function is created, and set up again where its snapshot cannot be
    /// restored (one saved by another build).  A body that does not compile
    /// fails here, with Python's error.
    pub fn define(&self) -> Result<AppSandbox, Error> {
        let exchange = Arc::new(Mutex::new(Exchange::default()));
        let mut builder = exchange_functions(SandboxBuilder::from_initrd(&self.initrd), &exchange);
        if let Some(mb) = self.scratch_mb {
            builder = builder.scratch_mb(mb);
        }
        let mut sandbox = builder.boot()?;
        let (args, returns) = self.types()?;
        let spec = serde_json::json!({
            "name": self.name,
            "args": self.args.iter().zip(&args).map(|((name, _), ty)| (name, ty.descriptor())).collect::<Vec<_>>(),
            "return": returns.descriptor(),
            "returns": self.returns,
            "body": self.body,
        });
        // Through the exchange, as a block's rows are: a body may be longer than one call carries
        exchange.lock().unwrap().begin(spec.to_string());
        sandbox.drain_output();
        if let Err(e) = sandbox.run(format!("{GUEST}\n_clickhouse_define()\n")) {
            return Err(failure(&sandbox, e));
        }
        sandbox.drain_output();
        Ok(sandbox)
    }
}

/// A call that failed in the guest: what Python printed of it, a
/// SyntaxError or an exception the guest's code did not catch, else the
/// call's own error.
fn failure(sandbox: &AppSandbox, error: hyperlight_unikraft::Error) -> Error {
    let stderr = sandbox.drain_stderr();
    // The driver's own last word, which says nothing more: older images print it after every
    // failed call, newer ones only when the driver itself fails
    let stderr = stderr
        .trim_end()
        .trim_end_matches("hl_driver: dispatch callback failed")
        .trim_end();
    Error::Guest(match error {
        hyperlight_unikraft::Error::CallFailed { status } if stderr.is_empty() => {
            format!(
                "the function ended its guest's call with status {status} (sys.exit, for example)"
            )
        }
        _ if stderr.is_empty() => error.to_string(),
        _ => stderr.to_string(),
    })
}

/// A block on its way in and its results on their way out, through the
/// host functions the guest calls.
#[derive(Default)]
pub struct Exchange {
    rows: String,
    sent: usize,
    results: String,
    error: Option<String>,
}

impl Exchange {
    /// Hand the guest a block's rows, a JSON array of arrays, or the
    /// function's description, to define it.
    pub fn begin(&mut self, rows: String) {
        *self = Exchange {
            rows,
            ..Exchange::default()
        };
    }

    /// The block's results, or the error the function raised.
    pub fn finish(&mut self) -> Result<String, String> {
        let exchange = std::mem::take(self);
        exchange.error.map_or(Ok(exchange.results), Err)
    }

    /// The next piece of the rows, as a JSON string; the empty one once
    /// all were sent.
    fn next_piece(&mut self) -> Result<String, String> {
        let rest = &self.rows[self.sent..];
        let mut cost = 2;
        let mut end = 0;
        for (i, c) in rest.char_indices() {
            cost += match c {
                '"' | '\\' => 2,
                // Not in JSON text, which escapes them in its strings; costed as quoting them would
                c if (c as u32) < 0x20 => 6,
                c => c.len_utf8(),
            };
            if cost > PIECE_BYTES {
                break;
            }
            end = i + c.len_utf8();
        }
        self.sent += end;
        serde_json::to_string(&rest[..end]).map_err(|e| e.to_string())
    }
}

/// The host functions the guest exchanges a block through, on `builder`.
fn exchange_functions(builder: SandboxBuilder, exchange: &Arc<Mutex<Exchange>>) -> SandboxBuilder {
    let (input, output, error) = (exchange.clone(), exchange.clone(), exchange.clone());
    builder
        .echo_output(false)
        .output_limit(OUTPUT_LIMIT)
        .host_function("clickhouse.input", move |_| {
            input.lock().unwrap().next_piece()
        })
        .host_function("clickhouse.output", move |args| {
            let [piece]: [String; 1] = serde_json::from_str(args).map_err(|e| e.to_string())?;
            let mut exchange = output.lock().unwrap();
            if exchange.results.len() + piece.len() > MAX_RESULTS_BYTES {
                return Err(format!(
                    "a block's results take more than {MAX_RESULTS_BYTES} bytes"
                ));
            }
            exchange.results.push_str(&piece);
            Ok(String::new())
        })
        .host_function("clickhouse.error", move |args| {
            let [message]: [String; 1] = serde_json::from_str(args).map_err(|e| e.to_string())?;
            error.lock().unwrap().error = Some(message);
            Ok(String::new())
        })
}

/// What a snapshot of this build holds, as a short hex string: the kernel
/// and the host contract it runs on, and this crate's guest code and host
/// functions.
fn build_id() -> &'static str {
    static ID: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    ID.get_or_init(|| {
        hex(&Sha256::new()
            .chain_update(hyperlight_unikraft::SNAPSHOT_KEY)
            .chain_update(GUEST)
            // And what tells the guest the function: the description `define` sends and the types' descriptors
            .chain_update(include_str!("function.rs"))
            .chain_update(include_str!("types.rs"))
            .chain_update(env!("CARGO_PKG_VERSION"))
            .finalize())
    })
}

/// Flush `dir`, its files and its subdirectories to disk.
fn sync_tree(dir: &Path) -> Result<(), Error> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            sync_tree(&entry.path())?;
        } else {
            fs::File::open(entry.path())?.sync_all()?;
        }
    }
    fs::File::open(dir)?.sync_all()?;
    Ok(())
}

/// What a `prepare` that failed for a snapshot leaves beside where it
/// would be, with why.
fn failed_marker(snapshot: &Path) -> PathBuf {
    snapshot.with_extension("failed")
}

/// Whether a `prepare` for `snapshot` failed within [`RETRY_AFTER`].
fn failed_lately(snapshot: &Path) -> bool {
    fs::metadata(failed_marker(snapshot))
        .and_then(|marker| marker.modified())
        .is_ok_and(|when| when.elapsed().is_ok_and(|age| age < RETRY_AFTER))
}

/// How long after a failed `prepare` the next is tried.
const RETRY_AFTER: std::time::Duration = std::time::Duration::from_secs(300);

/// The lock that `prepare` takes to save a snapshot in `dir`.
fn lock_file(dir: &Path) -> Result<fs::File, Error> {
    let file = dir.join("snapshot.lock");
    fs::File::create(&file).map_err(|e| Error::Function(format!("{}: {e}", file.display())))
}

/// The first 8 bytes of a digest, in hex.
fn hex(digest: &[u8]) -> String {
    digest[..8].iter().map(|b| format!("{b:02x}")).collect()
}

/// A guest with the function defined, and the snapshot each block starts
/// from.
pub struct Guest {
    sandbox: AppSandbox,
    snapshot: Arc<Snapshot>,
    exchange: Arc<Mutex<Exchange>>,
}

impl Guest {
    /// The function's guest, restored from this build's snapshot in `dir`.
    /// Where there is none yet, after an upgrade, `prepare` saves one first,
    /// in a process of its own: ClickHouse stops this one when its first
    /// block takes too long, but not that, so the next query finds it.
    pub fn start(function: &Function, dir: &Path) -> Result<Self, Error> {
        let mut path = match function.snapshot_dir(dir) {
            Ok(path) => path,
            // Moved, say: nothing to prepare from, but a snapshot holds all a guest needs
            Err(e) => Function::any_snapshot_of_this_build(dir)?.ok_or(e)?,
        };
        if !path.is_dir() {
            // Waited for here while another process prepares, where the wait ends with this process if ClickHouse
            // stops it; then looked at again
            let lock = lock_file(dir)?;
            lock.lock()?;
            // Unless it failed lately, for this rootfs and build: each process need not try again, but a failure
            // that passes (/dev/kvm busy) does not keep the function from ever being prepared
            let pending = !path.is_dir() && !failed_lately(&path);
            lock.unlock()?;
            // What this process's own prepare said, if it ran one and it left no marker. Another process may run
            // one at the same time: whichever takes the lock second finds the snapshot and leaves it.
            let ran = pending.then(|| Guest::prepare(dir));
            // Where a later prepare saved one, or failed to, if the rootfs changed again meanwhile
            if let Ok(now) = function.snapshot_dir(dir)
                && (now.is_dir() || failed_marker(&now).exists())
            {
                path = now;
            }
            if !path.is_dir() {
                // A rootfs this function does not run on, say: the old one's snapshot still serves, if any
                // The marker, which a prepare that ran writes; else what this process's own said, if it ran one
                // and was stopped before it wrote one, rather than an older marker's
                let marker = fs::read_to_string(failed_marker(&path)).ok();
                let why = match (marker, ran) {
                    (Some(why), _) if failed_lately(&path) => why,
                    (_, Some(Ok(why))) => why,
                    (_, Some(Err(e))) => e.to_string(),
                    (Some(why), None) => why,
                    (None, None) => "no reason given".into(),
                };
                let failed = Error::Guest(format!("the function could not be defined: {why}"));
                path = Function::any_snapshot_of_this_build(dir)?.ok_or(failed)?;
            }
        }
        // Loaded under the lock, shared: a prepare's cleanup, which takes it alone, cannot remove the snapshot
        // while it is read. Chosen again under it, if a prepare removed it since.
        let lock = lock_file(dir)?;
        lock.lock_shared()?;
        if !path.is_dir() {
            path = function
                .snapshot_dir(dir)
                .ok()
                .filter(|now| now.is_dir())
                .map_or_else(
                    || Function::any_snapshot_of_this_build(dir),
                    |now| Ok(Some(now)),
                )?
                .ok_or_else(|| Error::Guest("the function's snapshot was removed".into()))?;
        }
        let snapshot = hyperlight_unikraft::load_snapshot(&path)?;
        drop(lock);
        let exchange = Arc::new(Mutex::new(Exchange::default()));
        let builder = SandboxBuilder::from_snapshot(snapshot.clone());
        let sandbox = exchange_functions(builder, &exchange).boot()?;
        Ok(Guest {
            sandbox,
            snapshot,
            exchange,
        })
    }

    /// Run `prepare` on `dir` in a process of its own, out of the group
    /// ClickHouse's signals reach, and wait for it.  Whether it succeeds is
    /// told by what it leaves in `dir`; what it said, and how it ended, for
    /// when it leaves nothing (killed, say).
    fn prepare(dir: &Path) -> Result<String, Error> {
        let output = Command::new(crate::driver_path()?)
            .args(["prepare", "--unless-failed-lately"])
            .arg(dir)
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .output()?;
        if output.status.success() {
            // Without saving where this process looks: the rootfs changed again while it ran
            return Ok("the rootfs changed while the function was being prepared: the next query tries again".into());
        }
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stderr = stderr.trim_end();
        Ok(format!(
            "prepare ended with {}{}{}",
            output.status,
            if stderr.is_empty() { "" } else { ": " },
            stderr
                .strip_prefix("clickhouse-hyperlight: ")
                .unwrap_or(stderr)
        ))
    }

    /// Restore the guest from the snapshot, for the next block: done once
    /// a block's results are on their way, while ClickHouse makes the next.
    pub fn reset(&mut self) -> Result<(), Error> {
        Ok(self.sandbox.restore(self.snapshot.clone())?)
    }

    /// Run the function on a block, `rows` as a JSON array of arrays, and
    /// return the results as JSON.  The guest is as the snapshot left it,
    /// fresh or [`reset`](Self::reset).
    pub fn run(&mut self, rows: String) -> Result<String, Error> {
        self.sandbox.drain_output();
        self.exchange.lock().unwrap().begin(rows);
        let call = self.sandbox.call(BLOCK, "");
        let finished = self.exchange.lock().unwrap().finish();
        match (call, finished) {
            (_, Err(message)) => Err(Error::Guest(message)),
            (Err(e), Ok(_)) => Err(failure(&self.sandbox, e)),
            (Ok(_), Ok(results)) => Ok(results),
        }
    }
}
