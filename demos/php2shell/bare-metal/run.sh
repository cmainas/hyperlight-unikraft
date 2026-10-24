#!/bin/bash
# Run the payload on a normal host, for contrast with the hluk run.
#
# There is no host PHP requirement: the payload runs in a throwaway
# `php:cli` container with planted fake secrets, a writable webroot, and a
# C2 listener — so on "bare metal" every phase succeeds.  Everything is
# confined to a temp directory and torn down on exit; nothing touches the
# real filesystem.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEMO_DIR="$(dirname "$SCRIPT_DIR")"

command -v docker >/dev/null || { echo "this demo's bare-metal run needs Docker" >&2; exit 1; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/php2shell-demo.XXXXXX")
trap 'kill "${C2_PID:-}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

# ── Plant fake secrets in a throwaway HOME ───────────────────────────
mkdir -p "$WORK/home/.ssh" "$WORK/home/.aws" "$WORK/webroot"
cat > "$WORK/home/.ssh/id_rsa" <<'EOF'
-----BEGIN OPENSSH PRIVATE KEY-----
FAKE-KEY-FOR-DEMO-ONLY-NOT-A-REAL-SECRET-DO-NOT-USE-ANYWHERE
-----END OPENSSH PRIVATE KEY-----
EOF
cat > "$WORK/home/.aws/credentials" <<'EOF'
[default]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
EOF
cp "$DEMO_DIR/payload.php" "$WORK/webroot/payload.php"
cp "$DEMO_DIR/webroot/index.php" "$WORK/webroot/index.php"

# ── A C2 listener on the host loopback ───────────────────────────────
python3 "$DEMO_DIR/c2_server.py" &
C2_PID=$!
sleep 1

echo "== Bare metal: the payload runs with a popped PHP engine =="
# --network=host so the container's 127.0.0.1 is the host's C2; the fake
# HOME and a writable webroot are mounted in; env secrets are exported.
docker run --rm --network=host \
    -e HOME=/home -e DOCROOT=/webroot -e C2_HOST=127.0.0.1 -e C2_PORT=9000 \
    -e GITHUB_TOKEN=ghp_FAKEdemotokennotreal \
    -e AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY \
    -v "$WORK/home:/home" -v "$WORK/webroot:/webroot" \
    php:8.5-cli php /webroot/payload.php

echo ""
echo "-- Webroot after the run (the dropped webshell persists) --"
ls -1 "$WORK/webroot"
