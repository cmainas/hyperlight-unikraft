#!/usr/bin/env bash
# Shows what fills a self-hosted runner's disk, and removes what hluk and
# its tests leave there: these runners keep their disk between jobs.
# .github/actions/arm64-host runs it before anything else, so it needs no
# free space itself.  It only touches the runner user's own files.
#   MIN_FREE_GB  below this much free space after the cleanup, also remove
#                this checkout's target/ and cargo's download cache
#                (default 20)
#   SWEEP=1      remove all that hluk and this repo's jobs keep: the whole
#                hluk cache, every hluk-* temp directory, and this repo's
#                target/ directories (.github/workflows/runner-disk.yml)
set -uo pipefail

min_free_kb=$((${MIN_FREE_GB:-20} * 1024 * 1024))
cache="${HLUK_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/hluk}"
tmp="${TMPDIR:-/tmp}"
tmp="${tmp%/}"

free_kb() { df -Pk "$HOME" | awk 'NR == 2 { print $4 }'; }

# The paths given, largest first, in GB.
sizes() {
    for p in "$@"; do
        [ -e "$p" ] && du -sk "$p" 2>/dev/null
    done | sort -rn | awk '{ printf "%7.1f GB  %s\n", $1 / 1048576, $2 }'
}

before=$(free_kb)
echo "== Disk"
df -h "$HOME"
echo "== Largest in $HOME"
sizes "$HOME"/* "$HOME"/.[!.]* | head -15
if [ -n "${RUNNER_WORKSPACE:-}" ]; then
    echo "== Runner work directories"
    sizes "$RUNNER_WORKSPACE"/../* "$RUNNER_WORKSPACE"/*/target | head -15
fi
echo "== hluk cache ($cache)"
sizes "$cache"/* "$cache"/snapshots/* | head -15
echo "== Largest in $tmp"
sizes "$tmp"/* | head -10

# Warm snapshots and pulled images nobody has saved or pulled for a few
# days: an earlier hluk's or kernel's, which no run restores any more,
# and the next run that wants one makes it again.
echo "== Cleanup"
if [ "${SWEEP:-}" = 1 ]; then
    rm -rf "$cache" && echo "removed $cache"
    find "$tmp" -mindepth 1 -maxdepth 1 -name 'hluk-*' -print -exec rm -rf {} +
    if [ -n "${RUNNER_WORKSPACE:-}" ]; then
        for t in "$RUNNER_WORKSPACE"/*/target; do
            [ -d "$t" ] && rm -rf "$t" && echo "removed $t"
        done
    fi
fi
if [ -d "$cache/snapshots" ]; then
    find "$cache/snapshots" -mindepth 1 -maxdepth 1 \( -name '*.part' -o -mtime +2 \) \
        -print -exec rm -rf {} +
fi
if [ -d "$cache/rootfs" ]; then
    find "$cache/rootfs" -type f -mtime +7 -print -delete
fi
# Test directories a killed test left behind (they are removed on drop).
find "$tmp" -mindepth 1 -maxdepth 1 -name 'hluk-*' -mtime +0 -print -exec rm -rf {} +

if [ "$(free_kb)" -lt "$min_free_kb" ] && [ -n "${GITHUB_WORKSPACE:-}" ]; then
    echo "Under ${MIN_FREE_GB:-20} GB free: removing $GITHUB_WORKSPACE/target and cargo's download cache"
    rm -rf "$GITHUB_WORKSPACE/target" "${CARGO_HOME:-$HOME/.cargo}/registry/cache"
fi

after=$(free_kb)
awk -v b="$before" -v a="$after" 'BEGIN { printf "== Freed %.1f GB; %.1f GB free\n", (a - b) / 1048576, a / 1048576 }'
