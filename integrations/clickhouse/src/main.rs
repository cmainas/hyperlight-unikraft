// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Hyperlight Authors.

//! ClickHouse user-defined functions in Python, run in Hyperlight
//! micro-VMs: a driver for ClickHouse's driver-based executable UDFs.
//!
//! ```sql
//! CREATE FUNCTION py_words ARGUMENTS (s String) RETURNS UInt64
//! ENGINE = HyperlightPython AS 'return len(s.split())'
//! ```
//!
//! - `create` is the driver's `create_command`.  ClickHouse runs it in the
//!   function's own directory, with the signature as arguments and the
//!   body on standard input.  It boots a guest, defines the function in
//!   it, saves the guest's snapshot there and prints the configuration of
//!   an `executable_pool` UDF that runs `serve`.
//! - `serve` is that UDF's command.  It restores the snapshot, then runs
//!   each block ClickHouse sends in a guest restored afresh for it, so
//!   nothing one block leaves behind reaches the next.

mod function;
mod types;

use std::io::{self, BufRead, Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use function::{Function, Guest, MAX_RESULTS_BYTES};
use types::{Type, json_results_to_rowbinary};

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{0}")]
    Usage(String),
    #[error("{0}")]
    Function(String),
    #[error("{0}")]
    Io(#[from] io::Error),
    #[error("{0}")]
    Sandbox(#[from] hyperlight_unikraft::Error),
    /// The function, or the guest running it, failed.
    #[error("{0}")]
    Guest(String),
    /// The function's results do not fit its return type: its name, and
    /// what is wrong with which row's.
    #[error("{0}: {1}")]
    Results(String, String),
}

const USAGE: &str = "usage: clickhouse-hyperlight create --name NAME --return TYPE [--args SIGNATURE] < body\n       \
                     clickhouse-hyperlight serve DIRECTORY\n       \
                     clickhouse-hyperlight prepare DIRECTORY";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match args.first().map(String::as_str) {
        Some("create") => create(&args[1..]),
        Some("serve") if args.len() == 2 => serve(Path::new(&args[1])),
        // What `serve` runs where this build has no snapshot of the function yet, after an upgrade; an operator
        // can run it ahead of the first query
        // `--unless-failed-lately` as `serve` runs it: not again soon after a failure, which each process would
        // otherwise repeat
        Some("prepare") => match &args[1..] {
            [dir] if !dir.starts_with("--") => prepare(Path::new(dir), true),
            [flag, dir] if flag == "--unless-failed-lately" => prepare(Path::new(dir), false),
            _ => Err(Error::Usage(USAGE.into())),
        },
        _ => Err(Error::Usage(USAGE.into())),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            // ClickHouse shows the end of what a failed command wrote to its standard error. Not eprintln!, which
            // panics where that is a closed pipe (a prepare whose serve was stopped): the exit status still tells.
            let _ = writeln!(io::stderr(), "clickhouse-hyperlight: {e}");
            ExitCode::FAILURE
        }
    }
}

fn prepare(dir: &Path, retry: bool) -> Result<(), Error> {
    Function::load(dir)?.prepare(dir, retry)
}

/// The driver's `create_command`: define the function in a guest, save
/// its snapshot in this directory, and print the UDF's configuration.
fn create(args: &[String]) -> Result<(), Error> {
    let (mut name, mut returns, mut signature) = (None, None, String::new());
    let mut options = args.iter();
    while let Some(option) = options.next() {
        let value = options
            .next()
            .ok_or_else(|| Error::Usage(format!("{option} needs a value\n{USAGE}")))?;
        match option.as_str() {
            "--name" => name = Some(value.clone()),
            "--return" => returns = Some(value.clone()),
            "--args" => signature = value.clone(),
            _ => return Err(Error::Usage(format!("unknown option {option}\n{USAGE}"))),
        }
    }
    let name = name.ok_or_else(|| Error::Usage(format!("--name is required\n{USAGE}")))?;
    let returns = returns.ok_or_else(|| Error::Usage(format!("--return is required\n{USAGE}")))?;
    let mut body = String::new();
    io::stdin().read_to_string(&mut body)?;

    let function = Function::new(name, &signature, returns, body)?;
    let dir = std::env::current_dir()?;
    // Checked before the guest boots, which takes the time
    let config = udf_config(&function, &dir)?;
    function.save(&dir)?;
    // A directory of its own: ClickHouse gives each CREATE FUNCTION a new one, CREATE OR REPLACE included
    function.prepare(&dir, true)?;
    // Not print!, which panics where ClickHouse closed the pipe: reported as any other failure
    io::stdout().write_all(config.as_bytes())?;
    Ok(())
}

/// The `executable_pool` UDF that runs `serve` on the function's
/// directory.
fn udf_config(function: &Function, dir: &Path) -> Result<String, Error> {
    let pool_size: u64 = setting("CLICKHOUSE_HYPERLIGHT_POOL_SIZE")?.unwrap_or(16);
    let max_execution_time: u64 =
        setting("CLICKHOUSE_HYPERLIGHT_MAX_EXECUTION_TIME")?.unwrap_or(10);
    // A block's results come all at once, so waiting for them is the block's whole time; and the first block waits
    // to be read while its process starts
    let read_timeout = max_execution_time.checked_mul(1000).ok_or_else(|| {
        Error::Function(format!(
            "CLICKHOUSE_HYPERLIGHT_MAX_EXECUTION_TIME is too large: {max_execution_time}"
        ))
    })?;
    let exe = driver_path()?;
    // Run by `sh -c`: execute_direct would look for the command among ClickHouse's user scripts. With exec, as a
    // shell such as dash forks the command otherwise, and ClickHouse signals the shell alone when a block takes too
    // long: serve would live on, its guest running.
    let command = format!(
        "exec {} serve {}",
        shell_quote(utf8(&exe)?),
        shell_quote(utf8(dir)?)
    );
    let arguments: String = function
        .args
        .iter()
        .map(|(name, ty)| {
            format!(
                "        <argument><name>{}</name><type>{}</type></argument>\n",
                xml(name),
                xml(ty)
            )
        })
        .collect();
    Ok(format!(
        "<functions>
    <function>
        <type>executable_pool</type>
        <name>{name}</name>
        <return_type>{returns}</return_type>
{arguments}        <format>RowBinary</format>
        <command>{command}</command>
        <execute_direct>0</execute_direct>
        <send_chunk_header>1</send_chunk_header>
        <pool_size>{pool_size}</pool_size>
        <max_command_execution_time>{max_execution_time}</max_command_execution_time>
        <command_read_timeout>{read_timeout}</command_read_timeout>
        <command_write_timeout>{read_timeout}</command_write_timeout>
        <command_termination_timeout>1</command_termination_timeout>
    </function>
</functions>
",
        name = xml(&function.name),
        returns = xml(&function.returns),
        command = xml(&command),
    ))
}

/// The UDF's command: run each block ClickHouse sends, in a guest restored
/// for it, until ClickHouse closes the pipe.
fn serve(dir: &Path) -> Result<(), Error> {
    let function = Function::load(dir)?;
    let (args, returns) = function.types()?;
    let mut guest = Guest::start(&function, dir)?;
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    let mut header = String::new();
    let mut results = Vec::new();
    loop {
        // Each block starts with its number of rows, on a line
        header.clear();
        if input.read_line(&mut header)? == 0 {
            return Ok(());
        }
        let rows: usize = header.trim().parse().map_err(|_| {
            Error::Function(format!(
                "a block's header is not a number of rows: {header:?}"
            ))
        })?;
        let block = read_block(&mut input, &args, rows)?;
        let json = guest.run(block)?;
        results.clear();
        json_results_to_rowbinary(&json, &returns, rows, MAX_RESULTS_BYTES, &mut results)
            .map_err(|e| Error::Results(function.name.clone(), e))?;
        output.write_all(&results)?;
        output.flush()?;
        // Not kept at the size of one large block for the process's life, which a pool has many of
        if results.capacity() > 16 << 20 {
            results = Vec::new();
        }
        guest.reset()?;
    }
}

/// A block's rows, in RowBinary, as a JSON array of arrays of arguments.
fn read_block(input: &mut impl BufRead, args: &[Type], rows: usize) -> Result<String, Error> {
    let mut json = vec![b'['];
    let mut text = Vec::new();
    for row in 0..rows {
        if row > 0 {
            json.push(b',');
        }
        json.push(b'[');
        for (i, ty) in args.iter().enumerate() {
            if i > 0 {
                json.push(b',');
            }
            ty.rowbinary_to_json(input, &mut json, &mut text)
                .map_err(|e| {
                    Error::Function(format!(
                        "reading argument {} ({ty}) of row {} of {rows} from ClickHouse: {e}",
                        i + 1,
                        row + 1
                    ))
                })?;
        }
        json.push(b']');
    }
    json.push(b']');
    // Valid UTF-8: written from strings checked as they were read, serde_json's escapes and ASCII
    String::from_utf8(json).map_err(|e| Error::Function(e.to_string()))
}

/// A number of 1 or more from the driver's environment, if set: an empty
/// one, from an element left blank, is not.
pub fn setting<T: std::str::FromStr + PartialOrd + From<u8>>(
    name: &str,
) -> Result<Option<T>, Error> {
    let value = std::env::var(name).unwrap_or_default();
    if value.trim().is_empty() {
        return Ok(None);
    }
    match value.trim().parse() {
        Ok(number) if number >= T::from(1) => Ok(Some(number)),
        _ => Err(Error::Function(format!(
            "{name} is not a number of 1 or more: {value:?}"
        ))),
    }
}

/// This binary, as the driver ran it: the path it gave, or where on `PATH`
/// it found the name it gave.  Not `current_exe`, which resolves symbolic
/// links to a path an upgrade may remove, as a versioned install's is.
pub fn driver_path() -> Result<PathBuf, Error> {
    let argv0 = PathBuf::from(std::env::args_os().next().unwrap_or_default());
    if argv0.is_absolute() {
        return Ok(argv0);
    }
    if argv0.components().count() == 1 {
        // Without PATH, the shell's own default, which the create script's `exec` searched
        let search = std::env::var_os("PATH").unwrap_or_else(|| {
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin".into()
        });
        let found = std::env::split_paths(&search)
            .map(|dir| dir.join(&argv0))
            // As the shell looks: the first that is a file it may run
            .find(|path| {
                path.is_absolute()
                    && path
                        .metadata()
                        .is_ok_and(|file| file.is_file() && file.permissions().mode() & 0o111 != 0)
            });
        // The binary running, not another of the same name the shell did not pick (a hashed lookup, a
        // relative entry): its build's snapshots are another's
        let same_file = |path: &Path| -> std::io::Result<bool> {
            let (file, running) = (
                std::fs::metadata(path)?,
                std::fs::metadata(std::env::current_exe()?)?,
            );
            Ok(file.dev() == running.dev() && file.ino() == running.ino())
        };
        if let Some(path) = found
            && same_file(&path).unwrap_or(false)
        {
            return Ok(path);
        }
    }
    // Relative to a directory other than the one `serve` runs in, or not found where the shell found it
    Ok(std::env::current_exe()?)
}

/// A path as the UDF's configuration can name it: text, as it is not bytes.
fn utf8(path: &Path) -> Result<&str, Error> {
    path.to_str().ok_or_else(|| {
        Error::Function(format!(
            "{} is not UTF-8, which the UDF's configuration cannot name",
            path.display()
        ))
    })
}

fn shell_quote(text: &str) -> String {
    format!("'{}'", text.replace('\'', r"'\''"))
}

fn xml(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}
