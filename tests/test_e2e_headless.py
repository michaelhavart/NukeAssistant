"""End-to-end test: MCP tools -> socket -> bridge -> real Nuke in terminal mode.

Needs a Nuke install + licence. Runs only when NUKE_MCP_E2E=1:

    $env:NUKE_MCP_E2E=1; uv run pytest -s tests/test_e2e_headless.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile

import pytest

from nuke_mcp.server import mcp

pytestmark = pytest.mark.skipif(os.environ.get("NUKE_MCP_E2E") != "1", reason="set NUKE_MCP_E2E=1 to run")


async def call(tool_name, **args):
    result = await mcp.call_tool(tool_name, args)
    texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    images = [c for c in result.content if getattr(c, "type", "") == "image"]
    if result.is_error:
        raise AssertionError(f"{tool_name} failed: {texts}")
    data = json.loads(texts[-1]) if texts else None
    return (data, images) if images else data


async def expect_error(tool_name, fragment, **args):
    try:
        result = await mcp.call_tool(tool_name, args)
    except Exception as exc:  # ToolError raised directly by the SDK
        text = str(exc)
    else:
        assert result.is_error, f"{tool_name} should have failed"
        text = " ".join(c.text for c in result.content if getattr(c, "type", "") == "text")
    assert fragment.lower() in text.lower(), text
    return text


async def scenario(tmp):
    out = tmp.replace("\\", "/")
    launched = await call("launch_nuke", mode="headless", wait_seconds=240)
    print("launched", launched["session"])
    try:
        info = await call("get_session_info")
        assert info["gui"] is False

        await call("set_project_settings", first_frame=1, last_frame=10, format="1280x720", fps=25)
        info = await call("get_session_info")
        assert info["format"]["width"] == 1280 and info["fps"] == 25

        built = await call("create_nodes", nodes=[
            {"node_class": "CheckerBoard2", "id": "bg"},
            {"node_class": "Constant", "id": "fg", "knobs": {"color": [1, 0.2, 0.1, 1]}},
            {"node_class": "Transform", "id": "move", "inputs": ["fg"], "knobs": {"scale": 0.5}},
            {"node_class": "Merge2", "id": "comp", "name": "Comp", "inputs": ["bg", "move"],
             "knobs": {"operation": "over", "mix": 0.8}},
            {"node_class": "Blur", "id": "blur", "inputs": ["comp"], "knobs": {"size": 4}},
            {"node_class": "Write", "id": "out", "inputs": ["blur"],
             "knobs": {"file": f"{out}/render/comp.####.png", "file_type": "png"}},
        ])
        ids = built["ids"]
        assert ids["comp"] == "Comp"
        print("created", ids)

        res = await call("set_knobs", edits={
            ids["blur"]: {"size": {"expression": "frame*2"}, "tile_color": "#ff8800"},
            ids["move"]: {"translate": [100, 50], "bogus_knob": 1},
        })
        assert "errors" in res and "bogus_knob" in json.dumps(res["errors"])

        knobs = await call("get_knobs", node=ids["blur"], knobs=["size"], frame=5)
        assert knobs["size"]["value"] == [10.0, 10.0], knobs  # WH_Knob: [w, h]
        assert info["script"] is None or "--port" not in info["script"]

        schema = await call("describe_node", node_class="Merge2", filter="operation")
        ops = schema["knobs"][0]["options"]
        assert "screen" in ops

        anim = await call("set_keyframes", node=ids["move"], knob="rotate",
                          keys=[{"frame": 1, "value": 0}, {"frame": 10, "value": 90}], interpolation="linear")
        assert anim["animation"]["0"]["keys"] == [[1.0, 0.0], [10.0, 90.0]]
        anim = await call("set_keyframes", node=ids["move"], knob="translate",
                          keys=[{"frame": 1, "value": [0, 0]}, {"frame": 10, "value": [200, 100]}])
        assert len(anim["animation"]) == 2
        got = await call("get_knobs", node=ids["move"], knobs=["translate"], frame=10)
        assert got["translate"]["value"] == [200.0, 100.0], got

        ins = await call("insert_node", after=ids["comp"], node_class="Grade", knobs={"white": 1.2})
        grade = ins["inserted"]["name"]
        blur_info = await call("get_node_info", node=ids["blur"])
        assert blur_info["inputs"] == [grade], blur_info

        await call("layout_nodes", root=ids["out"])
        bd = await call("create_backdrop", nodes=[ids["bg"], ids["fg"]], label="Sources", color=[0.3, 0.4, 0.5])
        assert bd["class"] == "BackdropNode"

        img, images = await call("render_preview", node=ids["blur"], frame=5, max_size=256)
        assert images and images[0].mime_type == "image/jpeg" and img["width"] == 256
        await expect_error("render_preview", "Layer 'depth' not found", node=ids["blur"], layer="depth")

        px = await call("sample_pixel", node=ids["fg"], x=10, y=10)
        assert abs(px["values"]["rgba.green"] - 0.2) < 1e-4, px

        img_info = await call("get_image_info", node=ids["blur"])
        assert img_info["format"]["width"] == 1280 and "rgba" in img_info["layers"]

        rendered = await call("render", nodes=[ids["out"]], first=1, last=3)
        files = sorted(os.listdir(os.path.join(tmp, "render")))
        assert files == ["comp.0001.png", "comp.0002.png", "comp.0003.png"], files
        print("render", rendered)

        seqs = await call("list_sequences", directory=f"{out}/render")
        assert any("comp.####.png" in f or "comp.%04d.png" in f for f in seqs["files"]), seqs
        reads = await call("import_footage", path=f"{out}/render/comp.0002.png", name="Rendered")
        assert reads["reads"][0]["frame_range"] == [1, 3], reads

        nk = await call("export_nk", nodes=[ids["blur"]])
        assert "Blur {" in nk["nk"]
        pasted = await call("paste_nk", nk=nk["nk"])
        assert pasted["pasted"][0]["class"] == "Blur"

        dup = await call("duplicate_nodes", nodes=[grade, ids["blur"]])
        assert len(dup["mapping"]) == 2

        grp = await call("create_group", name="SoftGlow", inputs=[ids["comp"]], nodes=[
            {"node_class": "Input", "id": "in"},
            {"node_class": "Blur", "id": "glow_blur", "name": "GlowBlur", "inputs": ["in"], "knobs": {"size": 20}},
            {"node_class": "Merge2", "id": "plus", "inputs": ["in", "glow_blur"], "knobs": {"operation": "plus"}},
            {"node_class": "Output", "inputs": ["plus"]},
        ])
        gname = grp["group"]["name"]
        assert grp["group"]["inputs"] == [ids["comp"]], grp
        link = await call("add_knob", node=gname, knob_type="link", name="glow_size", label="Glow Size",
                          link_target="GlowBlur.size", tab="Controls")
        assert link["class"] == "Link_Knob"
        await call("add_knob", node=gname, knob_type="float", name="strength", value=0.5, min=0, max=2)
        await call("set_expression", node=f"{gname}.Merge1", knob="mix", expression="parent.strength")
        inner = await call("list_nodes", parent=gname)
        assert inner["count"] == 4, inner
        preview, _ = await call("render_preview", node=gname, max_size=128)
        assert preview["width"] == 128

        coll = await call("collapse_to_group", nodes=[ids["fg"], ids["move"]], name="FgChain")
        comp_info = await call("get_node_info", node="Comp")
        assert comp_info["inputs"][1] == coll["group"]["name"], comp_info
        assert any(c["class"] == "Output" for c in coll["children"]), coll
        assert coll["group"]["class"] == "Group"
        exp = await call("expand_group", node=coll["group"]["name"])
        assert sorted(n["class"] for n in exp["nodes"]) == ["Constant", "Transform"], exp
        comp_info = await call("get_node_info", node="Comp")
        moved = [n["name"] for n in exp["nodes"] if n["class"] == "Transform"][0]
        assert comp_info["inputs"][1] == moved, comp_info

        await call("create_node", node_class="Read", name="Missing", knobs={"file": f"{out}/nope.####.exr"})
        errs = await call("find_errors")
        assert any(n["name"] == "Missing" for n in errs["nodes"]), errs

        py = await call("execute_python", code="len(nuke.allNodes())")
        assert py["result"] > 5
        py = await call("execute_python", code="print('hi')\nresult = [n.name() for n in nuke.allNodes('Write')]")
        assert py["output"].strip() == "hi" and py["result"]
        await expect_error("execute_python", "ZeroDivisionError", code="1/0")
        tcl = await call("execute_tcl", code="value root.first_frame")
        assert tcl["result"] == "1"

        classes = await call("list_node_classes", filter="blur")
        assert any(c["class"] == "Blur" for c in classes["classes"])

        await expect_error("create_node", "list_node_classes", node_class="NotARealNode")
        await expect_error("get_node_info", "not found", node="Nope42")
        await expect_error("view_node", "interactive", node=ids["blur"])

        listing = await call("list_nodes", node_class=["Write", "Read"])
        assert listing["count"] >= 3

        script = f"{out}/scripts/e2e.nk"
        await call("save_script", path=script)
        assert os.path.isfile(script)
        await call("new_script", force=True)
        assert (await call("list_nodes"))["count"] == 0
        await call("open_script", path=script)
        assert (await call("list_nodes", pattern="Comp"))["count"] == 1
        await call("delete_nodes", nodes=["Comp"])
        assert (await call("list_nodes", pattern="Comp"))["count"] == 0
    finally:
        try:
            await call("shutdown_headless_nuke")
        except Exception as exc:
            print("shutdown failed:", exc)


def test_headless_end_to_end():
    tmp = tempfile.mkdtemp(prefix="nukeassistant_e2e_")
    try:
        asyncio.run(scenario(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
