"""Async client for the bridge socket that runs inside Nuke (see nuke_plugin/)."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 54321
PORT_SCAN = 10
STREAM_LIMIT = 256 * 1024 * 1024  # previews/exports can be large single lines


class NukeConnectionError(RuntimeError):
    """Nuke is not reachable (not running, bridge stopped, wrong port...)."""


class NukeCommandError(RuntimeError):
    """Nuke ran the command and reported an error."""

    def __init__(self, message: str, error_type: str | None = None, traceback: str | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.traceback = traceback


class NukeConnection:
    """One persistent TCP connection to a Nuke bridge, with lazy (re)connect."""

    def __init__(self, host: str | None = None, port: int | None = None, token: str | None = None):
        self.host = host or os.environ.get("NUKE_MCP_HOST", DEFAULT_HOST)
        self.port = int(port or os.environ.get("NUKE_MCP_PORT", DEFAULT_PORT))
        self.token = token if token is not None else os.environ.get("NUKE_MCP_TOKEN")
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    async def _connect(self, timeout: float = 5.0) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, limit=STREAM_LIMIT), timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            self._reader = self._writer = None
            raise NukeConnectionError(
                f"Cannot reach Nuke at {self.address} ({exc.__class__.__name__}: {exc}). "
                "Make sure Nuke is running with the NukeAssistant plugin (menu NukeAssistant > "
                "Start MCP Bridge), or call launch_nuke to start a session."
            ) from exc

    async def close(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                # Bounded: wait_closed can hang on Windows if the peer never acknowledges.
                await asyncio.wait_for(writer.wait_closed(), 2.0)
            except Exception:
                pass

    async def call(self, command: str, params: dict[str, Any] | None = None, timeout: float = 120.0) -> Any:
        """Send one command and return its result (raises NukeCommandError on failure)."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        request: dict[str, Any] = {"id": next(self._ids), "command": command, "params": params}
        if self.token:
            request["token"] = self.token
        payload = (json.dumps(request) + "\n").encode("utf-8")

        async with self._lock:
            for attempt in (1, 2):
                if self._writer is None:
                    await self._connect()
                assert self._writer is not None and self._reader is not None
                try:
                    self._writer.write(payload)
                    await self._writer.drain()
                except (ConnectionError, OSError):
                    # The request never reached Nuke: safe to reconnect and resend once.
                    await self.close()
                    if attempt == 2:
                        raise NukeConnectionError(f"Lost connection to Nuke at {self.address}.")
                    continue
                try:
                    line = await asyncio.wait_for(self._reader.readline(), timeout)
                except asyncio.TimeoutError as exc:
                    await self.close()  # a late reply would desync the stream
                    raise NukeCommandError(
                        f"Nuke did not answer '{command}' within {timeout:.0f}s. It may be busy "
                        "(rendering, modal dialog open) - the command may still complete."
                    ) from exc
                except (ConnectionError, OSError, asyncio.LimitOverrunError, ValueError) as exc:
                    await self.close()
                    raise NukeConnectionError(f"Connection to Nuke broke while waiting for '{command}': {exc}") from exc
                if not line:
                    await self.close()
                    # Nuke may have closed an idle connection; only retry if we sent on a stale socket.
                    if attempt == 1:
                        continue
                    raise NukeConnectionError(f"Nuke closed the connection during '{command}'.")
                break

        response = json.loads(line)
        if response.get("ok"):
            return response.get("result")
        raise NukeCommandError(
            response.get("error") or "Unknown error",
            response.get("error_type"),
            response.get("traceback"),
        )


async def probe(host: str, port: int, timeout: float = 1.0) -> dict[str, Any] | None:
    """Ping a bridge without disturbing the main connection."""
    conn = NukeConnection(host, port)
    try:
        await asyncio.wait_for(conn._connect(timeout), timeout)
        return await conn.call("ping", timeout=timeout + 2)
    except Exception:
        return None
    finally:
        await conn.close()
