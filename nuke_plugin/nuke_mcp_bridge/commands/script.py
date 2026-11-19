"""Script (.nk) and project-level commands."""

import os

import nuke

from ..registry import command
from ..utils import (
    BridgeError,
    deselect_all,
    find_group,
    format_dict,
    node_summary,
    norm_path,
    resolve_format,
    set_knob,
)


def _script_path():
    try:
        return nuke.scriptName()
    except RuntimeError:
        return None


def _guard_modified(force):
    if not force and nuke.modified() and nuke.root().nodes():
        raise BridgeError(
            "The current script has unsaved changes. Save it first or pass force=true to discard them."
        )


@command()
def new_script(force=False):
    """Clear the current script (File > Close without the dialog)."""
    _guard_modified(force)
    nuke.scriptClear()
    return {"script": _script_path(), "nodes": 0}


@command()
def open_script(path, force=False):
    """Replace the current script with the .nk file at ``path``."""
    path = norm_path(path)
    if not os.path.isfile(path):
        raise BridgeError("Script not found: %s" % path)
    _guard_modified(force)
    nuke.scriptClear()
    nuke.scriptOpen(path)
    return {"script": _script_path(), "top_level_nodes": len(nuke.root().nodes())}


@command(undo=True)
def import_script(path, parent=None):
    """Paste the nodes of a .nk file into the current script (File > Insert)."""
    path = norm_path(path)
    if not os.path.isfile(path):
        raise BridgeError("Script not found: %s" % path)
    group = find_group(parent)
    with group:
        deselect_all(group)
        nuke.nodePaste(path)
        pasted = nuke.selectedNodes()
    return {"imported": [node_summary(n) for n in pasted]}


@command()
def save_script(path=None, overwrite=True):
    """Save the script; with ``path`` acts as Save As."""
    if path:
        path = norm_path(path)
        if os.path.exists(path) and not overwrite:
            raise BridgeError("%s exists; pass overwrite=true to replace it." % path)
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        nuke.scriptSaveAs(path, 1 if overwrite else 0)
    else:
        if not _script_path():
            raise BridgeError("The script has never been saved; provide a path.")
        nuke.scriptSave()
    return {"script": _script_path(), "modified": bool(nuke.modified())}


@command(undo=True)
def set_project_settings(first_frame=None, last_frame=None, fps=None, format=None, knobs=None):
    """Set frame range, fps, format and/or any other Root knob."""
    root = nuke.root()
    if first_frame is not None:
        root["first_frame"].setValue(int(first_frame))
    if last_frame is not None:
        root["last_frame"].setValue(int(last_frame))
    if fps is not None:
        root["fps"].setValue(float(fps))
    if format is not None:
        root["format"].setValue(resolve_format(format))
    for name, value in (knobs or {}).items():
        set_knob(root, name, value)
    return {
        "first_frame": root.firstFrame(),
        "last_frame": root.lastFrame(),
        "fps": root["fps"].value(),
        "format": format_dict(root["format"].value()),
    }


@command()
def set_current_frame(frame):
    """Move the timeline to ``frame``."""
    nuke.frame(int(frame))
    return {"current_frame": nuke.frame()}


@command()
def list_formats():
    """All formats known to this session."""
    return [format_dict(f) for f in nuke.formats() if f.name()]
