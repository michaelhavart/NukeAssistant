"""Knob reading/writing, expressions, animation, user knobs and node schemas."""

import nuke

from ..registry import command
from ..utils import (
    BridgeError,
    apply_keys,
    enum_options,
    find_node,
    get_knob,
    iter_value_knobs,
    knob_entry,
    knob_value,
    node_ref,
    preserved_modified_state,
    set_knob,
    truncate,
    undo_disabled,
)


@command(undo=True)
def set_knobs(node=None, knobs=None, edits=None, parent=None):
    """Set knobs on one node ({knob: value}) or on many (edits={node: {knob: value}})."""
    if edits is None:
        if node is None or knobs is None:
            raise BridgeError("Pass node + knobs, or edits={node: {knob: value}}.")
        edits = {node: knobs}
    results = {}
    errors = {}
    for node_name, values in edits.items():
        n = find_node(node_name, parent)
        ref = node_ref(n)
        results[ref] = {}
        for knob_name, value in values.items():
            try:
                set_knob(n, knob_name, value)
                results[ref][knob_name] = knob_entry(n[knob_name])
            except Exception as exc:
                errors["%s.%s" % (ref, knob_name)] = str(exc)
    out = {"set": results}
    if errors:
        out["errors"] = errors
    return out


@command()
def get_knobs(node, knobs=None, frame=None, all_knobs=False, parent=None):
    """Read knob values (non-default ones when ``knobs`` is omitted)."""
    n = find_node(node, parent)
    if knobs:
        names = [knobs] if isinstance(knobs, str) else knobs
        return {name: knob_entry(get_knob(n, name), frame, verbose=True) for name in names}
    return {name: knob_entry(k, frame) for name, k in iter_value_knobs(n, include_defaults=all_knobs)}


@command(undo=True)
def set_expression(node, knob, expression, index=-1, parent=None):
    """Drive a knob with a Nuke expression (e.g. 'frame*2', 'Transform1.translate.x', '[value root.width]')."""
    n = find_node(node, parent)
    k = get_knob(n, knob)
    if not isinstance(k, nuke.Array_Knob):
        raise BridgeError("Knob %s (%s) does not take expressions." % (knob, k.Class()))
    k.setExpression(str(expression), int(index))
    return {"node": node_ref(n), "knob": knob, "script": truncate(k.toScript(), 1000)}


@command(undo=True)
def set_keyframes(node, knob, keys, index=None, interpolation=None, replace=False, parent=None):
    """Add keyframes: keys=[{"frame": 1, "value": 0}, {"frame": 50, "value": [1, 2]}]."""
    n = find_node(node, parent)
    k = get_knob(n, knob)
    touched = apply_keys(k, keys, index, interpolation, replace)
    return {"node": node_ref(n), "knob": knob, "animated_indices": touched, "animation": _curves(k)}


def _curves(knob):
    curves = {}
    try:
        size = knob.arraySize()
    except Exception:
        return curves
    for i in range(size):
        curve = knob.animation(i)
        if curve is None:
            continue
        entry = {"keys": [[key.x, key.y] for key in curve.keys()]}
        try:
            if curve.expression() and curve.expression() != "curve":
                entry["expression"] = curve.expression()
        except Exception:
            pass
        curves[str(i)] = entry
    return curves


@command()
def get_animation(node, knob, parent=None):
    """Keyframes ([frame, value]) and expressions for each channel of a knob."""
    n = find_node(node, parent)
    k = get_knob(n, knob)
    if not isinstance(k, nuke.Array_Knob):
        return {"node": node_ref(n), "knob": knob, "animated": False}
    return {"node": node_ref(n), "knob": knob, "animated": bool(k.isAnimated()),
            "has_expression": bool(k.hasExpression()), "curves": _curves(k)}


@command(undo=True)
def clear_animation(node, knob, index=-1, parent=None):
    """Remove keyframes/expressions from a knob (index -1 = all channels)."""
    n = find_node(node, parent)
    k = get_knob(n, knob)
    if not isinstance(k, nuke.Array_Knob):
        raise BridgeError("Knob %s is not animatable." % knob)
    k.clearAnimated(int(index))
    return {"node": node_ref(n), "knob": knob, "value": knob_value(k)}


# ---------------------------------------------------------------------------
# User knobs
# ---------------------------------------------------------------------------

def _make_knob(knob_type, name, label, options, link_target, command_text):
    t = knob_type.lower()
    simple = {
        "float": nuke.Double_Knob,
        "double": nuke.Double_Knob,
        "int": nuke.Int_Knob,
        "bool": nuke.Boolean_Knob,
        "boolean": nuke.Boolean_Knob,
        "string": nuke.String_Knob,
        "file": nuke.File_Knob,
        "rgb": nuke.Color_Knob,
        "color": nuke.AColor_Knob,
        "rgba": nuke.AColor_Knob,
        "xy": nuke.XY_Knob,
        "xyz": nuke.XYZ_Knob,
        "uv": nuke.UV_Knob,
        "wh": nuke.WH_Knob,
        "multiline": nuke.Multiline_Eval_String_Knob,
        "channel": nuke.Channel_Knob,
        "channels": nuke.ChannelMask_Knob,
        "format": nuke.Format_Knob,
        "box": nuke.BBox_Knob,
        "size": nuke.WH_Knob,
    }
    if t in simple:
        return simple[t](name, label)
    if t in ("enum", "enumeration", "pulldown"):
        return nuke.Enumeration_Knob(name, label, [str(o) for o in (options or [])])
    if t == "tab":
        return nuke.Tab_Knob(name, label)
    if t in ("text", "label"):
        return nuke.Text_Knob(name, label)
    if t == "divider":
        return nuke.Text_Knob(name, label or "")
    if t in ("button", "python_button"):
        return nuke.PyScript_Knob(name, label, command_text or "")
    if t == "link":
        if not link_target or "." not in link_target:
            raise BridgeError("link knobs need link_target='Node.knob'.")
        k = nuke.Link_Knob(name, label)
        k.setLink(link_target)
        return k
    raise BridgeError(
        "Unknown knob_type %r. Use one of: float, int, bool, string, file, rgb, rgba, xy, xyz, uv, wh, "
        "multiline, channel, channels, format, box, enum, tab, text, divider, button, link." % knob_type
    )


@command(undo=True)
def add_knob(node, knob_type, name, label=None, value=None, tooltip=None, tab=None, options=None,
             min=None, max=None, link_target=None, command=None, new_line=True, parent=None):
    """Add a user knob to a node (typically a Group/NoOp for building tools)."""
    n = find_node(node, parent)
    if n.knob(name) is not None:
        raise BridgeError("%s already has a knob named %r." % (node_ref(n), name))
    if tab and n.knob(tab) is None:
        n.addKnob(nuke.Tab_Knob(tab, tab))
    k = _make_knob(knob_type, name, label if label is not None else name, options, link_target, command)
    if tooltip:
        k.setTooltip(str(tooltip))
    if not new_line:
        try:
            k.clearFlag(nuke.STARTLINE)
        except Exception:
            pass
    if min is not None and max is not None:
        try:
            k.setRange(float(min), float(max))
        except Exception:
            pass
    n.addKnob(k)
    if value is not None:
        set_knob(n, name, value)
    return {"node": node_ref(n), "knob": name, "class": k.Class(), "value": knob_value(k)}


@command(undo=True)
def remove_knob(node, name, parent=None):
    """Remove a (user) knob from a node."""
    n = find_node(node, parent)
    k = get_knob(n, name)
    n.removeKnob(k)
    return {"node": node_ref(n), "removed": name}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

def _describe(n, include_hidden, name_filter):
    out = []
    for name, k in n.knobs().items():
        if name_filter and name_filter.lower() not in name.lower() and name_filter.lower() not in (k.label() or "").lower():
            continue
        try:
            hidden = bool(k.getFlag(nuke.INVISIBLE))
        except Exception:
            hidden = False
        if hidden and not include_hidden:
            continue
        cls = k.Class()
        if cls in ("Tab_Knob", "Text_Knob", "Obsolete_Knob", "BeginTabGroup_Knob", "EndTabGroup_Knob") and not include_hidden:
            continue
        entry = {"name": name, "class": cls}
        label = k.label()
        if label and label != name:
            entry["label"] = label
        entry["value"] = knob_value(k)
        if isinstance(k, nuke.Enumeration_Knob):
            entry["options"] = enum_options(k)
        try:
            if isinstance(k, nuke.Array_Knob) and k.arraySize() > 1:
                entry["size"] = k.arraySize()
        except Exception:
            pass
        try:
            tip = k.tooltip()
            if tip:
                entry["tooltip"] = truncate(tip.strip(), 240)
        except Exception:
            pass
        if hidden:
            entry["hidden"] = True
        out.append(entry)
    return out


@command()
def describe_node(node=None, node_class=None, include_hidden=False, filter=None, parent=None):
    """Knob schema (names, classes, defaults, enum options, tooltips) of a node or node class."""
    if node:
        n = find_node(node, parent)
        return {"node": node_ref(n), "class": n.Class(), "knobs": _describe(n, include_hidden, filter)}
    if not node_class:
        raise BridgeError("Pass node or node_class.")
    with undo_disabled(), preserved_modified_state():
        try:
            temp = getattr(nuke.nodes, str(node_class))()
        except Exception as exc:
            raise BridgeError("Cannot create %r: %s. Use list_node_classes." % (node_class, exc))
        try:
            data = {
                "class": temp.Class(),
                "min_inputs": temp.minInputs(),
                "max_inputs": temp.maxInputs(),
                "knobs": _describe(temp, include_hidden, filter),
            }
        finally:
            nuke.delete(temp)
    return data
