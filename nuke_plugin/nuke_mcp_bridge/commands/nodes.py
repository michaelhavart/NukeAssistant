"""Node creation, inspection and housekeeping."""

import fnmatch
import os

import nuke

from ..registry import command
from ..utils import (
    BridgeError,
    deselect_all,
    find_group,
    find_node,
    find_nodes,
    format_dict,
    input_names,
    iter_value_knobs,
    knob_entry,
    node_ref,
    node_size,
    node_summary,
    parent_group,
    preserved_selection,
    set_knob,
    temp_dir,
)

VERTICAL_GAP = 40


# ---------------------------------------------------------------------------
# Listing / inspection
# ---------------------------------------------------------------------------

@command()
def list_nodes(parent=None, node_class=None, pattern=None, selected_only=False, recursive=False,
               positions=False, limit=500):
    """List nodes in a group (root by default) with class, inputs and flags."""
    group = find_group(parent)
    if recursive:
        nodes = nuke.allNodes(group=group, recurseGroups=True)
    else:
        nodes = group.nodes()
    classes = None
    if node_class:
        classes = {c.lower() for c in ([node_class] if isinstance(node_class, str) else node_class)}
    result = []
    total = 0
    for n in nodes:
        if classes and n.Class().lower() not in classes:
            continue
        if pattern and not fnmatch.fnmatchcase(n.name(), pattern) and not fnmatch.fnmatchcase(n.fullName(), pattern):
            continue
        if selected_only and not n.isSelected():
            continue
        total += 1
        if len(result) < int(limit):
            result.append(node_summary(n, positions=positions))
    return {"group": node_ref(group), "count": total, "truncated": total > len(result), "nodes": result}


def _image_info(node, frame=None):
    info = {}
    try:
        info["format"] = format_dict(node.format())
    except Exception:
        return info
    try:
        box = node.bbox()
        info["bbox"] = [box.x(), box.y(), box.x() + box.w(), box.y() + box.h()]
    except Exception:
        pass
    try:
        channels = node.channels()
        layers = {}
        for ch in channels:
            layer, _, chan = ch.partition(".")
            layers.setdefault(layer, []).append(chan)
        info["layers"] = layers
    except Exception:
        pass
    try:
        info["frame_range"] = [node.firstFrame(), node.lastFrame()]
    except Exception:
        pass
    return info


@command()
def get_node_info(node, knobs=None, all_knobs=False, frame=None, include_image_info=True, parent=None):
    """Detailed info for one node: knobs (non-default by default), inputs, outputs, image info."""
    n = find_node(node, parent)
    info = node_summary(n, positions=True)
    info["inputs"] = input_names(n)
    try:
        info["max_inputs"] = n.maxInputs()
    except Exception:
        pass
    try:
        info["outputs"] = [node_ref(d) for d in n.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, False)]
    except Exception:
        pass
    if knobs:
        values = {}
        for name in ([knobs] if isinstance(knobs, str) else knobs):
            knob = n.knob(name)
            values[name] = knob_entry(knob, frame) if knob is not None else "<no such knob>"
        info["knobs"] = values
    else:
        info["knobs"] = {name: knob_entry(k, frame) for name, k in iter_value_knobs(n, include_defaults=all_knobs)}
        info["knobs_shown"] = "all" if all_knobs else "non-default only"
    if isinstance(n, nuke.Group) and not isinstance(n, nuke.Root):
        info["children"] = len(n.nodes())
    if include_image_info and not isinstance(n, nuke.Root):
        info.update(_image_info(n, frame))
    return info


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def _place_below(node, above):
    aw, ah = node_size(above)
    nw, _ = node_size(node)
    node.setXYpos(int(above.xpos() + (aw - nw) / 2), int(above.ypos() + ah + VERTICAL_GAP))


def _create_one(spec, group, resolver):
    node_class = spec.get("node_class") or spec.get("class")
    if not node_class:
        raise BridgeError("Node spec is missing 'node_class': %r" % (spec,))
    deselect_all(group)
    with group:
        try:
            node = nuke.createNode(str(node_class), inpanel=False)
        except RuntimeError as exc:
            raise BridgeError(
                "Cannot create %r: %s. Use list_node_classes to find valid class names." % (node_class, exc)
            )
    try:
        node.setSelected(False)
        if spec.get("name"):
            node.setName(str(spec["name"]))
        for knob_name, value in (spec.get("knobs") or {}).items():
            try:
                set_knob(node, knob_name, value)
            except BridgeError:
                raise
            except Exception as exc:
                raise BridgeError("Setting %s.%s=%r failed: %s" % (node.name(), knob_name, value, exc))
        inputs = spec.get("inputs") or []
        if isinstance(inputs, str):
            inputs = [inputs]
        for idx, src in enumerate(inputs):
            if src is None or src == "":
                continue
            src_node = resolver(src)
            if not node.setInput(idx, src_node):
                raise BridgeError(
                    "Could not connect %s to input %d of %s (max inputs %s)."
                    % (node_ref(src_node), idx, node.name(), node.maxInputs())
                )
        if spec.get("x") is not None and spec.get("y") is not None:
            node.setXYpos(int(spec["x"]), int(spec["y"]))
        elif inputs and inputs[0]:
            _place_below(node, resolver(inputs[0]))
        else:
            try:
                nuke.autoplace(node)
            except Exception:
                pass
    except Exception:
        nuke.delete(node)
        raise
    return node


def _resolver_for(group, created):
    def resolve(name):
        if isinstance(name, str) and name in created:
            return created[name]
        return find_node(name, parent=node_ref(group) if not isinstance(group, nuke.Root) else None)

    return resolve


@command(undo=True)
def create_node(node_class, name=None, knobs=None, inputs=None, parent=None, x=None, y=None):
    """Create a node, set knobs and connect inputs (list index = input number)."""
    group = find_group(parent)
    spec = {"node_class": node_class, "name": name, "knobs": knobs, "inputs": inputs, "x": x, "y": y}
    with preserved_selection(group):
        node = _create_one(spec, group, _resolver_for(group, {}))
    info = node_summary(node, positions=True)
    if name and node.name() != name:
        info["note"] = "Requested name %r was taken; Nuke named it %r." % (name, node.name())
    return info


@command(undo=True)
def create_nodes(nodes, parent=None):
    """Create several nodes in one undo step.

    Each spec: {"node_class", "name"?, "id"?, "knobs"?, "inputs"?, "x"?, "y"?}.
    Inputs may reference existing nodes or the "id"/"name" of an earlier spec.
    All-or-nothing: if one spec fails, the nodes created by this call are removed.
    """
    if not isinstance(nodes, list) or not nodes:
        raise BridgeError("'nodes' must be a non-empty list of node specs.")
    group = find_group(parent)
    created = {}
    order = []
    with preserved_selection(group):
        try:
            for i, spec in enumerate(nodes):
                try:
                    node = _create_one(spec, group, _resolver_for(group, created))
                except BridgeError as exc:
                    raise BridgeError("Spec #%d (%s): %s" % (i, spec.get("id") or spec.get("name") or spec.get("node_class"), exc))
                order.append(node)
                for key in (spec.get("id"), spec.get("name")):
                    if key:
                        created[str(key)] = node
        except Exception:
            for node in order:
                try:
                    nuke.delete(node)
                except Exception:
                    pass
            raise
    id_map = {key: node_ref(n) for key, n in created.items()}
    return {"created": [node_summary(n, positions=True) for n in order], "ids": id_map}


@command(undo=True)
def delete_nodes(nodes, parent=None):
    """Delete nodes by name."""
    targets = find_nodes(nodes, parent)
    names = [node_ref(n) for n in targets]
    for n in targets:
        if isinstance(n, nuke.Root):
            raise BridgeError("Cannot delete the Root node.")
    for n in targets:
        nuke.delete(n)
    return {"deleted": names}


@command(undo=True)
def rename_node(node, new_name, parent=None):
    """Rename a node (Nuke may append digits if the name is taken)."""
    n = find_node(node, parent)
    old = node_ref(n)
    n.setName(str(new_name))
    return {"old": old, "new": node_ref(n)}


@command(undo=True)
def duplicate_nodes(nodes, offset_x=150, offset_y=0, keep_inputs=True, parent=None):
    """Copy/paste nodes (keeping their internal connections), offset in the DAG."""
    targets = find_nodes(nodes, parent)
    if not targets:
        raise BridgeError("No nodes given.")
    group = parent_group(targets[0])
    external_inputs = {}
    target_set = set(id(t) for t in targets)
    if keep_inputs:
        for t in targets:
            for i in range(t.inputs()):
                inp = t.input(i)
                if inp is not None and id(inp) not in target_set:
                    external_inputs.setdefault(t.name(), []).append((i, inp))
    with preserved_selection(group), temp_dir() as tmp:
        path = os.path.join(tmp, "dup.nk").replace("\\", "/")
        with group:
            deselect_all(group)
            for t in targets:
                t.setSelected(True)
            nuke.nodeCopy(path)
            deselect_all(group)
            nuke.nodePaste(path)
            pasted = list(nuke.selectedNodes())
    if len(pasted) != len(targets):
        return {"created": [node_summary(n, positions=True) for n in pasted],
                "note": "Pasted node count differs from source; positions/inputs left as pasted."}
    # Paste keeps the relative layout, so matching by (y, x) order pairs each copy with its source.
    pairs = list(zip(sorted(targets, key=lambda n: (n.ypos(), n.xpos())),
                     sorted(pasted, key=lambda n: (n.ypos(), n.xpos()))))
    for src, new in pairs:
        new.setXYpos(int(src.xpos() + offset_x), int(src.ypos() + offset_y))
        if keep_inputs:
            for i, inp in external_inputs.get(src.name(), []):
                new.setInput(i, inp)
    return {"mapping": {node_ref(src): node_ref(new) for src, new in pairs},
            "created": [node_summary(n, positions=True) for _, n in pairs]}


@command(undo=True)
def set_node_positions(positions, parent=None):
    """Move nodes in the DAG: {"Blur1": [x, y], ...}."""
    moved = {}
    for name, pos in positions.items():
        n = find_node(name, parent)
        if isinstance(pos, dict):
            x, y = pos.get("x"), pos.get("y")
        else:
            x, y = pos
        n.setXYpos(int(x), int(y))
        moved[node_ref(n)] = [n.xpos(), n.ypos()]
    return {"moved": moved}


@command()
def select_nodes(nodes=None, add=False, parent=None):
    """Select the given nodes (replacing the selection unless add=true). Empty list clears it."""
    group = find_group(parent)
    if not add:
        deselect_all(group)
    targets = find_nodes(nodes or [], parent)
    for n in targets:
        n.setSelected(True)
    with group:
        return {"selected": [node_ref(n) for n in nuke.selectedNodes()]}


@command()
def get_selected_nodes(parent=None):
    """Nodes currently selected by the artist."""
    group = find_group(parent)
    with group:
        return {"selected": [node_summary(n, positions=True) for n in nuke.selectedNodes()]}
