"""Schema helpers for batch-generated place_discovery drafts."""

from __future__ import annotations

from typing import Any


def build_curated_payload(
    *,
    city_id: str,
    variant: str,
    schema_version: str,
    examples: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "task_type": "place_discovery",
        "city_id": city_id,
        "variant": variant,
        "examples": examples,
    }
