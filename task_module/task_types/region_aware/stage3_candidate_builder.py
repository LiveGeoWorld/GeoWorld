"""Author-side Region Stage 3 deterministic POI retrieval.

Stage 1 owns source-rule extraction and Stage 2 owns only the closed
``primary_type`` binding.  This module is deliberately the first place where
a concrete POI is considered.  A type reduces the search pool; it is never
evidence that a particular merchant satisfies a regional rule.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from task_module.task_types.region_aware.wikivoyage_source_pipeline import (
    _asset_hash,
    build_poi_type_catalog,
)
from world_module.service_detail_snapshot import (
    SERVICE_DETAIL_CACHE_SCHEMA,
    identity_key,
    place_identity_signature,
    public_identity,
)
SCHEMA_VERSION = "geoword.region_stage3_admission.v3"


def _normalized_search_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w\u3400-\u9fff]+", " ", text).split())


def _phrase_matches(phrase: str, text: str) -> bool:
    normalized_phrase = _normalized_search_text(phrase)
    normalized_text = _normalized_search_text(text)
    if not normalized_phrase:
        return False
    if re.search(r"[\u3400-\u9fff]", normalized_phrase):
        return normalized_phrase.replace(" ", "") in normalized_text.replace(" ", "")
    return re.search(rf"(?<!\w){re.escape(normalized_phrase)}(?!\w)", normalized_text) is not None


def _full_public_name_matches(left: Any, right: Any) -> bool:
    """Compare complete public names with the same Chinese spacing compatibility as recall."""
    normalized_left = _normalized_search_text(left)
    normalized_right = _normalized_search_text(right)
    if not normalized_left or not normalized_right:
        return False
    if re.search(r"[\u3400-\u9fff]", normalized_left + normalized_right):
        return normalized_left.replace(" ", "") == normalized_right.replace(" ", "")
    return normalized_left == normalized_right


def validate_stage3_retrieval_phrases(values: Any) -> list[str]:
    """Validate and canonicalize the single Stage-2 retrieval phrase list."""
    if not isinstance(values, list):
        raise ValueError("Stage 3 requires Stage 2 poi_retrieval_phrases")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        phrase = str(value or "").strip()
        normalized = _normalized_search_text(phrase)
        if not normalized:
            raise ValueError("Stage 2 POI retrieval phrases must not be empty")
        if normalized not in seen:
            seen.add(normalized)
            result.append(phrase)
    return result


@dataclass(frozen=True)
class RuleTypePool:
    rule_id: str
    affected_poi_type_indexes: tuple[int, ...]
    affected_poi_primary_types: tuple[str, ...]
    candidate_place_ids: tuple[str, ...]


class Stage3CandidateBuilder:
    """Validate inputs and validate author-model relevance evidence.

    The author model may decide that a public POI *name* semantically supports
    a rule (for example a dumpling name for a dumpling rule), or cite an exact
    quote from a frozen source-backed service document.  The decision is
    rejected unless at least one of those two public evidence channels can be
    verified locally.
    """

    def __init__(
        self,
        *,
        world: dict[str, Any],
        characters: dict[str, Any],
        rules_asset: dict[str, Any],
        stage2_asset: dict[str, Any],
        service_detail_cache: dict[str, Any],
        human_revisions_applied: bool = False,
    ) -> None:
        self.world = world
        self.characters = characters
        self.rules_asset = rules_asset
        self.stage2_asset = stage2_asset
        self.service_detail_cache = service_detail_cache
        self.human_revisions_applied = bool(human_revisions_applied)
        self.rules_by_id = {
            str(rule.get("rule_id") or ""): rule
            for rule in rules_asset.get("rules") or []
            if isinstance(rule, dict) and str(rule.get("rule_id") or "")
        }
        self.places = [
            place for place in world.get("places") or []
            if isinstance(place, dict)
            and str(place.get("place_id") or "")
            and str(place.get("primary_type") or "")
            and public_identity(place) is not None
        ]
        self.places_by_id = {str(place["place_id"]): place for place in self.places}
        self.service_records = {
            identity_key(identity): record
            for record in service_detail_cache.get("records") or []
            if isinstance(record, dict)
            and (identity := public_identity(record)) is not None
        }

    def inspect(self) -> dict[str, Any]:
        """Return auditable Stage-3 source pools without choosing a task."""
        self._validate_lineage()
        pools: list[RuleTypePool] = []
        rejected: list[dict[str, str]] = []
        for assessment in self.stage2_asset.get("assessments") or []:
            if not isinstance(assessment, dict):
                continue
            if assessment.get("status") == "needs_stage1_split":
                review_status = str((assessment.get("human_review") or {}).get("status") or "pending")
                reason = (
                    "human-approved Stage 2 rule contains multiple independent restrictions; "
                    "automatic Stage 1 refinement is blocked and human re-review is required"
                    if review_status == "approved" else
                    "Stage 2 rule contains multiple independent restrictions and requires local Stage 1 refinement"
                )
                rejected.append({"rule_id": str(assessment.get("rule_id") or ""), "reason": reason})
                continue
            if assessment.get("status") != "task_source_eligible":
                continue
            rule_id = str(assessment.get("rule_id") or "")
            if rule_id not in self.rules_by_id:
                rejected.append({"rule_id": rule_id, "reason": "Stage 2 assessment references an absent Stage 1 rule"})
                continue
            indexes, types = self._assessment_types(assessment)
            place_ids = tuple(sorted(
                str(place["place_id"]) for place in self.places
                if str(place.get("primary_type") or "") in types
            ))
            if not place_ids:
                rejected.append({"rule_id": rule_id, "reason": "bound types have no current frozen POIs"})
                continue
            pools.append(RuleTypePool(rule_id, indexes, types, place_ids))
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ready_for_stage3_retrieval",
            "rule_pool_count": len(pools),
            "character_count": len(self.characters.get("characters") or []),
            "service_detail_record_count": len(self.service_detail_cache.get("records") or []),
            "rule_type_pools": [
                {
                    "rule_id": pool.rule_id,
                    "affected_poi_type_indexes": list(pool.affected_poi_type_indexes),
                    "affected_poi_primary_types": list(pool.affected_poi_primary_types),
                    "candidate_place_count": len(pool.candidate_place_ids),
                }
                for pool in pools
            ],
            "rejected_rule_pools": rejected,
        }

    def candidate_rows(self, rule_id: str) -> list[dict[str, Any]]:
        """Return one closed author-review payload for a Stage-2-bound rule.

        ``record_index`` is local to this one model request.  No place ID,
        candidate score, GT, or private construction state is exposed to the
        model.  The same local index is used for output validation.
        """
        self._validate_lineage()
        pool = next((item for item in self._rule_type_pools() if item.rule_id == rule_id), None)
        if pool is None:
            raise ValueError(f"No bindable frozen POI pool for rule: {rule_id}")
        rows: list[dict[str, Any]] = []
        for index, place_id in enumerate(pool.candidate_place_ids, start=1):
            place = self.places_by_id[place_id]
            identity = public_identity(place)
            assert identity is not None
            record = self.service_records.get(identity_key(identity), {})
            documents = []
            for document in record.get("source_documents") or []:
                if not isinstance(document, dict) or document.get("extraction_status") != "source_backed":
                    continue
                text = str(document.get("visible_text") or "").strip()
                url = str(document.get("source_url") or document.get("final_url") or "").strip()
                if text and url:
                    documents.append({"source_url": url, "visible_text": text})
            rows.append({
                "record_index": index,
                "name": identity["name"],
                "location": identity["location"],
                "primary_type": str(place.get("primary_type") or ""),
                "service_documents": documents,
            })
        return rows

    def writer_match_context(self, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        """Project short verbatim excerpts for the Writer from frozen detail hits."""
        identity = public_identity(candidate["place"])
        phrases = candidate["retrieval_matches"]["service_detail"]
        if not identity or not phrases:
            return []
        record = self.service_records.get(identity_key(identity), {})
        excerpts = []
        seen = set()
        for document in record.get("source_documents") or []:
            if not isinstance(document, dict) or document.get("extraction_status") != "source_backed":
                continue
            url = document.get("source_url") or document.get("final_url")
            if not url:
                continue
            sentences = re.split(r"(?<=[.!?。！？])\s+|\n+", str(document.get("visible_text") or "").strip())
            for phrase in phrases:
                hits = (sentence for sentence in sentences
                        if 20 <= len(sentence) <= 350 and _phrase_matches(phrase, sentence))
                for sentence in hits:
                    key = (url, sentence)
                    if key not in seen:
                        seen.add(key)
                        excerpts.append({"source_url": url, "matched_phrase": phrase, "excerpt": sentence})
                    if sum(item["matched_phrase"] == phrase and item["source_url"] == url for item in excerpts) >= 3:
                        break
        return excerpts

    def allowed_primary_types(self, rule_id: str) -> list[str]:
        """Return the Stage-2-bound type values for one rule."""
        pool = next((item for item in self._rule_type_pools() if item.rule_id == rule_id), None)
        if pool is None:
            raise ValueError(f"No bindable frozen POI pool for rule: {rule_id}")
        return list(pool.affected_poi_primary_types)

    def search_candidate_rows(
        self, rule_id: str, retrieval_phrases: list[str],
    ) -> list[dict[str, Any]]:
        """Recall candidates by name or frozen source-backed detail text."""
        phrases = validate_stage3_retrieval_phrases(retrieval_phrases)
        recalled: list[dict[str, Any]] = []
        for row in self.candidate_rows(rule_id):
            name_matches = [
                phrase for phrase in phrases
                if _phrase_matches(phrase, row["name"])
            ]
            detail_matches: list[str] = []
            for document in row["service_documents"]:
                detail_matches.extend(
                    phrase for phrase in phrases
                    if _phrase_matches(phrase, document["visible_text"])
                )
            if not name_matches and not detail_matches:
                continue
            recalled.append({
                **row,
                "retrieval_matches": {
                    "poi_name": list(dict.fromkeys(name_matches)),
                    "service_detail": list(dict.fromkeys(detail_matches)),
                },
            })
        return recalled

    def _explicit_named_place_terms(
        self, rule_id: str,
    ) -> list[str]:
        """Return frozen full POI names explicitly present in the Stage-1 statement."""
        statement = str(self.rules_by_id[rule_id].get("statement") or "").strip()
        if not statement:
            return []
        return list(dict.fromkeys(
            row["name"] for row in self.candidate_rows(rule_id)
            if _phrase_matches(row["name"], statement)
        ))

    def places_for_rule(self, rule_id: str) -> list[dict[str, Any]]:
        """Return the private frozen POIs in this rule's Stage-2 type pool."""
        pool = next((item for item in self._rule_type_pools() if item.rule_id == rule_id), None)
        if pool is None:
            raise ValueError(f"No bindable frozen POI pool for rule: {rule_id}")
        return [self.places_by_id[place_id] for place_id in pool.candidate_place_ids]

    def retrieve_rule_candidates(
        self, rule_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Recall POIs deterministically with the phrases persisted by Stage 2."""
        assessment = next(
            (item for item in self.stage2_asset.get("assessments") or []
             if isinstance(item, dict) and str(item.get("rule_id") or "") == rule_id),
            None,
        )
        if assessment is None or assessment.get("status") != "task_source_eligible":
            raise ValueError(f"No eligible Stage 2 assessment for rule: {rule_id}")
        phrases = validate_stage3_retrieval_phrases(assessment.get("poi_retrieval_phrases"))
        recalled = self.search_candidate_rows(rule_id, phrases)
        named_place_terms = self._explicit_named_place_terms(rule_id)
        candidates = []
        for row in recalled:
            proves_named_place = not named_place_terms or any(
                _full_public_name_matches(name, row["name"])
                and any(
                    _full_public_name_matches(phrase, name)
                    for phrase in row["retrieval_matches"]["poi_name"]
                )
                for name in named_place_terms
            )
            if not proves_named_place:
                continue
            place = next(
                place for place in self.places
                if public_identity(place) == {"name": row["name"], "location": row["location"]}
            )
            detail_sources = [
                {"source_url": document["source_url"], "matched_phrases": [
                    phrase for phrase in row["retrieval_matches"]["service_detail"]
                    if _phrase_matches(phrase, document["visible_text"])
                ]}
                for document in row["service_documents"]
                if any(
                    _phrase_matches(phrase, document["visible_text"])
                    for phrase in row["retrieval_matches"]["service_detail"]
                )
            ]
            candidates.append({
                "place": place,
                "retrieval_matches": row["retrieval_matches"],
                "evidence_channels": [
                    channel for channel, values in (
                        ("poi_name", row["retrieval_matches"]["poi_name"]),
                        ("service_detail", row["retrieval_matches"]["service_detail"]),
                    ) if values
                ],
                "name_evidence": row["name"] if row["retrieval_matches"]["poi_name"] else "",
                "detail_evidence": detail_sources,
            })
        return candidates, {
            "retrieval_source": "stage2_assessment.poi_retrieval_phrases",
            "poi_retrieval_phrases": phrases,
            "candidate_pool_count": len(self.candidate_rows(rule_id)),
            "recalled_candidate_count": len(recalled),
            "explicit_named_place_terms": named_place_terms,
            "region_supported_candidate_count": len(candidates),
        }

    def _rule_type_pools(self) -> list[RuleTypePool]:
        inspection = self.inspect()
        return [
            RuleTypePool(
                str(item["rule_id"]),
                tuple(item["affected_poi_type_indexes"]),
                tuple(item["affected_poi_primary_types"]),
                tuple(sorted(
                    str(place["place_id"]) for place in self.places
                    if str(place.get("primary_type") or "") in set(item["affected_poi_primary_types"])
                )),
            )
            for item in inspection["rule_type_pools"]
        ]

    def _validate_lineage(self) -> None:
        pending = self.stage2_asset.get("incomplete_pending_rule_ids") or []
        if pending:
            raise ValueError(
                "Region Stage 3 requires complete Stage 2 assessments; pending rule IDs: "
                + ", ".join(str(value) for value in pending)
            )
        source_hash = str(self.rules_asset.get("source_page_hash") or "")
        if not source_hash or source_hash != str(self.stage2_asset.get("source_page_hash") or ""):
            raise ValueError("Region Stage 3 source-page lineage mismatch between rules and Stage 2")
        if not self.human_revisions_applied and str(self.stage2_asset.get("regional_rules_hash") or "") != _asset_hash(self.rules_asset):
            raise ValueError("Region Stage 3 regional-rules asset hash mismatch")
        catalog = build_poi_type_catalog(self.world)
        detail_binding = self.service_detail_cache.get("world_binding")
        if self.service_detail_cache.get("schema_version") != SERVICE_DETAIL_CACHE_SCHEMA:
            raise ValueError("Region Stage 3 requires a service-detail cache with the current schema")
        if not isinstance(detail_binding, dict):
            raise ValueError("Region Stage 3 service-detail cache lacks world binding")
        if int(detail_binding.get("source_place_count") or -1) != len(self.places):
            raise ValueError("Region Stage 3 service-detail cache does not cover every frozen POI")
        if str(detail_binding.get("source_place_identity_signature") or "") != place_identity_signature(self.places):
            raise ValueError("Region Stage 3 service-detail cache identity lineage mismatch")
        for assessment in self.stage2_asset.get("assessments") or []:
            if not isinstance(assessment, dict):
                raise ValueError("Region Stage 3 assessment row is not an object")
            rule = self.rules_by_id.get(str(assessment.get("rule_id") or ""))
            if rule is None:
                raise ValueError("Region Stage 3 assessment references an absent Stage 1 rule")
            if rule.get("extraction_status") != "supported":
                raise ValueError("Region Stage 3 assessment references a non-supported Stage 1 rule")
            _, types = self._assessment_types(assessment)
            # A city-level binding may refer to a type that is absent in this
            # particular frozen city version.  It is simply unavailable for
            # this Stage-3 run, not an invalid Region rule.

    def _assessment_types(self, assessment: dict[str, Any]) -> tuple[tuple[int, ...], tuple[str, ...]]:
        """Resolve city-level type bindings against this frozen map.

        Primary-type names are the reviewed Region asset.  The historical
        Stage-2 indexes belong to the catalog that existed when the binding
        was first generated and must not be reused against a reordered or
        changed city-version catalog.
        """
        catalog = build_poi_type_catalog(self.world)
        reviewed_types = tuple(sorted({str(value) for value in assessment.get("affected_poi_primary_types") or [] if str(value)}))
        if not reviewed_types:
            return (), ()
        types = tuple(value for value in reviewed_types if value in set(catalog))
        indexes = tuple(catalog.index(value) + 1 for value in types)
        return indexes, types
