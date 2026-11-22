"""Tests that run without Nuke: stdio MCP round-trip and the bridge client protocol."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from nuke_mcp.connection import NukeCommandError, NukeConnection
from nuke_mcp.launcher import find_nuke_executable

UNUSED_PORT = 54399
SERVER_SCRIPT = str(Path(__file__).resolve().parents[1] / "nuke_mcp_server.py")


def _interpreters():
    found = [pytest.param(sys.executable, id="dev-python")]
    try:
        nuke_python = Path(find_nuke_executable()).with_name("python.exe" if os.name == "nt" else "python3")
    except Exception:
        nuke_python = None
    if nuke_python and nuke_python.is_file():
        found.append(pytest.param(str(nuke_python), id="nuke-python"))
    return found


@pytest.mark.parametrize("python", _interpreters())
def test_stdio_server_with_sdk_client(python):
    """The official MCP SDK client talks to our stdlib server (also on Nuke's own Python)."""

    async def run():
        env = dict(os.environ, NUKE_MCP_PORT=str(UNUSED_PORT))
        env.pop("PYTHONPATH", None)
        params = StdioServerParameters(command=python, args=[SERVER_SCRIPT], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert "Nuke" in (init.instructions or "")
                tools = (await session.list_tools()).tools
                by_name = {t.name: t for t in tools}
                assert {"create_nodes", "render_preview", "set_knobs", "execute_python"} <= set(by_name)
                assert len(tools) >= 50
                schema = by_name["launch_nuke"].input_schema
                assert schema["properties"]["mode"]["enum"] == ["gui", "headless"]
                assert by_name["get_node_info"].annotations.read_only_hint is True
                result = await session.call_tool("get_session_info", {})
                assert result.is_error
                assert "Cannot reach Nuke" in result.content[0].text
                bad = await session.call_tool("list_nodes", {"no_such_arg": 1})
                assert bad.is_error and "no_such_arg" in bad.content[0].text

    asyncio.run(run())


def test_schema_and_argument_coercion():
    from nuke_mcp.server import mcp

    tools = {t.name: t for t in mcp.list_tools()}
    props = tools["render"].input_schema["properties"]
    assert props["nodes"] == {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                              "default": None}
    assert "node" in tools["get_node_info"].input_schema["required"]
    # JSON-encoded lists from clients are decoded; ints become floats for float params.
    args = tools["sample_pixel"].prepare_arguments({"node": "Blur1", "x": 3, "y": 4.0, "area": "[2, 2]"})
    assert args == {"node": "Blur1", "x": 3.0, "y": 4.0, "area": [2, 2]}


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
