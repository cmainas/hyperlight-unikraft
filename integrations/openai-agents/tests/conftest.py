"""The stand-in for `hluk` the tests run instead of the real CLI."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest


# A stand-in for `hluk run`: it records its arguments, then runs the guest script on the host.
# Each run of the real CLI maps every mount at its own path, so the script works unchanged.
_FAKE_HLUK = textwrap.dedent(
    """\
    import json, os, signal, subprocess, sys, time

    args = sys.argv[1:]
    if args == ["--version"]:
        print(os.environ.get("FAKE_HLUK_VERSION", "hluk 0.19.0"))
        sys.exit(0)
    log = os.environ["FAKE_HLUK_LOG"]
    mode = os.environ.get("FAKE_HLUK_MODE", "run")
    record = {"args": args}
    if not args[-1].startswith("-") and os.path.exists(args[-1]):
        spec = os.path.join(os.path.dirname(args[-1]), "spec.json")
        record["spec_mode"] = oct(os.stat(spec).st_mode & 0o777)
        with open(spec) as file:
            record["spec"] = file.read()
    with open(log, "a") as file:
        file.write(json.dumps(record) + "\\n")
    if "--exec" in args:
        if mode == "no-guest":
            print("No hypervisor was found", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    if mode == "crash":
        print("guest crashed", file=sys.stderr)
        sys.exit(1)
    if mode == "sleep":
        # Whole once there, as the tests read it as soon as it appears
        with open(os.environ["FAKE_HLUK_PID"] + ".tmp", "w") as file:
            file.write(str(os.getpid()))
        os.replace(os.environ["FAKE_HLUK_PID"] + ".tmp", os.environ["FAKE_HLUK_PID"])
        time.sleep(60)
    guest = subprocess.run([sys.executable, args[-1]], check=False)
    status = os.path.join(os.path.dirname(args[-1]), "status")
    output = os.path.join(os.path.dirname(args[-1]), "stdout")
    if mode == "linked-stdout":
        os.remove(output)
        os.symlink(os.environ["FAKE_HLUK_SECRET"], output)
    if mode == "fifo-stdout":
        os.remove(output)
        os.mkfifo(output)
    if mode == "finished-then-sleep":
        # The command finished, then the guest was slow to end
        with open(os.environ["FAKE_HLUK_PID"] + ".tmp", "w") as file:
            file.write(str(os.getpid()))
        os.replace(os.environ["FAKE_HLUK_PID"] + ".tmp", os.environ["FAKE_HLUK_PID"])
        time.sleep(60)
    if mode == "killed":
        # The command ran, then the guest stopped before the status was written: `kill -9 -1`.
        os.remove(status)
        print("error: the guest exited with status 137", file=sys.stderr)
        sys.exit(137)
    if mode == "aborted":
        # The command's memory fault aborted the guest's kernel.
        os.remove(status)
        print("CRIT: Cannot deliver signal 11 for pf at 0x0\\nerror: Guest aborted: 0", file=sys.stderr)
        sys.exit(1)
    if mode == "aborted-then-more":
        # hluk's abort line, then another: hluk's own failure after the guest's
        os.remove(status)
        print("CRIT: x\\nerror: Guest aborted: 0\\nerror: cannot save the snapshot", file=sys.stderr)
        sys.exit(1)
    if mode == "forged":
        # The guest printed hluk's last line itself, then hluk failed otherwise.
        os.remove(status)
        print("error: the guest exited with status 137", file=sys.stderr)
        sys.exit(1)
    if mode == "failed-during":
        # The command ran, then `hluk` or the hypervisor failed.
        os.remove(status)
        print("Internal Hyperlight VM error", file=sys.stderr)
        sys.exit(1)
    if mode == "linked-status":
        target = status + ".target"
        with open(target, "w") as file:
            file.write("0")
        os.remove(status)
        os.symlink(target, status)
    # As hluk exits when its guest's script fails
    code = guest.returncode if guest.returncode >= 0 else 128 - guest.returncode
    if code:
        print(f"error: the guest exited with status {code}", file=sys.stderr)
        sys.exit(code)
    """
)


@pytest.fixture
def fake_hluk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "hluk"
    path.write_text(f"#!{sys.executable}\n{_FAKE_HLUK}")
    path.chmod(0o755)
    log = tmp_path / "hluk.log"
    monkeypatch.setenv("FAKE_HLUK_LOG", str(log))
    monkeypatch.setenv("FAKE_HLUK_PID", str(tmp_path / "hluk.pid"))
    monkeypatch.delenv("FAKE_HLUK_MODE", raising=False)
    return path


@pytest.fixture(autouse=True)
def _state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The SDK saves a snapshot of each workspace the runner made under $XDG_STATE_HOME, and keeps it.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
