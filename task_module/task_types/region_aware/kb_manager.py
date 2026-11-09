"""City-level, Wikivoyage-backed cultural-rule knowledge base.

Raw Wikivoyage paragraphs are evidence only.  A task may use a rule only after
it has a source link, a structured executable requirement, and deterministic
validation.  Human audit remains the final admission gate for release tasks.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUTPUT_ROOT = Path("outputs/benchmarks/region_aware/cultural_constraints")
EVIDENCE_ROOT = Path("outputs/benchmarks/region_aware/cultural_sources/wikivoyage")

SUPPORTED_CITIES = ["shanghai_smoke", "tokyo_complex", "london_smoke", "tokyo", "dubai", "barcelona", "jerusalem", "singapore"]


class CulturalKB:
    """Manages city-level cultural constraint knowledge. No scene classification —
    constraints are indexed by keywords for combinatorial two-level random sampling."""

    def __init__(self, city_id: str, *, constraints_path: str | Path | None = None,
                 evidence_path: str | Path | None = None):
        self.city_id = city_id
        self._path = Path(constraints_path) if constraints_path else OUTPUT_ROOT / f"{city_id}.json"
        self._evidence_override = Path(evidence_path) if evidence_path else None
        if not self._path.exists():
            base_city = city_id
            for suffix in ("_corridor_smoke", "_smoke", "_complex", "_large", "_base"):
                if base_city.endswith(suffix):
                    base_city = base_city[:-len(suffix)]
                    break
            base_path = OUTPUT_ROOT / f"{base_city}.json"
            if base_path.exists():
                self._path = base_path
            for suffix in ("_smoke", "_complex", "_large", "_corridor_smoke", "_base"):
                if self._path.exists():
                    break
                alt = OUTPUT_ROOT / f"{city_id}{suffix}.json"
                if alt.exists():
                    self._path = alt
                    self.city_id = city_id  # keep canonical name
                    break
        self._data = self._load()

    def _load(self) -> dict:
        if not self._path.exists():
            return {"city": self.city_id, "constraints": [], "scenes": ["general"]}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for c in data.get("constraints", []):
                c["used_count"] = 0
            return data
        except (json.JSONDecodeError, FileNotFoundError):
            return {"city": self.city_id, "constraints": [], "scenes": ["general"]}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        out = {"city": self.city_id, "constraints": [], "scenes": self._data.get("scenes", [])}
        for c in self._data.get("constraints", []):
            entry = dict(c)
            entry.pop("used_count", None)  # used_count is batch-local, never persisted
            out["constraints"].append(entry)
        self._path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def scenes(self) -> list[str]:
        return self._data.get("scenes", ["general"])

    @property
    def all_constraints(self) -> list[dict]:
        return self._data.get("constraints", [])

    def get_approved(self) -> list[dict]:
        """Return approved constraints that satisfy the executable-rule contract."""
        evidence = self.load_evidence() or {}
        evidence_ids = {card.get("evidence_id", "") for card in evidence.get("evidence_cards", [])}
        return [
            c for c in self.all_constraints
            if c.get("review") == "approved" and validate_constraint(c, evidence_ids)["valid"]
        ]

    def get_task_eligible(self, admission_mode: str = "prototype_validated") -> list[dict]:
        """Return only source-backed, structured and automatically valid rules.

        Raw Wikivoyage paragraphs are evidence, not task constraints.  Human
        review may still be pending at task-bundle audit time, but a candidate
        rule must first pass this deterministic contract.
        """
        if admission_mode not in {"prototype_validated", "audit_approved_only"}:
            raise ValueError(f"Unsupported Region admission mode: {admission_mode}")
        allowed_reviews = {"approved"}
        if admission_mode == "prototype_validated":
            allowed_reviews.add("pending_task_audit")
        evidence = self.load_evidence() or {}
        evidence_ids = {card.get("evidence_id", "") for card in evidence.get("evidence_cards", [])}
        eligible = []
        for constraint in self.all_constraints:
            validation = validate_constraint(constraint, evidence_ids)
            constraint["validation"] = validation
            if validation["valid"] and constraint.get("review") in allowed_reviews:
                eligible.append(constraint)
        return eligible

    def get_by_ids(self, constraint_ids: list[str]) -> list[dict]:
        id_set = set(constraint_ids)
        return [c for c in self.all_constraints if c.get("id") in id_set]

    def increment_used(self, constraint_ids: list[str]) -> None:
        """Increment used_count for selected constraints."""
        for c in self.all_constraints:
            if c.get("id") in constraint_ids:
                c["used_count"] = c.get("used_count", 0) + 1

    def add_constraint(self, rule: str, constraint_type: str,
                        time_info: dict | None = None,
                        severity: str = "hard",
                        keywords: list | None = None,
                        source_type: str = "",
                        evidence_ids: list | None = None,
                        hidden_rule: dict | None = None,
                        verifiable_by: list | None = None) -> str | None:
        """Add a constraint if not duplicate. Returns constraint_id or None if duplicate."""
        normalized = _normalize_rule(rule)
        for existing in self.all_constraints:
            if _normalize_rule(existing.get("rule", "") or existing.get("rule_text", "")) == normalized:
                return existing.get("id")
        cid = _make_id(self.city_id, constraint_type, rule)
        entry = {
            "id": cid, "type": constraint_type,
            "rule_text": rule,
            "time": time_info or {"mode": "year_round"},
            "severity": severity, "review": "draft", "used_count": 0,
            "keywords": keywords or [],
        }
        if source_type: entry["source_type"] = source_type
        if evidence_ids: entry["evidence_ids"] = evidence_ids
        if hidden_rule: entry["hidden_rule"] = hidden_rule
        if verifiable_by: entry["verifiable_by"] = verifiable_by
        self._data.setdefault("constraints", []).append(entry)
        return cid

    def approve(self, constraint_id: str) -> bool:
        for c in self._data.get("constraints", []):
            if c.get("id") == constraint_id:
                c["review"] = "approved"
                c["updated_at"] = _utc_now()
                return True
        return False

    def reject(self, constraint_id: str) -> bool:
        for c in self._data.get("constraints", []):
            if c.get("id") == constraint_id:
                # Keep lineage resolvable.  Deleting the record would make an
                # audited task look valid simply because its dependency vanished.
                c["review"] = "rejected"
                c["updated_at"] = _utc_now()
                return True
        return False

    def update_rule(self, constraint_id: str, new_rule: str, new_time: dict | None = None,
                    hidden_rule: dict | None = None,
                    verifiable_by: list | None = None) -> bool:
        for c in self._data.get("constraints", []):
            if c.get("id") == constraint_id:
                c["rule_text"] = new_rule
                # Retain the legacy field for existing audit UI consumers.
                c["rule"] = new_rule
                if new_time is not None:
                    c["time"] = new_time
                if hidden_rule is not None:
                    c["hidden_rule"] = hidden_rule
                if verifiable_by is not None:
                    c["verifiable_by"] = verifiable_by
                c["updated_at"] = _utc_now()
                return True
        return False

    def to_llm_context(self, constraints: list[dict] | None = None) -> str:
        lines = [f"城市: {self.city_id}"]
        items = constraints or self.get_approved()
        for c in items:
            rule = c.get("rule_text") or c.get("rule", "")
            lines.append(f"- ({c['type']}/{c['severity']}) {rule}")
        return "\n".join(lines)

    def count_approved(self) -> int:
        return len(self.get_approved())

    # ── Wikivoyage evidence → candidate queue ──

    SECTION_TYPE_MAP = {
        "Respect": "etiquette", "Eat": "dietary", "Drink": "dietary",
        "Stay safe": "behavior", "Cope": "time_restriction",
        "Buy": "etiquette", "Understand": "etiquette",
        "Get around": "time_restriction", "Sleep": "venue_attribute",
    }
    HARD_KEYWORDS = {"prohibited", "illegal", "forbidden", "strict", "ban",
                     "fine", "jail", "arrest", "not allowed", "prison"}

    @classmethod
    def import_from_evidence(cls, city_id: str) -> int:
        """Register raw Wikivoyage cards as evidence-only candidates.

        This is deliberately *not* an LLM rule generator and never approves a
        rule.  An AI or human must later create a structured candidate with an
        executable ``hidden_rule`` before generation can use it.
        """
        path = cls(city_id)._evidence_path()
        if not path.exists():
            print(f"Evidence file not found: {path}")
            return 0
        evidence = json.loads(path.read_text(encoding="utf-8"))
        cards = evidence.get("evidence_cards", [])
        if not cards:
            print(f"No evidence cards in {path}")
            return 0

        kb = cls(city_id)
        # Earlier experiments labeled verbatim evidence paragraphs as approved
        # constraints.  Preserve them for audit history but revoke their task
        # admission instead of mixing them with structured rules.
        for legacy in kb.all_constraints:
            if legacy.get("source_type") == "wikivoyage_auto" and not legacy.get("hidden_rule"):
                legacy["review"] = "evidence_only"
                legacy["candidate_origin"] = "legacy_raw_wikivoyage_demoted"
                legacy["updated_at"] = _utc_now()
        added = 0
        for card in cards:
            evidence_id = card.get("evidence_id", "")
            if not evidence_id or any(evidence_id in c.get("evidence_ids", []) for c in kb.all_constraints):
                continue
            ctype = cls.SECTION_TYPE_MAP.get(card.get("section", ""), "etiquette")
            cid = kb.add_constraint(
                rule=card.get("text", ""), constraint_type=ctype,
                severity="evidence_only", keywords=card.get("keywords", []),
                time_info={"mode": "year_round"},
                source_type="wikivoyage_raw_evidence",
                evidence_ids=[evidence_id],
            )
            if cid:
                for c in kb.all_constraints:
                    if c["id"] == cid:
                        c["review"] = "evidence_only"
                        c["section"] = card.get("section", "")
                        c["candidate_origin"] = "raw_wikivoyage_requires_structuring"
                        c["updated_at"] = _utc_now()
                        break
                added += 1

        kb.save()
        print(f"[WikivoyageImport] {city_id}: {added} evidence-only candidates from {len(cards)} cards")
        return added

    # ── Evidence methods ──

    def load_evidence(self) -> dict | None:
        """Load Wikivoyage evidence for this city."""
        path = self._evidence_path()
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def _evidence_path(self) -> Path:
        """Resolve the actual city evidence file before a read *or* write."""
        if self._evidence_override is not None:
            return self._evidence_override
        exact = EVIDENCE_ROOT / f"{self.city_id}.json"
        if exact.exists():
            return exact
        base = self.city_id
        for suffix in ("_smoke", "_complex", "_large", "_corridor_smoke", "_base"):
            base = base.replace(suffix, "")
        fallback = EVIDENCE_ROOT / f"{base}.json"
        return fallback if fallback.exists() else exact

    def list_unused_evidence(self) -> list[dict]:
        """Return evidence cards not yet used in any constraint."""
        evidence = self.load_evidence()
        if not evidence:
            return []
        used_ids = set()
        for c in self.all_constraints:
            for eid in c.get("evidence_ids", []):
                used_ids.add(eid)
        return [card for card in evidence.get("evidence_cards", [])
                if card.get("evidence_id") not in used_ids]

    def create_constraint_from_evidence(
        self, evidence_id: str, rule_text: str,
        constraint_type: str = "etiquette", hidden_rule: dict | None = None,
        verifiable_by: list | None = None, severity: str = "hard",
    ) -> str | None:
        """Create a constraint backed by Wikivoyage evidence."""
        cid = self.add_constraint(
            rule=rule_text, constraint_type=constraint_type,
            time_info={"mode": "year_round"}, severity=severity,
            source_type="wikivoyage_backed_human_curated",
            evidence_ids=[evidence_id],
            hidden_rule=hidden_rule,
            verifiable_by=verifiable_by or [],
        )
        # Mark evidence as used
        evidence = self.load_evidence()
        if evidence and cid:
            for card in evidence.get("evidence_cards", []):
                if card.get("evidence_id") == evidence_id:
                    card.setdefault("used_for_constraints", []).append(cid)
            path = self._evidence_path()
            path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        return cid

    def update_evidence_card(self, evidence_id: str, updates: dict[str, Any]) -> bool:
        """Edit a source card without silently changing its derived rules.

        The caller must synchronize dependent tasks afterwards.  Keeping the
        rule and evidence versions separate makes a human correction auditable.
        """
        evidence = self.load_evidence()
        if not evidence:
            return False
        for card in evidence.get("evidence_cards", []):
            if card.get("evidence_id") == evidence_id:
                for field in ("text", "keywords", "section"):
                    if field in updates:
                        card[field] = updates[field]
                card["updated_at"] = _utc_now()
                path = self._evidence_path()
                path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
                return True
        return False

    def add_ai_candidate_from_evidence(self, evidence_id: str, rule_text: str, constraint_type: str,
                                       hidden_rule: dict[str, Any], verifiable_by: list[str]) -> str | None:
        """Store an AI-structured candidate for later task-bundle audit.

        The method deliberately does not approve the rule.  Deterministic
        validation may make it eligible for prototype construction, while the
        final human audit accepts, edits, or rejects the linked rule and task.
        """
        constraint_id = self.add_constraint(
            rule=rule_text, constraint_type=constraint_type, severity="hard",
            source_type="wikivoyage_ai_candidate", evidence_ids=[evidence_id],
            hidden_rule=hidden_rule, verifiable_by=verifiable_by,
        )
        for item in self.all_constraints:
            if item.get("id") == constraint_id:
                item["review"] = "pending_task_audit"
                item["candidate_origin"] = "ai_structured_from_wikivoyage"
        return constraint_id

    def stats_for_scene(self, scene: str) -> dict:
        approved = self.get_constraints_for_scene(scene, only_approved=True)
        total = self.get_constraints_for_scene(scene, only_approved=False)
        return {
            "approved_count": len(approved),
            "total_count": len(total),
            "total_used": sum(c.get("used_count", 0) for c in approved),
            "avg_used": round(sum(c.get("used_count", 0) for c in approved) / max(len(approved), 1), 1),
            "need_enrich": self.should_enrich(scene),
        }


def validate_constraint(constraint: dict[str, Any], available_evidence_ids: set[str] | None = None) -> dict[str, Any]:
    """Validate the evidence -> structured rule contract without an LLM."""
    reasons: list[str] = []
    source = constraint.get("source_type", "")
    if not source.startswith("wikivoyage_"):
        reasons.append("source_type is not Wikivoyage-backed")
    if not constraint.get("evidence_ids") or any(not item for item in constraint.get("evidence_ids", [])):
        reasons.append("missing evidence_ids")
    elif available_evidence_ids is not None and not set(constraint.get("evidence_ids", [])) <= available_evidence_ids:
        reasons.append("evidence_ids do not resolve in city evidence file")
    hidden_rule = constraint.get("hidden_rule")
    if not isinstance(hidden_rule, dict) or not hidden_rule:
        reasons.append("missing structured hidden_rule")
    elif not any(key in hidden_rule for key in (
        "forbidden_actions", "forbidden_time_ranges", "avoid_place_tags", "formal_business_venue_filter",
    )):
        reasons.append("hidden_rule has no executable requirement")
    verifiable = constraint.get("verifiable_by")
    if not isinstance(verifiable, list) or not verifiable:
        reasons.append("missing verifiable_by")
    if not (constraint.get("rule_text") or constraint.get("rule")):
        reasons.append("missing rule text")
    return {"valid": not reasons, "reasons": reasons, "schema": "geoword.region_constraint.v2"}


def _make_id(city: str, scope: str, rule: str) -> str:
    h = hashlib.md5(f"{city}|{scope}|{rule}".encode()).hexdigest()[:8]
    return f"c_{city}_{h}"


def constraint_fingerprint(constraint: dict[str, Any]) -> str:
    """Stable dependency fingerprint for the executable part of a rule."""
    payload = {
        "id": constraint.get("id", ""),
        "rule_text": constraint.get("rule_text") or constraint.get("rule", ""),
        "time": constraint.get("time", {}),
        "hidden_rule": constraint.get("hidden_rule", {}),
        "verifiable_by": constraint.get("verifiable_by", []),
        "review": constraint.get("review", ""),
        "evidence_ids": constraint.get("evidence_ids", []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evidence_fingerprint(card: dict[str, Any]) -> str:
    payload = {
        "evidence_id": card.get("evidence_id", ""), "section": card.get("section", ""),
        "text": card.get("text", ""), "keywords": card.get("keywords", []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_rule(rule: str) -> str:
    import re
    r = rule.strip().lower()
    r = re.sub(r'[^\u4e00-\u9fff\w]', '', r)
    r = r.replace("不能", "禁").replace("不可", "禁").replace("不允许", "禁").replace("必须", "须")
    return r


def get_city_kb(city_id: str) -> CulturalKB:
    return CulturalKB(city_id)


def list_available_cities() -> list[str]:
    if not OUTPUT_ROOT.exists(): return SUPPORTED_CITIES
    cities = [f.stem for f in OUTPUT_ROOT.iterdir() if f.suffix == ".json"]
    return cities or SUPPORTED_CITIES
