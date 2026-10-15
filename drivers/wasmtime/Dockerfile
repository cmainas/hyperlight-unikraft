# Wasmtime runtime for Hyperlight/Unikraft.
#
# The driver, hl_wasmtimedriver (drivers/wasmtime/driver), embeds
# Wasmtime and runs WebAssembly modules and components under WASI: the
# WebAssembly text a call carries, or a .wasm/.cwasm file in the guest
# filesystem or a mount.  It is a Rust static-PIE binary, so the rootfs is
# the driver and nothing else.
#
# Build context: repository root.

# The Rust rust-toolchain.toml pins: just build-rootfs passes its channel
# (docker build --build-arg RUST_VERSION=1.99.0 without it).
ARG RUST_VERSION
FROM rust:${RUST_VERSION}-bookworm AS build

COPY drivers/wasmtime/driver /src
WORKDIR /src
# A static PIE, as the elfloader wants (see examples/rust/README.md); arm64
# needs Rust 1.99 or later for one.  The getrandom crate calls the
# getrandom syscall directly: its default probe falls back to poll()ing
# /dev/random, which never reports ready in the guest.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    set -eu; \
    arch=$(uname -m); target=$arch-unknown-linux-gnu; \
    export RUSTFLAGS='-C target-feature=+crt-static -C relocation-model=pie --cfg getrandom_backend="linux_getrandom"'; \
    cargo build --release --locked --target $target && \
    cp target/$target/release/hl_wasmtimedriver /hl_wasmtimedriver

# A PIE with no ELF interpreter and no shared libraries: a dynamic one would
# need a loader and libraries the rootfs does not carry.
RUN set -eu; \
    export LC_ALL=C; \
    readelf -hldW /hl_wasmtimedriver > /tmp/elf; \
    if ! grep -q 'Type: *DYN' /tmp/elf || grep -q 'INTERP\|(NEEDED)' /tmp/elf; then \
        echo "hl_wasmtimedriver is not a static PIE" >&2; exit 1; \
    fi; \
    mkdir -p /rootfs/tmp /rootfs/usr/local/bin; \
    cp /hl_wasmtimedriver /rootfs/usr/local/bin/

FROM scratch
COPY --from=build /rootfs/ /
