# NukeAssistant - interactive session setup.
#
# Adds a "NukeAssistant" menu and starts the MCP bridge automatically.
# Set NUKE_MCP_AUTOSTART=0 to start it manually from the menu instead.

import os

import nuke

import nuke_mcp_bridge


def _start():
    host, port = nuke_mcp_bridge.start_server()
    nuke.tprint("[NukeAssistant] MCP bridge listening on %s:%d" % (host, port))


def _stop():
    if not nuke_mcp_bridge.stop_server():
        nuke.message("The NukeAssistant bridge is not running.")


def _status():
    address = nuke_mcp_bridge.server_address()
    if address:
        nuke.message("NukeAssistant MCP bridge is running on %s:%d" % (address[0], address[1]))
    else:
        nuke.message("NukeAssistant MCP bridge is stopped.")


if nuke.GUI:
    _menu = nuke.menu("Nuke").addMenu("NukeAssistant")
    _menu.addCommand("Start MCP Bridge", _start)
    _menu.addCommand("Stop MCP Bridge", _stop)
    _menu.addCommand("Bridge Status", _status)

    if os.environ.get("NUKE_MCP_AUTOSTART", "1") != "0":
        try:
            _start()
        except Exception as _exc:  # never break Nuke startup
            nuke.tprint("[NukeAssistant] could not start the MCP bridge: %s" % _exc)
