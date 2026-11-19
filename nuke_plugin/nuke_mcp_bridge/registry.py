"""Command registry for the NukeAssistant bridge.

Every function decorated with :func:`command` becomes callable over the bridge
socket. Commands always run on Nuke's main thread (see ``server.py``).
"""

COMMANDS = {}


class Command(object):
    __slots__ = ("name", "fn", "undo", "doc")

    def __init__(self, name, fn, undo):
        self.name = name
        self.fn = fn
        self.undo = undo
        self.doc = (fn.__doc__ or "").strip()


def command(name=None, undo=False):
    """Register a bridge command.

    Args:
        name: public command name (defaults to the function name).
        undo: wrap the call in a single named Nuke undo group, so whatever the
            assistant did can be undone in one Ctrl+Z.
    """

    def decorator(fn):
        cmd_name = name or fn.__name__
        COMMANDS[cmd_name] = Command(cmd_name, fn, undo)
        return fn

    return decorator


def get(name):
    return COMMANDS.get(name)


def names():
    return sorted(COMMANDS)
