"""Versioned evidence-to-task synchronization for Region-Aware tasks.

This module is intentionally deterministic.  It never asks an LLM to repair a
task after an audit edit: the natural-language prompt stays unchanged while the
private oracle is rebuilt from the edited, source-backed rule and the original
world POIs.  If that is impossible, the task is invalidated rather than tested
with stale ground truth.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.io import read_json, write_json
from task_module.task_types.region_aware.kb_manager import (
    CulturalKB,
    constraint_fingerprint,
    evidence_fingerprint,
    validate_constraint,
)
from task_module.task_types.region_aware.solver import solve_region_plan


LINEAGE_SCHEMA = "geoword.region_lineage.v1"


def build_lineage(kb: CulturalKB, constraints: list[dict[str, Any]],
                  admission_mode: str = "prototype_validated") -> dict[str, Any]:
    """Create the private provenance record stored with a generated task."""
    evidence_cards = (kb.load_evidence() or {}).get("evidence_cards", [])
    evidence_by_id = {card.get("evidence_id", ""): card for card in evidence_cards}
    evidence_ids = sorted({eid for rule in constraints for eid in rule.get("evidence_ids", []) if eid})
    return {
        "schema": LINEAGE_SCHEMA,
        "city_id": kb.city_id,
        "admission_mode": admission_mode,
        "status": "fresh",
        "generated_at": _utc_now(),
        "constraints": [
            {
                "constraint_id": rule.get("id", ""),
                "fingerprint": constraint_fingerprint(rule),
                "evidence_ids": list(rule.get("evidence_ids", [])),
            }
            for rule in constraints
        ],
        "evidence": [
            {
                "evidence_id": evidence_id,
                "fingerprint": evidence_fingerprint(evidence_by_id[evidence_id]),
            }
            for evidence_id in evidence_ids if evidence_id in evidence_by_id
        ],
        "sync_history": [],
    }


def synchronize_region_tasks(tasks_dir: str | Path, city_id: str,
                             changed_constraint_ids: set[str] | None = None,
                             changed_evidence_ids: set[str] | None = None) -> dict[str, list[str]]:
    """Refresh all task bundles depending on changed Region-Aware artifacts.

    A valid edited rule produces a new deterministic oracle and a task marked
    ``needs_fix`` for final human re-audit.  A rejected, removed, or malformed
    rule produces ``needs_rebuild`` and is excluded by the test runner.
    """
    root = Path(tasks_dir)
    kb = CulturalKB(city_id)
    changed_constraint_ids = changed_constraint_ids or set()
    changed_evidence_ids = changed_evidence_ids or set()
    result = {"refreshed": [], "invalidated": [], "untouched": []}
    if not root.exists():
        return result

    for task_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        sandbox_path = task_dir / "task_sandbox.json"
        if not sandbox_path.exists():
            continue
        sandbox = read_json(sandbox_path)
        if sandbox.get("task_type") != "region_aware" or sandbox.get("city") != city_id:
            continue
        lineage = _load_lineage(task_dir, sandbox)
        dependency_ids = {item.get("constraint_id", "") for item in lineage.get("constraints", [])}
        evidence_ids = {item.get("evidence_id", "") for item in lineage.get("evidence", [])}
        # Legacy batches predate lineage.json but still carry their sampled rule
        # IDs in the private sandbox.  They must be synchronizable too.
        if not dependency_ids:
            dependency_ids = set(sandbox.get("constraint_ids", []))
            legacy_rules = kb.get_by_ids(list(dependency_ids))
            evidence_ids = {eid for rule in legacy_rules for eid in rule.get("evidence_ids", [])}
        if changed_constraint_ids and not dependency_ids.intersection(changed_constraint_ids):
            result["untouched"].append(task_dir.name)
            continue
        if changed_evidence_ids and not evidence_ids.intersection(changed_evidence_ids):
            result["untouched"].append(task_dir.name)
            continue

        action = refresh_task_bundle(task_dir, kb, sandbox, lineage)
        result[action].append(task_dir.name)
    return result


def refresh_task_bundle(task_dir: str | Path, kb: CulturalKB, sandbox: dict[str, Any] | None = None,
                        lineage: dict[str, Any] | None = None) -> str:
    """Rebuild one task's private GT or make its invalid state explicit."""
    task_dir = Path(task_dir)
    sandbox_path = task_dir / "task_sandbox.json"
    sandbox = sandbox if sandbox is not None else read_json(sandbox_path)
    lineage = lineage if lineage is not None else _load_lineage(task_dir, sandbox)
    constraint_ids = list(sandbox.get("constraint_ids", []))
    rules_by_id = {rule.get("id", ""): rule for rule in kb.get_by_ids(constraint_ids)}
    evidence_ids = {card.get("evidence_id", "") for card in (kb.load_evidence() or {}).get("evidence_cards", [])}
    rules = [rules_by_id[rule_id] for rule_id in constraint_ids if rule_id in rules_by_id]
    invalid_reasons: list[str] = []
    if len(rules) != len(constraint_ids):
        invalid_reasons.append("referenced cultural rule no longer exists")
    for rule in rules:
        validation = validate_constraint(rule, evidence_ids)
        if not validation["valid"]:
            invalid_reasons.extend(validation["reasons"])
        if rule.get("review") == "rejected":
            invalid_reasons.append(f"rule rejected: {rule.get('id', '')}")

    if invalid_reasons:
        _invalidate(task_dir, sandbox, lineage, sorted(set(invalid_reasons)))
        return "invalidated"

    gt = solve_region_plan(
        rules,
        list(sandbox.get("main_pois", [])),
        list(sandbox.get("distractors", [])),
        route_data=sandbox.get("route_data", {}),
        character=sandbox.get("character", {}),
    )
    if not gt.get("expected_feasible"):
        _invalidate(task_dir, sandbox, lineage, ["edited rule leaves no reachable compliant POI plan"])
        return "invalidated"

    sandbox.update({
        "constraints": [_public_rule(rule) for rule in rules],
        "expected_feasible": gt["expected_feasible"],
        "expected_plan": gt["expected_plan"],
        "wrong_plans": gt["wrong_plans"],
        "explanation_keywords": gt["explanation_keywords"],
        "cultural_decisions": gt["cultural_decisions"],
        "cultural_requirements": gt["cultural_requirements"],
        "audit_state": {
            "status": "fresh_pending_task_audit",
            "reason": "source evidence or cultural rule changed; private GT regenerated",
            "updated_at": _utc_now(),
        },
    })
    refreshed = build_lineage(kb, rules, lineage.get("admission_mode", "prototype_validated"))
    refreshed["sync_history"] = list(lineage.get("sync_history", [])) + [{
        "at": _utc_now(), "action": "regenerated_private_oracle", "status": "fresh_pending_task_audit",
    }]
    _write_bundle(task_dir, sandbox, refreshed)
    return "refreshed"


def _invalidate(task_dir: Path, sandbox: dict[str, Any], lineage: dict[str, Any], reasons: list[str]) -> None:
    sandbox["audit_state"] = {"status": "needs_rebuild", "reasons": reasons, "updated_at": _utc_now()}
    lineage["status"] = "needs_rebuild"
    lineage["sync_history"] = list(lineage.get("sync_history", [])) + [{
        "at": _utc_now(), "action": "invalidated", "reasons": reasons,
    }]
    _write_bundle(task_dir, sandbox, lineage)


def _load_lineage(task_dir: Path, sandbox: dict[str, Any]) -> dict[str, Any]:
    path = task_dir / "generation_log" / "lineage.json"
    if path.exists():
        return read_json(path)
    return sandbox.get("region_lineage", {"schema": LINEAGE_SCHEMA, "constraints": [], "evidence": [], "sync_history": []})


def _write_bundle(task_dir: Path, sandbox: dict[str, Any], lineage: dict[str, Any]) -> None:
    sandbox["region_lineage"] = lineage
    write_json(task_dir / "task_sandbox.json", sandbox)
    log_dir = task_dir / "generation_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    write_json(log_dir / "lineage.json", lineage)


def _public_rule(rule: dict[str, Any]) -> dict[str, Any]:
    return {key: rule.get(key) for key in ("id", "type", "rule_text", "rule", "keywords", "time", "severity", "review", "evidence_ids")}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
