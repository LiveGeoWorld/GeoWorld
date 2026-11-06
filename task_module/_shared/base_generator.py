"""BaseTaskGenerator — abstract base class for all task type generators.

Each task type (place_grounding, place_discovery, etc.) extends this class
and overrides the type-specific methods. The base class handles:
  - Shared sampling (via _shared/sampler.py)
  - Shared judge assembly helpers
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from task_module.task_schema import (
    EnvEventNode, JudgeNode, JudgeStage,
)

from task_module._shared.sampler import TaskSampler
from task_module._shared.batch_state import attempt_seed as derive_attempt_seed, task_seed as derive_task_seed
from shared.io import read_json, write_json


class BaseTaskGenerator:
    """Abstract base for per-type task generators.

    Subclasses override sample_characters, sample_places,
    build_judge_sequence, and generate().
    """

    task_type: str = ""
    label_zh: str = ""
    default_count: int = 100
    min_chars: int = 1
    max_chars: int = 5
    preferred_service_types: list[str] = []
    env_event_categories: list[str] = []
    prompt_llm_endpoint: str | None = None

    def __init__(
        self,
        world: dict[str, Any],
        characters: dict[str, Any],
        env_events: list[EnvEventNode],
        judge_nodes: list[JudgeNode],
        output_dir: str,
        seed: int = 42,
        use_llm_prompt: bool = True,
        config_yaml: str = "config.yaml",
        curated_examples_dir: str = "",
    ):
        self.world = world
        self.characters = characters
        self.all_env_events = env_events
        self.all_judge_nodes = judge_nodes
        self.output_dir = Path(output_dir)
        self.seed = seed
        self.use_llm_prompt = use_llm_prompt
        self.config_yaml = config_yaml
        self.curated_examples_dir = curated_examples_dir

        city = (world.get("cities") or [{}])[0]
        self.city_id = city.get("city_id", "unknown")

        self.sampler = TaskSampler(world, characters, seed=seed)
        self.generated_at = datetime.now(timezone.utc).isoformat()
        self._formalized_count = 0
        self._rejected_count = 0
        self._task_world_binding: dict[str, Any] | None = None
        self._cost_ledger = None
        self._cost_unit_index = 1
        self._generation_invocation = 0
        self._active_seed_lineage: dict[str, int] | None = None

    def task_seed(self, task_index: int) -> int:
        binding = self._task_world_binding or {}
        return derive_task_seed(
            self.seed, str(binding.get("world_id") or ""), self.city_id,
            self.task_type, int(task_index),
        )

    def attempt_seed(self, task_index: int, attempt_index: int) -> int:
        return derive_attempt_seed(self.task_seed(task_index), int(attempt_index))

    def activate_task_attempt(self, task_index: int, attempt_index: int = 1) -> random.Random:
        task_seed_value = self.task_seed(task_index)
        attempt_seed_value = derive_attempt_seed(task_seed_value, int(attempt_index))
        self._active_seed_lineage = {
            "batch_seed": int(self.seed), "task_index": int(task_index),
            "task_seed": task_seed_value, "attempt_index": int(attempt_index),
            "attempt_seed": attempt_seed_value,
        }
        return random.Random(attempt_seed_value)

    def initialize_cost_ledger(self, binding: dict[str, Any]) -> None:
        """Attach the append-only cost ledger after formal world binding exists."""
        from task_module._shared.cost_ledger import TaskConstructionCostLedger

        context = binding.get("generation_context") or {}
        pricing_snapshot = self._resolved_pricing_snapshot()
        self._cost_ledger = TaskConstructionCostLedger(
            binding["batch_dir"],
            world_id=str(binding["world_id"]),
            city_id=str(binding["city_id"]),
            city_version=str(context.get("city_version") or context.get("version") or ""),
            task_type=str(binding["task_type"]),
            pricing_snapshot=pricing_snapshot,
        )

    def _resolved_pricing_snapshot(self) -> dict[str, Any] | None:
        """Freeze the selected construction endpoint's public price reference."""
        stage_model_key = str(getattr(self, "stage_model_key", "") or "")
        if not self.config_yaml or not stage_model_key:
            return None
        try:
            from shared.config_loader import PipelineConfig

            config = PipelineConfig(self.config_yaml)
            endpoint_name = config.stage_model(stage_model_key)
            endpoint = config.model_endpoint(endpoint_name)
        except Exception:
            return None

        # Keep only reproducibility and pricing fields. Endpoint URLs and keys
        # do not belong in a benchmark artifact.
        return {
            "endpoint_name": endpoint_name,
            "provider": str(endpoint.get("provider") or "unknown"),
            "model": str(endpoint.get("model") or ""),
            "pricing_reference": endpoint.get("pricing_reference"),
        }

    def next_generation_rng(self) -> random.Random:
        """Return a deterministic stream that is distinct for every repair call."""
        invocation = self._generation_invocation
        self._generation_invocation += 1
        return random.Random(self.seed + invocation * 1_000_003)

    @contextmanager
    def cost_capture(self):
        """Capture shared-client usage while one sequential generator is active."""
        if self._cost_ledger is None:
            yield
            return
        unit = f"slot_{self._cost_unit_index:04d}"
        with self._cost_ledger.unit(unit, "task_construction"):
            yield

    def _advance_cost_unit(self) -> None:
        self._cost_unit_index += 1
        if self._cost_ledger is not None:
            self._cost_ledger._active_unit = f"slot_{self._cost_unit_index:04d}"
            self._cost_ledger._active_phase = "task_construction"

    def set_cost_unit(self, unit_id: str, phase: str = "task_construction") -> None:
        """Attribute subsequent sequential external calls to one construction slot."""
        if self._cost_ledger is not None:
            self._cost_ledger._active_unit = unit_id
            self._cost_ledger._active_phase = phase

    def finalize_task_folder(self, task_dir: Path) -> dict[str, Any]:
        """Canonicalize and validate one fully written task before it is formal.

        Generators call this only after their task-specific files have been
        written.  A failed candidate is preserved outside ``tasks/`` with its
        validation report, so it cannot become an accidental benchmark input
        or block the already valid tasks in the same build.
        """
        from task_module._shared.task_contract import canonicalize_task_folder

        task_dir = Path(task_dir)
        task_path = task_dir / "task.json"
        if task_path.is_file() and self._active_seed_lineage:
            task_payload = read_json(task_path)
            metadata = task_payload.setdefault("metadata", {})
            metadata["seed_lineage"] = dict(self._active_seed_lineage)
            write_json(task_path, task_payload)
        requested = max(1, int(getattr(self, "_requested_count", 1) or 1))
        _emit_task_progress(
            f"[TaskProgress] phase=candidate written={self._formalized_count + self._rejected_count + 1} requested={requested}"
        )
        city = (self.world.get("cities") or [{}])[0]
        try:
            report = canonicalize_task_folder(
                task_dir,
                task_type=self.task_type,
                city_id=self.city_id,
                city_timezone=city.get("timezone", "UTC"),
            )
        except Exception as exc:  # Keep the actual candidate for diagnosis.
            report = {"task_dir": str(task_dir), "valid": False, "errors": [str(exc)]}
        report["task_type"] = self.task_type
        report["finalized_at"] = datetime.now(timezone.utc).isoformat()

        if report.get("valid"):
            duplicate_of = self._existing_semantic_duplicate(task_dir)
            if duplicate_of:
                report = {
                    **report,
                    "valid": False,
                    "errors": [f"semantic duplicate of formal task: {duplicate_of}"],
                    "duplicate_of": duplicate_of,
                }

        if report.get("valid"):
            try:
                # A successful task must be self-contained and world-bound
                # before it can survive a later batch-level failure.
                from task_module._shared.task_assets import localize_task_assets
                localize_task_assets(task_dir)
                binding = self._task_world_binding
                if binding:
                    from world_module.registry import write_task_world_provenance
                    write_task_world_provenance(
                        binding["world_id"], binding["city_id"], binding["batch_dir"],
                        task_type=binding["task_type"], mode=binding["mode"],
                        task_folders=[task_dir], generation_context=binding["generation_context"],
                    )
            except Exception as exc:  # Provenance failure means it is not formal.
                report = {**report, "valid": False, "errors": [f"formalization persistence failed: {exc}"]}

        if report.get("valid"):
            self._formalized_count += 1
            report["state"] = "formal"
            write_json(task_dir / "task_validation.json", report)
            if self._cost_ledger is not None:
                task_payload = read_json(task_dir / "task.json")
                self._cost_ledger.write_task_cost(
                    task_dir,
                    str(task_payload.get("task_id") or task_dir.name),
                    self._cost_ledger._active_unit,
                )
                self._advance_cost_unit()
            _emit_task_progress(
                f"[TaskProgress] phase=contract valid={self._formalized_count} rejected={self._rejected_count} requested={requested}"
            )
            return report

        self._rejected_count += 1
        report["state"] = "rejected"
        rejected_root = self.output_dir / "candidates" / "rejected"
        rejected_root.mkdir(parents=True, exist_ok=True)
        target = rejected_root / task_dir.name
        suffix = 2
        while target.exists():
            target = rejected_root / f"{task_dir.name}_{suffix}"
            suffix += 1
        try:
            write_json(task_dir / "task_validation.json", report)
            if self._cost_ledger is not None:
                self._cost_ledger.write_task_cost(
                    task_dir, f"candidate:{task_dir.name}", self._cost_ledger._active_unit,
                )
            shutil.move(str(task_dir), str(target))
            report["task_dir"] = str(target)
        except Exception as exc:  # Do not hide a filesystem failure as validity.
            report.setdefault("errors", []).append(f"candidate isolation failed: {exc}")
        _emit_task_progress(
            f"[TaskProgress] phase=contract valid={self._formalized_count} rejected={self._rejected_count} requested={requested}"
        )
        self._advance_cost_unit()
        return report

    def _existing_semantic_duplicate(self, task_dir: Path) -> str:
        """Reject a candidate that repeats a formal task within this batch.

        The generic signature catches exact public-task repeats. Safety adds its
        private target/risk/alternative identity because an LLM may paraphrase
        the visible notice while leaving the actual benchmark decision unchanged.
        """
        signature = self._semantic_signature(task_dir)
        if not signature:
            return ""
        tasks_root = self.output_dir / "tasks"
        for sibling in tasks_root.iterdir() if tasks_root.is_dir() else []:
            if sibling == task_dir or not sibling.is_dir():
                continue
            validation_path = sibling / "task_validation.json"
            if not validation_path.is_file():
                continue
            try:
                validation = read_json(validation_path)
            except Exception:
                continue
            if validation.get("state") != "formal":
                continue
            if self._semantic_signature(sibling) == signature:
                return sibling.name
        return ""

    def _semantic_signature(self, task_dir: Path) -> str:
        try:
            task = read_json(task_dir / "task.json")
        except Exception:
            return ""
        payload: dict[str, Any] = {
            "task_type": task.get("task_type") or self.task_type,
            "city_id": task.get("city_id") or self.city_id,
            "user_prompt": " ".join(str(task.get("user_prompt") or "").split()),
        }
        if self.task_type == "safety_planning":
            try:
                sandbox = read_json(task_dir / "task_sandbox.json")
            except Exception:
                return ""
            if "safety_choice" in sandbox:
                from task_module.task_types.safety_planning.decision_identity import decision_identity
                details = read_json(task_dir / "generation_log" / "details.json")
                payload["safety_decision"] = decision_identity(
                    sandbox["safety_choice"], details["spacetime_evidence"]["context"])
            else:
                payload["safety_decision"] = self._legacy_safety_identity(sandbox)
            # Author wording does not change the underlying decision.
            payload.pop("user_prompt", None)
        if self.task_type == "region_aware":
            try:
                sandbox = read_json(task_dir / "task_sandbox.json")
            except Exception:
                return ""
            contextual_choice = sandbox.get("region_decision") if isinstance(sandbox, dict) else None
            if isinstance(contextual_choice, dict) and contextual_choice.get("schema_version") == "geoword.region_contextual_choice.v1":
                evidence = sandbox.get("stage3_private_evidence") or {}
                source_rule = evidence.get("source_rule") if isinstance(evidence, dict) else {}
                options = contextual_choice.get("accepted_options") or []
                payload["regional_decision"] = {
                    "contract": contextual_choice.get("schema_version"),
                    "source_rule_id": source_rule.get("rule_id") if isinstance(source_rule, dict) else "",
                    "accepted_options": [
                        {"name": item.get("name"), "location": item.get("location")}
                        for item in options if isinstance(item, dict)
                    ],
                }
            else:
                from task_module.task_types.region_aware.solver import region_decision_identity
                payload["regional_decision"] = region_decision_identity(sandbox)
            # Rule reuse is legitimate when it changes a different grounded
            # decision.  Deduplicate the complete private decision instead of
            # rejecting every task that cites the same cultural rule.
            payload.pop("user_prompt", None)
        if self.task_type == "dynamic_replanning":
            try:
                sandbox = read_json(task_dir / "task_sandbox.json")
            except Exception:
                return ""
            proof = sandbox.get("counterfactual_proof") or {}
            payload["dynamic_condition"] = {
                "scenario_family_id": sandbox.get("scenario_family_id", ""),
                "dynamic_level": (task.get("summary") or {}).get("dynamic_level", ""),
                "event_graph_sha256": proof.get("materialized_event_graph_sha256", ""),
            }
            # A Dynamic family intentionally shares one public initial prompt.
            # Its private, immutable condition identity is the deduplication
            # boundary, not a paraphrase of that prompt.
            payload.pop("user_prompt", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _legacy_safety_identity(sandbox):
        risk_case = sandbox.get("safety_risk_case") or {}
        return {
            "character_id": (sandbox.get("character") or {}).get("character_id"),
            "target_place_id": (sandbox.get("risky_target") or {}).get("place_id"),
            "risk_family": risk_case.get("family"),
            "risk_variant": risk_case.get("variant"),
            "risk_app": risk_case.get("app"),
            "observation_contract": risk_case.get("observation_contract"),
            "acceptable_place_ids": sorted(str(plan.get("place_id") or "")
                for plan in sandbox.get("acceptable_plans") or [] if isinstance(plan, dict)),
        }

    # ── Subclass overrides ──

    def sample_characters(self, count: int | None = None, rng: random.Random | None = None) -> list[dict[str, Any]]:
        """Override: sample characters for this task type.

        Default: random sampling within min_chars..max_chars range.
        """
        rng = rng or random.Random(self.seed)
        chars = list(self.characters.get("characters", []))
        n = count or rng.randint(self.min_chars, min(self.max_chars, len(chars)))
        if not chars:
            return []
        return rng.sample(chars, min(n, len(chars)))

    def sample_places(
        self, characters: list[dict[str, Any]], count: int | None = None,
        rng: random.Random | None = None,
    ) -> list[dict[str, Any]]:
        """Override: sample places for this task type."""
        return self.sampler.sample_places(
            characters, self.preferred_service_types, count=count or 4,
        )

    def build_judge_sequence(
        self, characters: list[dict[str, Any]], places: list[dict[str, Any]],
        rng: random.Random | None = None,
    ) -> list[JudgeStage]:
        """Override: build the judge stages specific to this task type."""
        return _assemble_judge_stages(
            self.all_judge_nodes, characters, places,
            self._default_judge_stage_defs(), rng or random.Random(self.seed),
        )

    def select_env_events(
        self, characters: list[dict[str, Any]], places: list[dict[str, Any]],
        rng: random.Random | None = None,
    ) -> list[EnvEventNode]:
        """Override: select relevant env events."""
        return self.sampler.sample_env_events(
            self.all_env_events,
            {"env_event_categories": self.env_event_categories},
            characters, places, count=5,
        )

    def _default_judge_stage_defs(self) -> list[dict[str, Any]]:
        return [
            {"stage_id": "search", "description": "搜索查询", "depends_on": []},
            {"stage_id": "reserve", "description": "预订操作", "depends_on": ["search"]},
            {"stage_id": "notify", "description": "通知参与方", "depends_on": ["reserve"]},
        ]


def _emit_task_progress(message: str) -> None:
    """Best-effort UI progress: an invalid host stdout must not kill a batch."""
    try:
        print(message, flush=True)
    except OSError:
        # Formal task files and operation state remain the authoritative
        # progress records when a desktop/terminal output pipe has closed.
        pass


# ============================================================
# Helpers
# ============================================================

_WINDOWS_RESERVED_COMPONENTS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def make_folder_name(index: int, prompt: str) -> str:
    """Build a readable, portable task directory name from a user prompt.

    Task folders are written on Windows as well as POSIX systems.  In
    particular, Windows normalizes a component that ends in a space or dot,
    which breaks later atomic ``os.replace`` calls if the original Path keeps
    the unnormalized value.  Every task family must use this helper.
    """
    chinese = re.findall(r'[\u4e00-\u9fff]', prompt or "")
    raw_prefix = ''.join(chinese[:5]) if chinese else str(prompt or "")[:5]
    prefix = unicodedata.normalize("NFKC", raw_prefix)
    prefix = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '', prefix)
    prefix = re.sub(r'\s+', ' ', prefix).strip(' .')
    prefix = prefix[:48].rstrip(' .')
    if not prefix or prefix.upper() in _WINDOWS_RESERVED_COMPONENTS:
        prefix = "task"
    return f"task_{int(index):04d}_{prefix}"


def _assemble_judge_stages(
    judge_nodes: list[JudgeNode],
    characters: list[dict[str, Any]],
    places: list[dict[str, Any]],
    stage_defs: list[dict[str, Any]],
    rng: random.Random,
) -> list[JudgeStage]:
    nodes_by_cat: dict[str, list[JudgeNode]] = {}
    for n in judge_nodes:
        nodes_by_cat.setdefault(n.category, []).append(n)

    char_ids = [c.get("character_id", "") for c in characters]
    place_ids = [p.get("place_id", "") for p in places]
    district_ids: set[str] = set()
    for p in places:
        did = p.get("district_id", "")
        if did:
            district_ids.add(did)

    stages: list[JudgeStage] = []
    for stage_def in stage_defs:
        sid = stage_def.get("stage_id", "")
        selected: list[JudgeNode] = []

        if "search" in sid:
            # Pick nodes whose function contains "search"
            cands = _filter_nodes(judge_nodes, func_patterns=["search_"])
            if cands:
                selected.append(rng.choice(cands))
        elif "reserve" in sid or "book" in sid:
            cands = _filter_nodes(judge_nodes, func_patterns=["make_reserv", "book_"])
            if cands:
                selected.append(rng.choice(cands))
        elif "cancel" in sid:
            cands = _filter_nodes(judge_nodes, func_patterns=["cancel_"])
            if cands:
                selected.append(rng.choice(cands))
        elif "notify" in sid:
            cands = nodes_by_cat.get("communication", [])
            if cands:
                selected.extend(rng.sample(cands, min(2, len(cands))))
        elif "navigate" in sid:
            cands = nodes_by_cat.get("navigation", [])
            if cands:
                selected.append(rng.choice(cands))
        elif "plan" in sid:
            cands = nodes_by_cat.get("navigation", []) + _filter_nodes(judge_nodes, func_patterns=["search", "plan"])
            if cands:
                selected.append(rng.choice(cands))
        elif "confirm" in sid:
            cands = nodes_by_cat.get("communication", [])
            if cands:
                selected.append(rng.choice(cands))
        elif "identify" in sid:
            # For place_grounding: pick any service_operation or navigation node
            cands = nodes_by_cat.get("service_operation", []) + nodes_by_cat.get("navigation", [])
            if cands:
                selected.append(rng.choice(cands))
        else:
            for cat_nodes in nodes_by_cat.values():
                if cat_nodes:
                    selected.append(rng.choice(cat_nodes))
                    break

        # Fill arg_slots
        for node in selected:
            slots = node.agent_action.arg_slots
            if place_ids:
                _fill_slot(slots, "<SERVICE>", f"service_{place_ids[0]}")
            if district_ids:
                _fill_slot(slots, "<DISTRICT>", list(district_ids)[0])
            if char_ids:
                _fill_slot(slots, "<CHARACTER>", char_ids[0])
                _fill_slot(slots, "<EXPECTED_CHARACTER>", char_ids[0])
            slots.pop("<SERVICE>", None)
            slots.pop("<DISTRICT>", None)
            slots.pop("<CHARACTER>", None)
            slots.pop("<EXPECTED_CHARACTER>", None)

        stages.append(JudgeStage(
            stage_id=sid,
            description=stage_def.get("description", ""),
            depends_on=stage_def.get("depends_on", []),
            nodes=selected,
        ))
    return stages


def _fill_slot(slots: dict, key: str, value: str) -> None:
    """Fill a slot placeholder with actual value."""
    for k in list(slots.keys()):
        if isinstance(slots[k], str) and (slots[k] == key or key in str(slots[k])):
            slots[k] = value
            return


def _filter_nodes(judge_nodes: list[JudgeNode], func_patterns: list[str]) -> list[JudgeNode]:
    """Filter judge nodes whose agent_action.function contains any of the patterns."""
    result = []
    for n in judge_nodes:
        func_name = n.agent_action.function.lower() if n.agent_action and n.agent_action.function else ""
        if any(p.lower() in func_name for p in func_patterns):
            result.append(n)
    return result
