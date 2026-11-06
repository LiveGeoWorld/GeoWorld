"""Schema helpers for batch-generated place_grounding drafts."""

from __future__ import annotations

from typing import Any


REVIEW_STATUSES = {"draft", "approved", "needs_fix", "rejected"}
ATTRIBUTE_TYPES = {
    "appearance",
    "place_type",
    "district",
    "address_hint",
    "nearby_anchor",
    "human_context",
    "negative_constraint",
}


def validate_batch_example(example: dict[str, Any]) -> list[str]:
    """Return validation issues for a batch-generated grounding example."""
    issues: list[str] = []
    for field in [
        "example_id",
        "user_prompt",
        "prompt_language",
        "image_path",
        "grounding_target_type",
        "acceptable_granularity",
    ]:
        if not example.get(field):
            issues.append(f"missing required field: {field}")
    if example.get("prompt_language") != example.get("review_language") and not example.get("user_prompt_translation"):
        issues.append("missing user_prompt_translation for cross-language review")
    if example.get("review_status", "draft") not in REVIEW_STATUSES:
        issues.append(f"invalid review_status: {example.get('review_status')}")
    evidence = example.get("evidence_ledger")
    if not isinstance(evidence, dict):
        issues.append("missing evidence_ledger")
    else:
        if not evidence.get("image_assets"):
            issues.append("evidence_ledger.image_assets is empty")
        if not evidence.get("place_evidence"):
            issues.append("evidence_ledger.place_evidence is empty")
    profile = example.get("quality_profile") or example.get("difficulty_profile")
    if not isinstance(profile, dict):
        issues.append("missing quality_profile")
    else:
        coverage = set(profile.get("attribute_coverage", []))
        unknown = sorted(coverage - ATTRIBUTE_TYPES)
        if unknown:
            issues.append(f"unknown quality attributes: {unknown}")
    landscape = example.get("ambiguity_landscape")
    if not isinstance(landscape, dict) or "ambiguity_score" not in landscape:
        issues.append("missing ambiguity_landscape")
    if not isinstance(example.get("prompt_evidence_map"), dict):
        issues.append("missing prompt_evidence_map")
    return issues


def build_curated_payload(
    *,
    city_id: str,
    variant: str,
    schema_version: str,
    examples: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return an exporter-compatible curated payload."""
    return {
        "schema_version": schema_version,
        "task_type": "place_grounding",
        "city_id": city_id,
        "variant": variant,
        "examples": examples,
    }
