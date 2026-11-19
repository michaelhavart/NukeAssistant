"""Escape hatches: run arbitrary Python or TCL inside Nuke."""

import contextlib
import io
import traceback

import nuke

from ..registry import command
from ..utils import BridgeError, to_jsonable, truncate

_NAMESPACE = {}


def _namespace():
    if not _NAMESPACE:
        import math
        import os

        _NAMESPACE.update({"__name__": "__nukeassistant__", "nuke": nuke, "math": math, "os": os})
        try:
            import nukescripts

            _NAMESPACE["nukescripts"] = nukescripts
        except Exception:
            pass
    return _NAMESPACE


@command(undo=True)
def execute_python(code):
    """Run Python in Nuke (main thread). Globals persist between calls.

    A single expression returns its value; otherwise assign to ``result`` to return
    something. stdout/stderr are captured.
    """
    ns = _namespace()
    ns.pop("result", None)
    out = io.StringIO()
    value = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                compiled = compile(code, "<nukeassistant>", "eval")
            except SyntaxError:
                exec(compile(code, "<nukeassistant>", "exec"), ns)
                value = ns.get("result")
            else:
                value = eval(compiled, ns)
    except Exception:
        tb = traceback.format_exc()
        raise BridgeError("Python error:\n%s\nOutput before error:\n%s" % (truncate(tb, 6000), truncate(out.getvalue(), 4000)))
    return {"result": to_jsonable(value), "output": truncate(out.getvalue(), 20000)}


@command(undo=True)
def execute_tcl(code):
    """Run a TCL command in Nuke and return its string result."""
    try:
        return {"result": truncate(nuke.tcl(str(code)), 20000)}
    except RuntimeError as exc:
        raise BridgeError("TCL error: %s" % exc)
