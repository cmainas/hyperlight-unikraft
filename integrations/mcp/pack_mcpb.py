"""Packs mcpb/ into a Claude Desktop Extension, as `mcpb pack` does, without npm's code.

The publish workflow builds the released `.mcpb` with this, in the job whose files are published; CI runs it too, and
validates its output with the mcpb CLI.

Usage: python pack_mcpb.py OUTPUT.mcpb
"""

import os
import stat
import sys
import zipfile
from pathlib import Path


SOURCE = Path(__file__).parent / "mcpb"


def ignored() -> set[str]:
    # .mcpbignore's names of files or directories; anything else it could hold is refused rather than misread
    names = set()
    for line in (SOURCE / ".mcpbignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.strip("/")
        if set(name) & set("*?[!/\\ "):
            sys.exit(f"mcpb/.mcpbignore: {line!r} is not a plain name, which is all this packer honours")
        names.add(name)
    return names


def pack(output: Path):
    # Where the walk would find it, or an earlier bundle, and pack it into this one
    if output.resolve().is_relative_to(SOURCE.resolve()):
        sys.exit(f"{output}: must not be inside {SOURCE}, which is what is packed")
    skip = ignored()
    files = []
    for directory, dirnames, filenames in os.walk(SOURCE):
        # Dotfiles and directories too, as mcpb pack leaves them out; pruned, not walked: a venv holds thousands
        dirnames[:] = sorted(name for name in dirnames if name not in skip and not name.startswith("."))
        files += [Path(directory, name) for name in filenames if name not in skip and not name.startswith(".")]
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(files):
            # A fixed time, so that the same commit packs to the same bytes: CI's and the release's bundle alike
            entry = zipfile.ZipInfo("/".join(path.relative_to(SOURCE).parts), date_time=(1980, 1, 1, 0, 0, 0))
            # A regular file, as mcpb pack marks it, and not only its permissions
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.create_system = 3  # Unix, whichever system packs it
            bundle.writestr(entry, path.read_bytes(), zipfile.ZIP_DEFLATED)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().splitlines()[-1])
    pack(Path(sys.argv[1]))
