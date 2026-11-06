"""Visualization module — interactive city world map viewer.

Usage:
    python -m vis.server --world outputs/mapseeds/tokyo/complex/world.json --port 8765
"""

__all__ = ["make_server"]


def __getattr__(name: str):
    if name == "make_server":
        from vis.server import make_server
        return make_server
    raise AttributeError(name)
