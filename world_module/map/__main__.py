"""CLI entry point for ``python -m world_module.map``."""

from __future__ import annotations

from world_module.map.generator import main


if __name__ == "__main__":
    raise SystemExit(main())
