"""A small, standard-library-only MCP server (stdio transport, tools only).

NukeAssistant only needs tools, so it implements that part of the Model Context
Protocol itself instead of depending on the MCP SDK. This lets the server run on any
Python >= 3.10, including the python.exe that ships with Nuke, with nothing to install.

Wire format: JSON-RPC 2.0 messages, one per line, on stdin/stdout. Supports the
``initialize`` handshake (protocol revisions 2024-11-05 .. 2025-11-25), ``ping``,
``tools/list``, ``tools/call`` and ``notifications/cancelled``. Anything else is
answered with "method not found", which also makes newer clients that first probe
``server/discover`` fall back to the ``initialize`` handshake.

The API mirrors the subset of the SDK's ``MCPServer`` that server.py uses:
``MCPServer.tool()``, ``Image``, ``ToolError``, ``ToolAnnotations``,
``MCPServer.call_tool()`` and ``MCPServer.run("stdio")``.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import sys
import threading
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # newest first

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """Raised by a tool to report a failure to the client (an ``isError`` result)."""


@dataclass
class ToolAnnotations:
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    idempotent_hint: bool | None = None
    open_world_hint: bool | None = None
    title: str | None = None

    def to_json(self) -> dict[str, Any]:
        names = {"read_only_hint": "readOnlyHint", "destructive_hint": "destructiveHint",
                 "idempotent_hint": "idempotentHint", "open_world_hint": "openWorldHint", "title": "title"}
        return {wire: getattr(self, attr) for attr, wire in names.items() if getattr(self, attr) is not None}


class Image:
    """Image content returned from a tool."""

    def __init__(self, data: bytes, format: str = "png"):
        self.data = data
        self.mime_type = "image/" + ("jpeg" if format.lower() in ("jpg", "jpeg") else format.lower())


class Content(types.SimpleNamespace):
    """A content block (``type`` = "text" or "image"); attributes match the wire fields."""

    @property
    def mime_type(self) -> str | None:  # SDK-style name, for in-process callers
        return getattr(self, "mimeType", None)

    def to_json(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass
class CallToolResult:
    content: list[Content]
    is_error: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"content": [c.to_json() for c in self.content], "isError": self.is_error}


# ---------------------------------------------------------------------------
# JSON schema from type hints
# ---------------------------------------------------------------------------

_SIMPLE = {str: "string", int: "integer", float: "number", bool: "boolean", type(None): "null"}


def _schema(hint: Any) -> dict[str, Any]:
    if hint is Any or hint is inspect.Parameter.empty:
        return {}
    if hint in _SIMPLE:
        return {"type": _SIMPLE[hint]}
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin is typing.Literal:
        schema: dict[str, Any] = {"enum": list(args)}
        kinds = {_SIMPLE.get(type(a)) for a in args}
        if len(kinds) == 1 and None not in kinds:
            schema["type"] = kinds.pop()
        return schema
    if origin in (typing.Union, types.UnionType):
        return {"anyOf": [_schema(a) for a in args]}
    if hint is list or origin is list:
        return {"type": "array", "items": _schema(args[0])} if args else {"type": "array"}
    if hint is dict or origin is dict:
        if len(args) == 2:
            return {"type": "object", "additionalProperties": _schema(args[1])}
        return {"type": "object"}
    raise TypeError("Unsupported parameter type for a tool: %r" % (hint,))


def _types_in(schema: dict[str, Any]) -> set[str]:
    if "anyOf" in schema:
        return set().union(*(_types_in(s) for s in schema["anyOf"]))
    return {schema["type"]} if "type" in schema else {"any"}


@dataclass
class Tool:
    name: str
    fn: Callable[..., Any]
    description: str
    input_schema: dict[str, Any]
    annotations: ToolAnnotations | None

    def to_json(self) -> dict[str, Any]:
        data = {"name": self.name, "description": self.description, "inputSchema": self.input_schema}
        if self.annotations is not None:
            data["annotations"] = self.annotations.to_json()
        return data

    def prepare_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        props = self.input_schema["properties"]
        unknown = sorted(set(arguments) - set(props))
        if unknown:
            raise ToolError("Unknown argument(s) for %s: %s. Valid: %s" % (self.name, ", ".join(unknown),
                                                                         ", ".join(props) or "none"))
        missing = [p for p in self.input_schema.get("required", []) if p not in arguments]
        if missing:
            raise ToolError("Missing required argument(s) for %s: %s" % (self.name, ", ".join(missing)))
        prepared = {}
        for key, value in arguments.items():
            kinds = _types_in(props[key])
            if isinstance(value, str) and "string" not in kinds and "any" not in kinds:
                # Some clients send lists/objects/numbers JSON-encoded as strings.
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            if isinstance(value, int) and not isinstance(value, bool) and "number" in kinds and "integer" not in kinds:
                value = float(value)
            if isinstance(value, float) and value.is_integer() and "integer" in kinds and "number" not in kinds:
                value = int(value)
            prepared[key] = value
        return prepared


def _make_tool(fn: Callable[..., Any], name: str | None, description: str | None,
               annotations: ToolAnnotations | None) -> Tool:
    hints = typing.get_type_hints(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in inspect.signature(fn).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        prop = _schema(hints.get(param.name, Any))
        if param.default is param.empty:
            required.append(param.name)
        else:
            try:
                json.dumps(param.default)
                prop["default"] = param.default
            except TypeError:
                pass
        properties[param.name] = prop
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return Tool(name=name or fn.__name__, fn=fn, description=description or inspect.cleandoc(fn.__doc__ or ""),
                input_schema=schema, annotations=annotations)


def _to_content(value: Any) -> list[Content]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [block for item in value for block in _to_content(item)]
    if isinstance(value, Image):
        return [Content(type="image", data=base64.b64encode(value.data).decode("ascii"), mimeType=value.mime_type)]
    if isinstance(value, Content):
        return [value]
    if isinstance(value, str):
        return [Content(type="text", text=value)]
    return [Content(type="text", text=json.dumps(value, ensure_ascii=False, default=str))]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class MCPServer:
    def __init__(self, name: str, instructions: str | None = None, version: str = "0.1.0"):
        self.name = name
        self.instructions = instructions
        self.version = version
        self._tools: dict[str, Tool] = {}
        self._tasks: dict[Any, asyncio.Task] = {}
        self._out = None
        self._write_lock = threading.Lock()

    # -- registration ------------------------------------------------------

    def tool(self, name: str | None = None, description: str | None = None,
             annotations: ToolAnnotations | None = None, structured_output: bool | None = None):
        """Decorator registering a function (sync or async) as a tool. Parameter types
        come from its annotations, the description from its docstring."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool = _make_tool(fn, name, description, annotations)
            self._tools[tool.name] = tool
            return fn

        return decorator

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return CallToolResult([Content(type="text", text="Unknown tool: %s" % name)], is_error=True)
        try:
            result = tool.fn(**tool.prepare_arguments(dict(arguments or {})))
            if inspect.isawaitable(result):
                result = await result
            return CallToolResult(_to_content(result))
        except ToolError as exc:
            return CallToolResult([Content(type="text", text=str(exc))], is_error=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # report, don't crash the server
            message = "Error executing tool %s: %s: %s" % (name, type(exc).__name__, exc)
            return CallToolResult([Content(type="text", text=message)], is_error=True)

    # -- JSON-RPC ----------------------------------------------------------

    async def handle_message(self, message: Any) -> dict[str, Any] | None:
        """Handle one decoded JSON-RPC message; returns the response (None for notifications)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "method" not in message:
                return None  # a response to a server->client request; we never send any
            return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "Invalid request")
        method = message["method"]
        params = message.get("params") or {}
        if "id" not in message:
            if method == "notifications/cancelled":
                task = self._tasks.get(params.get("requestId"))
                if task is not None:
                    task.cancel()
            return None
        msg_id = message["id"]
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                result: dict[str, Any] = {
                    "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                }
                if self.instructions:
                    result["instructions"] = self.instructions
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [t.to_json() for t in self._tools.values()]}
            elif method == "tools/call":
                name = params.get("name")
                if name not in self._tools:
                    return _error(msg_id, INVALID_PARAMS, "Unknown tool: %s" % name)
                result = (await self.call_tool(name, params.get("arguments"))).to_json()
            else:
                return _error(msg_id, METHOD_NOT_FOUND, "Method not found: %s" % method)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _error(msg_id, INTERNAL_ERROR, "%s: %s" % (type(exc).__name__, exc))
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _send(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._write_lock:
            self._out.write(data)
            self._out.flush()

    async def _dispatch(self, message: Any) -> None:
        msg_id = message.get("id") if isinstance(message, dict) else None
        try:
            response = await self.handle_message(message)
        except asyncio.CancelledError:
            return  # cancelled by the client: no response is sent
        finally:
            if msg_id is not None:
                self._tasks.pop(msg_id, None)
        if response is not None:
            self._send(response)

    async def _serve_stdio(self) -> None:
        loop = asyncio.get_running_loop()
        lines: asyncio.Queue = asyncio.Queue()
        stdin = sys.stdin.buffer

        def reader() -> None:  # Windows can't await console/pipe stdin, so read on a thread
            try:
                for raw in iter(stdin.readline, b""):
                    loop.call_soon_threadsafe(lines.put_nowait, raw)
            finally:
                loop.call_soon_threadsafe(lines.put_nowait, None)

        threading.Thread(target=reader, name="mcp-stdin", daemon=True).start()
        while True:
            raw = await lines.get()
            if raw is None:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
            except ValueError as exc:
                self._send(_error(None, PARSE_ERROR, "Parse error: %s" % exc))
                continue
            for item in message if isinstance(message, list) else [message]:
                task = asyncio.ensure_future(self._dispatch(item))
                if isinstance(item, dict) and "id" in item and "method" in item:
                    self._tasks[item["id"]] = task
        for task in list(self._tasks.values()):
            task.cancel()

    def run(self, transport: str = "stdio") -> None:
        if transport != "stdio":
            raise ValueError("Only the stdio transport is supported.")
        # stdout carries the protocol: route stray prints to stderr.
        self._out = sys.stdout.buffer
        sys.stdout = sys.stderr
        try:
            asyncio.run(self._serve_stdio())
        except KeyboardInterrupt:
            pass


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
