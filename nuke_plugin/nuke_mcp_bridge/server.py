"""Socket server that lets the NukeAssistant MCP server drive Nuke.

Protocol: newline-delimited JSON over TCP (localhost by default).

    request  -> {"id": 1, "command": "create_node", "params": {...}, "token": "..."}
    response <- {"id": 1, "ok": true, "result": ...}
             <- {"id": 1, "ok": false, "error": "...", "error_type": "...", "traceback": "..."}

Connections are handled on background threads, but every command is executed on
Nuke's main thread:
  * GUI sessions use ``nuke.executeInMainThreadWithResult``.
  * Terminal sessions (``nuke -t``) run a job queue on the main thread
    (see :func:`serve_headless`).
"""

import json
import os
import socketserver
import sys
import threading
import traceback

try:
    import queue
except ImportError:  # pragma: no cover - Python 2 is not supported, kept for clarity
    import Queue as queue

import nuke

from . import registry
from . import commands  # noqa: F401  (registers all commands)
from .focus import no_focus_steal
from .utils import BridgeError, to_jsonable

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 54321
PORT_SCAN = 10  # try DEFAULT_PORT .. DEFAULT_PORT+9 when several Nukes are open
MAX_REQUEST_BYTES = 64 * 1024 * 1024

_lock = threading.Lock()
_state = {"server": None, "thread": None, "executor": None}


def _log(message):
    try:
        sys.stdout.write("[NukeAssistant] %s\n" % message)
    except Exception:
        pass


def _env_port():
    try:
        return int(os.environ.get("NUKE_MCP_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


# ---------------------------------------------------------------------------
# Main-thread executors
# ---------------------------------------------------------------------------

class _GuiExecutor(object):
    """Run callables on the Qt main thread of an interactive Nuke session."""

    mode = "gui"

    def run(self, fn):
        box = {}

        def job():
            try:
                with no_focus_steal():
                    box["value"] = fn()
            except BaseException as exc:  # noqa: B902 - forwarded to the caller
                box["exc"] = exc
                box["tb"] = traceback.format_exc()

        nuke.executeInMainThreadWithResult(job)
        if "exc" in box:
            box["exc"].__bridge_traceback__ = box.get("tb")
            raise box["exc"]
        if "value" not in box:
            raise BridgeError("Nuke's main thread did not run the command.")
        return box["value"]

    def stop(self):
        pass


class _QueueExecutor(object):
    """Run callables on whichever thread calls :meth:`serve` (terminal mode)."""

    mode = "headless"

    def __init__(self):
        self._jobs = queue.Queue()
        self._stopped = threading.Event()

    def run(self, fn):
        if self._stopped.is_set():
            raise BridgeError("The Nuke bridge is shutting down.")
        done = threading.Event()
        box = {}
        self._jobs.put((fn, box, done))
        done.wait()
        if "exc" in box:
            box["exc"].__bridge_traceback__ = box.get("tb")
            raise box["exc"]
        return box["value"]

    def serve(self):
        while not self._stopped.is_set():
            try:
                fn, box, done = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: B902
                box["exc"] = exc
                box["tb"] = traceback.format_exc()
            finally:
                done.set()

    def stop(self):
        self._stopped.set()


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------

def _execute(name, params):
    """Runs on the main thread."""
    cmd = registry.get(name)
    if cmd is None:
        raise BridgeError("Unknown command %r. Available: %s" % (name, ", ".join(registry.names())))
    use_undo = cmd.undo
    if use_undo:
        try:
            use_undo = not nuke.Undo.disabled()
        except Exception:
            use_undo = False
    if use_undo:
        nuke.Undo.begin("NukeAssistant: %s" % name)
    try:
        try:
            result = cmd.fn(**params)
        except TypeError as exc:
            # Turn bad argument names into a readable error.
            if str(exc).startswith(cmd.fn.__name__ + "()") and "argument" in str(exc):
                raise BridgeError("Bad parameters for %s: %s" % (name, exc))
            raise
        return to_jsonable(result)
    finally:
        if use_undo:
            nuke.Undo.end()


def handle_request(request, executor):
    req_id = request.get("id")
    expected_token = os.environ.get("NUKE_MCP_TOKEN")
    if expected_token and request.get("token") != expected_token:
        return {"id": req_id, "ok": False, "error": "Invalid or missing token.", "error_type": "AuthError"}
    name = request.get("command")
    params = request.get("params") or {}
    if not isinstance(name, str) or not isinstance(params, dict):
        return {"id": req_id, "ok": False, "error": "Malformed request.", "error_type": "ProtocolError"}
    try:
        result = executor.run(lambda: _execute(name, params))
        return {"id": req_id, "ok": True, "result": result}
    except BridgeError as exc:
        return {"id": req_id, "ok": False, "error": str(exc), "error_type": "BridgeError"}
    except Exception as exc:
        tb = getattr(exc, "__bridge_traceback__", None) or traceback.format_exc()
        return {
            "id": req_id,
            "ok": False,
            "error": "%s: %s" % (type(exc).__name__, exc),
            "error_type": type(exc).__name__,
            "traceback": tb[-6000:],
        }


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        executor = self.server.executor
        while True:
            try:
                line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            except (ConnectionError, OSError):
                return
            if not line:
                return
            if len(line) > MAX_REQUEST_BYTES:
                response = {"id": None, "ok": False, "error": "Request too large.", "error_type": "ProtocolError"}
            else:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line.decode("utf-8"))
                    if not isinstance(request, dict):
                        raise ValueError("request must be a JSON object")
                except ValueError as exc:
                    response = {"id": None, "ok": False, "error": "Invalid JSON: %s" % exc, "error_type": "ProtocolError"}
                else:
                    response = handle_request(request, executor)
            try:
                payload = json.dumps(response) + "\n"
            except (TypeError, ValueError) as exc:
                payload = json.dumps({"id": response.get("id"), "ok": False, "error": "Unserialisable result: %s" % exc}) + "\n"
            try:
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
            except (ConnectionError, OSError):
                return


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets two processes bind the same port - never use it.
    allow_reuse_address = False

    def __init__(self, address, executor):
        self.executor = executor
        socketserver.ThreadingTCPServer.__init__(self, address, _Handler)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_running():
    return _state["server"] is not None


def server_address():
    srv = _state["server"]
    return srv.server_address if srv else None


def start_server(port=None, host=None, headless=None):
    """Start the bridge. Returns (host, port). Safe to call twice."""
    with _lock:
        if _state["server"] is not None:
            return _state["server"].server_address
        if headless is None:
            headless = not nuke.GUI
        host = host or os.environ.get("NUKE_MCP_HOST", DEFAULT_HOST)
        base_port = int(port or _env_port())
        executor = _QueueExecutor() if headless else _GuiExecutor()
        srv = None
        last_error = None
        for candidate in range(base_port, base_port + PORT_SCAN):
            try:
                srv = _Server((host, candidate), executor)
                break
            except OSError as exc:
                last_error = exc
        if srv is None:
            raise RuntimeError("NukeAssistant: no free port in %d-%d (%s)" % (base_port, base_port + PORT_SCAN - 1, last_error))
        thread = threading.Thread(target=srv.serve_forever, name="NukeAssistantBridge")
        thread.daemon = True
        thread.start()
        _state.update(server=srv, thread=thread, executor=executor)
        _log("MCP bridge listening on %s:%d (%s mode)" % (srv.server_address[0], srv.server_address[1], executor.mode))
        return srv.server_address


def stop_server():
    with _lock:
        srv = _state["server"]
        executor = _state["executor"]
        if srv is None:
            return False
        _state.update(server=None, thread=None, executor=None)
    if executor is not None:
        executor.stop()
    srv.shutdown()
    srv.server_close()
    _log("MCP bridge stopped")
    return True


def request_shutdown():
    """Stop a terminal-mode session after the current request has been answered."""
    executor = _state["executor"]
    if executor is None or executor.mode != "headless":
        raise BridgeError("Only terminal-mode (nuke -t) sessions can be shut down remotely.")
    timer = threading.Timer(0.5, stop_server)
    timer.daemon = True
    timer.start()


def serve_headless(port=None, host=None):
    """Start the bridge and block, executing commands on this (main) thread.

    Intended for ``nuke -t start_headless.py``.
    """
    start_server(port=port, host=host, headless=True)
    executor = _state["executor"]
    try:
        executor.serve()
    except KeyboardInterrupt:
        pass
    finally:
        stop_server()
