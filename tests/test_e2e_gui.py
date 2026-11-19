"""GUI smoke test: launches interactive Nuke (a window opens), exercises GUI-only tools,
then kills that Nuke. Runs only when NUKE_MCP_E2E_GUI=1:

    $env:NUKE_MCP_E2E_GUI=1; uv run pytest -s tests/test_e2e_gui.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

from test_e2e_headless import call

pytestmark = pytest.mark.skipif(os.environ.get("NUKE_MCP_E2E_GUI") != "1", reason="set NUKE_MCP_E2E_GUI=1 to run")


async def scenario():
    launched = await call("launch_nuke", mode="gui", wait_seconds=300)
    pid = launched["pid"]
    try:
        info = await call("get_session_info")
        assert info["gui"] is True

        built = await call("create_nodes", nodes=[
            {"node_class": "CheckerBoard2", "id": "src"},
            {"node_class": "Grade", "id": "grade", "inputs": ["src"], "knobs": {"white": [1.2, 1, 0.8, 1]}},
            {"node_class": "Blur", "id": "blur", "inputs": ["grade"], "knobs": {"size": 8}},
        ])
        ids = built["ids"]

        # Undo works in GUI sessions: create_nodes is a single undo step.
        await call("create_node", node_class="NoOp", name="UndoMe")
        assert (await call("list_nodes", pattern="UndoMe"))["count"] == 1
        await call("undo")
        assert (await call("list_nodes", pattern="UndoMe"))["count"] == 0

        classes = await call("list_node_classes", filter="Blur")
        assert classes["source"] == "menus" and any(c["class"] == "Blur" for c in classes["classes"]), classes

        await call("view_node", node=ids["blur"])
        await call("zoom_to_nodes", nodes=list(ids.values()))
        await asyncio.sleep(2)  # let the UI paint
        # Screenshots are refused unless Nuke is the foreground window (Windows may deny
        # focus to a process started in the background), so either outcome is valid.
        for target in ("main", "dag", "viewer"):
            try:
                meta, images = await call("capture_screen", target=target, max_size=800, bring_to_front=True)
            except Exception as exc:
                assert "not the foreground window" in str(exc), exc
                print("capture refused (Nuke not in front):", target)
            else:
                assert images and meta["width"] > 0, (target, meta)
                print("captured", target, meta["width"], "x", meta["height"])

        coll = await call("collapse_to_group", nodes=[ids["grade"], ids["blur"]], name="Look")
        assert coll["group"]["class"] == "Group"
        await call("expand_group", node=coll["group"]["name"])

        preview, images = await call("render_preview", node=ids["src"], max_size=256)
        assert images and preview["width"] == 256

        sel = await call("select_nodes", nodes=[ids["src"]])
        assert sel["selected"] == [ids["src"]]
        assert (await call("get_selected_nodes"))["selected"][0]["name"] == ids["src"]
    finally:
        # Nuke has unsaved changes and the bridge refuses to quit GUI sessions: kill our own instance.
        # /T also ends the Frame Server workers Nuke spawns.
        os.system(f"taskkill /PID {pid} /T /F >NUL 2>&1" if os.name == "nt" else f"pkill -9 -P {pid}; kill -9 {pid}")


def test_gui_end_to_end():
    asyncio.run(scenario())
