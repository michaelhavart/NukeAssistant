"""Add (or remove) the NukeAssistant plugin path in ~/.nuke/init.py.

    python install_nuke_plugin.py            # install
    python install_nuke_plugin.py --uninstall

Any Python works, including Nuke's own: "C:/Program Files/Nuke15.1v3/python.exe".

Alternative without touching ~/.nuke: add nuke_plugin/ to the NUKE_PATH environment variable.
"""

import os
import sys

MARKER = "# NukeAssistant MCP bridge"
PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nuke_plugin").replace("\\", "/")
LINE = 'nuke.pluginAddPath(r"%s")  %s\n' % (PLUGIN_DIR, MARKER)


def main():
    init_py = os.path.join(os.path.expanduser("~"), ".nuke", "init.py")
    os.makedirs(os.path.dirname(init_py), exist_ok=True)
    text = ""
    if os.path.exists(init_py):
        with open(init_py) as fh:
            text = fh.read()
    lines = [l for l in text.splitlines(True) if MARKER not in l]
    if "--uninstall" in sys.argv:
        with open(init_py, "w") as fh:
            fh.write("".join(lines))
        print("Removed NukeAssistant from %s" % init_py)
        return
    body = "".join(lines)
    if body and not body.endswith("\n"):
        body += "\n"
    if "import nuke" not in body:
        body = "import nuke\n" + body
    with open(init_py, "w") as fh:
        fh.write(body + LINE)
    print("Added NukeAssistant to %s\n  %s" % (init_py, LINE.strip()))


if __name__ == "__main__":
    main()
