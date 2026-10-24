# PHP RCE containment demo

A safe, educational demo: when a flaw in the PHP engine hands an attacker unauthenticated remote code execution in the PHP process, how far does the payload get if the application runs inside a Hyperlight micro-VM with `hluk`?

## Background

Flaws of this class turn a request into arbitrary code running as the web application — no credentials, no user interaction. Once an attacker has code execution, the engine flaw itself stops mattering; what follows is the usual post-exploitation playbook, and that is what a sandbox has to contain:

1. Read secrets off the host — SSH keys, cloud credentials, `/etc/shadow`.
2. Harvest secrets from the process environment.
3. Exfiltrate to a command-and-control host.
4. Pivot to the cloud metadata service for instance credentials.
5. Drop a webshell into the document root for durable access.

## The demo

`payload.php` is that playbook. It runs **as if the engine were already popped** — the demo does not exploit any real flaw, it assumes the attacker won and asks what the payload can reach. A tiny legitimate app (`webroot/index.php`) runs alongside it to show the sandbox does not get in the way of real work.

Two runs, the same payload:

**Bare metal** (`just bare-metal`, a throwaway `php:cli` container with planted fake secrets and a C2 listener) — secrets stolen, exfiltration sent, webshell dropped and left behind. The attack succeeds.

**Hyperlight micro-VM** (`just run`, `--mount ./webroot:/app:ro` and no network) — the legitimate app still answers, and every phase is contained:

| Phase | What happens | Why |
|---|---|---|
| Host secrets | `~/.ssh/id_rsa`, `~/.aws/credentials`, `/etc/shadow` → blocked | hluk's cap-std mount scopes the filesystem to the mounted directory; there is no HOME and nothing outside `/app`. |
| Environment secrets | `GITHUB_TOKEN`, `AWS_SECRET_ACCESS_KEY` → not set | the host environment is never forwarded into the guest. |
| C2 exfiltration | `127.0.0.1:9000` → blocked | with no network policy the guest has no egress at all, and a scoped policy (`--net-allow`/`--net-block`) also blocks loopback, so host-local services stay out of reach. (Blanket `--net` is all-or-nothing and does permit loopback — the demo never uses it.) |
| Cloud metadata | `169.254.169.254` → blocked | every network policy blocks link-local (169.254.0.0/16, fe80::/10) unconditionally. |
| Persistence | webshell → `/app/shell.php` → blocked | the document root is mounted read-only, and a guest restored from its warm snapshot discards anything written to scratch anyway. |

`just run-scoped` adds `--net-allow example.com`: legitimate outbound works, while the C2 and the link-local metadata address stay blocked.

## Running it

Linux, with KVM and Docker. Build the PHP rootfs once from the repo root:

```bash
just build-rootfs php
```

Then, from this directory:

```bash
just bare-metal    # no sandbox: the attack succeeds
just run           # hluk, read-only mount, no network: contained
just run-scoped    # hluk, egress to example.com allowed: still contained
just c2            # a C2 listener, to watch exfil arrive (split terminal)
```

Everything the bare-metal run plants lives in a temporary directory that is removed on exit; nothing touches your real filesystem, and the secrets are fake.
