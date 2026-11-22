"""Start the NukeAssistant MCP server (stdio).

Needs nothing but Python 3.10+, so Nuke's own interpreter works:

    "C:/Program Files/Nuke15.1v3/python.exe" nuke_mcp_server.py
"""

import os
import sys

if sys.version_info < (3, 10):
    sys.exit("NukeAssistant's MCP server needs Python 3.10+ (Nuke 15 or newer ships it); this is %s."
             % sys.version.split()[0])

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from nuke_mcp.server import main  # noqa: E402

main()
