"""Tests that run without Nuke: stdio MCP round-trip and the bridge client protocol."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from nuke_mcp.connection import NukeCommandError, NukeConnection

UNUSED_PORT = 54399


def test_stdio_server_lists_tools_and_reports_missing_nuke():
    async def run():
        env = dict(os.environ, NUKE_MCP_PORT=str(UNUSED_PORT))
        params = StdioServerParameters(command=sys.executable, args=["-m", "nuke_mcp"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert "Nuke" in (init.instructions or "")
                tools = (await session.list_tools()).tools
                names = {t.name for t in tools}
                assert {"create_nodes", "render_preview", "set_knobs", "execute_python"} <= names
                assert len(tools) >= 50
                result = await session.call_tool("get_session_info", {})
                assert result.is_error
                assert "Cannot reach Nuke" in result.content[0].text

    asyncio.run(run())


def test_connection_protocol_against_fake_bridge():
    async def run():
        connections = []

        async def handle(reader, writer):
            connections.append(writer)
            while line := await reader.readline():
                req = json.loads(line)
                if req["command"] == "boom":
                    resp = {"id": req["id"], "ok": False, "error": "it broke", "error_type": "BridgeError"}
                elif req["command"] == "hangup":
                    writer.close()
                    return
                else:
                    resp = {"id": req["id"], "ok": True, "result": {"echo": req["params"]}}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        conn = NukeConnection("127.0.0.1", port)
        try:
            assert await conn.call("ping", {"a": 1, "skip": None}) == {"echo": {"a": 1}}
            try:
                await conn.call("boom")
            except NukeCommandError as exc:
                assert str(exc) == "it broke" and exc.error_type == "BridgeError"
            else:
                raise AssertionError("expected NukeCommandError")
            # Server drops an idle connection: the client reconnects transparently.
            connections[-1].close()
            await asyncio.sleep(0.05)
            assert await conn.call("ping", {"b": 2}) == {"echo": {"b": 2}}
            assert len(connections) == 2
        finally:
            await conn.close()
            server.close()

    asyncio.run(run())
