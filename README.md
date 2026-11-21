# NukeAssistant — MCP server for Foundry Nuke

Lets an AI assistant (Claude Code, Claude Desktop or any MCP client) drive Nuke:
build and edit node graphs, set knobs, keyframes and expressions, build Groups/gizmos,
render previews it can look at, render Write nodes, and run Python/TCL.

```
MCP client ──stdio──▶ nuke-mcp (src/nuke_mcp, Python ≥3.10, mcp SDK 2.x)
                          │  newline-delimited JSON over TCP 127.0.0.1:54321
                          ▼
              Nuke ─ nuke_mcp_bridge (nuke_plugin/, stdlib only)
                     runs every command on Nuke's main thread
```

Tested with Nuke 15.1v3 (Python 3.10, PySide2) in GUI and terminal (`-t`) mode.
The bridge also targets PySide6 for Nuke 16.

## Setup

1. **Python side** (optional): install [uv](https://docs.astral.sh/uv/). The client
   starts the server with `uv run`, which creates `.venv` and installs the locked
   dependencies on first start. Running `uv sync` once beforehand avoids a slow first
   start that some clients may time out on, and shows install errors in your terminal.
2. **Nuke side**, pick one:
   - `python install_nuke_plugin.py` — adds one `nuke.pluginAddPath(...)` line to
    `~/.nuke/init.py` (`--uninstall` removes it), or
   - add `C:/git/nuke/NukeAssistant/nuke_plugin` to `NUKE_PATH`.

    Interactive Nuke then starts the bridge automatically and shows a **NukeAssistant**
    menu (Start / Stop / Status). Set `NUKE_MCP_AUTOSTART=0` to start it by hand.
3. **Client**:
   - Claude Code: `.mcp.json` in this folder already registers the `nuke` server.
   - Claude Desktop / others:
     ```json
     { "mcpServers": { "nuke": { "command": "uv",
         "args": ["run", "--directory", "C:/git/nuke/NukeAssistant", "nuke-mcp"] } } }
     ```

Without step 2 the assistant can still call `launch_nuke`, which starts Nuke (GUI or
headless) with the plugin injected through `NUKE_PATH`.

## Tools (57)

| Area | Tools |
|---|---|
| Session | `get_session_info`, `list_nuke_sessions`, `connect_to_nuke`, `launch_nuke`, `shutdown_headless_nuke`, `undo`, `redo` |
| Script | `new_script`, `open_script`, `import_script`, `save_script`, `set_project_settings`, `set_current_frame`, `list_formats` |
| Nodes | `list_nodes`, `get_node_info`, `create_node`, `create_nodes` (whole trees, one undo step), `delete_nodes`, `rename_node`, `duplicate_nodes`, `set_node_positions`, `select_nodes`, `get_selected_nodes` |
| Knobs | `set_knobs` (values, expressions, keys; single node or batch), `get_knobs`, `set_expression`, `set_keyframes`, `get_animation`, `clear_animation`, `add_knob`, `remove_knob`, `describe_node` (knob schema incl. enum options) |
| Graph | `connect_nodes`, `disconnect_inputs`, `insert_node`, `get_node_tree`, `find_errors`, `layout_nodes`, `create_backdrop`, `create_group`, `collapse_to_group`, `expand_group`, `paste_nk`, `export_nk` |
| Images | `render_preview` (returns the image), `render`, `sample_pixel`, `get_image_info`, `list_sequences`, `import_footage` |
| Code | `execute_python`, `execute_tcl` |
| Discovery / UI | `list_node_classes`, `view_node`, `zoom_to_nodes`, `capture_screen` |

Design notes:

- Every state-changing call is one named undo step (`NukeAssistant: <command>`), so an artist can Ctrl+Z it.
- Knob dumps show only non-default knobs unless asked, to keep responses small.
- `render_preview` uses temporary Reformat/Write nodes that are kept out of undo and the
modified flag.
- `capture_screen` reads screen pixels (Nuke's Viewer/DAG are OpenGL), so it refuses
unless Nuke is the foreground window — it can never capture other applications.
- Tools don't pull Nuke to the front. When Nuke is in the background, render progress
dialogs stay off screen and other windows open without taking focus. If Nuke takes the
foreground anyway, the bridge gives it straight back, unless you just clicked or typed,
in which case it assumes you switched to Nuke yourself. When you're working in Nuke,
nothing changes.
- `collapse_to_group` uses its own implementation everywhere: Nuke's `collapseToGroup`
is GUI-only and can open a modal "Group Output" question. `expand_group` has a
terminal-mode implementation.
- `launch_nuke` starts Nuke with a hidden console, so Frame Server workers don't open console windows.

## Configuration

| Variable | Where | Default | Purpose |
|---|---|---|---|
| `NUKE_MCP_PORT` | both | `54321` | Base port; Nuke uses the next free one up to +9 when several sessions run |
| `NUKE_MCP_HOST` | both | `127.0.0.1` | Bind/connect address |
| `NUKE_MCP_TOKEN` | both | – | Shared secret required on every request when set |
| `NUKE_MCP_AUTOSTART` | Nuke | `1` | Start the bridge when the GUI starts |
| `NUKE_EXE` | server | auto-detected | Nuke executable for `launch_nuke` |
| `NUKE_MCP_PLUGIN_DIR` | server | `./nuke_plugin` | Plugin folder injected by `launch_nuke` |

**Security:** the bridge executes arbitrary Python inside Nuke for anything that can
reach its port. It binds to localhost only; set `NUKE_MCP_TOKEN` on shared machines.

## Headless use

```
Nuke15.1.exe -t nuke_plugin/start_headless.py [script.nk]
```

Serves until `shutdown_headless_nuke` (or Ctrl+C). Needs a render or interactive licence.

## Tests

```
uv run pytest                                            # offline: stdio MCP + protocol
$env:NUKE_MCP_E2E=1;     uv run pytest tests/test_e2e_headless.py   # real Nuke, terminal mode
$env:NUKE_MCP_E2E_GUI=1; uv run pytest tests/test_e2e_gui.py        # opens a Nuke window
```

## Layout

```
nuke_plugin/                 # goes on NUKE_PATH
  init.py, menu.py           # menu + autostart
  start_headless.py          # nuke -t entry point
  nuke_mcp_bridge/
    server.py                # socket server + main-thread executors
    registry.py, utils.py    # command registry, knob/node helpers
    commands/                # session, script, nodes, knobs, graph, render, code, catalog, ui
src/nuke_mcp/                # MCP server: server.py (tools), connection.py, launcher.py
tests/
```
