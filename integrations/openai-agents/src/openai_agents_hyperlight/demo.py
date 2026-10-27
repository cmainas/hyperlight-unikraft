"""Run a shell command in a Hyperlight micro-VM through the Agents SDK's Runner, with no model API call."""

from __future__ import annotations

import argparse
import asyncio

from agents import RunConfig, Runner
from agents.items import ToolCallOutputItem
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.errors import WorkspaceStartError
from agents.testing import ScriptedModel, assistant_message, function_call

from . import HyperlightSandboxClient, HyperlightSandboxClientOptions


async def run(hluk: str | None, initrd: str | None) -> None:
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "uname -s; echo hyperlight provider works"}, call_id="demo")],
            [assistant_message("Demo complete.")],
        ]
    )
    agent = SandboxAgent(name="Hyperlight provider demo", model=model, capabilities=[Shell()])
    result = await Runner.run(
        agent,
        "Run the demo command.",
        run_config=RunConfig(
            tracing_disabled=True,
            sandbox=SandboxRunConfig(
                client=HyperlightSandboxClient(hluk_path=hluk),
                options=HyperlightSandboxClientOptions(initrd=initrd),
            ),
        ),
    )
    model.assert_complete()
    # The command's output, rather than the scripted final answer
    outputs = [str(item.output) for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    if not any("Unikraft" in output and "hyperlight provider works" in output for output in outputs):
        raise SystemExit(f"The guest command did not succeed: {outputs!r}")
    print("hyperlight provider works")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hluk", help="the hluk CLI to run (default: hluk on PATH)")
    parser.add_argument("--initrd", help="a local rootfs CPIO to boot instead of the agent runtime image")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.hluk, args.initrd))
    except WorkspaceStartError as error:
        raise SystemExit(
            f"{error}\nInstall hluk v0.19.0 or later on a Linux host with KVM or MSHV: "
            "https://github.com/hyperlight-dev/hyperlight-unikraft#quick-start"
        ) from None


if __name__ == "__main__":
    main()
