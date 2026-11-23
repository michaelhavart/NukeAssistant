"""Rendering, previews, pixel sampling, metadata and footage import."""

import base64
import os
import re
import time

import nuke

from ..registry import command
from ..utils import (
    BridgeError,
    find_group,
    find_node,
    find_nodes,
    node_has_error,
    node_ref,
    node_summary,
    norm_path,
    parent_group,
    preserved_modified_state,
    preserved_selection,
    temp_dir,
    to_jsonable,
    truncate,
    undo_disabled,
)
from .nodes import _image_info


def _layer_channels(node, layer):
    chans = [c for c in node.channels() if c.split(".")[0] == layer]
    if not chans:
        layers = sorted(set(c.split(".")[0] for c in node.channels()))
        raise BridgeError("Layer %r not found on %s. Available layers: %s" % (layer, node_ref(node), ", ".join(layers)))
    return chans


@command()
def render_preview(node, frame=None, max_size=1024, layer="rgba", image_format="jpeg", parent=None):
    """Render one frame of any image node to a small JPEG/PNG and return it base64-encoded.

    Uses temporary Reformat/Write nodes that are removed afterwards (no undo entry).
    ``layer`` picks another layer (e.g. "depth", "motion"); single-channel layers show as grey.
    """
    src = find_node(node, parent)
    frame = int(frame) if frame is not None else int(nuke.frame())
    max_size = max(16, int(max_size))
    try:
        fmt = src.format()
    except Exception:
        raise BridgeError("%s (%s) does not output an image." % (node_ref(src), src.Class()))
    ext = "png" if str(image_format).lower() == "png" else "jpg"
    group = parent_group(src)
    temps = []
    with undo_disabled(), preserved_modified_state(), preserved_selection(group), temp_dir() as tmp:
        path = "%s/preview.%s" % (tmp, ext)
        try:
            with group:
                current = src
                if layer and layer not in ("rgba", "rgb"):
                    chans = _layer_channels(src, layer)
                    picks = (chans * 3)[:3] if len(chans) < 3 else chans[:3]
                    expr = nuke.nodes.Expression()
                    temps.append(expr)
                    expr.setInput(0, current)
                    for i, ch in enumerate(picks):
                        expr["expr%d" % i].setValue(ch)
                    current = expr
                width, height = fmt.width(), fmt.height()
                if max(width, height) > max_size:
                    ref = nuke.nodes.Reformat()
                    temps.append(ref)
                    ref.setInput(0, current)
                    ref["type"].setValue("to box")
                    ref["box_width"].setValue(max_size)
                    ref["box_height"].setValue(max_size)
                    ref["box_fixed"].setValue(True)
                    ref["resize"].setValue("fit")
                    scale = float(max_size) / max(width, height)
                    width, height = int(round(width * scale)), int(round(height * scale))
                    current = ref
                write = nuke.nodes.Write()
                temps.append(write)
                write.setInput(0, current)
                write["file"].setValue(path)
                write["file_type"].setValue("png" if ext == "png" else "jpeg")
                write["channels"].setValue("rgb")
                if ext == "jpg" and write.knob("_jpeg_quality") is not None:
                    write["_jpeg_quality"].setValue(0.9)
                try:
                    nuke.execute(write, frame, frame, 1)
                except RuntimeError as exc:
                    raise BridgeError("Render of %s at frame %d failed: %s" % (node_ref(src), frame, exc))
        finally:
            for t in reversed(temps):
                try:
                    nuke.delete(t)
                except Exception:
                    pass
        if not os.path.isfile(path):
            raise BridgeError("Nuke did not write the preview image.")
        with open(path, "rb") as fh:
            data = fh.read()
    return {
        "node": node_ref(src),
        "frame": frame,
        "layer": layer,
        "width": width,
        "height": height,
        "source_format": [fmt.width(), fmt.height()],
        "mime_type": "image/png" if ext == "png" else "image/jpeg",
        "image_base64": base64.b64encode(data).decode("ascii"),
    }


@command()
def render(nodes=None, first=None, last=None, step=1, views=None, continue_on_error=False, parent=None):
    """Execute Write (or other executable) nodes over a frame range. Blocks until finished."""
    if nodes:
        targets = find_nodes(nodes, parent)
    else:
        targets = [n for n in nuke.allNodes("Write", group=find_group(parent), recurseGroups=True)
                   if not n["disable"].value()]
    if not targets:
        raise BridgeError("No Write nodes to render.")
    root = nuke.root()
    first = int(first if first is not None else root.firstFrame())
    last = int(last if last is not None else root.lastFrame())
    results = []
    for node in targets:
        out_file = None
        file_knob = node.knob("file")
        if file_knob is not None:
            try:
                out_file = file_knob.evaluate(first) if hasattr(file_knob, "evaluate") else file_knob.value()
                folder = os.path.dirname(out_file)
                if folder and not os.path.isdir(folder):
                    os.makedirs(folder)
            except Exception:
                pass
        start = time.time()
        try:
            nuke.execute(node, first, last, int(step), list(views) if views else list(nuke.views()),
                         bool(continue_on_error))
        except RuntimeError as exc:
            raise BridgeError("Render of %s failed: %s" % (node_ref(node), exc))
        results.append({
            "node": node_ref(node),
            "frames": [first, last, int(step)],
            "file": file_knob.value() if file_knob is not None else None,
            "first_output": out_file,
            "seconds": round(time.time() - start, 2),
        })
    return {"rendered": results}


@command()
def sample_pixel(node, x, y, frame=None, layer="rgba", area=None, parent=None):
    """Read pixel values of a node at (x, y) (pixel coordinates, origin bottom-left).

    ``area`` [w, h] averages a box instead of a single pixel.
    """
    n = find_node(node, parent)
    chans = _layer_channels(n, layer)
    frame = int(frame) if frame is not None else int(nuke.frame())
    values = {}
    for ch in chans:
        if area:
            values[ch] = n.sample(ch, float(x), float(y), float(area[0]), float(area[1]), frame)
        else:
            values[ch] = n.sample(ch, float(x) + 0.5, float(y) + 0.5, 1, 1, frame)
    return {"node": node_ref(n), "frame": frame, "x": x, "y": y, "values": values}


@command()
def get_image_info(node, frame=None, metadata=True, metadata_filter=None, parent=None):
    """Format, bounding box, layers/channels, frame range and metadata of a node's output."""
    n = find_node(node, parent)
    info = {"node": node_ref(n), "class": n.Class()}
    info.update(_image_info(n, frame))
    try:
        info["has_error"] = node_has_error(n)
        info["tree_has_error"] = node_has_error(n, tree=True)
    except Exception:
        pass
    if metadata:
        try:
            md = n.metadata(None, float(frame)) if frame is not None else n.metadata()
        except Exception:
            md = {}
        md = md or {}
        keys = sorted(md)
        if metadata_filter:
            keys = [k for k in keys if metadata_filter.lower() in k.lower()]
        info["metadata_count"] = len(md)
        info["metadata"] = {k: truncate(to_jsonable(md[k]), 300) if isinstance(md[k], str) else to_jsonable(md[k])
                            for k in keys[:200]}
    return info


# ---------------------------------------------------------------------------
# Footage
# ---------------------------------------------------------------------------

def _sequences(directory):
    try:
        entries = nuke.getFileNameList(directory, False, False, False, False) or []
    except Exception as exc:
        raise BridgeError("Cannot list %s: %s" % (directory, exc))
    return entries


def _match_entry(entries, filename):
    """Find the getFileNameList entry ('name.####.exr 1-10') that contains ``filename``."""
    for entry in entries:
        pattern = entry.rsplit(" ", 1)[0] if re.search(r" \d+-\d+$", entry) else entry
        if pattern == filename:
            return entry
        regex = re.escape(pattern)
        regex = re.sub(r"(\\#)+", lambda m: r"\d{%d,}" % (len(m.group(0)) // 2), regex)
        regex = re.sub(r"%0(\d)d", lambda m: r"\d{%s,}" % m.group(1), regex)
        regex = re.sub(r"%d", lambda m: r"\d+", regex)
        if regex != re.escape(pattern) and re.match("^%s$" % regex, filename):
            return entry
    return None


@command()
def list_sequences(directory):
    """List files in a directory with image sequences collapsed ('plate.####.exr 1001-1100')."""
    directory = norm_path(directory)
    if not os.path.isdir(directory):
        raise BridgeError("Not a directory: %s" % directory)
    entries = _sequences(directory)
    dirs = sorted(d for d in os.listdir(directory) if os.path.isdir(os.path.join(directory, d)))
    return {"directory": directory, "subdirectories": dirs, "files": sorted(entries)}


@command(undo=True)
def import_footage(path, name=None, parent=None, x=None, y=None, knobs=None):
    """Create Read node(s) with the frame range detected.

    ``path`` can be a file of a sequence (frame range is detected), a pattern
    ('plate.####.exr' / 'plate.%04d.exr'), a movie, or a directory (one Read per
    sequence/file found).
    """
    from ..utils import deselect_all, set_knob

    path = norm_path(path)
    group = find_group(parent)
    user_texts = []
    if os.path.isdir(path):
        for entry in _sequences(path):
            user_texts.append("%s/%s" % (path.rstrip("/"), entry))
        if not user_texts:
            raise BridgeError("No files found in %s" % path)
    else:
        directory, filename = os.path.split(path)
        if not os.path.isdir(directory):
            raise BridgeError("Directory does not exist: %s" % directory)
        entry = _match_entry(_sequences(directory), filename)
        if entry is None and not os.path.isfile(path):
            raise BridgeError("File not found: %s" % path)
        user_texts.append("%s/%s" % (directory, entry) if entry else path)
    created = []
    with preserved_selection(group):
        with group:
            for i, text in enumerate(user_texts):
                deselect_all(group)
                read = nuke.createNode("Read", inpanel=False)
                read.setSelected(False)
                read["file"].fromUserText(text)
                if name and len(user_texts) == 1:
                    read.setName(str(name))
                for knob_name, value in (knobs or {}).items():
                    set_knob(read, knob_name, value)
                if x is not None and y is not None:
                    read.setXYpos(int(x) + i * 120, int(y))
                created.append(read)
    out = []
    for read in created:
        info = node_summary(read, positions=True)
        info["file"] = read["file"].value()
        info["frame_range"] = [int(read["first"].value()), int(read["last"].value())]
        try:
            info["format"] = read.format().name() or "%dx%d" % (read.format().width(), read.format().height())
            info["colorspace"] = read["colorspace"].value()
        except Exception:
            pass
        out.append(info)
    return {"reads": out}
