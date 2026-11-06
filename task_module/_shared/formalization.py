"""Formal task eligibility and auditable legacy-task validation.

The task-folder contract validates generic files.  This module adds the
world-scoped provenance checks required before a folder can be used by the
default benchmark, evaluation, or review flows.  It intentionally performs
only local, deterministic checks: formalizing history must never call an LLM
or Google API, and must never rewrite task content.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.io import read_json
from task_module._shared.task_contract import validate_task_folder, validate_task_language_contract


FORMALIZATION_SCHEMA_VERSION = "geoword.task.formalization.v1"
VALIDATOR_VERSION = "legacy_static_contract.v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_formal_task(task_dir: Path, task_type: str = "") -> bool:
    """Return whether a task is eligible for normal benchmark consumption."""
    batch_metadata = task_dir.parent.parent / "batch_metadata.json"
    if batch_metadata.is_file():
        try:
            batch = read_json(batch_metadata)
        except Exception:
            return False
        if batch.get("evaluation_eligibility") == "ineligible":
            return False
    stamp_path = task_dir / "task_validation.json"
    if not stamp_path.is_file():
        return False
    try:
        stamp = read_json(stamp_path)
    except Exception:
        return False
    if not (stamp.get("state") == "formal" and stamp.get("valid") is True):
        return False
    return bool(validate_task_folder(task_dir, task_type).get("valid"))


def validate_legacy_task(task_dir: Path, *, batch_dir: Path, task_type: str,
                         world_id: str, city_id: str) -> dict[str, Any]:
    """Run reproducible static checks for one legacy task folder.

    A pass means its current files and available batch provenance are coherent
    enough to become a formal benchmark input.  It does not claim to rerun the
    historical model-quality gate or recreate external evidence.
    """
    errors: list[str] = []
    checks: list[dict[str, Any]] = []
    contract = validate_task_folder(task_dir, task_type)
    _check(checks, "file_contract", bool(contract.get("valid")), "; ".join(contract.get("errors") or []) or "ok")
    errors.extend(contract.get("errors") or [])

    task: dict[str, Any] = {}
    sandbox: dict[str, Any] = {}
    try:
        task = read_json(task_dir / "task.json")
    except Exception as exc:
        errors.append(f"unreadable task.json: {exc}")
    sandbox_name = "route_sandbox.json" if task_type == "route_planning" else "task_sandbox.json"
    try:
        sandbox = read_json(task_dir / sandbox_name)
    except Exception as exc:
        errors.append(f"unreadable {sandbox_name}: {exc}")

    _check(checks, "task_scope", task.get("task_type") == task_type and task.get("city_id") == city_id,
           f"task_type={task.get('task_type')!r}; city_id={task.get('city_id')!r}")
    if task.get("task_type") != task_type:
        errors.append("task.json task_type does not match batch scope")
    if task.get("city_id") != city_id:
        errors.append("task.json city_id does not match batch scope")
    _check(checks, "sandbox_identity", sandbox.get("task_type") in {None, "", task_type}
           and sandbox.get("task_id") in {None, "", task.get("task_id")}, "ok")
    if sandbox and sandbox.get("task_type") not in {None, "", task_type}:
        errors.append(f"{sandbox_name} task_type does not match task scope")
    if sandbox and sandbox.get("task_id") not in {None, "", task.get("task_id")}:
        errors.append(f"{sandbox_name} task_id does not match task.json")

    provenance = _read_provenance(batch_dir, task_dir)
    provenance_errors = _validate_provenance(provenance, world_id, city_id, task_type, task_dir)
    _check(checks, "world_provenance", not provenance_errors, "; ".join(provenance_errors) or "ok")
    errors.extend(provenance_errors)

    semantic_errors = _validate_type_shape(task_type, task, sandbox, task_dir)
    _check(checks, "task_type_static_shape", not semantic_errors, "; ".join(semantic_errors) or "ok")
    errors.extend(semantic_errors)
    return {
        "schema_version": FORMALIZATION_SCHEMA_VERSION,
        "task_dir": _safe_text(str(task_dir)),
        "task_type": task_type,
        "world_id": world_id,
        "city_id": city_id,
        "valid": not errors,
        "errors": errors,
        "checks": checks,
        "task_sha256": sha256_file(task_dir / "task.json") if (task_dir / "task.json").is_file() else "",
        "provenance_source": _safe_text(str(provenance.get("_source", ""))),
        "validator_version": VALIDATOR_VERSION,
    }


def make_legacy_stamp(report: dict[str, Any], *, state: str = "formal") -> dict[str, Any]:
    """Produce a durable stamp without mutating task payloads or GT."""
    valid = state == "formal" and bool(report.get("valid"))
    return {
        **report,
        "valid": valid,
        "state": state,
        "formalization_source": "legacy_backfill",
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }


def _check(checks: list[dict[str, Any]], name: str, passed: bool, message: str) -> None:
    checks.append({"name": name, "passed": passed, "message": message})


def _read_provenance(batch_dir: Path, task_dir: Path) -> dict[str, Any]:
    task_path = task_dir / "task_provenance.json"
    source = task_path if task_path.is_file() else batch_dir / "world_provenance.json"
    if not source.is_file():
        return {"_source": ""}
    try:
        value = read_json(source)
    except Exception as exc:
        return {"_source": str(source), "_error": str(exc)}
    return {**value, "_source": str(source)} if isinstance(value, dict) else {"_source": str(source), "_error": "provenance is not an object"}


def _validate_provenance(provenance: dict[str, Any], world_id: str, city_id: str,
                         task_type: str, task_dir: Path) -> list[str]:
    errors: list[str] = []
    if not provenance.get("_source"):
        return ["missing task_provenance.json and batch world_provenance.json"]
    if provenance.get("_error"):
        return [f"unreadable provenance: {provenance['_error']}"]
    if provenance.get("world_id") != world_id:
        errors.append("provenance world_id does not match directory world")
    if provenance.get("city_id") not in {None, "", city_id}:
        errors.append("provenance city_id does not match directory city")
    if provenance.get("task_type") not in {None, "", task_type}:
        errors.append("provenance task_type does not match directory task type")
    context = provenance.get("generation_context") if isinstance(provenance.get("generation_context"), dict) else provenance
    required = context.get("required_city_versions") or provenance.get("required_city_versions") or {}
    if not isinstance(required, dict) or not required:
        errors.append("provenance missing required_city_versions")
    elif not required.get(city_id):
        errors.append("provenance does not require the directory city version")
    task_hash = provenance.get("task_json_sha256")
    if task_hash and task_hash != sha256_file(task_dir / "task.json"):
        errors.append("task_provenance task_json_sha256 does not match task.json")
    return errors


def _validate_type_shape(task_type: str, task: dict[str, Any], sandbox: dict[str, Any], task_dir: Path) -> list[str]:
    """Minimal executable shape checks shared by current and future families."""
    errors: list[str] = []
    if not isinstance(sandbox, dict) or not sandbox:
        return ["task sandbox must be a non-empty object"]
    if task_type == "place_grounding":
        summary = task.get("summary") or {}
        gold = summary.get("gold_summary") or sandbox.get("gold") or {}
        if not (gold.get("place_id") or gold.get("candidate_id")):
            errors.append("place_grounding missing gold place identifier")
        assets = summary.get("input_assets") or {}
        image = assets.get("image_path") or sandbox.get("image_path")
        if not image:
            errors.append("place_grounding missing image evidence reference")
        elif not (task_dir / str(image)).is_file():
            errors.append("place_grounding image evidence is not localized in the task bundle")
    elif task_type == "place_discovery":
        gold = (task.get("summary") or {}).get("gold_summary") or sandbox.get("gold") or {}
        if not (
            gold.get("candidates")
            or gold.get("place_id")
            or gold.get("service_id")
            or (gold.get("top_candidate") and int(gold.get("candidate_count") or 0) >= 1)
        ):
            errors.append("place_discovery missing ranked or gold candidate")
    elif task_type == "route_planning":
        stages = _read_list(task_dir / "judge_sequence.json", "stages")
        if not stages:
            errors.append("route_planning requires a non-empty judge sequence")
    elif task_type == "multi_entity":
        if not isinstance(sandbox.get("participants"), list) or not sandbox["participants"]:
            errors.append("multi_entity missing participants")
        if not isinstance(sandbox.get("candidate_restaurants"), list) or not sandbox["candidate_restaurants"]:
            errors.append("multi_entity missing candidate places")
    elif task_type == "itinerary":
        if not (sandbox.get("gt") or sandbox.get("correct_itinerary")):
            errors.append("itinerary missing itinerary ground truth")
    elif task_type == "safety_planning":
        if not (sandbox.get("gt") or sandbox.get("expected_plan")):
            errors.append("safety_planning missing safety ground truth")
    elif task_type == "region_aware":
        if not (sandbox.get("gt") or sandbox.get("expected_plan")):
            errors.append("region_aware missing region-aware ground truth")
    elif task_type == "dynamic_replanning":
        if not _read_list(task_dir / "env_events.json", "env_events"):
            errors.append("dynamic_replanning requires at least one environment event")
    return errors


def _validate_task_language_contract(task: dict[str, Any]) -> list[str]:
    """Backward-compatible internal alias for the shared release gate."""
    return validate_task_language_contract(task)


def _read_list(path: Path, field: str) -> list[Any]:
    try:
        value = read_json(path)
    except Exception:
        return []
    return value.get(field, []) if isinstance(value, dict) and isinstance(value.get(field), list) else []


def _safe_text(value: str) -> str:
    """Keep audit JSON valid even for a historical Windows filename with surrogates."""
    return value.encode("utf-8", "backslashreplace").decode("utf-8")
