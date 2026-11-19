"""NukeAssistant bridge - lets the NukeAssistant MCP server control this Nuke session.

    import nuke_mcp_bridge
    nuke_mcp_bridge.start_server()   # GUI: listens on 127.0.0.1:54321
    nuke_mcp_bridge.stop_server()

Only the Python standard library and Nuke's own modules are used.
"""

__version__ = "0.1.0"

from .server import (  # noqa: E402
    DEFAULT_PORT,
    is_running,
    serve_headless,
    server_address,
    start_server,
    stop_server,
)

__all__ = ["DEFAULT_PORT", "is_running", "serve_headless", "server_address", "start_server", "stop_server"]
