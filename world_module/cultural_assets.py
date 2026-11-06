"""Build-time Wikivoyage evidence and candidate-constraint assets.

This module deliberately creates auditable candidates only.  It never promotes
an automatically derived rule to human-approved task truth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shared.io import read_json, write_json
from world_module.culture.kb_manager import CulturalKB, EVIDENCE_ROOT
from world_module.culture.wikivoyage_ingestor import WikivoyageIngestor
from world_module.wikivoyage_download import ensure_wikivoyage_dump


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
WIKIVOYAGE_DUMP = PIPELINE_ROOT / "data" / "raw" / "wikivoyage" / "enwikivoyage" / "enwikivoyage-latest-pages-articles.xml.bz2"


def ensure_cultural_assets(
    city_id: str, city_name: str, *, source_titles: list[str] | None = None,
) -> dict[str, Any]:
    """Ensure source evidence and evidence-only KB candidates for one city."""
    exact_evidence_path = EVIDENCE_ROOT / f"{city_id}.json"
    extracted = False
    source_attempts: list[dict[str, Any]] = []
    if not exact_evidence_path.is_file():
        if not WIKIVOYAGE_DUMP.is_file():
            try:
                ensure_wikivoyage_dump(WIKIVOYAGE_DUMP)
            except (OSError, ValueError) as exc:
                return {
                    "status": "unavailable", "reason": f"wikivoyage_download_failed: {exc}",
                    "city_id": city_id, "city_name": city_name, "source_attempts": source_attempts,
                }
        ingestor = WikivoyageIngestor(str(WIKIVOYAGE_DUMP), str(EVIDENCE_ROOT))
        selected_evidence: dict[str, Any] | None = None
        for title in _source_titles(city_name, source_titles):
            evidence = ingestor.extract_city(title)
            if evidence is None:
                source_attempts.append({"title": title, "status": "page_not_found"})
                continue
            cards = evidence.get("evidence_cards", []) or []
            source_attempts.append({
                "title": title,
                "status": "evidence_found" if cards else "page_without_eligible_evidence",
                "resolved_page_title": evidence.get("page_title", title),
                "evidence_cards": len(cards),
            })
            if cards:
                selected_evidence = evidence
                break
        if selected_evidence is None:
            reason = "wikivoyage_city_page_not_found" if not source_attempts or all(
                item["status"] == "page_not_found" for item in source_attempts
            ) else "wikivoyage_page_without_eligible_evidence"
            return {
                "status": "unavailable", "reason": reason,
                "city_id": city_id, "city_name": city_name, "source_attempts": source_attempts,
            }
        extracted = True
        ingestor.write_evidence(selected_evidence, city_id)

    kb = CulturalKB(city_id)
    evidence_path = kb._evidence_path()
    if not evidence_path.is_file():
        return {
            "status": "unavailable", "reason": "wikivoyage_evidence_write_failed",
            "city_id": city_id, "city_name": city_name, "source_attempts": source_attempts,
        }

    evidence = kb.load_evidence() or {}
    evidence_cards = evidence.get("evidence_cards", []) or []
    if not evidence_cards:
        return {
            "status": "unavailable", "reason": "wikivoyage_page_without_eligible_evidence",
            "city_id": city_id, "city_name": city_name, "source_attempts": source_attempts,
            "evidence_path": str(evidence_path), "evidence_cards": 0,
        }

    imported = CulturalKB.import_from_evidence(city_id)
    kb = CulturalKB(city_id)
    if not kb._path.is_file():
        return {
            "status": "unavailable", "reason": "cultural_constraint_write_failed",
            "city_id": city_id, "city_name": city_name, "source_attempts": source_attempts,
            "evidence_path": str(evidence_path), "evidence_cards": len(evidence_cards),
        }
    return {
        "status": "available",
        "city_id": city_id,
        "city_name": city_name,
        "evidence_path": str(evidence_path),
        "evidence_cards": len(evidence_cards),
        "candidate_constraints_added": imported,
        "constraint_path": str(kb._path),
        "extracted_from_dump": extracted,
        "source_attempts": source_attempts,
        "review_policy": "evidence_only_candidates_require_task_audit",
    }


def materialize_world_cultural_assets(
    world_id: str,
    city_id: str,
    city_name: str,
    *,
    source_titles: list[str] | None = None,
) -> dict[str, Any]:
    """Create world-local, immutable cultural asset snapshots for one city.

    Wikivoyage evidence is external source material rather than a previous
    world's asset, but task generation must still bind a concrete evidence and
    constraint snapshot to the selected world/version.
    """
    source = ensure_cultural_assets(city_id, city_name, source_titles=source_titles)
    if source.get("status") != "available":
        return source
    evidence_source = Path(str(source["evidence_path"]))
    constraint_source = Path(str(source["constraint_path"]))
    target_root = PIPELINE_ROOT / "outputs" / "worlds" / world_id / "artifacts" / "cultural" / city_id
    evidence_target = target_root / "wikivoyage_evidence.json"
    constraint_target = target_root / "cultural_constraints.json"
    evidence = read_json(evidence_source)
    constraints = read_json(constraint_source)
    provenance = {
        "world_id": world_id,
        "city_id": city_id,
        "materialization": "world_local_snapshot",
        "source_kind": "wikivoyage_external_evidence",
        "source_paths": {"evidence": str(evidence_source), "constraints": str(constraint_source)},
    }
    evidence = {**evidence, "world_provenance": provenance}
    constraints = {**constraints, "world_provenance": provenance}
    write_json(evidence_target, evidence)
    write_json(constraint_target, constraints)
    return {
        **source,
        "world_id": world_id,
        "evidence_path": str(evidence_target),
        "constraint_path": str(constraint_target),
        "materialization": "world_local_snapshot",
    }


def _source_titles(city_name: str, source_titles: list[str] | None) -> list[str]:
    """Return ordered, de-duplicated source page titles without changing city identity."""
    values = [*list(source_titles or []), city_name]
    seen: set[str] = set()
    titles: list[str] = []
    for value in values:
        title = str(value or "").strip()
        key = title.casefold()
        if title and key not in seen:
            seen.add(key)
            titles.append(title)
    return titles
