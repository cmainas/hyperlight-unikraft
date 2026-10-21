// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Hyperlight Authors.

//! What both test files share: the python rootfs the tests need, and
//! `create` run as ClickHouse runs it.

#![allow(dead_code)]

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

pub const BIN: &str = env!("CARGO_BIN_EXE_clickhouse-hyperlight");

/// The python rootfs to run functions in, from CLICKHOUSE_HYPERLIGHT_INITRD;
/// None, and a note, where it is not set: the tests that boot guests are
/// skipped.
pub fn initrd() -> Option<PathBuf> {
    let initrd = std::env::var_os("CLICKHOUSE_HYPERLIGHT_INITRD").map(PathBuf::from);
    if initrd.is_none() {
        eprintln!("skipped: set CLICKHOUSE_HYPERLIGHT_INITRD to a python rootfs to run the guests");
    }
    initrd
}

/// Run `create` in `dir`, the body on its standard input, as ClickHouse's
/// driver does.
pub fn create(
    dir: &Path,
    initrd: &Path,
    name: &str,
    args: &str,
    returns: &str,
    body: &str,
) -> Output {
    let mut child = Command::new(BIN)
        .args([
            "create", "--name", name, "--return", returns, "--args", args,
        ])
        .current_dir(dir)
        .env("CLICKHOUSE_HYPERLIGHT_INITRD", initrd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(body.as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}

pub fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}
