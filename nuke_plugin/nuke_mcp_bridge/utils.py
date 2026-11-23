"""Shared helpers: node lookup, knob (de)serialisation, selection/undo helpers."""

import contextlib
import math
import os
import re
import shutil
import tempfile

import nuke


class BridgeError(Exception):
    """An error whose message is meant to be read by the MCP client / LLM."""


# Knobs that carry no value worth reporting.
NON_VALUE_KNOB_CLASSES = {
    "Tab_Knob",
    "Text_Knob",
    "PyScript_Knob",
    "Script_Knob",
    "Help_Knob",
    "Obsolete_Knob",
    "BeginTabGroup_Knob",
    "EndTabGroup_Knob",
    "PyCustom_Knob",
    "Button_Knob",
}

# Knobs reported elsewhere (or pure UI state) - hidden from knob dumps.
NOISY_KNOBS = {"xpos", "ypos", "selected", "name", "indicators", "dope_sheet", "bookmark"}

COLOR_KNOBS = {"tile_color", "gl_color", "note_font_color"}

MAX_STRING = 4000


# ---------------------------------------------------------------------------
# JSON conversion
# ---------------------------------------------------------------------------

def to_jsonable(obj, _depth=0):
    """Convert any value returned by a command into JSON-safe data.

    Never calls ``str()`` on a Node: that dumps its whole TCL script.
    """
    if _depth > 50:
        return "<max depth>"
    if obj is None or isinstance(obj, (bool, int)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return str(obj)
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v, _depth + 1) for v in obj]
    if isinstance(obj, nuke.Node):
        return node_ref(obj)
    if isinstance(obj, nuke.Knob):
        return obj.name()
    if isinstance(obj, nuke.Format):
        return format_dict(obj)
    text = repr(obj)
    return text if len(text) <= MAX_STRING else text[:MAX_STRING] + "...<truncated>"


def truncate(text, limit=MAX_STRING):
    if isinstance(text, str) and len(text) > limit:
        return text[:limit] + "...<truncated %d chars>" % (len(text) - limit)
    return text


# ---------------------------------------------------------------------------
# Node lookup
# ---------------------------------------------------------------------------

def node_ref(node):
    """Stable, fully qualified name of a node ("root" for the Root node)."""
    if node is None:
        return None
    if isinstance(node, nuke.Root):
        return "root"
    return node.fullName()


def find_node(name, parent=None):
    """Resolve a node from a (full) name, optionally relative to a group."""
    if isinstance(name, nuke.Node):
        return name
    if not isinstance(name, str) or not name.strip():
        raise BridgeError("A node name is required.")
    name = name.strip()
    if name.lower() == "root":
        return nuke.root()
    if name.startswith("root."):
        name = name[5:]
    node = None
    if parent:
        group = find_group(parent)
        with group:
            node = nuke.toNode(name)
    if node is None:
        node = nuke.toNode(name)
    if node is None:
        where = " inside %r" % parent if parent else ""
        raise BridgeError(
            "Node %r not found%s. Use list_nodes to see existing nodes "
            "(names inside groups are written Group1.Blur1)." % (name, where)
        )
    return node


def find_nodes(names, parent=None):
    if names is None:
        return []
    if isinstance(names, str):
        names = [names]
    return [find_node(n, parent) for n in names]


def find_group(path):
    """Resolve a group context: None/'root' -> Root, else a Group/Gizmo node."""
    if path is None or (isinstance(path, str) and path.strip() in ("", "root", "/", ".")):
        return nuke.root()
    node = find_node(path)
    if not isinstance(node, nuke.Group):
        raise BridgeError("%r is a %s, not a Group." % (path, node.Class()))
    return node


def parent_group(node):
    """The Group (or Root) that contains ``node``."""
    full = node.fullName()
    if "." in full:
        with nuke.root():
            return nuke.toNode(full.rsplit(".", 1)[0])
    return nuke.root()


def node_has_error(node, tree=False):
    """node.hasError() (or treeHasError()), evaluated in the node's own group.

    Outside its group's context a node inside a Group/Gizmo can't resolve its
    inputs or ``parent.`` expressions, so Nuke reports every such node as erroring.
    """
    check = node.treeHasError if tree else node.hasError
    group = parent_group(node) if not isinstance(node, nuke.Root) else None
    if group is None:
        return bool(check())
    with group:
        return bool(check())


def input_names(node):
    names = []
    try:
        count = node.inputs()
    except Exception:
        return names
    for i in range(count):
        inp = node.input(i)
        names.append(node_ref(inp) if inp is not None else None)
    return names


def node_summary(node, positions=False):
    """Compact description of a node, cheap enough to return for whole graphs."""
    info = {"name": node_ref(node), "class": node.Class()}
    ins = input_names(node)
    if any(i is not None for i in ins):
        info["inputs"] = ins
    disable = node.knob("disable")
    try:
        if disable is not None and disable.value():
            info["disabled"] = True
    except Exception:
        pass
    label = node.knob("label")
    try:
        if label is not None and label.value():
            info["label"] = truncate(label.value(), 200)
    except Exception:
        pass
    if positions:
        info["x"] = node.xpos()
        info["y"] = node.ypos()
    if isinstance(node, nuke.Group) and not isinstance(node, nuke.Root):
        info["is_group"] = True
    try:
        if node_has_error(node):
            info["error"] = True
    except Exception:
        pass
    return info


def node_size(node):
    """Screen size of a node in the DAG (falls back to defaults in terminal mode)."""
    try:
        w, h = node.screenWidth(), node.screenHeight()
    except Exception:
        w, h = 0, 0
    return (w or 80, h or 18)


# ---------------------------------------------------------------------------
# Formats & colours
# ---------------------------------------------------------------------------

def format_dict(fmt):
    if fmt is None:
        return None
    return {
        "name": fmt.name(),
        "width": fmt.width(),
        "height": fmt.height(),
        "pixel_aspect": fmt.pixelAspect(),
    }


def resolve_format(spec):
    """Return the name of a Nuke format matching ``spec``, creating it if needed.

    ``spec`` may be a format name ("HD_1080"), "1920x1080", "1920 1080" or a dict
    {"width", "height", "pixel_aspect"?, "name"?}.
    """
    width = height = None
    aspect = 1.0
    name = None
    if isinstance(spec, dict):
        width = int(spec["width"])
        height = int(spec["height"])
        aspect = float(spec.get("pixel_aspect", 1.0))
        name = spec.get("name")
    elif isinstance(spec, str):
        for f in nuke.formats():
            if f.name() == spec:
                return spec
        m = re.match(r"^\s*(\d+)\s*[xX* ]\s*(\d+)\s*(?:[ @]\s*([\d.]+))?\s*$", spec)
        if not m:
            known = ", ".join(sorted(f.name() for f in nuke.formats() if f.name()))
            raise BridgeError("Unknown format %r. Known formats: %s" % (spec, known))
        width, height = int(m.group(1)), int(m.group(2))
        if m.group(3):
            aspect = float(m.group(3))
    else:
        raise BridgeError("Format must be a name, 'WxH' string or a dict.")
    if not name:
        for f in nuke.formats():
            if f.width() == width and f.height() == height and abs(f.pixelAspect() - aspect) < 1e-6 and f.name():
                return f.name()
        name = "NA_%dx%d" % (width, height)
    name = re.sub(r"\s+", "_", name)
    nuke.addFormat("%d %d %s %s" % (width, height, repr(float(aspect)), name))
    return name


def parse_color(value):
    """Convert a colour to Nuke's packed 0xRRGGBBAA int.

    Accepts ints, '#RRGGBB', '#RRGGBBAA', '0xRRGGBBAA' and [r, g, b(, a)] floats 0-1.
    """
    if isinstance(value, bool):
        raise BridgeError("Invalid colour %r" % (value,))
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text.startswith("#"):
            text = text[1:]
            if len(text) == 6:
                text += "ff"
            return int(text, 16)
        if text.startswith("0x"):
            return int(text, 16)
        return int(text)
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        comps = list(value) + ([1.0] if len(value) == 3 else [])
        ints = [max(0, min(255, int(round(float(c) * 255)))) for c in comps]
        return (ints[0] << 24) | (ints[1] << 16) | (ints[2] << 8) | ints[3]
    raise BridgeError("Invalid colour %r" % (value,))


# ---------------------------------------------------------------------------
# Knob values
# ---------------------------------------------------------------------------

def enum_options(knob):
    try:
        return [v.split("\t")[0] for v in knob.values()]
    except Exception:
        return None


def knob_value(knob, frame=None):
    """JSON-friendly value of a knob, optionally evaluated at ``frame``."""
    cls = knob.Class()
    try:
        if cls == "Format_Knob":
            return format_dict(knob.value())
        if frame is not None and isinstance(knob, nuke.Array_Knob) and not isinstance(knob, nuke.Enumeration_Knob):
            size = knob.arraySize()
            if size <= 1:
                return knob.getValueAt(frame)
            return [knob.getValueAt(frame, i) for i in range(size)]
        value = knob.value()
    except Exception:
        try:
            return truncate(knob.toScript())
        except Exception:
            return None
    if isinstance(value, str):
        return truncate(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return to_jsonable(value)
    if isinstance(value, (list, tuple)):
        return to_jsonable(list(value))
    try:
        return truncate(knob.toScript())
    except Exception:
        return to_jsonable(value)


def knob_entry(knob, frame=None, verbose=False):
    """Value of a knob plus animation/expression state when relevant."""
    value = knob_value(knob, frame)
    extra = {}
    if isinstance(knob, nuke.Array_Knob):
        try:
            if knob.hasExpression():
                extra["expression"] = truncate(knob.toScript(), 1000)
            elif knob.isAnimated():
                extra["animated"] = True
                extra["keyframes"] = sum(
                    len(knob.animation(i).keys()) for i in range(knob.arraySize()) if knob.animation(i)
                )
        except Exception:
            pass
    if not extra and not verbose:
        return value
    entry = {"value": value}
    entry.update(extra)
    if verbose:
        entry["class"] = knob.Class()
    return entry


def iter_value_knobs(node, include_defaults=False, include_hidden=False):
    for name, knob in node.knobs().items():
        if name in NOISY_KNOBS or knob.Class() in NON_VALUE_KNOB_CLASSES:
            continue
        if not include_hidden:
            try:
                if knob.getFlag(nuke.INVISIBLE):
                    continue
            except Exception:
                pass
        if not include_defaults:
            try:
                if not knob.notDefault():
                    continue
            except Exception:
                pass
        yield name, knob


def get_knob(node, name):
    knob = node.knob(name)
    if knob is None:
        raise BridgeError(
            "Node %s (%s) has no knob %r. Use describe_node to list its knobs."
            % (node_ref(node), node.Class(), name)
        )
    return knob


def _is_numeric_knob(knob):
    return isinstance(knob, nuke.Array_Knob) and not isinstance(
        knob, (nuke.Enumeration_Knob, nuke.Boolean_Knob)
    )


def set_plain_value(knob, value):
    name = knob.name()
    if name in COLOR_KNOBS and not isinstance(value, bool):
        knob.setValue(parse_color(value))
        return
    if knob.Class() == "Format_Knob":
        knob.setValue(resolve_format(value))
        return
    if isinstance(value, str) and _is_numeric_knob(knob):
        try:
            value = float(value)
        except ValueError:
            knob.fromScript(value)
            return
    if isinstance(value, (list, tuple)):
        values = list(value)
        if isinstance(knob, nuke.Array_Knob):
            if len(values) > knob.arraySize():
                raise BridgeError(
                    "Knob %s takes at most %d values, got %d." % (name, knob.arraySize(), len(values))
                )
            try:
                if knob.setValue(values) is not False:
                    return
            except Exception:
                pass
            for i, v in enumerate(values):
                knob.setValue(v, i)
            return
        if knob.setValue(values) is False:
            raise BridgeError("Could not set %s to %r" % (name, value))
        return
    if isinstance(knob, nuke.Enumeration_Knob) and isinstance(value, str):
        options = enum_options(knob) or []
        if value not in options:
            matches = [o for o in options if o.lower() == value.lower()]
            if len(matches) != 1:
                raise BridgeError("Invalid value %r for %s. Options: %s" % (value, name, ", ".join(options)))
            value = matches[0]
    # Note: setValue() returns False when the value is unchanged, so its result is not checked.
    knob.setValue(value)


def apply_keys(knob, keys, index=None, interpolation=None, replace=False):
    """Set keyframes on a knob.

    keys: [{"frame": f, "value": v | [v0, v1, ...], "index"?: i}, ...]
    """
    if not isinstance(knob, nuke.Array_Knob):
        raise BridgeError("Knob %s (%s) cannot be animated." % (knob.name(), knob.Class()))
    size = knob.arraySize()
    if replace:
        knob.clearAnimated()
    touched = set()
    for key in keys:
        if "frame" not in key or "value" not in key:
            raise BridgeError("Each key needs 'frame' and 'value': %r" % (key,))
        frame = float(key["frame"])
        value = key["value"]
        if isinstance(value, (list, tuple)):
            pairs = list(enumerate(value))
        else:
            idx = key.get("index", index)
            if idx is None or idx == -1:
                pairs = [(i, value) for i in range(size)]
            else:
                pairs = [(int(idx), value)]
        for i, v in pairs:
            if i >= size:
                raise BridgeError("Index %d out of range for %s (size %d)" % (i, knob.name(), size))
            if not knob.isAnimated(i):
                knob.setAnimated(i)
            knob.setValueAt(float(v), frame, i)
            touched.add(i)
    if interpolation:
        interp = interpolation_constant(interpolation)
        for i in touched:
            curve = knob.animation(i)
            if curve is not None:
                curve.changeInterpolation(curve.keys(), interp)
    return sorted(touched)


def interpolation_constant(name):
    mapping = {
        "smooth": "SMOOTH",
        "linear": "LINEAR",
        "constant": "CONSTANT",
        "step": "CONSTANT",
        "cubic": "CUBIC",
        "catmull_rom": "CATMULL_ROM",
        "catmull-rom": "CATMULL_ROM",
        "horizontal": "HORIZONTAL",
        "break": "BREAK",
    }
    attr = mapping.get(str(name).lower())
    if attr is None or not hasattr(nuke, attr):
        raise BridgeError("Unknown interpolation %r. Use one of: %s" % (name, ", ".join(sorted(mapping))))
    return getattr(nuke, attr)


def set_knob(node, name, value):
    """Set a knob from a JSON value.

    Plain values (number, string, bool, list) are set directly. A dict allows:
      {"expression": "frame*2", "index": -1}
      {"keys": [{"frame": 1, "value": 0}, ...], "interpolation": "linear"}
      {"script": "{curve x1 0 x10 5}"}     (raw TCL via fromScript)
      {"value": ...}
    """
    knob = get_knob(node, name)
    if isinstance(value, dict):
        if "script" in value:
            knob.fromScript(str(value["script"]))
        if "value" in value:
            set_plain_value(knob, value["value"])
        if "keys" in value:
            apply_keys(knob, value["keys"], value.get("index"), value.get("interpolation"), value.get("replace", False))
        if "expression" in value:
            if not isinstance(knob, nuke.Array_Knob):
                raise BridgeError("Knob %s (%s) does not take expressions." % (name, knob.Class()))
            knob.setExpression(str(value["expression"]), int(value.get("index", -1)))
        return
    set_plain_value(knob, value)


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def undo_disabled():
    """Keep temporary helper nodes out of the user's undo history."""
    was_disabled = True
    try:
        was_disabled = nuke.Undo.disabled()
        if not was_disabled:
            nuke.Undo.disable()
    except Exception:
        pass
    try:
        yield
    finally:
        if not was_disabled:
            try:
                nuke.Undo.enable()
            except Exception:
                pass


@contextlib.contextmanager
def preserved_modified_state():
    """Restore the script's 'modified' flag after temporary changes."""
    try:
        was_modified = nuke.modified()
    except Exception:
        was_modified = None
    try:
        yield
    finally:
        if was_modified is not None:
            try:
                nuke.root().setModified(was_modified)
            except Exception:
                pass


def deselect_all(group=None):
    """Clear the selection in every group: nodeCopy refuses selections spanning groups.

    ``group`` is accepted for readability at call sites; the whole script is cleared.
    """
    for n in nuke.allNodes(group=nuke.root(), recurseGroups=True):
        if n.isSelected():
            n.setSelected(False)


@contextlib.contextmanager
def preserved_selection(group=None):
    group = group or nuke.root()
    with group:
        previous = list(nuke.selectedNodes())
    try:
        yield
    finally:
        try:
            deselect_all(group)
            for n in previous:
                try:
                    n.setSelected(True)
                except Exception:
                    pass  # node deleted meanwhile
        except Exception:
            pass


@contextlib.contextmanager
def temp_dir():
    path = tempfile.mkdtemp(prefix="nukeassistant_")
    try:
        yield path.replace("\\", "/")
    finally:
        shutil.rmtree(path, ignore_errors=True)


def norm_path(path):
    return os.path.expanduser(str(path)).replace("\\", "/")
