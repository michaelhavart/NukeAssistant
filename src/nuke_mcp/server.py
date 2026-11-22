"""NukeAssistant MCP server: exposes Foundry Nuke to MCP clients (Claude Code, Claude Desktop...).

Talks to the bridge running inside Nuke (nuke_plugin/nuke_mcp_bridge) over a local socket.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import json
import os
from typing import Any, Literal

from . import launcher
from .mcp_lite import Image, MCPServer, ToolAnnotations, ToolError
from .connection import DEFAULT_PORT, PORT_SCAN, NukeCommandError, NukeConnection, NukeConnectionError, probe

INSTRUCTIONS = """\
Control a running Foundry Nuke session: build and edit node graphs, set knobs, animate,
render previews and Write nodes, and run Python/TCL inside Nuke.

Conventions
- Nodes are addressed by full name: "Blur1" at root, "Group1.Blur1" inside a group.
  Use parent="Group1" on create/list tools to work inside a group. The Root node is "root".
- Inputs are lists indexed by input number. Merge2: input 0 = B, 1 = A, 2 = mask.
  Most filters: input 0 = source, last input = mask.
- Unsure about knob names or enum values? Call describe_node(node_class="Blur") first.
- Knob values: numbers, strings, bools, lists for multi-value knobs ([x, y], [r, g, b, a]).
  Expressions: {"expression": "frame*2"}; keys: {"keys": [{"frame": 1, "value": 0}]}.
  Colours for tile_color/gl_color: "#ff8800" or [r, g, b] floats.
- Prefer create_nodes (one call, one undo step) to build whole trees, or paste_nk for .nk text.
- Check your work visually with render_preview, and find_errors after building.
- Each tool call is one undo step in Nuke; use undo to revert mistakes.
- If Nuke is not reachable, call list_nuke_sessions or launch_nuke.
"""

mcp = MCPServer(name="nuke", instructions=INSTRUCTIONS)

_state: dict[str, Any] = {"conn": NukeConnection()}


def _conn() -> NukeConnection:
    return _state["conn"]


async def _call(command: str, /, timeout: float = 120.0, **params: Any) -> Any:
    # `command` is positional-only so tools may forward a parameter named "command".
    try:
        return await _conn().call(command, params, timeout=timeout)
    except NukeConnectionError as exc:
        raise ToolError(str(exc)) from exc
    except NukeCommandError as exc:
        message = str(exc)
        if exc.traceback and exc.error_type not in ("BridgeError",):
            message += "\n\nNuke traceback:\n" + exc.traceback[-2500:]
        raise ToolError(message) from exc


def _json(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _tool(read_only: bool = False, destructive: bool = False, idempotent: bool = False):
    return mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=destructive,
            idempotent_hint=idempotent,
            open_world_hint=False,
        ),
    )


def _image_result(result: dict[str, Any]) -> list[Any]:
    data = base64.b64decode(result.pop("image_base64"))
    fmt = result.get("mime_type", "image/png").split("/")[-1]
    return [Image(data=data, format=fmt), _json(result)]


# ===========================================================================
# Session
# ===========================================================================

@_tool(read_only=True)
async def get_session_info() -> str:
    """Nuke version, GUI/terminal mode, current script path, frame range, fps, format,
    colour management and node counts. Call this first to orient yourself."""
    return _json(await _call("session_info"))


@_tool(read_only=True)
async def list_nuke_sessions() -> str:
    """Find Nuke sessions with a running bridge on this machine (ports 54321-54330)
    and show which one this server is talking to."""
    host = _conn().host
    base = int(os.environ.get("NUKE_MCP_PORT", DEFAULT_PORT))
    ports = list(range(base, base + PORT_SCAN))
    results = await asyncio.gather(*(probe(host, p) for p in ports))
    sessions = [dict(port=p, **r) for p, r in zip(ports, results) if r]
    return _json({"connected_to": _conn().address, "sessions": sessions})


@_tool()
async def connect_to_nuke(port: int, host: str = "127.0.0.1") -> str:
    """Switch this server to the Nuke session listening on host:port
    (see list_nuke_sessions)."""
    info = await probe(host, port, timeout=3)
    if not info:
        raise ToolError(f"No NukeAssistant bridge answering on {host}:{port}.")
    await _conn().close()
    _state["conn"] = NukeConnection(host, port)
    return _json({"connected_to": f"{host}:{port}", "session": info})


@_tool()
async def launch_nuke(
    mode: Literal["gui", "headless"] = "gui",
    script: str | None = None,
    variant: Literal["nuke", "nukex", "nukestudio"] = "nuke",
    wait_seconds: int = 180,
) -> str:
    """Start a new Nuke with the bridge loaded and connect to it.

    Args:
        mode: "gui" opens the interactive app; "headless" runs `nuke -t` (no UI,
            needs a render or interactive licence, closed with shutdown_headless_nuke).
        script: optional .nk file to open.
        variant: licence flavour to request.
        wait_seconds: how long to wait for the bridge to come up.
    The executable is found via the NUKE_EXE env var or standard install folders.
    """
    host = _conn().host
    base = int(os.environ.get("NUKE_MCP_PORT", DEFAULT_PORT))
    port = await launcher.free_port(host, base, PORT_SCAN)
    try:
        proc, log_path = launcher.launch(mode=mode, script=script, variant=variant, port=port)
    except launcher.LaunchError as exc:
        raise ToolError(str(exc)) from exc
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        if proc.poll() is not None:
            raise ToolError(
                f"Nuke exited with code {proc.returncode} before the bridge started.\n"
                + launcher.tail(log_path)
            )
        info = await probe(host, port, timeout=1)
        if info:
            await _conn().close()
            _state["conn"] = NukeConnection(host, port)
            return _json({"connected_to": f"{host}:{port}", "pid": proc.pid, "mode": mode, "session": info,
                          "log": log_path})
        await asyncio.sleep(1.0)
    raise ToolError(
        f"Nuke started (pid {proc.pid}) but the bridge did not answer on port {port} within "
        f"{wait_seconds}s.\n" + launcher.tail(log_path)
    )


@_tool(destructive=True)
async def shutdown_headless_nuke() -> str:
    """Quit the connected terminal-mode (nuke -t) session. Refuses to close a GUI Nuke."""
    result = await _call("shutdown_headless")
    await _conn().close()
    return _json(result)


@_tool()
async def undo(steps: int = 1) -> str:
    """Undo the last N actions in Nuke (each tool call that changes the script is one step)."""
    return _json(await _call("undo", steps=steps))


@_tool()
async def redo(steps: int = 1) -> str:
    """Redo N undone actions."""
    return _json(await _call("redo", steps=steps))


# ===========================================================================
# Script / project
# ===========================================================================

@_tool(destructive=True)
async def new_script(force: bool = False) -> str:
    """Clear the current script. Fails if there are unsaved changes unless force=true."""
    return _json(await _call("new_script", force=force))


@_tool(destructive=True)
async def open_script(path: str, force: bool = False) -> str:
    """Open a .nk script, replacing the current one (force=true discards unsaved changes)."""
    return _json(await _call("open_script", timeout=600, path=path, force=force))


@_tool()
async def import_script(path: str, parent: str | None = None) -> str:
    """Insert the nodes of another .nk file into the current script (File > Insert)."""
    return _json(await _call("import_script", timeout=600, path=path, parent=parent))


@_tool()
async def save_script(path: str | None = None, overwrite: bool = True) -> str:
    """Save the script. With a path this is Save As (folders are created)."""
    return _json(await _call("save_script", timeout=600, path=path, overwrite=overwrite))


@_tool()
async def set_project_settings(
    first_frame: int | None = None,
    last_frame: int | None = None,
    fps: float | None = None,
    format: str | dict[str, Any] | None = None,
    knobs: dict[str, Any] | None = None,
) -> str:
    """Set the project frame range, fps, output format and/or any other Root knob.

    format: a format name ("HD_1080", "UHD_4K"), "1920x1080", or
    {"width": 2048, "height": 858, "pixel_aspect": 1, "name": "Scope2K"}.
    knobs: extra Root knobs, e.g. {"colorManagement": "OCIO", "proxy_type": "scale"}.
    """
    return _json(await _call("set_project_settings", first_frame=first_frame, last_frame=last_frame,
                             fps=fps, format=format, knobs=knobs))


@_tool()
async def set_current_frame(frame: int) -> str:
    """Move the timeline to a frame."""
    return _json(await _call("set_current_frame", frame=frame))


@_tool(read_only=True)
async def list_formats() -> str:
    """All image formats defined in this Nuke session."""
    return _json(await _call("list_formats"))


# ===========================================================================
# Nodes
# ===========================================================================

@_tool(read_only=True)
async def list_nodes(
    parent: str | None = None,
    node_class: str | list[str] | None = None,
    pattern: str | None = None,
    selected_only: bool = False,
    recursive: bool = False,
    positions: bool = False,
    limit: int = 500,
) -> str:
    """List nodes (name, class, inputs, disabled/error flags) in the root or a group.

    Args:
        parent: group to list (default root).
        node_class: only these classes, e.g. "Read" or ["Read", "Write"].
        pattern: glob on node name, e.g. "Grade*".
        recursive: include nodes inside groups.
        positions: include DAG x/y.
    """
    return _json(await _call("list_nodes", parent=parent, node_class=node_class, pattern=pattern,
                             selected_only=selected_only, recursive=recursive, positions=positions,
                             limit=limit))


@_tool(read_only=True)
async def get_node_info(
    node: str,
    knobs: list[str] | None = None,
    all_knobs: bool = False,
    frame: float | None = None,
    include_image_info: bool = True,
) -> str:
    """Details of one node: knob values (non-default ones unless all_knobs or a knob list
    is given), inputs, outputs, and its output format/bbox/layers/frame range.
    Animated knobs report their value at `frame` when given."""
    return _json(await _call("get_node_info", node=node, knobs=knobs, all_knobs=all_knobs, frame=frame,
                             include_image_info=include_image_info))


@_tool()
async def create_node(
    node_class: str,
    name: str | None = None,
    knobs: dict[str, Any] | None = None,
    inputs: list[str | None] | None = None,
    parent: str | None = None,
    x: int | None = None,
    y: int | None = None,
) -> str:
    """Create one node.

    Args:
        node_class: Nuke class, e.g. "Grade", "Merge2", "Transform", "Read", "Write",
            "Blur", "Roto", or a gizmo name (see list_node_classes).
        name: desired name (Nuke appends digits if taken).
        knobs: {"knob": value}. See the server instructions for value formats.
        inputs: node names by input index, e.g. ["Read1"] or ["BG_Grade", "FG_Grade"]
            for a Merge2 (B, A). Use null to skip an input.
        parent: group to create the node in.
        x, y: DAG position; by default the node is placed below its first input.
    """
    return _json(await _call("create_node", node_class=node_class, name=name, knobs=knobs, inputs=inputs,
                             parent=parent, x=x, y=y))


@_tool()
async def create_nodes(nodes: list[dict[str, Any]], parent: str | None = None) -> str:
    """Create many nodes at once (one undo step, all-or-nothing).

    Each spec: {"node_class": "Grade", "id": "fg_grade", "name"?: str, "knobs"?: {...},
    "inputs"?: [...], "x"?: int, "y"?: int}. Inputs can reference existing node names
    or the "id"/"name" of earlier specs in the same list, so a whole tree can be built:
      [{"node_class": "Read", "id": "fg", "knobs": {"file": "C:/fg.####.exr"}},
       {"node_class": "Read", "id": "bg", "knobs": {"file": "C:/bg.####.exr"}},
       {"node_class": "Merge2", "id": "comp", "inputs": ["bg", "fg"], "knobs": {"operation": "over"}},
       {"node_class": "Write", "inputs": ["comp"], "knobs": {"file": "C:/out/comp.####.exr"}}]
    The result maps each id to the actual node name.
    """
    return _json(await _call("create_nodes", nodes=nodes, parent=parent))


@_tool(destructive=True)
async def delete_nodes(nodes: list[str]) -> str:
    """Delete nodes by name."""
    return _json(await _call("delete_nodes", nodes=nodes))


@_tool()
async def rename_node(node: str, new_name: str) -> str:
    """Rename a node."""
    return _json(await _call("rename_node", node=node, new_name=new_name))


@_tool()
async def duplicate_nodes(nodes: list[str], offset_x: int = 150, offset_y: int = 0, keep_inputs: bool = True) -> str:
    """Duplicate nodes (internal connections kept; external inputs reconnected when
    keep_inputs). Returns the old->new name mapping."""
    return _json(await _call("duplicate_nodes", nodes=nodes, offset_x=offset_x, offset_y=offset_y,
                             keep_inputs=keep_inputs))


@_tool()
async def set_node_positions(positions: dict[str, list[int]]) -> str:
    """Move nodes in the Node Graph: {"Blur1": [x, y], "Grade1": [x, y]}.
    Nuke's y axis points down; nodes are ~80x18 units, spacing ~100-150 looks tidy."""
    return _json(await _call("set_node_positions", positions=positions))


@_tool()
async def select_nodes(nodes: list[str] | None = None, add: bool = False, parent: str | None = None) -> str:
    """Select nodes in the Node Graph (replaces the selection unless add=true;
    an empty list clears it)."""
    return _json(await _call("select_nodes", nodes=nodes or [], add=add, parent=parent))


@_tool(read_only=True)
async def get_selected_nodes(parent: str | None = None) -> str:
    """Nodes the artist currently has selected - use this to act on 'these nodes'."""
    return _json(await _call("get_selected_nodes", parent=parent))


# ===========================================================================
# Knobs
# ===========================================================================

@_tool()
async def set_knobs(
    node: str | None = None,
    knobs: dict[str, Any] | None = None,
    edits: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Set knob values on one node (node + knobs) or many (edits={"Grade1": {...}, "Blur1": {...}}).

    Values: number | string | bool | list (multi-value knobs, e.g. "translate": [10, 20],
    "white": [1.1, 1.0, 0.9, 1]) | dict:
      {"expression": "frame/10", "index": -1}
      {"keys": [{"frame": 1, "value": 0}, {"frame": 24, "value": 1}], "interpolation": "linear"}
      {"script": "<raw TCL value>"}
    Enum knobs take the option label ("operation": "screen"). Per-knob failures are
    reported in "errors" without aborting the others.
    """
    return _json(await _call("set_knobs", node=node, knobs=knobs, edits=edits))


@_tool(read_only=True)
async def get_knobs(node: str, knobs: list[str] | None = None, frame: float | None = None,
                    all_knobs: bool = False) -> str:
    """Read knob values (with expression/animation state). Without `knobs`, returns
    the non-default knobs (or every knob with all_knobs=true)."""
    return _json(await _call("get_knobs", node=node, knobs=knobs, frame=frame, all_knobs=all_knobs))


@_tool()
async def set_expression(node: str, knob: str, expression: str, index: int = -1) -> str:
    """Drive a knob with a Nuke expression, e.g. "sin(frame/5)*10",
    "Transform1.translate.x", "[value root.format.width]/2". index -1 = all channels."""
    return _json(await _call("set_expression", node=node, knob=knob, expression=expression, index=index))


@_tool()
async def set_keyframes(
    node: str,
    knob: str,
    keys: list[dict[str, Any]],
    index: int | None = None,
    interpolation: Literal["smooth", "linear", "constant", "cubic", "catmull_rom", "horizontal", "break"] | None = None,
    replace: bool = False,
) -> str:
    """Keyframe a knob. keys: [{"frame": 1, "value": 0}, {"frame": 48, "value": 100}].
    For multi-value knobs give a list per key ({"frame": 1, "value": [0, 0]}) or a
    single number plus `index` (channel). replace=true clears existing animation first."""
    return _json(await _call("set_keyframes", node=node, knob=knob, keys=keys, index=index,
                             interpolation=interpolation, replace=replace))


@_tool(read_only=True)
async def get_animation(node: str, knob: str) -> str:
    """Keyframes ([frame, value] per channel) and expressions of a knob."""
    return _json(await _call("get_animation", node=node, knob=knob))


@_tool()
async def clear_animation(node: str, knob: str, index: int = -1) -> str:
    """Remove keyframes and expressions from a knob (index -1 = all channels)."""
    return _json(await _call("clear_animation", node=node, knob=knob, index=index))


@_tool()
async def add_knob(
    node: str,
    knob_type: Literal["float", "int", "bool", "string", "file", "rgb", "rgba", "xy", "xyz", "uv", "wh",
                       "multiline", "channel", "channels", "format", "box", "enum", "tab", "text",
                       "divider", "button", "link"],
    name: str,
    label: str | None = None,
    value: Any = None,
    tooltip: str | None = None,
    tab: str | None = None,
    options: list[str] | None = None,
    min: float | None = None,
    max: float | None = None,
    link_target: str | None = None,
    command: str | None = None,
    new_line: bool = True,
) -> str:
    """Add a user knob - for building tools/gizmos out of Groups or NoOps.

    enum needs `options`; link needs link_target="InnerNode.knob" (exposes an inner
    knob on a Group); button runs the Python in `command`. `tab` is created if missing.
    Combine with set_expression to wire user knobs to inner nodes.
    """
    return _json(await _call("add_knob", node=node, knob_type=knob_type, name=name, label=label, value=value,
                             tooltip=tooltip, tab=tab, options=options, min=min, max=max,
                             link_target=link_target, command=command, new_line=new_line))


@_tool(destructive=True)
async def remove_knob(node: str, name: str) -> str:
    """Remove a user knob from a node."""
    return _json(await _call("remove_knob", node=node, name=name))


@_tool(read_only=True)
async def describe_node(node: str | None = None, node_class: str | None = None, filter: str | None = None,
                        include_hidden: bool = False) -> str:
    """Knob reference for a node class or existing node: names, types, current/default
    values, enum options and tooltips. `filter` matches knob names/labels
    (e.g. filter="size"). Use it before setting knobs you are unsure about."""
    return _json(await _call("describe_node", node=node, node_class=node_class, filter=filter,
                             include_hidden=include_hidden))


# ===========================================================================
# Graph
# ===========================================================================

@_tool()
async def connect_nodes(connections: list[dict[str, Any]]) -> str:
    """Connect nodes: [{"source": "Grade1", "target": "Merge1", "input": 1}].
    "source": null disconnects that input. Merge2 inputs: 0=B, 1=A, 2=mask."""
    return _json(await _call("connect_nodes", connections=connections))


@_tool()
async def disconnect_inputs(node: str, inputs: list[int] | None = None) -> str:
    """Disconnect the given input indices of a node (all inputs when omitted)."""
    return _json(await _call("disconnect_inputs", node=node, inputs=inputs))


@_tool()
async def insert_node(after: str, node_class: str, name: str | None = None,
                      knobs: dict[str, Any] | None = None) -> str:
    """Insert a new node right after `after`, re-routing everything that was connected
    to `after`'s output through the new node."""
    return _json(await _call("insert_node", after=after, node_class=node_class, name=name, knobs=knobs))


@_tool(read_only=True)
async def get_node_tree(node: str, direction: Literal["upstream", "downstream"] = "upstream",
                        depth: int = 50, positions: bool = False) -> str:
    """Every node feeding into (upstream) or fed by (downstream) a node."""
    return _json(await _call("get_node_tree", node=node, direction=direction, depth=depth, positions=positions))


@_tool(read_only=True)
async def find_errors(parent: str | None = None) -> str:
    """Nodes currently in an error state (missing files, broken expressions, etc.)."""
    return _json(await _call("find_errors", parent=parent))


@_tool()
async def layout_nodes(root: str | None = None, nodes: list[str] | None = None,
                       method: Literal["tree", "autoplace"] = "tree",
                       spacing_x: int = 130, spacing_y: int = 90, parent: str | None = None) -> str:
    """Tidy the Node Graph. "tree" arranges everything upstream of `root` (e.g. the Write)
    with input 0 straight above; "autoplace" uses Nuke's autoplace on `nodes`."""
    return _json(await _call("layout_nodes", root=root, nodes=nodes, method=method, spacing_x=spacing_x,
                             spacing_y=spacing_y, parent=parent))


@_tool()
async def create_backdrop(nodes: list[str], label: str = "", color: str | list[float] | None = None,
                          font_size: int = 42, padding: int = 60) -> str:
    """Draw a Backdrop around nodes to organise the script. color: "#4a6b8a" or [r, g, b]."""
    return _json(await _call("create_backdrop", nodes=nodes, label=label, color=color, font_size=font_size,
                             padding=padding))


@_tool()
async def create_group(
    name: str | None = None,
    nodes: list[dict[str, Any]] | None = None,
    inputs: list[str | None] | None = None,
    knobs: dict[str, Any] | None = None,
    parent: str | None = None,
) -> str:
    """Create a Group and build its internals in one step.

    `nodes` uses the create_nodes spec format and is built inside the group: include
    {"node_class": "Input", "id": "in"} nodes for each pipe and an
    {"node_class": "Output", "inputs": [...]} node. `inputs` then connects the group's
    inputs to outside nodes. Add user knobs afterwards with add_knob (type "link" to
    expose inner knobs).
    """
    return _json(await _call("create_group", name=name, nodes=nodes, inputs=inputs, knobs=knobs, parent=parent))


@_tool()
async def collapse_to_group(nodes: list[str], name: str | None = None) -> str:
    """Collapse existing nodes into a Group (external connections become Input/Output)."""
    return _json(await _call("collapse_to_group", nodes=nodes, name=name))


@_tool()
async def expand_group(node: str) -> str:
    """Explode a Group's contents back into its parent graph."""
    return _json(await _call("expand_group", node=node))


@_tool()
async def paste_nk(nk: str, parent: str | None = None) -> str:
    """Paste Nuke script text (the .nk format Nuke copies to the clipboard) into the
    script or a group. Good for complex setups (Roto shapes, Trackers, gizmo internals)."""
    return _json(await _call("paste_nk", nk=nk, parent=parent))


@_tool(read_only=True)
async def export_nk(nodes: list[str] | None = None, include_upstream: bool = False,
                    parent: str | None = None) -> str:
    """Return nodes as .nk text (whole group when `nodes` is omitted). Useful to inspect
    exact settings, back up a setup, or copy it elsewhere with paste_nk."""
    return _json(await _call("export_nk", nodes=nodes, include_upstream=include_upstream, parent=parent))


# ===========================================================================
# Rendering & images
# ===========================================================================

@_tool(read_only=True)
async def render_preview(node: str, frame: int | None = None, max_size: int = 1024,
                         layer: str = "rgba", image_format: Literal["jpeg", "png"] = "jpeg") -> list[Any]:
    """Render a frame of any node and look at it. Returns the image (downscaled to
    max_size) plus info. `layer` shows another layer, e.g. "depth" or "motion".
    Rendering uses the Write node's default output transform (sRGB/display)."""
    result = await _call("render_preview", timeout=600, node=node, frame=frame, max_size=max_size,
                         layer=layer, image_format=image_format)
    return _image_result(result)


@_tool()
async def render(nodes: list[str] | None = None, first: int | None = None, last: int | None = None,
                 step: int = 1, views: list[str] | None = None, continue_on_error: bool = False,
                 timeout_seconds: int = 3600) -> str:
    """Render Write nodes (all enabled Writes when `nodes` is omitted) over a frame
    range (project range by default). Blocks Nuke until done; output folders are created."""
    return _json(await _call("render", timeout=timeout_seconds, nodes=nodes, first=first, last=last, step=step,
                             views=views, continue_on_error=continue_on_error))


@_tool(read_only=True)
async def sample_pixel(node: str, x: float, y: float, frame: int | None = None, layer: str = "rgba",
                       area: list[float] | None = None) -> str:
    """Exact (linear, unclamped) pixel values of a node at x, y (origin bottom-left).
    area=[w, h] averages a region. Good for checking blacks, colour matching, QC."""
    return _json(await _call("sample_pixel", node=node, x=x, y=y, frame=frame, layer=layer, area=area))


@_tool(read_only=True)
async def get_image_info(node: str, frame: int | None = None, metadata: bool = True,
                         metadata_filter: str | None = None) -> str:
    """Output format, bbox, layers/channels, frame range, error state and metadata of a node."""
    return _json(await _call("get_image_info", node=node, frame=frame, metadata=metadata,
                             metadata_filter=metadata_filter))


@_tool(read_only=True)
async def list_sequences(directory: str) -> str:
    """List a folder as Nuke sees it, with image sequences collapsed
    ("plate.####.exr 1001-1100")."""
    return _json(await _call("list_sequences", directory=directory))


@_tool()
async def import_footage(path: str, name: str | None = None, knobs: dict[str, Any] | None = None,
                         parent: str | None = None, x: int | None = None, y: int | None = None) -> str:
    """Create Read node(s) with the frame range detected. `path` may be one frame of a
    sequence, a pattern (plate.####.exr / plate.%04d.exr), a movie or a folder (one Read
    per sequence). knobs, e.g. {"colorspace": "ACES - ACEScg"}."""
    return _json(await _call("import_footage", timeout=300, path=path, name=name, knobs=knobs, parent=parent,
                             x=x, y=y))


# ===========================================================================
# Code
# ===========================================================================

@_tool(destructive=True)
async def execute_python(code: str, timeout_seconds: int = 300) -> str:
    """Run Python inside Nuke (main thread, `nuke` and `nukescripts` imported, globals
    persist). A lone expression returns its value; otherwise set `result = ...`.
    Prints are captured. Use for anything the dedicated tools don't cover."""
    return _json(await _call("execute_python", timeout=timeout_seconds, code=code))


@_tool(destructive=True)
async def execute_tcl(code: str) -> str:
    """Run a TCL command in Nuke, e.g. "value root.first_frame" or "knob Blur1.size 5"."""
    return _json(await _call("execute_tcl", code=code))


# ===========================================================================
# Discovery & UI
# ===========================================================================

@_tool(read_only=True)
async def list_node_classes(filter: str | None = None, refresh: bool = False) -> str:
    """Node classes you can create, with their Nodes-menu path (GUI sessions include
    gizmos and studio tools). filter matches class or menu, e.g. "key", "Deep/"."""
    return _json(await _call("list_node_classes", filter=filter, refresh=refresh))


@_tool()
async def view_node(node: str, input_index: int = 0, frame: int | None = None) -> str:
    """Show a node in the artist's Viewer (GUI only), optionally at a frame."""
    return _json(await _call("view_node", node=node, input_index=input_index, frame=frame))


@_tool()
async def zoom_to_nodes(nodes: list[str] | None = None, parent: str | None = None) -> str:
    """Frame nodes in the Node Graph so the artist can see them (GUI only)."""
    return _json(await _call("zoom_to_nodes", nodes=nodes, parent=parent))


@_tool(read_only=True)
async def capture_screen(target: Literal["main", "dag", "viewer"] = "main", max_size: int = 1600,
                         bring_to_front: bool = False) -> list[Any]:
    """Screenshot the Nuke window, the Node Graph ("dag") or the Viewer (GUI only).

    Only works while Nuke is the foreground window (so other apps are never captured);
    bring_to_front=true asks Nuke to raise itself first. To inspect pixels of a node,
    prefer render_preview."""
    return _image_result(await _call("capture_screen", target=target, max_size=max_size,
                                     bring_to_front=bring_to_front))


# ===========================================================================

def main() -> None:
    atexit.register(launcher.cleanup)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
