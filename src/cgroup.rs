// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Hyperlight Authors.

//! The memory limit `hluk` runs under, for sizing a guest.
//!
//! The limit is the memory cgroup's: `memory.max` of the process's cgroup
//! under cgroup v2, `memory.limit_in_bytes` under v1.  A limit may sit on
//! an ancestor instead (a pod's, with the container's left open; a systemd
//! slice's `MemoryMax` on a host), so every cgroup up to the hierarchy's
//! mount is read and the tightest counts, as the kernel enforces each of
//! them alike.  `/proc/self/cgroup` names the process's cgroup from the
//! hierarchy's root, and `/proc/self/mountinfo` says where the hierarchy
//! is mounted and from which of its directories: a container's runtime
//! mounts the container's own cgroup, so what is above it is out of sight
//! and the walk ends at the container's limit, while a cgroup below it,
//! made inside the container for a child, is read first.

#[cfg(any(target_os = "linux", test))]
use std::path::{Path, PathBuf};

/// Memory `hluk` keeps for itself out of a memory limit when it sizes a
/// guest by it, in MiB.  The host side of a guest measures about 15 MiB
/// of anonymous memory beyond what the guest touched, booting from a
/// rootfs or restored from a snapshot; what else the host maps, the
/// initrd and the snapshot, is file-backed and reclaimable.  Saving a
/// snapshot is another matter: the blob is built in memory, so a save
/// takes about as much again as the guest has used, which this does not
/// cover.
pub const CONTAINER_SCRATCH_HEADROOM_MB: usize = 64;

/// The memory limit this process runs under, in MiB, or `None` when
/// there is none: no cgroup memory limit at any level in sight, or not
/// Linux.
pub fn cgroup_memory_limit_mb() -> Option<usize> {
    #[cfg(target_os = "linux")]
    {
        let cgroups = std::fs::read_to_string("/proc/self/cgroup").ok()?;
        let mountinfo = std::fs::read_to_string("/proc/self/mountinfo").ok()?;
        let bytes = memory_limit_in(&memory_mounts(&mountinfo), &cgroups)?;
        usize::try_from(bytes / MIB).ok()
    }
    #[cfg(not(target_os = "linux"))]
    {
        None
    }
}

/// The guest memory a limit of `limit_mb` MiB leaves, in MiB: the limit
/// less [`CONTAINER_SCRATCH_HEADROOM_MB`], or `None` when that leaves
/// less than the headroom itself, too little to size a guest by.
pub fn scratch_mb_within(limit_mb: usize) -> Option<usize> {
    let mb = limit_mb.checked_sub(CONTAINER_SCRATCH_HEADROOM_MB)?;
    (mb >= CONTAINER_SCRATCH_HEADROOM_MB).then_some(mb)
}

#[cfg(any(target_os = "linux", test))]
const MIB: u64 = crate::MIB as u64;

/// A mount of a cgroup hierarchy that holds the memory controller: the
/// unified hierarchy (v2), or a v1 hierarchy mounted with `memory`.
#[cfg(any(target_os = "linux", test))]
#[derive(Debug, PartialEq, Eq)]
struct MemoryMount {
    v2: bool,
    /// The directory of the hierarchy that is mounted: `/` for the whole
    /// of it, a container's own cgroup where its runtime mounted that.
    root: PathBuf,
    /// Where it is mounted.
    point: PathBuf,
}

/// The cgroup mounts that hold the memory controller, from the contents
/// of `/proc/self/mountinfo`: a line per mount, `id parent dev root point
/// options [tags...] - fstype source superopts`, with spaces in paths
/// written as `\040`.
#[cfg(any(target_os = "linux", test))]
fn memory_mounts(mountinfo: &str) -> Vec<MemoryMount> {
    mountinfo
        .lines()
        .filter_map(|line| {
            let (head, tail) = line.split_once(" - ")?;
            let mut head = head.split(' ');
            let root = head.nth(3)?;
            let point = head.next()?;
            let mut tail = tail.split(' ');
            let fstype = tail.next()?;
            let superopts = tail.nth(1)?;
            let v2 = match fstype {
                "cgroup2" => true,
                "cgroup" if superopts.split(',').any(|o| o == "memory") => false,
                _ => return None,
            };
            Some(MemoryMount {
                v2,
                root: unescape(root),
                point: unescape(point),
            })
        })
        .collect()
}

/// A path as `/proc/self/mountinfo` writes it, with a space, tab, newline
/// or backslash as a three-digit octal escape, unescaped.
#[cfg(any(target_os = "linux", test))]
fn unescape(field: &str) -> PathBuf {
    let mut out = String::with_capacity(field.len());
    let mut rest = field;
    while let Some(i) = rest.find('\\') {
        out.push_str(&rest[..i]);
        match rest
            .get(i + 1..i + 4)
            .and_then(|octal| u8::from_str_radix(octal, 8).ok())
        {
            Some(byte) => {
                out.push(byte as char);
                rest = &rest[i + 4..];
            }
            None => {
                out.push('\\');
                rest = &rest[i + 1..];
            }
        }
    }
    out.push_str(rest);
    PathBuf::from(out)
}

/// The tightest memory limit among the process's cgroup and its ancestors
/// in sight, in bytes, from the contents of `/proc/self/cgroup` and the
/// memory `mounts`.  A v2 hierarchy is the line `0::/path`; a v1 memory
/// controller is a line `N:memory:/path` (or one listing memory among
/// controllers).  The cgroup's directory is its path less the mount's
/// root, under the mount point; a cgroup outside what is mounted is out
/// of sight.
#[cfg(any(target_os = "linux", test))]
fn memory_limit_in(mounts: &[MemoryMount], proc_self_cgroup: &str) -> Option<u64> {
    let mut tightest: Option<u64> = None;
    for line in proc_self_cgroup.lines() {
        let mut fields = line.splitn(3, ':');
        let (Some(id), Some(controllers), Some(path)) =
            (fields.next(), fields.next(), fields.next())
        else {
            continue;
        };
        let v2 = id == "0" && controllers.is_empty();
        if !v2 && !controllers.split(',').any(|c| c == "memory") {
            continue;
        }
        let (file, none) = if v2 {
            ("memory.max", "max")
        } else {
            ("memory.limit_in_bytes", "")
        };
        for mount in mounts.iter().filter(|m| m.v2 == v2) {
            let Ok(below) = Path::new(path).strip_prefix(&mount.root) else {
                continue;
            };
            let dir = mount.point.join(below);
            for dir in dir.ancestors().take_while(|d| d.starts_with(&mount.point)) {
                let Ok(value) = std::fs::read_to_string(dir.join(file)) else {
                    continue;
                };
                let value = value.trim();
                if value == none {
                    continue;
                }
                let Ok(limit) = value.parse::<u64>() else {
                    continue;
                };
                // cgroup v1 reports "no limit" as a huge number, not a word.
                if limit >= V1_UNLIMITED {
                    continue;
                }
                tightest = Some(tightest.map_or(limit, |t| t.min(limit)));
            }
        }
    }
    tightest
}

/// What cgroup v1 writes for a memory limit that is not set: `i64::MAX`
/// rounded down to a page, whose size is the kernel's (4 KiB on x86, 16 or
/// 64 KiB on some arm64 kernels).  Anything from 4 EiB up, which no
/// machine has, is no limit.
#[cfg(any(target_os = "linux", test))]
const V1_UNLIMITED: u64 = 1 << 62;

#[cfg(test)]
mod tests {
    use super::*;

    fn write(dir: &Path, name: &str, value: &str) {
        std::fs::create_dir_all(dir).unwrap();
        std::fs::write(dir.join(name), format!("{value}\n")).unwrap();
    }

    fn mount(v2: bool, root: &str, point: &Path) -> MemoryMount {
        MemoryMount {
            v2,
            root: PathBuf::from(root),
            point: point.to_path_buf(),
        }
    }

    #[test]
    fn v2_takes_the_tightest_limit_up_the_tree() {
        let tree = tempfile::tempdir().unwrap();
        let root = tree.path();
        let mounts = [mount(true, "/", root)];
        write(root, "memory.max", "max");
        write(&root.join("pod"), "memory.max", &(512 * MIB).to_string());
        write(&root.join("pod/ctr"), "memory.max", "max");
        assert_eq!(
            memory_limit_in(&mounts, "0::/pod/ctr\n"),
            Some(512 * MIB),
            "the pod's limit counts when the container's is open"
        );
        write(
            &root.join("pod/ctr"),
            "memory.max",
            &(256 * MIB).to_string(),
        );
        assert_eq!(memory_limit_in(&mounts, "0::/pod/ctr\n"), Some(256 * MIB));
        write(
            &root.join("pod/ctr"),
            "memory.max",
            &(1024 * MIB).to_string(),
        );
        assert_eq!(
            memory_limit_in(&mounts, "0::/pod/ctr\n"),
            Some(512 * MIB),
            "an ancestor's tighter limit wins"
        );
    }

    #[test]
    fn a_container_sees_its_own_cgroup_mounted() {
        let tree = tempfile::tempdir().unwrap();
        let root = tree.path();
        write(root, "memory.max", &(768 * MIB).to_string());
        // With a cgroup namespace the container's cgroup is `/`; without
        // one its path is the host's, and its runtime mounted just it.
        for (mounts, proc) in [
            ([mount(true, "/", root)], "0::/\n"),
            (
                [mount(true, "/kubepods/pod1/ctr", root)],
                "0::/kubepods/pod1/ctr\n",
            ),
        ] {
            assert_eq!(memory_limit_in(&mounts, proc), Some(768 * MIB));
        }
        write(root, "memory.max", "max");
        assert_eq!(memory_limit_in(&[mount(true, "/", root)], "0::/\n"), None);
        assert_eq!(
            memory_limit_in(&[mount(true, "/", root)], "0::/gone\n"),
            None,
            "a missing cgroup is no limit"
        );
        assert_eq!(
            memory_limit_in(
                &[mount(true, "/kubepods/pod1/ctr", root)],
                "0::/kubepods/pod2/ctr\n"
            ),
            None,
            "a cgroup outside what is mounted is out of sight"
        );
    }

    #[test]
    fn a_child_of_the_mounted_cgroup_is_read_first() {
        // A cgroup made inside the container (a daemon's, for one child)
        // sits below the mounted directory: its own limit counts, and so
        // does the container's above it.
        let tree = tempfile::tempdir().unwrap();
        let root = tree.path();
        let mounts = [mount(false, "/docker/abc", root)];
        write(root, "memory.limit_in_bytes", &(400 * MIB).to_string());
        write(
            &root.join("worker"),
            "memory.limit_in_bytes",
            &(200 * MIB).to_string(),
        );
        assert_eq!(
            memory_limit_in(&mounts, "4:memory:/docker/abc/worker\n"),
            Some(200 * MIB)
        );
        assert_eq!(
            memory_limit_in(&mounts, "4:memory:/docker/abc\n"),
            Some(400 * MIB)
        );
        write(
            &root.join("worker"),
            "memory.limit_in_bytes",
            &(600 * MIB).to_string(),
        );
        assert_eq!(
            memory_limit_in(&mounts, "4:memory:/docker/abc/worker\n"),
            Some(400 * MIB),
            "the container's tighter limit still counts"
        );
    }

    #[test]
    fn v1_reads_the_memory_controller_and_ignores_unlimited() {
        let tree = tempfile::tempdir().unwrap();
        let root = tree.path();
        let mounts = [mount(false, "/", root)];
        let proc = "12:pids:/docker/abc\n4:memory:/docker/abc\n1:name=systemd:/docker/abc\n";
        write(root, "memory.limit_in_bytes", &V1_UNLIMITED.to_string());
        write(
            &root.join("docker/abc"),
            "memory.limit_in_bytes",
            "9223372036854771712",
        );
        assert_eq!(memory_limit_in(&mounts, proc), None);
        write(
            &root.join("docker/abc"),
            "memory.limit_in_bytes",
            "9223372036854710272",
        );
        assert_eq!(
            memory_limit_in(&mounts, proc),
            None,
            "no limit on a kernel with 64 KiB pages"
        );
        write(
            &root.join("docker/abc"),
            "memory.limit_in_bytes",
            &(300 * MIB).to_string(),
        );
        assert_eq!(memory_limit_in(&mounts, proc), Some(300 * MIB));
        assert_eq!(
            memory_limit_in(&mounts, "4:cpu,memory:/docker/abc\n"),
            Some(300 * MIB),
            "memory among other controllers on one line, wherever mounted"
        );
        assert_eq!(memory_limit_in(&mounts, "4:cpu:/docker/abc\n"), None);
    }

    #[test]
    fn a_hybrid_host_reads_v1_memory_and_skips_the_unified_mount() {
        let unified = tempfile::tempdir().unwrap();
        let memory = tempfile::tempdir().unwrap();
        let mounts = [
            mount(true, "/", unified.path()),
            mount(false, "/", memory.path()),
        ];
        write(
            &memory.path().join("docker/abc"),
            "memory.limit_in_bytes",
            &(300 * MIB).to_string(),
        );
        assert_eq!(
            memory_limit_in(&mounts, "0::/docker/abc\n4:memory:/docker/abc\n"),
            Some(300 * MIB)
        );
    }

    #[test]
    fn the_memory_mounts_come_from_mountinfo() {
        let mountinfo = "\
35 24 0:30 / /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime shared:9 - cgroup2 cgroup rw,nsdelegate,memory_recursiveprot
40 35 0:34 /docker/abc /sys/fs/cgroup/memory rw,nosuid,nodev,noexec,relatime - cgroup cgroup rw,memory
41 35 0:35 / /sys/fs/cgroup/cpu,cpuacct rw,nosuid - cgroup cgroup rw,cpu,cpuacct
42 35 0:36 / /sys/fs/cgroup/unified rw shared:10 - cgroup2 cgroup rw,nsdelegate
43 35 0:37 / /sys/fs/cgroup/memory,cpu rw - cgroup cgroup rw,memory,cpu
44 35 0:38 /a\\040b /mnt/c\\040d rw - cgroup cgroup rw,memory
1 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw
not a mountinfo line
";
        assert_eq!(
            memory_mounts(mountinfo),
            [
                mount(true, "/", Path::new("/sys/fs/cgroup")),
                mount(false, "/docker/abc", Path::new("/sys/fs/cgroup/memory")),
                mount(true, "/", Path::new("/sys/fs/cgroup/unified")),
                mount(false, "/", Path::new("/sys/fs/cgroup/memory,cpu")),
                mount(false, "/a b", Path::new("/mnt/c d")),
            ]
        );
    }

    #[test]
    fn the_guest_gets_the_limit_less_the_headroom() {
        assert_eq!(scratch_mb_within(1536), Some(1472));
        assert_eq!(scratch_mb_within(512), Some(448));
        assert_eq!(scratch_mb_within(128), Some(64));
        assert_eq!(
            scratch_mb_within(127),
            None,
            "less than a 64 MiB guest is no size"
        );
        assert_eq!(scratch_mb_within(0), None);
    }
}
