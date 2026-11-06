"""World builder — thin re-export of the internal substrate + live_overlay pipeline."""

from world_module.map._internal.substrate_builder import build_world

__all__ = ["build_world"]
