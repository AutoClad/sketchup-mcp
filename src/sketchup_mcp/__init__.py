"""Sketchup integration through Model Context Protocol"""

__version__ = "0.2.0"


def __getattr__(name):
    # Expose the server lazily, so importing sketchup_mcp.launcher (the
    # sketchup-launcher CLI, and the pool's scheduled tasks) doesn't start
    # the server's logging.
    if name == "mcp":
        from .server import mcp
        return mcp
    raise AttributeError(name)
