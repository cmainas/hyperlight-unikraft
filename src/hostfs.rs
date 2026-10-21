// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Hyperlight Authors.

//! Host filesystem — `fs_*` host functions backed by an [`FsBackend`].
//!
//! A mount of a host directory is a capability: a [`cap_std::fs::Dir`]
//! opened once on the host path, after which guest paths are resolved
//! *beneath* it by cap-std (no `..` escapes, no absolute symlink targets)
//! on every platform.  A mount made with [`Mount::backend`] is served by
//! the embedder's [`FsBackend`] instead, which sees each operation and
//! decides it; the wire format below is this module's either way.
//!
//! ## Mount indices
//!
//! Every host function takes `mount_idx: i32` as its first parameter,
//! selecting which mount point to operate on. Out-of-range indices
//! return `-EINVAL`.
//!
//! ## Read-only mounts
//!
//! Mounts may be marked read-only. Write operations (`fs_write_bytes`,
//! `fs_mkdir`, `fs_unlink`, `fs_truncate`, `fs_rename`, `fs_symlink`,
//! `fs_link`, `fs_chmod`) return `-EROFS` on read-only mounts. Read
//! operations (`fs_stat`, `fs_read_bytes`, `fs_list`, `fs_readlink`)
//! work regardless.
//!
//! ## Return conventions
//!
//! - **`i32` returns**: 0 on success, `-errno` on error.
//! - **`Vec<u8>` returns**: first 4 bytes are `i32` status (0 or `-errno`),
//!   followed by operation-specific data on success.
//! - A reply is one host call, so at most [`HOST_CALL_MAX`] bytes: reads
//!   are chunked ([`CHUNK`]), and a directory listing that would not fit
//!   fails with `-EOVERFLOW` rather than break the call.  TODO: page
//!   `fs_list` so a large directory lists in several calls.

use std::io::{self, Read, Seek, SeekFrom, Write};
use std::sync::Arc;

use cap_std::ambient_authority;
use cap_std::fs::{Dir, OpenOptions};
use hyperlight_host::func::Registerable;
use tracing::{debug, trace};

use crate::{HOST_CALL_MAX, Mount, errno};

/// Maximum bytes per read/write host call.  The guest queries this
/// value via `GetHostFsChunkSize` at mount time — changing it here
/// does not require a kernel rebuild.  64 KiB: over the virtqueues a
/// 64 KiB chunk reads and writes about 40% faster than a 32 KiB one,
/// and larger ones gain little for the pool memory they cost.
pub(crate) const CHUNK: usize = 64 * 1024;

/// A listing's status and count, before its entries.
const LIST_HDR: usize = 8;

/// An entry's is-directory byte and name length, before its name.
const LIST_ENTRY_HDR: usize = 3;

/// The longest name a listing hands the kernel.
const NAME_MAX: usize = 255;

/// The longest link target `fs_readlink` replies with, NUL excluded.
const PATH_MAX: usize = 4095;

// The chunk and its 4-byte status are one host call's reply.
const _: () = assert!(CHUNK + 4 <= HOST_CALL_MAX);

/// The type bits of a [`FileStat`] mode, as in `st_mode`.
pub const S_IFMT: u32 = 0o170000;
/// A directory's type bits.
pub const S_IFDIR: u32 = 0o040000;
/// A regular file's type bits.
pub const S_IFREG: u32 = 0o100000;
/// A symbolic link's type bits, which only a directory mount reports.
const S_IFLNK: u32 = 0o120000;

/// What `stat` tells the guest about a path: a file or a directory, as
/// the kernel makes no other kind of node on a hostfs mount.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub struct FileStat {
    /// Size in bytes.
    pub size: u64,
    /// Type and permission bits, as in `st_mode` ([`S_IFDIR`] or
    /// [`S_IFREG`], and `0o7777`).
    pub mode: u32,
}

impl FileStat {
    /// A regular file of `size` bytes, with permission bits `perm`.
    pub fn file(size: u64, perm: u32) -> Self {
        Self {
            size,
            mode: S_IFREG | (perm & 0o7777),
        }
    }

    /// A directory, with permission bits `perm`.
    pub fn dir(perm: u32) -> Self {
        Self {
            size: 0,
            mode: S_IFDIR | (perm & 0o7777),
        }
    }
}

/// An entry of a directory listing.
#[derive(Clone, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub struct DirEntry {
    /// The entry's name: one path component, at most 255 bytes.
    pub name: String,
    /// Whether it is a directory.
    pub is_dir: bool,
}

impl DirEntry {
    /// An entry named `name`, a directory if `is_dir`.
    pub fn new(name: impl Into<String>, is_dir: bool) -> Self {
        Self {
            name: name.into(),
            is_dir,
        }
    }
}

/// What serves a guest mount's file operations: the host side of the
/// guest kernel's hostfs.
///
/// Every argument comes from the guest and is untrusted. A guest program
/// goes through the kernel's VFS, which hands over clean paths (relative
/// to the mount, `/`-separated, the empty path being the mount's root, at
/// most 1023 bytes), but native code in the guest can make the host calls
/// itself with any string: absolute, with `..` or empty components, a few
/// tens of KiB long. Confining paths to the backend's own files is the
/// backend's to do, as is bounding a write's `offset`, which may be any
/// `u64`.
///
/// Errors reach the guest as errno: give them an [`io::ErrorKind`] such
/// as `QuotaExceeded` (`EDQUOT`), `FileTooLarge` (`EFBIG`) or
/// `PermissionDenied` (`EACCES`), or a raw OS error of the host's OS. A
/// panic in a method is caught (unless the host is built with
/// `panic = "abort"`), and the guest told `EIO`.
///
/// Each method runs while the guest is stopped in a host call. The guest
/// has no handles: every read and write names its path and offset, and a
/// large transfer is many calls of at most 64 KiB. A directory is read by
/// calling [`list`](Self::list) once per entry, taking the next entry of
/// each listing by its position, so a listing must come back in the same
/// order while the directory is unchanged, and a large directory costs
/// that many listings. On a read-only mount the writing methods are not
/// called; the guest is told `EROFS`.
///
/// Methods added to this trait will come with defaults.
pub trait FsBackend: Send + Sync {
    fn stat(&self, path: &str) -> io::Result<FileStat>;
    /// Read into `buf` from `offset`; fewer bytes than asked means the end.
    fn read(&self, path: &str, offset: u64, buf: &mut [u8]) -> io::Result<usize>;
    /// Write `data` at `offset`, or at the end for `None`, creating the
    /// file if it does not exist.
    fn write(&self, path: &str, offset: Option<u64>, data: &[u8]) -> io::Result<()>;
    fn create_dir(&self, path: &str) -> io::Result<()>;
    /// Remove a file or an empty directory.
    fn remove(&self, path: &str) -> io::Result<()>;
    fn truncate(&self, path: &str, len: u64) -> io::Result<()>;
    fn list(&self, path: &str) -> io::Result<Vec<DirEntry>>;
    fn rename(&self, from: &str, to: &str) -> io::Result<()>;
    /// Make `link` a symbolic link to `target`.  Refused by default.
    fn symlink(&self, _link: &str, _target: &str) -> io::Result<()> {
        Err(io::ErrorKind::PermissionDenied.into())
    }
    /// The target of a symbolic link.  Reserved: the kernel makes no
    /// link nodes on a hostfs mount yet, so a guest program never asks.
    fn read_link(&self, _path: &str) -> io::Result<String> {
        Err(io::ErrorKind::InvalidInput.into())
    }
    /// Make `dst` a hard link to `src`.  Refused by default.
    fn hard_link(&self, _src: &str, _dst: &str) -> io::Result<()> {
        Err(io::ErrorKind::PermissionDenied.into())
    }
    /// Set the permission bits.  Ignored by default.
    fn chmod(&self, _path: &str, _mode: u32) -> io::Result<()> {
        Ok(())
    }
}

/// A host directory, the backend of a mount made with [`Mount::rw`] or
/// [`Mount::ro`].
struct DirBackend(Dir);

impl FsBackend for DirBackend {
    fn stat(&self, path: &str) -> io::Result<FileStat> {
        // Empty path = stat the mount root itself.
        let m = if path.is_empty() {
            self.0.dir_metadata()?
        } else {
            self.0.metadata(path)?
        };
        Ok(FileStat {
            size: m.len(),
            mode: synth_mode(&m),
        })
    }

    fn read(&self, path: &str, offset: u64, buf: &mut [u8]) -> io::Result<usize> {
        // TODO: the protocol is stateless, so every chunk (here and in
        // write) reopens the file; a handle-based open/read/close would
        // make a large transfer one open.
        let mut file = self.0.open(path)?;
        if offset > 0 {
            file.seek(SeekFrom::Start(offset))?;
        }
        file.read(buf)
    }

    fn write(&self, path: &str, offset: Option<u64>, data: &[u8]) -> io::Result<()> {
        match offset {
            None => self
                .0
                .open_with(path, OpenOptions::new().append(true).create(true))?
                .write_all(data),
            Some(offset) => {
                let mut f = self
                    .0
                    .open_with(path, OpenOptions::new().write(true).create(true))?;
                if offset > 0 {
                    f.seek(SeekFrom::Start(offset))?;
                }
                f.write_all(data)
            }
        }
    }

    fn create_dir(&self, path: &str) -> io::Result<()> {
        self.0.create_dir(path)
    }

    fn remove(&self, path: &str) -> io::Result<()> {
        // Try file first, then directory.
        // TODO: removes a file or an empty directory alike; POSIX unlink
        // refuses a directory with EISDIR and rmdir a file with ENOTDIR,
        // which needs the call to say which it is.
        self.0
            .remove_file(path)
            .or_else(|_| self.0.remove_dir(path))
    }

    fn truncate(&self, path: &str, len: u64) -> io::Result<()> {
        self.0
            .open_with(path, OpenOptions::new().write(true))?
            .set_len(len)
    }

    fn list(&self, path: &str) -> io::Result<Vec<DirEntry>> {
        let path = if path.is_empty() { "." } else { path };
        // TODO: an entry that cannot be read is skipped and a non-UTF-8
        // name is decoded lossily, neither reported to the guest.
        let mut entries = Vec::new();
        // What `fs_list` would send so far: stop one entry past what a
        // reply holds, which it refuses with EOVERFLOW, rather than read
        // (and keep, on the host) a directory of any size first.
        let mut size = LIST_HDR;
        for entry in self.0.read_dir(path)?.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            size += LIST_ENTRY_HDR + name.len();
            entries.push(DirEntry {
                is_dir: entry.metadata().map(|m| m.is_dir()).unwrap_or(false),
                name,
            });
            if size > HOST_CALL_MAX {
                break;
            }
        }
        Ok(entries)
    }

    fn rename(&self, from: &str, to: &str) -> io::Result<()> {
        self.0.rename(from, &self.0, to)
    }

    fn symlink(&self, link: &str, target: &str) -> io::Result<()> {
        #[cfg(unix)]
        return self.0.symlink(target, link);
        #[cfg(windows)]
        return self
            .0
            .symlink_file(target, link)
            .or_else(|_| self.0.symlink_dir(target, link));
    }

    fn read_link(&self, path: &str) -> io::Result<String> {
        Ok(self.0.read_link(path)?.to_string_lossy().into_owned())
    }

    fn hard_link(&self, src: &str, dst: &str) -> io::Result<()> {
        self.0.hard_link(src, &self.0, dst)
    }

    fn chmod(&self, path: &str, mode: u32) -> io::Result<()> {
        #[cfg(unix)]
        {
            use cap_std::fs::PermissionsExt;
            self.0
                .set_permissions(path, cap_std::fs::Permissions::from_mode(mode))
        }
        #[cfg(windows)]
        {
            // On Windows only the readonly bit matters.
            let mut perms = self.0.metadata(path)?.permissions();
            perms.set_readonly(mode & 0o222 == 0);
            self.0.set_permissions(path, perms)
        }
    }
}

/// The backend serving `mount`: its own, or its host directory's.
fn open_backend(mount: &Mount) -> crate::Result<Arc<dyn FsBackend>> {
    if let Some(backend) = &mount.backend {
        return Ok(backend.clone());
    }
    let d = Dir::open_ambient_dir(&mount.host_path, ambient_authority()).map_err(|source| {
        crate::Error::Mount {
            host_path: mount.host_path.clone(),
            guest_path: mount.guest_path.clone(),
            source,
        }
    })?;
    Ok(Arc::new(DirBackend(d)))
}

/// The mounts a guest's `fs_*` calls name by index.
struct Mounts(Vec<(Arc<dyn FsBackend>, bool)>);

impl Mounts {
    /// The backend of mount `idx`, or `EINVAL`.
    fn get(&self, idx: i32) -> Result<Guarded<'_>, i32> {
        usize::try_from(idx)
            .ok()
            .and_then(|i| self.0.get(i))
            .map(|(b, _)| Guarded(&**b))
            .ok_or(errno::EINVAL)
    }

    /// The backend of mount `idx` for a write: `EROFS` if it is read-only.
    fn writable(&self, idx: i32) -> Result<Guarded<'_>, i32> {
        let backend = self.get(idx)?;
        if self.0[idx as usize].1 {
            return Err(errno::EROFS);
        }
        Ok(backend)
    }
}

/// A backend whose panics are the guest's `EIO`, not the host's: the
/// embedder's code must not take the sandbox down with it.
struct Guarded<'a>(&'a dyn FsBackend);

impl Guarded<'_> {
    fn ask<T>(&self, op: impl FnOnce(&dyn FsBackend) -> io::Result<T>) -> io::Result<T> {
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| op(self.0)))
            .unwrap_or_else(|_| Err(io::Error::other("the mount's backend panicked")))
    }
}

impl FsBackend for Guarded<'_> {
    fn stat(&self, path: &str) -> io::Result<FileStat> {
        self.ask(|b| b.stat(path))
    }
    fn read(&self, path: &str, offset: u64, buf: &mut [u8]) -> io::Result<usize> {
        self.ask(|b| b.read(path, offset, buf))
    }
    fn write(&self, path: &str, offset: Option<u64>, data: &[u8]) -> io::Result<()> {
        self.ask(|b| b.write(path, offset, data))
    }
    fn create_dir(&self, path: &str) -> io::Result<()> {
        self.ask(|b| b.create_dir(path))
    }
    fn remove(&self, path: &str) -> io::Result<()> {
        self.ask(|b| b.remove(path))
    }
    fn truncate(&self, path: &str, len: u64) -> io::Result<()> {
        self.ask(|b| b.truncate(path, len))
    }
    fn list(&self, path: &str) -> io::Result<Vec<DirEntry>> {
        self.ask(|b| b.list(path))
    }
    fn rename(&self, from: &str, to: &str) -> io::Result<()> {
        self.ask(|b| b.rename(from, to))
    }
    fn symlink(&self, link: &str, target: &str) -> io::Result<()> {
        self.ask(|b| b.symlink(link, target))
    }
    fn read_link(&self, path: &str) -> io::Result<String> {
        self.ask(|b| b.read_link(path))
    }
    fn hard_link(&self, src: &str, dst: &str) -> io::Result<()> {
        self.ask(|b| b.hard_link(src, dst))
    }
    fn chmod(&self, path: &str, mode: u32) -> io::Result<()> {
        self.ask(|b| b.chmod(path, mode))
    }
}

/// Register all `fs_*` host functions.
///
/// Opens the backend of each mount in `mounts`. Each host function
/// takes `mount_idx` as its first parameter to select the mount; a
/// read-only mount refuses writes with `EROFS`.  With no mounts the
/// functions are still registered and answer `EINVAL` for any index, so
/// a restore offers every host function the snapshot's guest can call.
pub(crate) fn register(
    target: &mut impl Registerable,
    mounts: &[Mount],
    profile: &std::sync::Arc<crate::profile::Profile>,
) -> crate::Result<()> {
    let mut opened = Vec::with_capacity(mounts.len());
    for (i, m) in mounts.iter().enumerate() {
        opened.push((open_backend(m)?, m.readonly));
        debug!(
            idx = i,
            host = %m.host_path.display(),
            guest = %m.guest_path,
            mode = if m.readonly { "ro" } else { "rw" },
            backend = m.backend.is_some(),
            "hostfs: opened mount",
        );
    }
    let mounts = Arc::new(Mounts(opened));

    // ── fs_stat ─────────────────────────────────────────────────
    //
    // Returns Vec<u8>:
    //   [0..4]   i32  status (0 or -errno)
    //   [4..12]  u64  size
    //   [12..16] u32  mode
    //   [16]     u8   is_dir
    //   [17]     u8   is_file
    {
        let mounts = mounts.clone();
        let prof = profile.clone();
        target.register_host_function(
            "fs_stat",
            move |mount_idx: i32, path: String| -> hyperlight_host::Result<Vec<u8>> {
                let _profile = prof.host("fs_stat");
                let stat = match mounts.get(mount_idx) {
                    Ok(b) => b.stat(&path),
                    Err(code) => return Ok(status_vec(code)),
                };
                Ok(match stat {
                    Ok(st) => {
                        let mut buf = Vec::with_capacity(18);
                        buf.extend(0i32.to_le_bytes());
                        buf.extend(st.size.to_le_bytes());
                        buf.extend(st.mode.to_le_bytes());
                        buf.push((st.mode & S_IFMT == S_IFDIR) as u8);
                        buf.push((st.mode & S_IFMT == S_IFREG) as u8);
                        buf
                    }
                    Err(e) => errno_vec(e),
                })
            },
        )?;
    }

    // ── fs_read_bytes ───────────────────────────────────────────
    //
    // Returns Vec<u8>:
    //   [0..4]  i32   status (0 or -errno)
    //   [4..]   bytes data (length = returned_len - 4)
    //
    // EOF is implicit: data shorter than requested → at end.
    {
        let mounts = mounts.clone();
        let prof = profile.clone();
        target.register_host_function(
            "fs_read_bytes",
            move |mount_idx: i32,
                  path: String,
                  offset: u64,
                  len: u64|
                  -> hyperlight_host::Result<Vec<u8>> {
                let _profile = prof.host("fs_read_bytes");
                let backend = match mounts.get(mount_idx) {
                    Ok(b) => b,
                    Err(code) => return Ok(status_vec(code)),
                };
                let len = (len.min(CHUNK as u64) as usize).max(1);
                let mut buf = vec![0u8; 4 + len];
                Ok(match backend.read(&path, offset, &mut buf[4..]) {
                    Ok(n) => {
                        // A backend that says it read more than it was given
                        // room for is cut to the room.
                        buf.truncate(4 + n.min(len));
                        buf
                    }
                    Err(e) => errno_vec(e),
                })
            },
        )?;
    }

    // ── fs_write_bytes ──────────────────────────────────────────
    //
    // `append`: 0 = write at offset, nonzero = append (O_APPEND).
    // Returns i32: 0 or -errno.
    {
        let mounts = mounts.clone();
        let prof = profile.clone();
        target.register_host_function(
            "fs_write_bytes",
            move |mount_idx: i32,
                  path: String,
                  offset: u64,
                  append: i32,
                  data: Vec<u8>|
                  -> hyperlight_host::Result<i32> {
                let _profile = prof.host("fs_write_bytes");
                let offset = (append == 0).then_some(offset);
                Ok(status(
                    mounts
                        .writable(mount_idx)
                        .map(|b| b.write(&path, offset, &data)),
                ))
            },
        )?;
    }

    // ── fs_mkdir ────────────────────────────────────────────────
    {
        let mounts = mounts.clone();
        let prof = profile.clone();
        target.register_host_function(
            "fs_mkdir",
            move |mount_idx: i32, path: String| -> hyperlight_host::Result<i32> {
                let _profile = prof.host("fs_mkdir");
                Ok(status(
                    mounts.writable(mount_idx).map(|b| b.create_dir(&path)),
                ))
            },
        )?;
    }

    // ── fs_unlink ───────────────────────────────────────────────
    {
        let mounts = mounts.clone();
        let prof = profile.clone();
        target.register_host_function(
            "fs_unlink",
            move |mount_idx: i32, path: String| -> hyperlight_host::Result<i32> {
                let _profile = prof.host("fs_unlink");
                Ok(status(mounts.writable(mount_idx).map(|b| b.remove(&path))))
            },
        )?;
    }

    // ── fs_truncate ─────────────────────────────────────────────
    {
        let mounts = mounts.clone();
        let prof = profile.clone();
        target.register_host_function(
            "fs_truncate",
            move |mount_idx: i32, path: String, length: u64| -> hyperlight_host::Result<i32> {
                let _profile = prof.host("fs_truncate");
                Ok(status(
                    mounts
                        .writable(mount_idx)
                        .map(|b| b.truncate(&path, length)),
                ))
            },
        )?;
    }

    // ── fs_list ─────────────────────────────────────────────────
    //
    // Returns Vec<u8>:
    //   [0..4]  i32  status (0 or -errno)
    //   [4..8]  u32  entry_count
    //   [8..]   entries, each:
    //     u8   is_dir
    //     u16  name_len (LE)
    //     [name_len bytes] name (UTF-8, no NUL)
    //
    // An error reply carries a zero count too: the kernel's readdir wants
    // the eight bytes before it looks at the status.
    {
        let mounts = mounts.clone();
        let list_error = |code: i32| {
            let mut buf = Vec::with_capacity(8);
            buf.extend((-code).to_le_bytes());
            buf.extend(0u32.to_le_bytes());
            buf
        };
        let prof = profile.clone();
        target.register_host_function(
            "fs_list",
            move |mount_idx: i32, path: String| -> hyperlight_host::Result<Vec<u8>> {
                let _profile = prof.host("fs_list");
                let entries = match mounts.get(mount_idx) {
                    Ok(b) => b.list(&path),
                    Err(code) => return Ok(list_error(code)),
                };
                let entries = match entries {
                    Ok(entries) => entries,
                    Err(e) => return Ok(list_error(errno::from_io(&e))),
                };
                let mut buf = Vec::with_capacity(256);
                buf.extend(0i32.to_le_bytes()); // status
                buf.extend(0u32.to_le_bytes()); // count placeholder
                let mut count = 0u32;
                for entry in &entries {
                    let name_bytes = entry.name.as_bytes();
                    // A name the kernel could not take as one component
                    // (and whose length would not fit its u16) is left
                    // out; a host directory never has one.
                    if name_bytes.is_empty()
                        || name_bytes.len() > NAME_MAX
                        || entry.name.contains(['/', '\0'])
                        || entry.name == "."
                        || entry.name == ".."
                    {
                        debug!(name = %entry.name.escape_debug(), "hostfs: listing skips a name");
                        continue;
                    }
                    // One host call carries HOST_CALL_MAX bytes; a listing
                    // past that would fail the call itself (and with it the
                    // sandbox), so refuse it here.
                    if buf.len() + LIST_ENTRY_HDR + name_bytes.len() > HOST_CALL_MAX {
                        return Ok(list_error(errno::EOVERFLOW));
                    }
                    buf.push(entry.is_dir as u8);
                    // At most NAME_MAX, checked above.
                    buf.extend((name_bytes.len() as u16).to_le_bytes());
                    buf.extend_from_slice(name_bytes);
                    count += 1;
                }
                buf[4..8].copy_from_slice(&count.to_le_bytes());
                Ok(buf)
            },
        )?;
    }

    // ── fs_rename ───────────────────────────────────────────────
    {
        let mounts = mounts.clone();
        let prof = profile.clone();
        target.register_host_function(
            "fs_rename",
            move |mount_idx: i32, from: String, to: String| -> hyperlight_host::Result<i32> {
                let _profile = prof.host("fs_rename");
                Ok(status(
                    mounts.writable(mount_idx).map(|b| b.rename(&from, &to)),
                ))
            },
        )?;
    }

    // ── fs_symlink ──────────────────────────────────────────────
    {
        let mounts = mounts.clone();
        let prof = profile.clone();
        target.register_host_function(
            "fs_symlink",
            move |mount_idx: i32,
                  link_path: String,
                  target_path: String|
                  -> hyperlight_host::Result<i32> {
                let _profile = prof.host("fs_symlink");
                Ok(status(
                    mounts
                        .writable(mount_idx)
                        .map(|b| b.symlink(&link_path, &target_path)),
                ))
            },
        )?;
    }

    // ── fs_readlink ─────────────────────────────────────────────
    //
    // Returns Vec<u8>:
    //   [0..4]  i32   status (0 or -errno)
    //   [4..]   bytes target path (UTF-8)
    {
        let mounts = mounts.clone();
        let prof = profile.clone();
        target.register_host_function(
            "fs_readlink",
            move |mount_idx: i32, path: String| -> hyperlight_host::Result<Vec<u8>> {
                let _profile = prof.host("fs_readlink");
                let target_path = match mounts.get(mount_idx) {
                    Ok(b) => b.read_link(&path),
                    Err(code) => return Ok(status_vec(code)),
                };
                Ok(match target_path {
                    Ok(target_path) if target_path.len() > PATH_MAX => {
                        status_vec(errno::ENAMETOOLONG)
                    }
                    Ok(target_path) => {
                        let mut buf = Vec::with_capacity(4 + target_path.len());
                        buf.extend(0i32.to_le_bytes());
                        buf.extend_from_slice(target_path.as_bytes());
                        buf
                    }
                    Err(e) => errno_vec(e),
                })
            },
        )?;
    }

    // ── fs_link ─────────────────────────────────────────────────
    {
        let mounts = mounts.clone();
        let prof = profile.clone();
        target.register_host_function(
            "fs_link",
            move |mount_idx: i32, src: String, dst: String| -> hyperlight_host::Result<i32> {
                let _profile = prof.host("fs_link");
                Ok(status(
                    mounts.writable(mount_idx).map(|b| b.hard_link(&src, &dst)),
                ))
            },
        )?;
    }

    // ── fs_chmod ────────────────────────────────────────────────
    {
        let prof = profile.clone();
        target.register_host_function(
            "fs_chmod",
            move |mount_idx: i32, path: String, mode: i32| -> hyperlight_host::Result<i32> {
                let _profile = prof.host("fs_chmod");
                // The kernel sends the mode as a C int.  Only the
                // permission bits, for any backend: setuid, setgid and
                // sticky would let a guest plant a setuid program on the
                // host.
                let mode = mode as u32 & 0o777;
                Ok(status(
                    mounts.writable(mount_idx).map(|b| b.chmod(&path, mode)),
                ))
            },
        )?;
    }

    Ok(())
}

// ── Helpers ──────────────────────────────────────────────────────────

/// Synthesize a POSIX-ish mode from cap_std metadata.
fn synth_mode(m: &cap_std::fs::Metadata) -> u32 {
    let kind = if m.is_dir() {
        S_IFDIR
    } else if m.is_symlink() {
        S_IFLNK
    } else {
        S_IFREG
    };
    let perm = if m.permissions().readonly() {
        0o555u32
    } else {
        0o755u32
    };
    kind | perm
}

/// The `i32` reply of a call that names a mount: 0, or `-errno` for the
/// mount it could not use or the operation's failure.
fn status(r: Result<io::Result<()>, i32>) -> i32 {
    match r {
        Ok(Ok(())) => 0,
        Ok(Err(e)) => neg_errno(e),
        Err(code) => -code,
    }
}

/// A 4-byte `-errno` Vec, for `code`.
fn status_vec(code: i32) -> Vec<u8> {
    (-code).to_le_bytes().to_vec()
}

/// Encode an I/O error as a 4-byte `-errno` Vec.
fn errno_vec(e: io::Error) -> Vec<u8> {
    neg_errno(e).to_le_bytes().to_vec()
}

/// Convert an I/O error to `-errno` as i32.
fn neg_errno(e: io::Error) -> i32 {
    let code = errno::from_io(&e);
    trace!(errno = code, err = %e, "hostfs: operation failed");
    -code
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A directory listing stops just past what one reply holds, instead
    /// of reading the whole directory into host memory first.
    #[test]
    fn directory_listing_stops_at_the_reply_budget() {
        let dir = tempfile::tempdir().unwrap();
        // 100-byte names: about 670 fill a reply, so 2,000 overflow it.
        for i in 0..2000 {
            std::fs::write(dir.path().join(format!("{i:0100}")), "").unwrap();
        }
        let backend = DirBackend(Dir::open_ambient_dir(dir.path(), ambient_authority()).unwrap());
        let listed = backend.list("").unwrap();
        let size: usize = LIST_HDR
            + listed
                .iter()
                .map(|e| LIST_ENTRY_HDR + e.name.len())
                .sum::<usize>();
        assert!(size > HOST_CALL_MAX, "the listing must still overflow");
        assert!(listed.len() < 2000, "listed {} entries", listed.len());
        assert_eq!(
            listed.len(),
            HOST_CALL_MAX.saturating_sub(LIST_HDR) / 103 + 1
        );
    }
}
