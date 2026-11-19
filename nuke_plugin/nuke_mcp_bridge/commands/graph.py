"""Node-graph topology: connections, trees, layout, backdrops, groups, .nk snippets."""

import os

import nuke

from ..registry import command
from ..utils import (
    BridgeError,
    deselect_all,
    find_group,
    find_node,
    find_nodes,
    node_ref,
    node_size,
    node_summary,
    parse_color,
    parent_group,
    preserved_selection,
    temp_dir,
    truncate,
)
from .nodes import _create_one, _place_below, _resolver_for

MAX_NK_CHARS = 400000


@command(undo=True)
def connect_nodes(connections, parent=None):
    """Connect nodes: [{"source": "Blur1", "target": "Merge1", "input": 1}, ...].

    source=null disconnects that input. For Merge nodes input 0 is B, input 1 is A,
    input 2 is the mask.
    """
    if isinstance(connections, dict):
        connections = [connections]
    done = []
    for conn in connections:
        target = find_node(conn["target"], parent)
        idx = int(conn.get("input", 0))
        src_name = conn.get("source")
        src = find_node(src_name, parent) if src_name else None
        if not target.setInput(idx, src):
            raise BridgeError(
                "Could not connect %s -> %s input %d (max inputs %d). Nodes must be in the same group."
                % (src_name, node_ref(target), idx, target.maxInputs())
            )
        done.append({"target": node_ref(target), "input": idx, "source": node_ref(src) if src else None})
    return {"connections": done}


@command(undo=True)
def disconnect_inputs(node, inputs=None, parent=None):
    """Disconnect some (list of indices) or all inputs of a node."""
    n = find_node(node, parent)
    indices = range(n.inputs()) if inputs is None else [int(i) for i in inputs]
    for i in indices:
        n.setInput(i, None)
    return node_summary(n)


@command(undo=True)
def insert_node(after, node_class, name=None, knobs=None, parent=None):
    """Create a node directly below ``after`` and re-route ``after``'s outputs through it."""
    upstream = find_node(after, parent)
    group = parent_group(upstream)
    downstream = []
    for dep in upstream.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, False):
        for i in range(dep.inputs()):
            if dep.input(i) is upstream:
                downstream.append((dep, i))
    with preserved_selection(group):
        spec = {"node_class": node_class, "name": name, "knobs": knobs, "inputs": [upstream]}
        new = _create_one(spec, group, _resolver_for(group, {}))
    for dep, i in downstream:
        dep.setInput(i, new)
    # Push the downstream nodes down to make room.
    _, h = node_size(new)
    for dep, _ in downstream:
        if dep.ypos() <= new.ypos() + h:
            dep.setXYpos(dep.xpos(), new.ypos() + h + 40)
    return {"inserted": node_summary(new, positions=True),
            "rewired": [{"node": node_ref(d), "input": i} for d, i in downstream]}


def _walk(node, direction, depth, seen, out):
    if node is None or id(node) in seen:
        return
    seen.add(id(node))
    out.append(node)
    if depth == 0:
        return
    if direction == "upstream":
        nxt = [node.input(i) for i in range(node.inputs())]
    else:
        nxt = node.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, False)
    for n in nxt:
        _walk(n, direction, depth - 1, seen, out)


@command()
def get_node_tree(node, direction="upstream", depth=50, positions=False, parent=None):
    """All nodes upstream (feeding into) or downstream (fed by) a node."""
    if direction not in ("upstream", "downstream"):
        raise BridgeError("direction must be 'upstream' or 'downstream'.")
    start = find_node(node, parent)
    out = []
    _walk(start, direction, int(depth), set(), out)
    return {"root": node_ref(start), "direction": direction, "nodes": [node_summary(n, positions) for n in out]}


@command()
def find_errors(parent=None, recursive=True):
    """Nodes currently in an error state (missing files, bad expressions...)."""
    group = find_group(parent)
    nodes = nuke.allNodes(group=group, recurseGroups=bool(recursive))
    errors = []
    for n in nodes:
        try:
            if n.hasError():
                entry = node_summary(n)
                knob = n.knob("file")
                if knob is not None:
                    entry["file"] = knob.value()
                errors.append(entry)
        except Exception:
            pass
    return {"count": len(errors), "nodes": errors}


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _tree_layout(root_node, spacing_x, spacing_y):
    """Lay out the upstream tree of ``root_node``: input 0 straight up, others to the right."""
    placed = {}
    group_nodes = set(id(n) for n in parent_group(root_node).nodes())

    def width(node, visiting):
        if id(node) in visiting:
            return 0
        visiting = visiting | {id(node)}
        total = 0
        for i in range(node.inputs()):
            inp = node.input(i)
            if inp is not None and id(inp) in group_nodes:
                total += width(inp, visiting)
        return max(1, total)

    def place(node, col, row):
        if id(node) in placed:
            return
        w, _ = node_size(node)
        x = base_x + col * spacing_x - w // 2
        y = base_y - row * spacing_y
        node.setXYpos(int(x), int(y))
        placed[id(node)] = node
        offset = col
        for i in range(node.inputs()):
            inp = node.input(i)
            if inp is None or id(inp) not in group_nodes or id(inp) in placed:
                continue
            place(inp, offset, row + 1)
            offset += width(inp, frozenset())

    rw, _ = node_size(root_node)
    base_x = root_node.xpos() + rw // 2
    base_y = root_node.ypos()
    place(root_node, 0, 0)
    return list(placed.values())


@command(undo=True)
def layout_nodes(nodes=None, root=None, method="tree", spacing_x=130, spacing_y=90, parent=None):
    """Tidy the DAG.

    method="tree": arrange everything upstream of ``root`` (e.g. a Write) as a tree.
    method="autoplace": run Nuke's autoplace on ``nodes`` (or all nodes in the group).
    """
    if method == "tree":
        if not root:
            raise BridgeError("method='tree' needs root=<output node name>.")
        placed = _tree_layout(find_node(root, parent), int(spacing_x), int(spacing_y))
        return {"method": "tree", "moved": [node_ref(n) for n in placed]}
    if method == "autoplace":
        targets = find_nodes(nodes, parent) if nodes else find_group(parent).nodes()
        for n in targets:
            try:
                nuke.autoplace(n)
            except Exception:
                pass
        return {"method": "autoplace", "moved": [node_ref(n) for n in targets]}
    raise BridgeError("method must be 'tree' or 'autoplace'.")


@command(undo=True)
def create_backdrop(nodes, label="", color=None, padding=60, font_size=42, name=None, parent=None):
    """Draw a BackdropNode around the given nodes."""
    targets = find_nodes(nodes, parent)
    if not targets:
        raise BridgeError("No nodes given for the backdrop.")
    group = parent_group(targets[0])
    left = min(n.xpos() for n in targets) - padding
    top = min(n.ypos() for n in targets) - padding - int(font_size * 1.5)
    right = max(n.xpos() + node_size(n)[0] for n in targets) + padding
    bottom = max(n.ypos() + node_size(n)[1] for n in targets) + padding
    with group:
        bd = nuke.nodes.BackdropNode(
            xpos=int(left), ypos=int(top), bdwidth=int(right - left), bdheight=int(bottom - top),
            label=str(label), note_font_size=int(font_size),
        )
        if color is not None:
            bd["tile_color"].setValue(parse_color(color))
        if name:
            bd.setName(str(name))
    return node_summary(bd, positions=True)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

@command(undo=True)
def collapse_to_group(nodes, name=None, parent=None):
    """Collapse nodes into a Group (connections to the outside become Input/Output nodes)."""
    targets = find_nodes(nodes, parent)
    if not targets:
        raise BridgeError("No nodes given.")
    group = parent_group(targets[0])
    # Not nuke.collapseToGroup: it is GUI-only and, when it cannot tell which node is the
    # output, opens a modal "Group Output" dialog that blocks until someone answers.
    new_group = _collapse_manually(targets, group)
    if new_group is None:
        raise BridgeError("collapseToGroup failed.")
    if name:
        new_group.setName(str(name))
    new_group.setSelected(False)
    return {"group": node_summary(new_group, positions=True),
            "children": [node_summary(c) for c in new_group.nodes()]}


def _collapse_manually(targets, outer):
    """Equivalent of nuke.collapseToGroup that never asks questions."""
    inside = set(id(n) for n in targets)
    ext_inputs = []  # (source node, [(target name, input index)])
    for n in targets:
        for i in range(n.inputs()):
            src = n.input(i)
            if src is not None and id(src) not in inside:
                for entry in ext_inputs:
                    if entry[0] is src:
                        entry[1].append((n.name(), i))
                        break
                else:
                    ext_inputs.append((src, [(n.name(), i)]))
    ext_outputs = []  # (inner node name, downstream node, input index)
    for n in targets:
        for dep in n.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, False):
            if id(dep) in inside:
                continue
            for i in range(dep.inputs()):
                if dep.input(i) is n:
                    ext_outputs.append((n.name(), dep, i))
    out_names = sorted(set(o[0] for o in ext_outputs))
    if len(out_names) > 1:
        raise BridgeError("Nodes %s all feed outside nodes; a group can only have one output." % out_names)
    if not out_names:
        # No downstream link: use the lowest node in the DAG as the output.
        out_names = [max(targets, key=lambda n: n.ypos()).name()]

    cx = sum(n.xpos() for n in targets) // len(targets)
    cy = sum(n.ypos() for n in targets) // len(targets)
    with temp_dir() as tmp:
        path = os.path.join(tmp, "collapse.nk").replace("\\", "/")
        with outer:
            deselect_all(outer)
            for n in targets:
                n.setSelected(True)
            nuke.nodeCopy(path)
            deselect_all(outer)
            grp = nuke.nodes.Group()
        with grp:
            nuke.nodePaste(path)
            inner = {n.name(): n for n in nuke.selectedNodes()}
            for n in inner.values():
                n.setSelected(False)
            for idx, (src, links) in enumerate(ext_inputs):
                pipe = nuke.nodes.Input(name="Input%d" % (idx + 1))
                first = inner.get(links[0][0])
                if first is not None:
                    pipe.setXYpos(first.xpos(), first.ypos() - 80)
                for target_name, i in links:
                    if target_name in inner:
                        inner[target_name].setInput(i, pipe)
            out_node = inner.get(out_names[0])
            output = nuke.nodes.Output()
            output.setInput(0, out_node)
            if out_node is not None:
                output.setXYpos(out_node.xpos(), out_node.ypos() + 80)
    for idx, (src, _) in enumerate(ext_inputs):
        grp.setInput(idx, src)
    for _, dep, i in ext_outputs:
        dep.setInput(i, grp)
    for n in targets:
        nuke.delete(n)
    grp.setXYpos(int(cx), int(cy))
    return grp


@command(undo=True)
def create_group(name=None, nodes=None, inputs=None, knobs=None, parent=None, x=None, y=None):
    """Create a Group and build its contents in one call.

    ``nodes`` are create_nodes specs built inside the group; use node_class
    "Input"/"Output" for the group's pipes. ``inputs`` connects the group's inputs
    to outside nodes.
    """
    from .nodes import create_nodes

    outer = find_group(parent)
    resolve = _resolver_for(outer, {})
    with preserved_selection(outer):
        grp = _create_one({"node_class": "Group", "name": name, "knobs": knobs, "x": x, "y": y},
                          outer, resolve)
    built = None
    try:
        if nodes:
            built = create_nodes(nodes, parent=node_ref(grp))
        # The group only grows input pipes once its Input nodes exist.
        for idx, src in enumerate(inputs or []):
            if src and not grp.setInput(idx, resolve(src)):
                raise BridgeError("Group has %d inputs; could not connect input %d. Add Input nodes."
                                  % (grp.maxInputs(), idx))
        if inputs and inputs[0] and x is None:
            _place_below(grp, resolve(inputs[0]))
    except Exception:
        nuke.delete(grp)
        raise
    return {"group": node_summary(grp, positions=True), "contents": built}


@command(undo=True)
def expand_group(node, parent=None):
    """Explode a Group back into its parent graph."""
    n = find_node(node, parent)
    if not isinstance(n, nuke.Group) or isinstance(n, nuke.Root):
        raise BridgeError("%s is not a Group." % node_ref(n))
    if nuke.GUI:
        n.expand()
        return {"expanded": node}
    created = _expand_manually(n)  # Group.expand is GUI-only
    return {"expanded": node, "nodes": [node_summary(c, positions=True) for c in created]}


def _expand_manually(grp):
    """Terminal-mode equivalent of Group.expand()."""
    outer = parent_group(grp)
    with grp:
        content = [c for c in grp.nodes() if c.Class() not in ("Input", "Output")]
        pipe_links = []  # (inner node, input index, group input number)
        for c in content:
            for i in range(c.inputs()):
                src = c.input(i)
                if src is not None and src.Class() == "Input":
                    pipe_links.append((c, i, int(src["number"].value())))
        outputs = [c for c in grp.nodes() if c.Class() == "Output"]
        out_src = outputs[0].input(0) if outputs else None
    if not content:
        nuke.delete(grp)
        return []
    group_inputs = [grp.input(k) for k in range(grp.inputs())]
    downstream = [(dep, i) for dep in grp.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, False)
                  for i in range(dep.inputs()) if dep.input(i) is grp]
    with temp_dir() as tmp:
        path = os.path.join(tmp, "expand.nk").replace("\\", "/")
        with grp:
            deselect_all(grp)
            for c in content:
                c.setSelected(True)
            nuke.nodeCopy(path)
            deselect_all(grp)
        with outer:
            deselect_all(outer)
            nuke.nodePaste(path)
            pasted = list(nuke.selectedNodes())
            for p in pasted:
                p.setSelected(False)
    if len(pasted) != len(content):
        raise BridgeError("Expanding %s pasted %d nodes instead of %d." % (node_ref(grp), len(pasted), len(content)))
    # Pasting keeps relative layout, so (y, x) order pairs each copy with its original.
    order = lambda n: (n.ypos(), n.xpos())  # noqa: E731
    mapping = dict(zip([id(c) for c in sorted(content, key=order)], sorted(pasted, key=order)))
    dx = grp.xpos() - sum(c.xpos() for c in content) // len(content)
    dy = grp.ypos() - sum(c.ypos() for c in content) // len(content)
    for c in content:
        mapping[id(c)].setXYpos(int(c.xpos() + dx), int(c.ypos() + dy))
    for c, i, number in pipe_links:
        if number < len(group_inputs) and group_inputs[number] is not None:
            mapping[id(c)].setInput(i, group_inputs[number])
    new_out = mapping.get(id(out_src)) if out_src is not None else None
    for dep, i in downstream:
        dep.setInput(i, new_out)
    nuke.delete(grp)
    return [mapping[id(c)] for c in content]


# ---------------------------------------------------------------------------
# .nk snippets
# ---------------------------------------------------------------------------

@command(undo=True)
def paste_nk(nk, parent=None):
    """Paste a .nk script snippet (the text Nuke puts on the clipboard) into a group."""
    group = find_group(parent)
    with temp_dir() as tmp:
        path = os.path.join(tmp, "paste.nk").replace("\\", "/")
        with open(path, "w") as fh:
            fh.write(nk)
        with group:
            deselect_all(group)
            nuke.nodePaste(path)
            pasted = list(nuke.selectedNodes())
            for n in pasted:
                n.setSelected(False)
    return {"pasted": [node_summary(n, positions=True) for n in pasted]}


@command()
def export_nk(nodes=None, parent=None, include_upstream=False):
    """Return nodes as .nk text (all nodes of the group when ``nodes`` is omitted)."""
    group = find_group(parent)
    if nodes:
        targets = find_nodes(nodes, parent)
        if include_upstream:
            seen, out = set(), []
            for t in targets:
                _walk(t, "upstream", 1000, seen, out)
            targets = out
    else:
        targets = group.nodes()
    if not targets:
        return {"nk": "", "nodes": 0}
    group = parent_group(targets[0])
    with preserved_selection(group), temp_dir() as tmp:
        path = os.path.join(tmp, "copy.nk").replace("\\", "/")
        with group:
            deselect_all(group)
            for n in targets:
                n.setSelected(True)
            nuke.nodeCopy(path)
        with open(path) as fh:
            text = fh.read()
    return {"nodes": len(targets), "nk": truncate(text, MAX_NK_CHARS)}
