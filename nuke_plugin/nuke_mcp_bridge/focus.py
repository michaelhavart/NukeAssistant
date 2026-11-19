"""Keep bridge commands from pulling Nuke to the front of the desktop.

In GUI sessions ``nuke.execute`` opens a modal "Progress" dialog, and Windows gives it
the foreground, so every render_preview/render used to bring Nuke to the front.

While a command runs and Nuke is *not* the active application:
  * progress dialogs (top-level windows containing a QProgressBar) are kept off screen
    with ``Qt.WA_DontShowOnScreen``; the render itself runs normally;
  * any other window (e.g. a ``nuke.message`` from execute_python) still appears, so
    nothing can block on an invisible prompt, but is asked to show without activating.

When the artist is working in Nuke, nothing is changed.
"""

import contextlib
import os
import sys
import threading

import nuke

_PROP = "_q_showWithoutActivating"  # read by Qt's platform plugins when showing a window
_filter = {"obj": None}


def qt():
    try:
        from PySide2 import QtCore, QtGui, QtWidgets  # Nuke 11-15
    except ImportError:
        from PySide6 import QtCore, QtGui, QtWidgets  # Nuke 16+
    return QtCore, QtGui, QtWidgets


def nuke_is_foreground():
    """True when a Nuke window is the active, front-most application window."""
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return pid.value == os.getpid()
        except Exception:
            pass
    _, _, QtWidgets = qt()
    return QtWidgets.QApplication.activeWindow() is not None


def _make_filter():
    QtCore, _, QtWidgets = qt()
    Qt = QtCore.Qt

    class _QuietWindows(QtCore.QObject):
        def __init__(self):
            QtCore.QObject.__init__(self)
            self.touched = []  # (widget, attribute)

        def eventFilter(self, obj, event):
            # The Show event is sent before Qt makes the native window visible, and both
            # WA_DontShowOnScreen and the QWindow property are read at that point.
            if event.type() != QtCore.QEvent.Show or not isinstance(obj, QtWidgets.QWidget):
                return False
            try:
                if not obj.isWindow():
                    return False
                if obj.findChildren(QtWidgets.QProgressBar):
                    if not obj.testAttribute(Qt.WA_DontShowOnScreen):
                        obj.setAttribute(Qt.WA_DontShowOnScreen, True)
                        self.touched.append((obj, Qt.WA_DontShowOnScreen))
                elif not obj.testAttribute(Qt.WA_ShowWithoutActivating):
                    obj.setAttribute(Qt.WA_ShowWithoutActivating, True)
                    handle = obj.windowHandle()
                    if handle is not None:
                        handle.setProperty(_PROP, True)
                    self.touched.append((obj, Qt.WA_ShowWithoutActivating))
            except RuntimeError:  # widget already deleted on the C++ side
                pass
            return False

        def restore(self):
            for widget, attr in self.touched:
                try:
                    if attr == Qt.WA_DontShowOnScreen and widget.isVisible():
                        widget.hide()  # still "shown" off screen; hide before restoring
                    widget.setAttribute(attr, False)
                    if attr == Qt.WA_ShowWithoutActivating:
                        handle = widget.windowHandle()
                        if handle is not None:
                            handle.setProperty(_PROP, False)
                except RuntimeError:
                    pass
            del self.touched[:]

    return _QuietWindows()


class _WinForeground(object):
    """Hand the foreground back to the previous window when Nuke takes it (Windows only).

    Nuke activates itself during renders, which Windows allows once the user has been
    idle for a while. A watcher thread notices the switch and, when the user did not
    cause it (no keyboard/mouse input just before), immediately gives the foreground
    back - allowed because Nuke owns the foreground at that moment.
    """

    POLL = 0.02
    USER_SWITCH_MS = 400  # input this recent means the user switched to Nuke themselves

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        self._lii = LASTINPUTINFO
        self.pid = os.getpid()
        self.previous = self.user32.GetForegroundWindow()
        self.enabled = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="NukeAssistantFocus")
        self._thread.daemon = True
        self._thread.start()

    def _idle_ms(self):
        info = self._lii()
        info.cbSize = self.ctypes.sizeof(info)
        if not self.user32.GetLastInputInfo(self.ctypes.byref(info)):
            return None
        return (self.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF

    def _nuke_has_foreground(self):
        pid = self.ctypes.c_ulong()
        self.user32.GetWindowThreadProcessId(self.user32.GetForegroundWindow(), self.ctypes.byref(pid))
        return pid.value == self.pid

    def _check(self):
        """Returns False once the user has deliberately moved to Nuke (stop guarding)."""
        if not self.enabled or not self._nuke_has_foreground():
            return True
        idle = self._idle_ms()
        if idle is not None and idle < self.USER_SWITCH_MS:
            _note("left Nuke in front: user input %dms before the switch" % idle)
            return False
        if self.previous and self.user32.IsWindow(self.previous):
            ok = self.user32.SetForegroundWindow(self.previous)
            _note("gave the foreground back (%s)" % ("ok" if ok else "refused"))
        return True

    def _watch(self):
        try:
            while not self._stop.wait(self.POLL):
                if not self._check():
                    self.enabled = False
                    return
        except Exception:
            pass

    def finish(self):
        self._stop.set()
        self._thread.join(1.0)
        try:
            self._check()
        except Exception:
            pass


_active = {"guard": None, "app": None}
history = []  # recent focus decisions, for troubleshooting (focus.history)


def _note(message):
    history.append(message)
    del history[:-20]


def allow_foreground():
    """Let the running command bring Nuke to the front on purpose (e.g. capture_screen)."""
    guard = _active["guard"]
    if guard is not None:
        guard.enabled = False
    app = _active["app"]
    if app is not None and _filter["obj"] is not None:
        app.removeEventFilter(_filter["obj"])


@contextlib.contextmanager
def no_focus_steal():
    """Context manager for the GUI main thread; a no-op when Nuke already has focus."""
    app = None
    if nuke.GUI:
        try:
            app = qt()[2].QApplication.instance()
        except Exception:
            app = None
    if app is None or nuke_is_foreground():
        yield
        return
    foreground = None
    if sys.platform == "win32":
        try:
            foreground = _WinForeground()
        except Exception:
            foreground = None
    if _filter["obj"] is None:
        _filter["obj"] = _make_filter()
    flt = _filter["obj"]
    app.installEventFilter(flt)
    _active.update(guard=foreground, app=app)
    try:
        yield
    finally:
        _active.update(guard=None, app=None)
        app.removeEventFilter(flt)
        flt.restore()
        if foreground is not None:
            try:
                app.processEvents()  # let pending activations happen before the last check
                foreground.finish()
            except Exception:
                pass
