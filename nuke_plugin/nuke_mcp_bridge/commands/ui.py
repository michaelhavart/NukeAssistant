"""Interactive-session helpers: viewer, DAG zoom, screenshots."""

import base64
import time

import nuke

from ..focus import allow_foreground, nuke_is_foreground, qt
from ..registry import command
from ..utils import BridgeError, find_node, find_nodes, node_ref, parent_group, deselect_all


def _require_gui():
    if not nuke.GUI:
        raise BridgeError("This needs an interactive Nuke session (not nuke -t).")


_qt = qt
_nuke_is_foreground = nuke_is_foreground


@command()
def view_node(node, input_index=0, frame=None, parent=None):
    """Connect a node to the active Viewer (number key ``input_index`` + 1)."""
    _require_gui()
    n = find_node(node, parent)
    nuke.connectViewer(int(input_index), n)
    if frame is not None:
        nuke.frame(int(frame))
    viewer = nuke.activeViewer()
    return {"viewing": node_ref(n), "viewer": node_ref(viewer.node()) if viewer else None,
            "frame": nuke.frame()}


@command()
def zoom_to_nodes(nodes=None, parent=None):
    """Frame nodes in the Node Graph (all nodes when omitted); also selects them."""
    _require_gui()
    targets = find_nodes(nodes, parent) if nodes else None
    group = parent_group(targets[0]) if targets else nuke.root()
    with group:
        deselect_all(group)
        for n in targets or group.nodes():
            n.setSelected(True)
        nuke.zoomToFitSelected()
    return {"framed": [node_ref(n) for n in (targets or group.nodes())]}


def _find_widget(target):
    QtCore, QtGui, QtWidgets = _qt()
    app = QtWidgets.QApplication.instance()
    main = None
    for w in app.topLevelWidgets():
        if w.inherits("QMainWindow") and w.isVisible():
            main = w
            break
    if target == "main":
        return main
    wanted = {"dag": ("DAG", "Node Graph"), "viewer": ("Viewer", "Viewer")}.get(target)
    if wanted is None:
        raise BridgeError("target must be 'main', 'dag' or 'viewer'.")
    prefix, title = wanted
    best = None
    for w in app.allWidgets():
        if not w.isVisible():
            continue
        name = w.objectName() or ""
        if name.startswith(prefix + ".") or (w.windowTitle() or "").startswith(title):
            area = w.width() * w.height()
            if best is None or area > best[0]:
                best = (area, w)
    return best[1] if best else main


@command()
def capture_screen(target="main", max_size=1600, bring_to_front=False):
    """Screenshot the Nuke window, Node Graph ("dag") or Viewer ("viewer") as PNG.

    Nuke draws the Viewer and Node Graph with OpenGL, which can only be captured from
    screen pixels. To never capture other applications, this refuses unless Nuke is
    the foreground window (``bring_to_front`` asks Nuke to raise itself first).
    """
    _require_gui()
    QtCore, QtGui, QtWidgets = _qt()
    widget = _find_widget(target)
    if widget is None:
        raise BridgeError("Could not find a visible %s widget." % target)
    app = QtWidgets.QApplication.instance()
    if not _nuke_is_foreground() and bring_to_front:
        allow_foreground()
        window = widget.window()
        if window.isMinimized():
            window.showNormal()
        window.raise_()
        window.activateWindow()
        deadline = time.time() + 1.5
        while time.time() < deadline and not _nuke_is_foreground():
            app.processEvents()
            time.sleep(0.05)
        app.processEvents()
        time.sleep(0.2)  # let the window repaint
    if not _nuke_is_foreground():
        raise BridgeError(
            "Nuke is not the foreground window, so a screenshot could include other applications. "
            "Ask the artist to bring Nuke to the front, retry with bring_to_front=true, or use "
            "render_preview to look at an image."
        )
    top_left = widget.mapToGlobal(QtCore.QPoint(0, 0))
    screen = None
    try:
        screen = app.screenAt(widget.mapToGlobal(widget.rect().center()))
    except Exception:
        pass
    screen = screen or app.primaryScreen()
    geo = screen.geometry()
    # Grabbing from the screen captures OpenGL content (viewer, DAG) that QWidget.grab misses.
    pixmap = screen.grabWindow(0, top_left.x() - geo.x(), top_left.y() - geo.y(), widget.width(), widget.height())
    if pixmap.isNull():
        pixmap = widget.grab()
    if max(pixmap.width(), pixmap.height()) > int(max_size):
        pixmap = pixmap.scaled(int(max_size), int(max_size), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.WriteOnly)
    pixmap.save(buf, "PNG")
    data = bytes(buf.data())
    return {"target": target, "width": pixmap.width(), "height": pixmap.height(), "mime_type": "image/png",
            "image_base64": base64.b64encode(data).decode("ascii")}
