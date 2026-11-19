"""Run a terminal-mode Nuke session controlled by the NukeAssistant MCP server.

    Nuke15.1.exe -t start_headless.py [--port 54321] [script.nk]

Blocks until the MCP server sends ``shutdown_headless`` or Ctrl+C is pressed.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import nuke  # noqa: E402

import nuke_mcp_bridge  # noqa: E402


def _parse(argv):
    port = None
    script = None
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--port" and args:
            port = int(args.pop(0))
        elif arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
        else:
            script = arg
    return port, script


def main():
    port, script = _parse(sys.argv[1:])
    if script:
        nuke.scriptOpen(script.replace("\\", "/"))
    elif sys.argv[1:]:
        # Nuke names the script after the first -t argument (e.g. "--port"); keep it untitled.
        nuke.root()["name"].setValue("")
    nuke_mcp_bridge.serve_headless(port=port)


# Nuke does not always run -t scripts as __main__, so start unconditionally.
main()
