"""Prefetch the same Wikivoyage dump used by city registration."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world_module.cultural_assets import WIKIVOYAGE_DUMP
from world_module.wikivoyage_download import ensure_wikivoyage_dump

if __name__ == "__main__":
    print(ensure_wikivoyage_dump(WIKIVOYAGE_DUMP))
