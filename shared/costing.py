"""Auditable reference-cost calculations for external model calls.

This module intentionally keeps an estimated public list price separate from a
provider's actual bill.  Reseller endpoints can expose a model name without
using the upstream provider's billing schedule.
"""

from __future__ import annotations

from typing import Any, Mapping


def apply_llm_pricing_reference(
    telemetry: Mapping[str, Any],
    pricing_reference: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a ledger snapshot with an optional public-price estimate.

    The configured reference must explicitly opt in.  Without one, billing is
    unknown rather than guessed from an endpoint name or model string.
    """
    snapshot = dict(telemetry)
    usage = dict(snapshot.get("token_usage") or {})
    snapshot["token_usage"] = usage
    if not pricing_reference:
        return snapshot

    rates = (
        pricing_reference.get("rates_per_million_tokens")
        or pricing_reference.get("rates_usd_per_million_tokens")
        or {}
    )
    pricing_tiers = pricing_reference.get("pricing_tiers") or []
    try:
        input_rate = float(rates["input"])
        output_rate = float(rates["output"])
    except (KeyError, TypeError, ValueError):
        return snapshot
    cached_input_rate = float(rates.get("cached_input", input_rate))

    prompt_tokens = int(usage.get("prompt", 0) or 0)
    completion_tokens = int(usage.get("completion", 0) or 0)
    cached_input_tokens = int(usage.get("cached_input", 0) or 0)
    request_usage = [dict(item) for item in telemetry.get("request_usage", []) if isinstance(item, Mapping)]
    selected_tiers: list[dict[str, Any]] = []
    if pricing_tiers and not request_usage:
        snapshot.update({
            "cost_status": "pricing_reference_requires_per_request_usage",
            "money_estimate": None,
            "actual_billed_cost": None,
            "actual_provider": pricing_reference.get("actual_provider"),
            "reference_provider": pricing_reference.get("reference_provider"),
            "reference_model": pricing_reference.get("reference_model"),
            "price_source_url": pricing_reference.get("source_url"),
            "price_retrieved_on": pricing_reference.get("retrieved_on"),
        })
        return snapshot

    def calculate_one(item: Mapping[str, Any], tier_rates: Mapping[str, Any]) -> float:
        request_prompt = int(item.get("prompt", 0) or 0)
        request_cached = int(item.get("cached_input", 0) or 0)
        request_completion = int(item.get("completion", 0) or 0)
        return (
            max(0, request_prompt - request_cached) * float(tier_rates.get("input", input_rate))
            + request_cached * float(tier_rates.get("cached_input", cached_input_rate))
            + request_completion * float(tier_rates.get("output", output_rate))
        ) / 1_000_000

    if pricing_tiers:
        estimated_cost = 0.0
        for item in request_usage:
            request_prompt = int(item.get("prompt", 0) or 0)
            tier = next(
                (candidate for candidate in pricing_tiers
                 if request_prompt <= int(candidate.get("max_input_tokens", -1))),
                None,
            )
            if tier is None:
                snapshot.update({
                    "cost_status": "pricing_reference_tier_not_configured",
                    "money_estimate": None,
                    "actual_billed_cost": None,
                    "actual_provider": pricing_reference.get("actual_provider"),
                    "reference_provider": pricing_reference.get("reference_provider"),
                    "reference_model": pricing_reference.get("reference_model"),
                    "price_source_url": pricing_reference.get("source_url"),
                    "price_retrieved_on": pricing_reference.get("retrieved_on"),
                })
                return snapshot
            selected_tiers.append({"max_input_tokens": tier.get("max_input_tokens"), "request_count": 1})
            estimated_cost += calculate_one(item, tier)
    else:
        estimated_cost = calculate_one(
            {"prompt": prompt_tokens, "cached_input": cached_input_tokens, "completion": completion_tokens},
            rates,
        )
    currency = str(pricing_reference.get("currency", "USD"))

    snapshot.update({
        "cost_status": "reference_estimate_not_actual_billed_cost",
        "money_estimate": round(estimated_cost, 8),
        "money_estimate_currency": currency,
        "money_estimate_kind": "public_list_price_reference",
        "estimated_public_list_cost": round(estimated_cost, 8),
        "estimated_public_list_cost_currency": currency,
        "estimated_reference_cost_usd": round(estimated_cost, 8) if currency == "USD" else None,
        "actual_billed_cost": None,
        "actual_provider": pricing_reference.get("actual_provider"),
        "reference_provider": pricing_reference.get("reference_provider"),
        "reference_model": pricing_reference.get("reference_model"),
        "pricing_mode": pricing_reference.get("pricing_mode", "standard"),
        "price_source_url": pricing_reference.get("source_url"),
        "price_retrieved_on": pricing_reference.get("retrieved_on"),
        "rates_per_million_tokens": {
            "input": input_rate,
            "cached_input": cached_input_rate,
            "output": output_rate,
        },
        "cached_input_tokens": cached_input_tokens,
        "cached_input_assumption": (
            "not reported by provider; estimated as zero"
            if "cached_input" not in usage
            else "reported by provider"
        ),
    })
    if selected_tiers:
        tier_counts: dict[str, int] = {}
        for item in selected_tiers:
            key = str(item["max_input_tokens"])
            tier_counts[key] = tier_counts.get(key, 0) + 1
        snapshot["pricing_tier_request_counts"] = tier_counts
    return snapshot
