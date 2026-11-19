"""Locate and start Nuke with the NukeAssistant bridge on its plugin path."""

from __future__ import annotations

import glob
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from .connection import probe

_children: list[subprocess.Popen] = []


class LaunchError(RuntimeError):
    pass


def plugin_dir() -> Path:
    """Folder containing the Nuke-side plugin (init.py, menu.py, nuke_mcp_bridge/)."""
    env = os.environ.get("NUKE_MCP_PLUGIN_DIR")
    if env:
        return Path(env)
    # src/nuke_mcp/launcher.py -> repo root / nuke_plugin
    return Path(__file__).resolve().parents[2] / "nuke_plugin"


def _version_key(path: str) -> tuple[int, ...]:
    m = re.search(r"Nuke(\d+)\.(\d+)v(\d+)", path)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def find_nuke_executable() -> str:
    env = os.environ.get("NUKE_EXE")
    if env:
        if not os.path.isfile(env):
            raise LaunchError(f"NUKE_EXE points to a missing file: {env}")
        return env
    if sys.platform == "win32":
        patterns = [r"C:\Program Files\Nuke*\Nuke*.exe"]
    elif sys.platform == "darwin":
        patterns = ["/Applications/Nuke*/Nuke*.app/Contents/MacOS/Nuke*"]
    else:
        patterns = ["/usr/local/Nuke*/Nuke*", "/opt/Nuke*/Nuke*"]
    candidates = []
    for pattern in patterns:
        for path in glob.glob(pattern):
            name = os.path.basename(path)
            if re.match(r"^Nuke\d+\.\d+(\.exe)?$", name) and os.path.isfile(path):
                candidates.append(path)
    if not candidates:
        raise LaunchError("Could not find a Nuke executable. Set the NUKE_EXE environment variable.")
    return max(candidates, key=_version_key)


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


async def free_port(host: str, base: int, scan: int) -> int:
    for port in range(base, base + scan):
        if _port_is_free(host, port) and not await probe(host, port, timeout=0.3):
            return port
    raise LaunchError(f"No free bridge port in {base}-{base + scan - 1}.")


def launch(mode: str, script: str | None, variant: str, port: int) -> tuple[subprocess.Popen, str]:
    exe = find_nuke_executable()
    plugins = plugin_dir()
    if not (plugins / "nuke_mcp_bridge").is_dir():
        raise LaunchError(f"Nuke plugin folder not found at {plugins}. Set NUKE_MCP_PLUGIN_DIR.")
    env = os.environ.copy()
    env["NUKE_PATH"] = os.pathsep.join(p for p in (str(plugins), env.get("NUKE_PATH", "")) if p)
    env["NUKE_MCP_PORT"] = str(port)
    env["NUKE_MCP_AUTOSTART"] = "1"

    flags = {"nuke": [], "nukex": ["--nukex"], "nukestudio": ["--studio"]}[variant]
    if mode == "headless":
        # The port goes through NUKE_MCP_PORT: Nuke treats extra -t arguments as the script name.
        cmd = [exe, "-t", *flags, str(plugins / "start_headless.py")]
    else:
        cmd = [exe, *flags]
    if script:
        cmd.append(script)

    log = tempfile.NamedTemporaryFile(prefix=f"nukeassistant_{mode}_", suffix=".log", delete=False)
    kwargs: dict = {"env": env, "stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        # CREATE_NO_WINDOW (not DETACHED_PROCESS) gives Nuke a hidden console that its
        # console-mode children (Frame Server workers) inherit; without it each of them
        # opens a visible console window on top of the desktop.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = mode == "gui"
    proc = subprocess.Popen(cmd, **kwargs)
    log.close()
    if mode == "headless":
        _children.append(proc)  # GUI sessions belong to the artist and outlive this server
    return proc, log.name


def tail(path: str, lines: int = 25) -> str:
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return ""
    return "Nuke output (last lines):\n" + "\n".join(text.splitlines()[-lines:])


def cleanup() -> None:
    """Terminate headless sessions we started, so they don't hold licences."""
    for proc in _children:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


__all__ = ["LaunchError", "cleanup", "find_nuke_executable", "free_port", "launch", "plugin_dir", "tail"]
