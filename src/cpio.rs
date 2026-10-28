// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Hyperlight Authors.

//! Turn a filesystem tar (what `docker export` streams) into the newc CPIO
//! the kernel takes as its initrd, the way `find . | cpio -o -H newc` lays
//! it out: a `.` entry first, then every path bare of a leading `./`, so
//! the driver detection and the guest see the same names as in the
//! published images.  Pure Rust, so building an initrd needs no host
//! `cpio`; `hluk build` uses it on the output of `docker export`.
//!
//! ```no_run
//! # fn main() -> std::io::Result<()> {
//! let tar = std::io::BufReader::new(std::fs::File::open("rootfs.tar")?);
//! let out = std::io::BufWriter::new(std::fs::File::create("rootfs.cpio")?);
//! hyperlight_unikraft::cpio::from_tar(tar, out)?;
//! # Ok(())
//! # }
//! ```

use std::fmt::Write as _;
use std::io::{self, Read, Write};

const S_IFREG: u32 = 0o100000;
const S_IFDIR: u32 = 0o040000;
const S_IFLNK: u32 = 0o120000;
const S_IFCHR: u32 = 0o020000;
const S_IFBLK: u32 = 0o060000;
const S_IFIFO: u32 = 0o010000;

/// `docker export` hands these over as the empty files it bind-mounts at
/// run time, so the archive gets working ones instead: a hostname that
/// resolves (`unikraft` is Unikraft's `gethostname()`), a lookup order,
/// and public resolvers.  `single-request` serialises the A and AAAA
/// queries glibc would otherwise send in parallel, which the guest's socket
/// layer does not support.  Matches the justfile's `_export-cpio`; a
/// `--resolv-conf` at run time still overrides the resolver file.
const FIXED_ETC: &[(&str, &str)] = &[
    (
        "etc/hosts",
        "127.0.0.1 localhost unikraft\n::1 localhost unikraft\n",
    ),
    ("etc/nsswitch.conf", "hosts: files dns\n"),
    (
        "etc/resolv.conf",
        "nameserver 8.8.8.8\nnameserver 1.1.1.1\noptions single-request\n",
    ),
];

/// What a conversion produced.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub struct Stats {
    /// Entries written, the fixed `/etc` files among them; the root and
    /// the trailer are not counted.
    pub entries: u64,
    /// File and symlink contents written.  Headers and padding make the
    /// archive larger than this.
    pub bytes: u64,
}

/// The ownership, permissions and time an entry carries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Attrs {
    pub mode: u32,
    pub uid: u32,
    pub gid: u32,
    pub mtime: u32,
}

impl Attrs {
    /// Root-owned, with `mode`, dated the epoch.
    pub const fn root(mode: u32) -> Attrs {
        Attrs {
            mode,
            uid: 0,
            gid: 0,
            mtime: 0,
        }
    }
}

/// A newc CPIO writer.
pub(crate) struct Writer<W: Write> {
    out: W,
    ino: u32,
    bytes: u64,
    entries: u64,
    /// One header at a time, reused: an allocation per entry adds up over
    /// a rootfs of tens of thousands.
    header: String,
}

impl<W: Write> Writer<W> {
    /// Start an archive with its root `.` directory.
    pub fn new(out: W) -> io::Result<Self> {
        let mut w = Writer {
            out,
            ino: 0,
            bytes: 0,
            entries: 0,
            header: String::new(),
        };
        w.header(".", S_IFDIR | 0o755, Attrs::root(0), 2, 0, (0, 0))?;
        Ok(w)
    }

    pub fn dir(&mut self, name: &str, attrs: Attrs) -> io::Result<()> {
        self.header(name, S_IFDIR | (attrs.mode & 0o7777), attrs, 2, 0, (0, 0))
    }

    pub fn file(
        &mut self,
        name: &str,
        attrs: Attrs,
        size: u64,
        data: &mut impl Read,
    ) -> io::Result<()> {
        self.header(
            name,
            S_IFREG | (attrs.mode & 0o7777),
            attrs,
            1,
            size,
            (0, 0),
        )?;
        let copied = io::copy(&mut data.take(size), &mut self.out)?;
        if copied != size {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                format!("{copied} of {size} bytes"),
            ));
        }
        self.bytes += copied;
        self.pad(copied)
    }

    pub fn symlink(&mut self, name: &str, target: &str, attrs: Attrs) -> io::Result<()> {
        let target = target.as_bytes();
        self.header(name, S_IFLNK | 0o777, attrs, 1, target.len() as u64, (0, 0))?;
        self.out.write_all(target)?;
        self.bytes += target.len() as u64;
        self.pad(target.len() as u64)
    }

    /// A character or block device, or a FIFO; `kind` is one of the
    /// `S_IF*` type bits.
    pub fn special(
        &mut self,
        name: &str,
        kind: u32,
        attrs: Attrs,
        rdev: (u32, u32),
    ) -> io::Result<()> {
        self.header(name, kind | (attrs.mode & 0o7777), attrs, 1, 0, rdev)
    }

    /// Write the trailer and hand the writer back.
    pub fn finish(mut self) -> io::Result<(W, Stats)> {
        self.header("TRAILER!!!", 0, Attrs::root(0), 1, 0, (0, 0))?;
        self.out.flush()?;
        let stats = Stats {
            // The root and the trailer are bookkeeping, not content.
            entries: self.entries - 2,
            bytes: self.bytes,
        };
        Ok((self.out, stats))
    }

    fn header(
        &mut self,
        name: &str,
        mode: u32,
        attrs: Attrs,
        nlink: u32,
        filesize: u64,
        rdev: (u32, u32),
    ) -> io::Result<()> {
        self.ino += 1;
        self.entries += 1;
        let filesize = u32::try_from(filesize).map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("{filesize} bytes, over the 4 GiB a newc entry can hold"),
            )
        })?;
        let namesize = name.len() as u32 + 1;
        // Thirteen 8-digit hex fields after the magic, then the name: ino,
        // mode, uid, gid, nlink, mtime, filesize, dev major and minor, rdev
        // major and minor, namesize, and a check field unused by 070701.
        let fields = [
            self.ino,
            mode,
            attrs.uid,
            attrs.gid,
            nlink,
            attrs.mtime,
            filesize,
            0,
            0,
            rdev.0,
            rdev.1,
            namesize,
            0,
        ];
        let header = &mut self.header;
        header.clear();
        header.push_str("070701");
        for field in fields {
            let _ = write!(header, "{field:08X}");
        }
        header.push_str(name);
        header.push('\0');
        self.out.write_all(header.as_bytes())?;
        // The name is padded so the data starts on a 4-byte boundary.
        self.pad(110 + namesize as u64)
    }

    fn pad(&mut self, written: u64) -> io::Result<()> {
        let pad = (4 - (written % 4)) % 4;
        self.out.write_all(&[0u8; 4][..pad as usize])
    }
}

/// Convert a tar stream to a CPIO archive.
///
/// The archive always carries its own `etc/hosts`, `etc/nsswitch.conf`
/// and `etc/resolv.conf`, whatever the tar holds at those paths:
/// `docker export` empties them, and an initrd without them cannot
/// resolve names.  The hostname is `unikraft` and the resolvers are
/// 8.8.8.8 and 1.1.1.1; [`SandboxBuilder::resolv_conf`] overrides the
/// resolver file at boot.
///
/// `tar` is read to its end, past the archive's end blocks, so a process
/// writing it through a pipe is not cut off; anything but zeros there is
/// an error.  Buffer it if it is a pipe or a file, since the archive is
/// read a block at a time.  As in `docker export`, a directory must come
/// before what it holds: the guest creates no missing parents.
///
/// [`SandboxBuilder::resolv_conf`]: crate::SandboxBuilder::resolv_conf
pub fn from_tar(tar: impl Read, out: impl Write) -> io::Result<Stats> {
    let mut archive = tar::Archive::new(tar);
    let mut w = Writer::new(out).map_err(|e| context("writing .", e))?;
    let mut has_etc = false;
    // Reused, like the writer's header buffer.
    let mut name = String::new();
    let mut link = String::new();
    let mut absolute = String::new();
    for entry in archive.entries().map_err(|e| context("reading tar", e))? {
        let mut entry = entry.map_err(|e| context("reading tar", e))?;
        {
            let path = entry.path().map_err(|e| context("reading tar", e))?;
            let path = path.to_string_lossy();
            let Some(normalized) = normalize(&path) else {
                continue;
            };
            name.clear();
            name.push_str(normalized);
        }
        let name = name.as_str();
        if FIXED_ETC.iter().any(|(fixed, _)| *fixed == name) {
            continue;
        }
        let header = entry.header();
        let kind = header.entry_type();
        // Extended headers are consumed by the tar reader; anything else
        // (a sparse file, a GNU volume header) has no place in a rootfs,
        // and is dropped before its ids can refuse the image.
        use tar::EntryType as E;
        if !matches!(
            kind,
            E::Directory
                | E::Regular
                | E::Continuous
                | E::Symlink
                | E::Link
                | E::Char
                | E::Block
                | E::Fifo
        ) {
            continue;
        }
        if name == "etc" {
            // The fixed files go under it, and through a link they would
            // land somewhere else, or nowhere.
            if !kind.is_dir() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "etc: not a directory, and /etc/hosts and the rest need one",
                ));
            }
            has_etc = true;
        }
        let header = entry.header();
        let id = |id: u64, what: &str| {
            u32::try_from(id).map_err(|_| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("{name}: {what} {id}, over the 32 bits a newc entry holds"),
                )
            })
        };
        let attrs = Attrs {
            mode: header.mode().unwrap_or(0o644),
            uid: id(header.uid().unwrap_or(0), "uid")?,
            gid: id(header.gid().unwrap_or(0), "gid")?,
            // A time past 2106 is no reason to refuse an image.
            mtime: u32::try_from(header.mtime().unwrap_or(0)).unwrap_or(u32::MAX),
        };
        link.clear();
        if let Some(target) = entry.link_name().ok().flatten() {
            link.push_str(&target.to_string_lossy());
        }
        let result = match kind {
            tar::EntryType::Directory => w.dir(name, attrs),
            tar::EntryType::Regular | tar::EntryType::Continuous => {
                // The entry's, not the header's: past 8 GiB the size is in
                // a PAX record and the header says 0.
                let size = entry.size();
                w.file(name, attrs, size, &mut entry)
            }
            // ukcpio cannot make a link to nothing.
            tar::EntryType::Symlink if link.is_empty() => Ok(()),
            tar::EntryType::Symlink => w.symlink(name, &link, attrs),
            // A hard link becomes a symlink to its target: the archive is
            // streamed, so the target's data is gone by the time the link
            // arrives, and a read-only rootfs cannot tell the two apart.
            tar::EntryType::Link => match normalize(&link) {
                Some(target) => {
                    absolute.clear();
                    absolute.push('/');
                    absolute.push_str(target);
                    w.symlink(name, &absolute, attrs)
                }
                // A link to nothing, or to the root, has no file to stand for.
                None => Ok(()),
            },
            tar::EntryType::Char | tar::EntryType::Block | tar::EntryType::Fifo => {
                let kind_bits = match kind {
                    tar::EntryType::Char => S_IFCHR,
                    tar::EntryType::Block => S_IFBLK,
                    _ => S_IFIFO,
                };
                let rdev = (
                    header.device_major().ok().flatten().unwrap_or(0),
                    header.device_minor().ok().flatten().unwrap_or(0),
                );
                w.special(name, kind_bits, attrs, rdev)
            }
            // Dropped above.
            _ => Ok(()),
        };
        result.map_err(|e| context(&format!("writing {name}"), e))?;
    }
    if !has_etc {
        w.dir("etc", Attrs::root(0o755))
            .map_err(|e| context("writing etc", e))?;
    }
    for (name, contents) in FIXED_ETC {
        w.file(
            name,
            Attrs::root(0o644),
            contents.len() as u64,
            &mut contents.as_bytes(),
        )
        .map_err(|e| context(&format!("writing {name}"), e))?;
    }
    let (_, stats) = w
        .finish()
        .map_err(|e| context("finishing the archive", e))?;
    // The reader stops at the first of the two end blocks, or at a
    // stream that ends between entries, which looks the same to it.  The
    // second block tells them apart.  Past it there should be only
    // padding; anything else is a second archive, which would otherwise
    // be lost without a word.
    let mut rest = archive.into_inner();
    let mut buf = [0u8; 8192];
    let mut zeros = 0;
    loop {
        match rest.read(&mut buf) {
            Ok(0) => break,
            Ok(n) if buf[..n].iter().all(|&b| b == 0) => zeros += n,
            Ok(_) => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "data after the end of the tar: a second archive?",
                ));
            }
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(context("reading tar", e)),
        }
    }
    if zeros < 512 {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "reading tar: no end blocks, so cut short?",
        ));
    }
    Ok(stats)
}

/// `e` with what was being done, keeping its kind, so a caller can
/// still tell a full disk from a bad archive.
fn context(what: &str, e: io::Error) -> io::Error {
    io::Error::new(e.kind(), format!("{what}: {e}"))
}

/// A tar path as a cpio name: no leading `./` or `/`, no trailing `/`;
/// `None` for the root itself.
fn normalize(path: &str) -> Option<&str> {
    let mut name = path;
    loop {
        if let Some(rest) = name.strip_prefix("./") {
            name = rest;
        } else if let Some(rest) = name.strip_prefix('/') {
            name = rest;
        } else {
            break;
        }
    }
    let name = name.trim_end_matches('/');
    if name.is_empty() || name == "." {
        None
    } else {
        Some(name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Read a newc archive back as (name, mode, data) triples.
    fn read_cpio(bytes: &[u8]) -> Vec<(String, u32, Vec<u8>)> {
        let mut out = Vec::new();
        let mut pos = 0;
        loop {
            let header = std::str::from_utf8(&bytes[pos..pos + 110]).unwrap();
            assert_eq!(&header[..6], "070701");
            let field = |i: usize| u32::from_str_radix(&header[6 + i * 8..14 + i * 8], 16).unwrap();
            let (mode, filesize, namesize) = (field(1), field(6) as usize, field(11) as usize);
            let name_start = pos + 110;
            let name = std::str::from_utf8(&bytes[name_start..name_start + namesize - 1])
                .unwrap()
                .to_string();
            let data_start = (name_start + namesize).next_multiple_of(4);
            let data = bytes[data_start..data_start + filesize].to_vec();
            pos = (data_start + filesize).next_multiple_of(4);
            if name == "TRAILER!!!" {
                break;
            }
            out.push((name, mode, data));
        }
        out
    }

    fn tar_with(entries: &[(&str, tar::EntryType, &[u8], Option<&str>)]) -> Vec<u8> {
        let mut b = tar::Builder::new(Vec::new());
        for (name, kind, data, link) in entries {
            let mut h = tar::Header::new_gnu();
            h.set_entry_type(*kind);
            h.set_mode(if kind.is_dir() { 0o755 } else { 0o644 });
            h.set_uid(0);
            h.set_gid(0);
            h.set_mtime(1_700_000_000);
            h.set_size(data.len() as u64);
            if let Some(link) = link {
                b.append_link(&mut h, name, link).unwrap();
            } else {
                b.append_data(&mut h, name, *data).unwrap();
            }
        }
        b.into_inner().unwrap()
    }

    #[test]
    fn lays_out_like_find_cpio() {
        let tar = tar_with(&[
            ("./etc/", tar::EntryType::Directory, b"", None),
            ("./etc/hosts", tar::EntryType::Regular, b"", None),
            ("usr/", tar::EntryType::Directory, b"", None),
            ("usr/local/", tar::EntryType::Directory, b"", None),
            ("usr/local/bin/", tar::EntryType::Directory, b"", None),
            (
                "usr/local/bin/hl_pydriver",
                tar::EntryType::Regular,
                b"ELF\x7f",
                None,
            ),
            ("bin/sh", tar::EntryType::Symlink, b"", Some("busybox")),
            ("bin/ash", tar::EntryType::Link, b"", Some("bin/busybox")),
        ]);
        let mut out = Vec::new();
        let stats = from_tar(&tar[..], &mut out).unwrap();
        let entries = read_cpio(&out);
        let names: Vec<&str> = entries.iter().map(|(n, _, _)| n.as_str()).collect();
        assert_eq!(
            names,
            [
                ".",
                "etc",
                "usr",
                "usr/local",
                "usr/local/bin",
                "usr/local/bin/hl_pydriver",
                "bin/sh",
                "bin/ash",
                "etc/hosts",
                "etc/nsswitch.conf",
                "etc/resolv.conf",
            ]
        );
        let driver = &entries[5];
        assert_eq!(driver.1, S_IFREG | 0o644);
        assert_eq!(driver.2, b"ELF\x7f");
        let sh = &entries[6];
        assert_eq!(sh.1, S_IFLNK | 0o777);
        assert_eq!(sh.2, b"busybox");
        assert_eq!(
            entries[7].2, b"/bin/busybox",
            "a hard link becomes an absolute symlink"
        );
        assert!(
            std::str::from_utf8(&entries[8].2)
                .unwrap()
                .contains("unikraft")
        );
        assert!(
            std::str::from_utf8(&entries[10].2)
                .unwrap()
                .contains("single-request")
        );
        assert_eq!(stats.entries, 10);
        // Every header is 4-byte aligned, which the reader above asserts by
        // parsing; the archive itself ends aligned too.
        assert_eq!(out.len() % 4, 0);
    }

    #[test]
    fn adds_etc_when_the_image_has_none() {
        let tar = tar_with(&[("app", tar::EntryType::Regular, b"x", None)]);
        let mut out = Vec::new();
        from_tar(&tar[..], &mut out).unwrap();
        let names: Vec<String> = read_cpio(&out).into_iter().map(|(n, _, _)| n).collect();
        assert_eq!(names[..3], [".", "app", "etc"]);
        assert!(names.contains(&"etc/resolv.conf".to_string()));
    }

    #[test]
    fn normalizes_names() {
        assert_eq!(normalize("./a/b/"), Some("a/b"));
        assert_eq!(normalize("/a"), Some("a"));
        assert_eq!(normalize("./"), None);
        assert_eq!(normalize("."), None);
        assert_eq!(normalize("a"), Some("a"));
    }

    #[test]
    fn checks_ids_and_clamps_times() {
        let entry = |uid: u64, mtime: u64| {
            let mut h = tar::Header::new_gnu();
            h.set_entry_type(tar::EntryType::Regular);
            h.set_path("app").unwrap();
            h.set_mode(0o644);
            h.set_size(0);
            h.set_uid(uid);
            h.set_mtime(mtime);
            h.set_cksum();
            let mut tar = h.as_bytes().to_vec();
            tar.resize(tar.len() + 1024, 0);
            tar
        };
        let err = from_tar(&entry(1 << 32, 0)[..], io::sink()).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidData, "{err}");
        let mut out = Vec::new();
        from_tar(&entry(1000, (1 << 32) + 5)[..], &mut out).unwrap();
        // After the 112 bytes of `.`: the magic, then mtime is field 5.
        assert_eq!(&out[112 + 6 + 5 * 8..][..8], b"FFFFFFFF");
    }

    #[test]
    fn refuses_an_etc_that_is_no_directory() {
        let tar = tar_with(&[("etc", tar::EntryType::Symlink, b"", Some("private/etc"))]);
        let err = from_tar(&tar[..], io::sink()).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidData, "{err}");
    }

    #[test]
    fn drops_unsupported_entries_before_checking_them() {
        let mut h = tar::Header::new_gnu();
        // A GNU volume header.
        h.set_entry_type(tar::EntryType::new(b'V'));
        h.set_path("volume").unwrap();
        h.set_size(0);
        h.set_uid(1 << 32);
        h.set_cksum();
        let mut tar = h.as_bytes().to_vec();
        tar.resize(tar.len() + 1024, 0);
        let mut out = Vec::new();
        from_tar(&tar[..], &mut out).unwrap();
        let names: Vec<String> = read_cpio(&out).into_iter().map(|(n, _, _)| n).collect();
        assert!(!names.contains(&"volume".to_string()), "{names:?}");
    }

    #[test]
    fn takes_sizes_from_pax_records() {
        // What Go's archive/tar, and so docker export, writes for a file
        // too large for the header: size 0 there, the real one in PAX.
        let mut b = tar::Builder::new(Vec::new());
        b.append_pax_extensions([("size", &b"5"[..])]).unwrap();
        let mut h = tar::Header::new_ustar();
        h.set_entry_type(tar::EntryType::Regular);
        h.set_path("app").unwrap();
        h.set_mode(0o644);
        h.set_size(0);
        h.set_cksum();
        // As given, header and data, with no size of its own.
        b.append(&h, &b"hello"[..]).unwrap();
        let tar = b.into_inner().unwrap();
        let mut out = Vec::new();
        from_tar(&tar[..], &mut out).unwrap();
        let app = read_cpio(&out)
            .into_iter()
            .find(|(n, _, _)| n == "app")
            .unwrap();
        assert_eq!(app.2, b"hello");
    }

    #[test]
    fn takes_ids_from_pax_records() {
        // A uid past the header's 7 octal digits, as docker export writes
        // it for an OpenShift-style random uid.  The tar reader copies PAX
        // ids into the header, unlike the size; this keeps it so.
        let mut b = tar::Builder::new(Vec::new());
        b.append_pax_extensions([("uid", &b"1000680000"[..]), ("gid", &b"1000680001"[..])])
            .unwrap();
        let mut h = tar::Header::new_ustar();
        h.set_entry_type(tar::EntryType::Regular);
        h.set_path("app").unwrap();
        h.set_mode(0o644);
        h.set_size(0);
        h.set_cksum();
        b.append(&h, io::empty()).unwrap();
        let tar = b.into_inner().unwrap();
        let mut out = Vec::new();
        from_tar(&tar[..], &mut out).unwrap();
        // After the 112 bytes of `.`: the magic, then uid and gid are
        // fields 2 and 3.
        let field = |i: usize| &out[112 + 6 + i * 8..][..8];
        assert_eq!(field(2), format!("{:08X}", 1000680000).as_bytes());
        assert_eq!(field(3), format!("{:08X}", 1000680001).as_bytes());
    }

    #[test]
    fn reads_the_tar_to_its_end() {
        let mut tar = tar_with(&[("app", tar::EntryType::Regular, b"x", None)]);
        // Record padding after the end blocks, as docker export writes.
        tar.resize(tar.len() + 8192, 0);
        let mut rest = &tar[..];
        from_tar(&mut rest, io::sink()).unwrap();
        assert!(rest.is_empty(), "{} bytes left unread", rest.len());
    }

    #[test]
    fn refuses_a_second_archive() {
        let one = tar_with(&[("a", tar::EntryType::Regular, b"x", None)]);
        let two = tar_with(&[("b", tar::EntryType::Regular, b"y", None)]);
        let err = from_tar(&[one, two].concat()[..], io::sink()).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidData, "{err}");
    }

    #[test]
    fn refuses_a_tar_cut_short_between_entries() {
        let whole = tar_with(&[
            ("a", tar::EntryType::Regular, b"x", None),
            ("b", tar::EntryType::Regular, b"y", None),
        ]);
        // After `a`'s header and data block, before `b`.
        for tar in [&whole[..1024], &[][..]] {
            let err = from_tar(tar, io::sink()).unwrap_err();
            assert_eq!(err.kind(), io::ErrorKind::UnexpectedEof, "{err}");
        }
    }

    #[test]
    fn drops_links_to_nothing() {
        let tar = tar_with(&[
            ("bin/ash", tar::EntryType::Link, b"", Some("./")),
            ("bin/sh", tar::EntryType::Symlink, b"", None),
        ]);
        let mut out = Vec::new();
        from_tar(&tar[..], &mut out).unwrap();
        let names: Vec<String> = read_cpio(&out).into_iter().map(|(n, _, _)| n).collect();
        assert!(!names.iter().any(|n| n.starts_with("bin/")), "{names:?}");
    }

    #[test]
    fn errors_keep_their_kind() {
        // A newc entry holds at most 4 GiB; the header says more.
        let mut header = tar::Header::new_gnu();
        header.set_entry_type(tar::EntryType::Regular);
        header.set_mode(0o644);
        header.set_path("big").unwrap();
        header.set_size(1 << 32);
        header.set_cksum();
        let mut tar = header.as_bytes().to_vec();
        tar.resize(tar.len() + 1024, 0);
        let err = from_tar(&tar[..], io::sink()).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
        assert_eq!(
            err.to_string(),
            "writing big: 4294967296 bytes, over the 4 GiB a newc entry can hold"
        );
    }
}
