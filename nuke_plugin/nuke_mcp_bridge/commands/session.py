"""Session-level commands: ping, info, undo/redo, shutdown."""

import os
import sys

import nuke

from .. import registry
from ..registry import command
from ..utils import BridgeError, format_dict


def _script_path():
    try:
        return nuke.scriptName()
    except RuntimeError:
        return None


def _env_flags():
    flags = {}
    for key in ("nukex", "studio", "hiero", "indie", "ple", "assist"):
        try:
            flags[key] = bool(nuke.env.get(key))
        except Exception:
            pass
    return flags


@command()
def ping():
    """Cheap liveness check."""
    from .. import __version__

    return {
        "pong": True,
        "pid": os.getpid(),
        "gui": bool(nuke.GUI),
        "nuke_version": nuke.NUKE_VERSION_STRING,
        "script": _script_path(),
        "bridge_version": __version__,
    }


@command()
def session_info():
    """Nuke version, licence flavour, current script and project settings."""
    from .. import __version__

    root = nuke.root()
    info = {
        "nuke_version": nuke.NUKE_VERSION_STRING,
        "python": sys.version.split()[0],
        "gui": bool(nuke.GUI),
        "flavour": _env_flags(),
        "bridge_version": __version__,
        "pid": os.getpid(),
        "script": _script_path(),
        "modified": bool(nuke.modified()),
        "current_frame": nuke.frame(),
        "first_frame": root.firstFrame(),
        "last_frame": root.lastFrame(),
        "fps": root["fps"].value(),
        "format": format_dict(root["format"].value()),
        "proxy": bool(root["proxy"].value()),
        "views": list(nuke.views()),
        "top_level_nodes": len(nuke.root().nodes()),
        "total_nodes": len(nuke.allNodes(group=nuke.root(), recurseGroups=True)),
    }
    for knob_name in ("colorManagement", "OCIO_config", "workingSpaceLUT", "project_directory"):
        knob = root.knob(knob_name)
        if knob is not None:
            try:
                info[knob_name] = knob.value()
            except Exception:
                pass
    return info


@command()
def list_commands():
    """All bridge commands with their docstrings."""
    return {name: registry.get(name).doc.split("\n")[0] for name in registry.names()}


@command()
def undo(steps=1):
    """Undo the last ``steps`` actions (each assistant tool call is one undo step)."""
    done = []
    for _ in range(int(steps)):
        if nuke.Undo.undoSize() <= 0:
            break
        done.append(nuke.Undo.undoDescribe(0))
        nuke.Undo.undo()
    return {"undone": done, "remaining_undo": nuke.Undo.undoSize()}


@command()
def redo(steps=1):
    """Redo ``steps`` previously undone actions."""
    done = []
    for _ in range(int(steps)):
        if nuke.Undo.redoSize() <= 0:
            break
        done.append(nuke.Undo.redoDescribe(0))
        nuke.Undo.redo()
    return {"redone": done, "remaining_redo": nuke.Undo.redoSize()}


@command()
def shutdown_headless():
    """Quit a terminal-mode (nuke -t) bridge session."""
    from ..server import request_shutdown

    if nuke.GUI:
        raise BridgeError("Refusing to quit an interactive Nuke session.")
    request_shutdown()
    return {"shutting_down": True}
