"""What a release publishes besides the package names its version, which must be the package's own."""

import json
import re
from pathlib import Path


ROOT = Path(__file__).parent.parent


def field(pyproject: Path, name: str) -> str:
    # Not tomllib, which Python 3.10 lacks
    match = re.search(rf"^{name} = (.+)$", pyproject.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"no {name} in {pyproject}"
    return match.group(1)


def version() -> str:
    return json.loads(field(ROOT / "pyproject.toml", "version"))


def test_registry_entry_names_this_version():
    entry = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert entry["version"] == version()
    assert [package["version"] for package in entry["packages"]] == [version()]
    # The registry checks the package's README on PyPI for its name
    assert f"mcp-name: {entry['name']}" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_desktop_extension_runs_this_version():
    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == version()
    assert json.loads(field(ROOT / "mcpb" / "pyproject.toml", "version")) == version()
    assert json.loads(field(ROOT / "mcpb" / "pyproject.toml", "dependencies")) == [
        f"mcp-server-hyperlight=={version()}"
    ]
    # The README links the release that the publish workflow attaches it to
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"`mcp-server-hyperlight-{version()}.mcpb`" in readme
    assert f"/releases/tag/mcp-server-hyperlight-v{version()})" in readme
