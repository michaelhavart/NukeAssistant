"""Discover which node classes this Nuke can create."""

import os
import re

import nuke

from ..registry import command

# Matches nuke.createNode("Blur"), nukescripts.createNodeLocal("Blur") and the
# backslash-escaped forms found inside toolbars.py command strings.
_CREATE_RE = re.compile(r"""createNode(?:Local)?\(\s*\\?["']([^"'\\]+)""")
_MENU_RE = re.compile(r"""(\w+)\s*=\s*\w+\.addMenu\(\s*["']([^"']+)["']""")
_COMMAND_RE = re.compile(r"""(\w+)\.addCommand\(\s*["']([^"']+)["']\s*,\s*["'](.*?)["']\s*[,)]""")
_cache = {}


def _walk_menu(menu, path, out):
    for item in menu.items():
        name = item.name()
        if isinstance(item, nuke.Menu):
            _walk_menu(item, path + [name], out)
            continue
        try:
            script = item.script()
        except Exception:
            script = ""
        match = _CREATE_RE.search(script or "")
        if match:
            cls = match.group(1)
            if cls.endswith(".gizmo"):
                cls = cls[:-6]
            out.setdefault(cls, "/".join(path + [name]))


def _from_menus():
    out = {}
    try:
        nodes_menu = nuke.menu("Nodes")
    except RuntimeError:
        return out
    if nodes_menu is not None:
        _walk_menu(nodes_menu, [], out)
    return out


def _from_toolbar_source():
    """Parse Nuke's own toolbars.py (menus are unavailable in terminal mode)."""
    out = {}
    try:
        import nukescripts

        path = os.path.join(os.path.dirname(nukescripts.__file__), "toolbars.py")
        with open(path) as fh:
            source = fh.read()
    except Exception:
        return out
    menus = {}
    for line in source.splitlines():
        m = _MENU_RE.search(line)
        if m:
            menus[m.group(1)] = m.group(2)
            continue
        m = _COMMAND_RE.search(line)
        if m:
            created = _CREATE_RE.search(m.group(3))
            if created:
                cls = created.group(1)
                if cls.endswith(".gizmo"):
                    cls = cls[:-6]
                out.setdefault(cls, "%s/%s" % (menus.get(m.group(1), "?"), m.group(2)))
    return out


def _from_plugins():
    out = _from_toolbar_source()
    exts = ("*.gizmo", "*.dll", "*.so", "*.dylib")
    for pattern in exts:
        try:
            files = nuke.plugins(nuke.ALL | nuke.NODIR, pattern)
        except Exception:
            files = []
        for f in files:
            cls = os.path.splitext(f)[0]
            if cls.startswith("lib"):
                continue
            out.setdefault(cls, "(plugin)" if not f.endswith(".gizmo") else "(gizmo)")
    return out


@command()
def list_node_classes(filter=None, refresh=False, limit=2000):
    """Node classes available in this session, with their menu path when known.

    In GUI sessions the Nodes toolbar menus are scanned (includes gizmos and custom
    tools); in terminal sessions the plugin path is scanned.
    """
    if refresh or "classes" not in _cache:
        classes = _from_menus()
        source = "menus"
        if not classes:
            classes = _from_plugins()
            source = "toolbars.py + plugin path"
        _cache["classes"] = classes
        _cache["source"] = source
    classes = _cache["classes"]
    items = sorted(classes.items(), key=lambda kv: kv[0].lower())
    if filter:
        f = filter.lower()
        items = [(c, m) for c, m in items if f in c.lower() or f in m.lower()]
    return {
        "source": _cache["source"],
        "count": len(items),
        "classes": [{"class": c, "menu": m} for c, m in items[: int(limit)]],
    }
