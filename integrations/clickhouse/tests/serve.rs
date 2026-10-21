// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Hyperlight Authors.

//! `create` and `serve` without ClickHouse: blocks written in RowBinary by
//! hand, results read back the same way.  Each test boots guests, so needs
//! KVM (or MSHV) and CLICKHOUSE_HYPERLIGHT_INITRD.

mod common;

use std::io::Write;
use std::path::Path;
use std::process::{Command, Output, Stdio};

use common::{BIN, create, initrd, stderr};

/// `serve` the function in `dir` on `input`, ClickHouse's side of the pipe.
fn serve(dir: &Path, input: &[u8]) -> Output {
    let mut child = Command::new(BIN)
        .arg("serve")
        .arg(dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let input = input.to_vec();
    // From a thread of its own: serve answers each block before it reads the next
    let writer = std::thread::spawn(move || stdin.write_all(&input));
    let output = child.wait_with_output().unwrap();
    writer.join().unwrap().ok();
    output
}

/// A block of `rows` rows, each argument already in RowBinary.
fn block(rows: &[Vec<u8>]) -> Vec<u8> {
    let mut out = format!("{}\n", rows.len()).into_bytes();
    for row in rows {
        out.extend_from_slice(row);
    }
    out
}

fn string(text: &str) -> Vec<u8> {
    assert!(text.len() < 0x80, "a one-byte length");
    let mut out = vec![text.len() as u8];
    out.extend_from_slice(text.as_bytes());
    out
}

fn create_ok(dir: &Path, initrd: &Path, args: &str, returns: &str, body: &str) {
    let output = create(dir, initrd, "f", args, returns, body);
    assert!(
        output.status.success(),
        "create failed: {}",
        stderr(&output)
    );
    let config = String::from_utf8(output.stdout).unwrap();
    assert!(config.contains("<format>RowBinary</format>"), "{config}");
    // exec'd by the shell, so that ClickHouse's signal reaches serve itself
    assert!(config.contains("<command>exec '"), "{config}");
    assert_eq!(snapshots(dir).len(), 1);
    assert!(dir.join("function.json").is_file());
}

/// The snapshot directories in a function's directory, one per build.
fn snapshots(dir: &Path) -> Vec<String> {
    std::fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .filter(|name| name.starts_with("snapshot-"))
        .collect()
}

#[test]
fn runs_blocks() {
    let Some(initrd) = initrd() else { return };
    let dir = tempfile::tempdir().unwrap();
    create_ok(
        dir.path(),
        &initrd,
        "s String, n UInt64",
        "String",
        "return s * n",
    );
    let mut input = block(&[[string("ab"), 3u64.to_le_bytes().to_vec()].concat()]);
    input.extend(block(&[
        [string("é"), 1u64.to_le_bytes().to_vec()].concat(),
        [string("x"), 0u64.to_le_bytes().to_vec()].concat(),
    ]));
    let output = serve(dir.path(), &input);
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(output.stdout, b"\x06ababab\x02\xc3\xa9\x00");
}

#[test]
fn starts_each_block_from_the_snapshot() {
    let Some(initrd) = initrd() else { return };
    let dir = tempfile::tempdir().unwrap();
    // What one block leaves in the guest, the next does not see
    let body = "global seen\ntry:\n    seen += 1\nexcept NameError:\n    seen = 1\nreturn seen";
    create_ok(dir.path(), &initrd, "x UInt8", "UInt8", body);
    let mut input = block(&[vec![0], vec![0], vec![0]]);
    input.extend(block(&[vec![0], vec![0]]));
    let output = serve(dir.path(), &input);
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(output.stdout, [1, 2, 3, 1, 2]);

    // But random numbers are not replayed: the generator imported before the snapshot is seeded again per block
    let dir = tempfile::tempdir().unwrap();
    let body = "import random\nreturn random.getrandbits(64)";
    create_ok(dir.path(), &initrd, "x UInt8", "UInt64", body);
    let mut input = block(&[vec![0]]);
    input.extend(block(&[vec![0]]));
    let output = serve(dir.path(), &input);
    assert!(output.status.success(), "{}", stderr(&output));
    let (first, second) = output.stdout.split_at(8);
    assert_ne!(first, second);
    // Nor across processes
    let again = serve(dir.path(), &block(&[vec![0]]));
    assert!(again.status.success(), "{}", stderr(&again));
    assert_eq!(again.stdout.len(), 8);
    assert_ne!(again.stdout, first);
}

#[test]
fn moves_blocks_larger_than_a_call() {
    let Some(initrd) = initrd() else { return };
    let dir = tempfile::tempdir().unwrap();
    create_ok(
        dir.path(),
        &initrd,
        "n UInt32",
        "Array(Nullable(Float64))",
        "return [None, float('nan'), *range(n)]",
    );
    // Some 3 MB of results, past the 64 KiB a guest function call carries, and quotes in the rows
    let n = 200_000u32;
    let output = serve(dir.path(), &block(&[n.to_le_bytes().to_vec()]));
    assert!(output.status.success(), "{}", stderr(&output));
    let out = &output.stdout;
    // A three-byte length, then a NULL, a NaN, and 0 to n - 1
    assert_eq!(out[..3], [0xc2, 0x9a, 0x0c]);
    assert_eq!(out[3], 1);
    assert!(f64::from_le_bytes(out[5..13].try_into().unwrap()).is_nan());
    let last = &out[out.len() - 8..];
    assert_eq!(
        f64::from_le_bytes(last.try_into().unwrap()),
        f64::from(n - 1)
    );
    assert_eq!(out.len(), 3 + 1 + 9 * (n as usize + 1));

    let dir = tempfile::tempdir().unwrap();
    create_ok(dir.path(), &initrd, "s String", "UInt64", "return len(s)");
    let long = format!("\"\\{}", "\"".repeat(100_000));
    let mut row = Vec::new();
    // The length as a varint, 100,002 bytes
    row.extend_from_slice(&[0xa2, 0x8d, 0x06]);
    row.extend_from_slice(long.as_bytes());
    let output = serve(dir.path(), &block(&[row]));
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(output.stdout, 100_002u64.to_le_bytes());
}

#[test]
fn reports_what_fails() {
    let Some(initrd) = initrd() else { return };
    let dir = tempfile::tempdir().unwrap();
    let output = create(dir.path(), &initrd, "f", "x UInt8", "UInt8", "return x +");
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("SyntaxError"),
        "{}",
        stderr(&output)
    );

    let output = create(
        dir.path(),
        &initrd,
        "f",
        "x Decimal(10, 2)",
        "UInt8",
        "return 1",
    );
    assert!(
        stderr(&output).contains("unsupported type Decimal(10, 2)"),
        "{}",
        stderr(&output)
    );

    let dir = tempfile::tempdir().unwrap();
    create_ok(
        dir.path(),
        &initrd,
        "x UInt8",
        "UInt8",
        "if x == 7:\n    raise ValueError('no sevens')\nreturn x * 100",
    );
    let output = serve(dir.path(), &block(&[vec![1], vec![7]]));
    assert!(!output.status.success());
    let error = stderr(&output);
    assert!(
        error.contains("ValueError: no sevens")
            && error.contains("f failed on row 2 of the block, [7]"),
        "{error}"
    );

    let output = serve(dir.path(), &block(&[vec![1], vec![3]]));
    assert!(
        stderr(&output)
            .ends_with("f: the result on row 2 of the block: 300 is out of range for UInt8\n"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn defines_the_function_again_for_another_build() {
    let Some(initrd) = initrd() else { return };
    let dir = tempfile::tempdir().unwrap();
    create_ok(dir.path(), &initrd, "x Int64", "Int64", "return -x");
    // As another build would have left it: a snapshot this build does not restore
    let [ours] = snapshots(dir.path()).try_into().unwrap();
    let other = dir.path().join("snapshot-0000000000000000");
    std::fs::rename(dir.path().join(&ours), &other).unwrap();
    let input = block(&[i64::MIN.wrapping_add(1).to_le_bytes().to_vec()]);
    let output = serve(dir.path(), &input);
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(output.stdout, i64::MAX.to_le_bytes());
    // This build's saved for the next process, and the other's gone
    assert_eq!(snapshots(dir.path()), [ours]);
    assert_eq!(serve(dir.path(), &input).stdout, i64::MAX.to_le_bytes());

    // A rootfs moved away: the snapshot still restores
    let file = dir.path().join("function.json");
    let text = std::fs::read_to_string(&file).unwrap();
    let moved = text.replace(initrd.to_str().unwrap(), "/nonexistent/python.cpio");
    assert_ne!(text, moved);
    std::fs::write(&file, moved).unwrap();
    let output = serve(dir.path(), &input);
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(output.stdout, i64::MAX.to_le_bytes());
}

#[test]
fn keeps_the_old_snapshot_when_a_new_rootfs_fails() {
    let Some(initrd) = initrd() else { return };
    let dir = tempfile::tempdir().unwrap();
    create_ok(dir.path(), &initrd, "x Int64", "Int64", "return -x");
    // A rootfs that does not boot, in place of the one the snapshot is of
    let broken = dir.path().join("broken.cpio");
    std::fs::write(&broken, b"not a cpio").unwrap();
    let file = dir.path().join("function.json");
    let text = std::fs::read_to_string(&file).unwrap();
    std::fs::write(
        &file,
        text.replace(initrd.to_str().unwrap(), broken.to_str().unwrap()),
    )
    .unwrap();
    let input = block(&[7i64.to_le_bytes().to_vec()]);
    for _ in 0..2 {
        // The first tries the new rootfs and fails; the second does not try again
        let output = serve(dir.path(), &input);
        assert!(output.status.success(), "{}", stderr(&output));
        assert_eq!(output.stdout, (-7i64).to_le_bytes());
    }
    let markers = std::fs::read_dir(dir.path())
        .unwrap()
        .filter(|entry| {
            entry
                .as_ref()
                .unwrap()
                .path()
                .extension()
                .is_some_and(|e| e == "failed")
        })
        .count();
    assert_eq!(markers, 1);
    assert_eq!(
        snapshots(dir.path()).len(),
        2,
        "the old one, and the failure"
    );
}

#[test]
fn defines_bodies_as_written() {
    let Some(initrd) = initrd() else { return };
    let dir = tempfile::tempdir().unwrap();
    // Named after a builtin it calls, with a string whose lines no indenting may touch, in a body indented whole
    let body = "    t = \"\"\"a\n  b\"\"\"\n    return len(s + t)\n";
    let output = create(dir.path(), &initrd, "len", "s String", "UInt64", body);
    assert!(output.status.success(), "{}", stderr(&output));
    let output = serve(dir.path(), &block(&[string("xy")]));
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(output.stdout, 7u64.to_le_bytes());

    // Calling itself by its name, with a body longer than one guest function call carries
    let dir = tempfile::tempdir().unwrap();
    let body = format!(
        "pad = '{}'\nreturn 1 if n <= 1 else n * fact(n - 1)",
        "x".repeat(100_000)
    );
    let output = create(dir.path(), &initrd, "fact", "n UInt64", "UInt64", &body);
    assert!(output.status.success(), "{}", stderr(&output));
    let output = serve(dir.path(), &block(&[5u64.to_le_bytes().to_vec()]));
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(output.stdout, 120u64.to_le_bytes());

    // A dedented else, which the block an indented body is parsed in would take for its own and drop
    let dir = tempfile::tempdir().unwrap();
    let body = "    return 1\nelse:\n    return 2";
    let output = create(dir.path(), &initrd, "f", "x UInt8", "UInt8", body);
    assert!(
        stderr(&output).contains("IndentationError"),
        "{}",
        stderr(&output)
    );

    // One list, changed and returned on every row: each row keeps what it was then
    let dir = tempfile::tempdir().unwrap();
    let body =
        "global kept\nkept = globals().get('kept', [])\nkept.clear()\nkept.append(x)\nreturn kept";
    create_ok(dir.path(), &initrd, "x UInt8", "Array(UInt8)", body);
    let output = serve(dir.path(), &block(&[vec![1], vec![2], vec![3]]));
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(output.stdout, [1, 1, 1, 2, 1, 3]);

    let dir = tempfile::tempdir().unwrap();
    let output = create(
        dir.path(),
        &initrd,
        "f",
        "from String",
        "String",
        "return 1",
    );
    assert!(
        stderr(&output).contains("argument 'from' is not a name Python can use"),
        "{}",
        stderr(&output)
    );
}
