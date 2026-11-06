"""Release-facing generation telemetry for cost, latency, cache, and failures."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.io import read_json, write_json


def write_generation_telemetry(
    output_dir: str | Path,
    *,
    task_type: str,
    city_id: str,
    requested_count: int,
    generated_count: int,
    elapsed_s: float,
    route_cache: Any | None = None,
    llm_usage: dict[str, int | float] | None = None,
    skipped_count: int = 0,
    failure_reasons: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Write one machine-readable report per batch without guessing provider price."""
    route_stats = route_cache.stats_snapshot() if route_cache is not None else {}
    manifest_path = Path(output_dir) / "release_cost_manifest.json"
    pricing = read_json(manifest_path) if manifest_path.exists() else {}
    route_unit = pricing.get("route_request_usd")
    prompt_unit = pricing.get("llm_prompt_token_usd")
    completion_unit = pricing.get("llm_completion_token_usd")
    llm = llm_usage or {}
    estimated_cost = None
    if all(isinstance(value, (int, float)) for value in (route_unit, prompt_unit, completion_unit)):
        estimated_cost = round(
            route_stats.get("api_requests", 0) * route_unit
            + llm.get("prompt", 0) * prompt_unit
            + llm.get("completion", 0) * completion_unit,
            8,
        )
    report = {
        "schema_version": "geoword.generation_telemetry.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task_type": task_type,
        "city_id": city_id,
        "throughput": {
            "requested_count": requested_count,
            "generated_count": generated_count,
            "skipped_count": skipped_count,
            "elapsed_s": round(elapsed_s, 3),
            "tasks_per_hour": round(generated_count / max(elapsed_s, 1e-9) * 3600, 2),
        },
        "route_api": route_stats,
        "llm": llm,
        "failure_reasons": failure_reasons or {},
        "cost_policy": {
            "route_request_count": route_stats.get("api_requests", 0),
            "pricing_manifest": str(manifest_path) if manifest_path.exists() else None,
            "pricing_effective_date": pricing.get("effective_date"),
            "route_provider_unit_price_usd": route_unit,
            "llm_prompt_token_usd": prompt_unit,
            "llm_completion_token_usd": completion_unit,
            "estimated_external_cost_usd": estimated_cost,
            "reason": (
                "Measured request/token usage is always retained. Cost is calculated only from "
                "a dated release manifest; copy release_cost_manifest.template.json beside the batch."
            ),
        },
    }
    write_json(Path(output_dir) / "generation_telemetry.json", report)
    return report
