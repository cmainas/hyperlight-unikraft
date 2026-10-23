// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Hyperlight Authors.

//! Two guests restored from one snapshot draw different random numbers:
//! the kernel reseeds its CSPRNG on restore (docs/random.md).  Each test
//! uses the generators before the snapshot, so they are seeded and hold
//! state, then draws from them in two restores of it.

mod common;

use common::{BIN_MOUNT, require_bin, require_rootfs, temp_dir};
use hyperlight_unikraft::{Exec, Mount, SandboxBuilder};

/// Boot `builder`, run `warm`, snapshot, and run `draw` in two restores
/// of the snapshot, with `mounts`: what each printed.
fn two_clones_with(
    builder: SandboxBuilder,
    mounts: Vec<Mount>,
    warm: Exec,
    draw: Exec,
) -> [String; 2] {
    let snap_dir = temp_dir("snapshot-random");
    let mut sandbox = builder.mounts(mounts.clone()).boot().unwrap();
    sandbox.run(warm).unwrap();
    sandbox.drain_output();
    sandbox.snapshot_to(&snap_dir).unwrap();
    drop(sandbox);
    [0, 1].map(|_| {
        let mut clone = SandboxBuilder::from_snapshot_dir(&snap_dir)
            .unwrap()
            .mounts(mounts.clone())
            .boot()
            .unwrap();
        clone.run(draw.clone()).unwrap();
        clone.drain_output()
    })
}

/// Each line of the two clones' output differs: one generator a line.
fn assert_each_line_differs(outputs: &[String; 2], generators: &[&str]) {
    let a: Vec<&str> = outputs[0].lines().collect();
    let b: Vec<&str> = outputs[1].lines().collect();
    assert_eq!(a.len(), generators.len(), "{outputs:?}");
    assert_eq!(b.len(), generators.len(), "{outputs:?}");
    for ((x, y), name) in a.iter().zip(&b).zip(generators) {
        assert_ne!(x, y, "two clones drew the same from {name}: {outputs:?}");
    }
}

/// A program the exec driver starts runs after the restore, so what its
/// runtime seeds (Go's math/rand from AT_RANDOM, Rust's HashMap keys from
/// getrandom) seeds from the kernel, reseeded: nothing for a driver to
/// do, but checked.
#[test]
fn go_and_rust_clones_draw_their_own() {
    for (runtime, bin, scratch) in [("go", "random", 128), ("rust", "random", 64)] {
        let bins = require_bin(runtime, bin);
        let builder = SandboxBuilder::from_initrd(require_rootfs(runtime)).scratch_mb(scratch);
        let run = Exec::Guest(format!("{BIN_MOUNT}/{bin}"));
        let outputs = two_clones_with(builder, vec![Mount::ro(&bins, BIN_MOUNT)], run.clone(), run);
        assert_each_line_differs(&outputs, &[runtime]);
    }
}
