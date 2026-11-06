"""Export curated examples into the benchmark task folder format."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.io import write_json

from task_module._shared.base_generator import make_folder_name
from task_module._shared.batch_state import existing_gold_ids, next_task_index
from task_module._shared.output_writer import write_task_folder
from task_module.curated.schema import load_curated_examples, load_task_specs, pipeline_root, resolve_asset_path
from task_module.task_schema import (
    AgentAction,
    BenchmarkTask,
    CheckItem,
    JudgeNode,
    JudgeStage,
    WorldSliceSummary,
)


def generate_tasks_from_specs(
    generator: Any,
    specs_path: str,
    count: int | None = None,
    *,
    env_seed: int | None = None,
    export_task_spec: bool = True,
) -> list[BenchmarkTask]:
    """Replay approved task specs into benchmark folders (no LLM / streetview)."""
    from task_module._shared.sampler import TaskSampler

    payload = load_task_specs(generator.task_type, specs_path, city_id=getattr(generator, "city_id", None))
    examples = payload["examples"]
    gold_field = "place_id" if generator.task_type == "place_grounding" else "service_id"
    used_gold_ids = existing_gold_ids(generator.output_dir, gold_field)
    if used_gold_ids:
        examples = [
            example for example in examples
            if (example.get("gold") or {}).get(gold_field, "") not in used_gold_ids
        ]
    if count is not None:
        examples = examples[:count]

    sampler = TaskSampler(generator.world, generator.characters, seed=env_seed if env_seed is not None else generator.seed)

    tasks: list[BenchmarkTask] = []
    generated_at = datetime.now(timezone.utc).isoformat()
    start_index = next_task_index(generator.output_dir)
    print(
        f"[{generator.task_type}] Replaying {len(examples)} novel specs from {specs_path} "
        f"at index {start_index}",
        flush=True,
    )
    for index, example in enumerate(examples, start=start_index):
        places = _places_for_example(generator.world, example)
        characters = _characters_for_example(generator.characters, example)
        # Curated static tasks must not accidentally regain an event overlay
        # when they are replayed from task specifications.
        env_events = []
        task = write_evidence_task_from_example(
            generator,
            example,
            index,
            env_events=env_events,
            source_payload=payload,
            generated_at=generated_at,
            export_task_spec=export_task_spec,
        )
        tasks.append(task)
        print(f"  [{generator.task_type}] {index}/{len(examples)} {task.metadata.get('folder', '')} OK", flush=True)
    return tasks


def export_specs_bundle(
    *,
    task_type: str,
    city_id: str,
    variant: str,
    schema_version: str,
    examples: list[dict[str, Any]],
    output_path: str | Path,
) -> str:
    """Write a task_specs.json bundle from generated examples."""
    payload = {
        "schema_version": schema_version,
        "task_type": task_type,
        "city_id": city_id,
        "variant": variant,
        "examples": examples,
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, payload)
    return str(path)


def generate_curated_tasks(generator: Any, count: int | None = None) -> list[BenchmarkTask]:
    """Generate tasks for a generator from curated examples."""
    payload = load_curated_examples(
        generator.task_type,
        getattr(generator, "curated_examples_dir", ""),
        city_id=getattr(generator, "city_id", None),
    )
    examples = payload["examples"]
    gold_field = "place_id" if generator.task_type == "place_grounding" else "service_id"
    used_gold_ids = existing_gold_ids(generator.output_dir, gold_field)
    if used_gold_ids:
        examples = [
            example for example in examples
            if (example.get("gold") or {}).get(gold_field, "") not in used_gold_ids
        ]
    if count is not None:
        examples = examples[:count]

    task_type_dir = Path(generator.output_dir) / "tasks"
    task_type_dir.mkdir(parents=True, exist_ok=True)

    tasks: list[BenchmarkTask] = []
    generated_at = datetime.now(timezone.utc).isoformat()
    start_index = next_task_index(generator.output_dir)
    for index, example in enumerate(examples, start=start_index):
        task = write_evidence_task_from_example(
            generator,
            example,
            index,
            env_events=[],
            source_payload=payload,
            generated_at=generated_at,
        )
        tasks.append(task)

    print(f"[{generator.task_type}] Generated {len(tasks)} curated tasks")
    return tasks


def write_evidence_task_from_example(
    generator: Any,
    example: dict[str, Any],
    index: int,
    *,
    env_events: list[Any] | None = None,
    source_payload: dict[str, Any] | None = None,
    generated_at: str | None = None,
    export_task_spec: bool = True,
) -> BenchmarkTask:
    """Write one evidence-backed example into a benchmark task folder."""
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    judge_payload = _resolve_judge_payload(example, generator.task_type)
    task = _build_task(
        generator,
        example,
        index,
        generated_at,
        judge_payload,
        env_events=env_events or [],
    )
    folder_name = make_folder_name(index, task.user_prompt)
    task.metadata["folder"] = folder_name
    task_type_dir = Path(generator.output_dir) / "tasks"
    task_type_dir.mkdir(parents=True, exist_ok=True)
    task_dir = task_type_dir / folder_name
    write_task_folder(
        task_dir,
        task,
        task.env_event_details,
        task.judge_stages,
        task.world_slice_summary,
    )
    write_json(task_dir / "judge_sequence.json", judge_payload)
    _write_gold_file(task_dir, task.task_type, example)
    provenance = _curated_source_payload(example, source_payload or {"schema_version": "inline"})
    write_json(task_dir / "spec_provenance.json", provenance)
    write_json(task_dir / "curated_source.json", provenance)
    if export_task_spec:
        write_json(task_dir / "task_spec.json", example)
    generator.finalize_task_folder(task_dir)
    return task


def _build_task(
    generator: Any,
    example: dict[str, Any],
    index: int,
    generated_at: str,
    judge_payload: dict[str, Any],
    env_events: list[Any] | None = None,
) -> BenchmarkTask:
    task_type = generator.task_type
    places = _places_for_example(generator.world, example)
    characters = _characters_for_example(generator.characters, example)
    ws_summary = _world_slice_summary(generator.world, characters, places, example)
    judge_stages = (
        _grounding_judge_stages(example)
        if task_type == "place_grounding"
        else _discovery_judge_stages(example)
    )

    payload_stages = judge_payload.get("stages", [])
    judge_node_count = sum(len(s.get("nodes", [])) for s in payload_stages)
    judge_check_count = sum(
        len(n.get("hard_checks", [])) + len(n.get("soft_checks", []))
        for s in payload_stages
        for n in s.get("nodes", [])
    )
    key_node_count = sum(
        1 for s in payload_stages for n in s.get("nodes", []) if n.get("is_key")
    )
    env_event_details = list(env_events or [])

    summary = {
        "curated": True,
        "evidence_backed": True,
        "example_id": example["example_id"],
        "character_count": len(characters),
        "place_count": len(places),
        "judge_stage_count": len(payload_stages),
        "judge_node_count": judge_node_count,
        "key_node_count": key_node_count,
        "total_judge_checks": judge_check_count,
        "completion_policy": judge_payload.get("completion_policy", "key_nodes"),
        "gold_summary": _gold_summary(task_type, example),
    }
    if task_type == "place_grounding":
        summary["input_assets"] = {
            "image_path": example["image_path"],
            "resolved_image_path": str(resolve_asset_path(example["image_path"], pipeline_root())),
            "prompt_reference_image_path": example.get("prompt_reference_image_path", ""),
            "resolved_prompt_reference_image_path": (
                str(resolve_asset_path(example["prompt_reference_image_path"], pipeline_root()))
                if example.get("prompt_reference_image_path")
                else ""
            ),
        }
        summary["review_status"] = example.get("review_status", "")
        summary["prompt_language"] = example.get("prompt_language", "")
        summary["review_language"] = example.get("review_language", "")
        summary["user_prompt_translation"] = example.get("user_prompt_translation", "")
        summary["review_translation"] = example.get("review_translation", {})
        summary["grounding_target_type"] = example.get("grounding_target_type", "")
        summary["acceptable_granularity"] = example.get("acceptable_granularity", "")
        summary["candidate_scope"] = example.get("candidate_scope", {})
        summary["difficulty_profile"] = example.get("difficulty_profile", {})
        summary["quality_profile"] = example.get("quality_profile", example.get("difficulty_profile", {}))
        summary["ambiguity_landscape"] = example.get("ambiguity_landscape", {})
        summary["quality_gate"] = example.get("quality_gate", {})
        summary["env_event_count"] = len(env_event_details)
    elif task_type == "place_discovery":
        summary["review_status"] = example.get("review_status", "")
        summary["prompt_language"] = example.get("prompt_language", "")
        summary["review_language"] = example.get("review_language", "")
        summary["user_prompt_translation"] = example.get("user_prompt_translation", "")
        summary["review_translation"] = example.get("review_translation", {})
        summary["service_type"] = example.get("service_type", "")
        summary["start"] = example.get("start", {})
        summary["ranking_criteria"] = example.get("ranking_criteria", [])
        summary["candidate_scope"] = example.get("candidate_scope", {})
        summary["difficulty_profile"] = example.get("difficulty_profile", {})
        summary["quality_profile"] = example.get("quality_profile", example.get("difficulty_profile", {}))
        summary["search_landscape"] = example.get("search_landscape", {})
        summary["quality_gate"] = example.get("quality_gate", {})
        summary["distractor_count"] = len(example.get("distractors", []))
        summary["env_event_count"] = len(env_event_details)

    return BenchmarkTask(
        task_id=f"task_{generator.city_id}_{task_type}_curated_{index:04d}",
        task_type=task_type,
        difficulty=example.get("difficulty", "medium"),
        city_id=generator.city_id,
        variant=getattr(generator, "variant", ""),
        generated_at=generated_at,
        user_prompt=example["user_prompt"],
        summary=summary,
        world_slice=_world_slice(characters, places),
        world_slice_summary=ws_summary,
        involved_env_events=[_env_node_id(e) for e in env_event_details],
        env_event_details=env_event_details,
        judge_stages=judge_stages,
        metadata={
            "seed": getattr(generator, "seed", None),
            "task_type": task_type,
            "curated_example_id": example["example_id"],
        },
    )


JUDGE_SCHEMA_VERSION = "curated.judge.v1"


def _resolve_judge_payload(example: dict[str, Any], task_type: str) -> dict[str, Any]:
    """Return the editable rich judge payload.

    If the curated example carries a ``judge_sequence_override`` (written back by
    the visualization editor), it wins so manual edits survive regeneration.
    Otherwise the default rule-derived stages are serialized.
    """
    override = example.get("judge_sequence_override")
    if isinstance(override, dict) and isinstance(override.get("stages"), list) and override["stages"]:
        payload = dict(override)
        payload.setdefault("schema_version", JUDGE_SCHEMA_VERSION)
        payload.setdefault("completion_policy", "key_nodes")
        return payload

    stages = (
        _grounding_judge_stages(example)
        if task_type == "place_grounding"
        else _discovery_judge_stages(example)
    )
    return _serialize_stages_rich(stages, task_type)


def _serialize_stages_rich(stages: list[JudgeStage], task_type: str) -> dict[str, Any]:
    return {
        "schema_version": JUDGE_SCHEMA_VERSION,
        "completion_policy": "key_nodes",
        "stages": [_serialize_stage(stage, task_type) for stage in stages],
    }


def _serialize_stage(stage: JudgeStage, task_type: str) -> dict[str, Any]:
    return {
        "stage_id": stage.stage_id,
        "description": stage.description,
        "depends_on": list(stage.depends_on),
        "nodes": [_serialize_node(node, task_type) for node in stage.nodes],
    }


def _serialize_node(node: JudgeNode, task_type: str) -> dict[str, Any]:
    action = node.agent_action
    hard_checks: list[dict[str, Any]] = []
    soft_checks: list[dict[str, Any]] = []
    for check in node.checks:
        item = _serialize_check(check)
        (hard_checks if check.weight == "hard" else soft_checks).append(item)
    ui = _node_ui_metadata(node, task_type)
    return {
        "node_id": node.node_id,
        "category": node.category,
        "agent_action": f"{action.app}.{action.function}" if action else "",
        "arg_slots": dict(action.arg_slots) if action else {},
        "description_zh": node.description_zh,
        "label_zh": ui["label_zh"],
        "role": ui["role"],
        "verdict_target": ui["verdict_target"],
        "is_key": _is_key_node(node.node_id, task_type),
        "depends_on": list(node.depends_on),
        "hard_checks": hard_checks,
        "soft_checks": soft_checks,
    }


def _serialize_check(check: CheckItem) -> dict[str, Any]:
    return {
        "check_type": check.check_type,
        "field": check.field,
        "expected": check.expected,
        "tolerance": check.tolerance,
        "weight": check.weight,
    }


def _is_key_node(node_id: str, task_type: str) -> bool:
    """Key nodes drive task-completion judging (GAIA2-style oracle steps)."""
    if task_type == "place_grounding":
        return node_id.endswith("_select") or node_id.endswith("_respond")
    return node_id.endswith("_search") or node_id.endswith("_detail") or node_id.endswith("_rank")


def _node_ui_metadata(node: JudgeNode, task_type: str) -> dict[str, str]:
    """Compact UI metadata for flowchart nodes."""
    node_id = node.node_id
    action = node.agent_action
    action_name = f"{action.app}.{action.function}" if action else ""

    label = "评判节点"
    role = "fallback"
    verdict_target = "supporting_step"

    if task_type == "place_grounding":
        if node_id.endswith("_search"):
            role = "search"
            verdict_target = "candidate_retrieval"
            label = "检索定位候选"
        elif node_id.endswith("_select"):
            role = "locate"
            verdict_target = "final_grounded_location"
            label = "选择定位结果"
        elif node_id.endswith("_respond"):
            role = "respond"
            verdict_target = "final_user_response"
            label = "最终用户回复"
        elif node_id.endswith("_explain"):
            role = "evidence"
            label = "说明不确定性" if _is_person_grounding_id(node_id) else "交叉验证线索"
        elif node_id.endswith("_nearby"):
            role = "search"
            label = "附近需求检索" if _is_person_grounding_id(node_id) else "确认区域上下文"
    else:
        if node_id.endswith("_area"):
            role = "locate"
            label = "确定搜索范围"
        elif node_id.endswith("_search"):
            role = "filter"
            verdict_target = "candidate_service_retrieval"
            label = "检索服务候选"
        elif node_id.endswith("_detail"):
            role = "evidence"
            verdict_target = "service_detail_inspection"
            label = "查看商户详情"
        elif node_id.endswith("_exclude"):
            role = "exclude"
            label = "排除干扰项"
        elif node_id.endswith("_compare"):
            role = "evidence"
            verdict_target = "candidate_tradeoff_comparison"
            label = "比较候选取舍"
        elif node_id.endswith("_rank"):
            role = "rank"
            verdict_target = "final_recommendation"
            label = "最终推荐"

    if label == "评判节点" and "send_message_to_user" in action_name:
        label = "结果说明"
        role = "rank"
    return {
        "label_zh": label,
        "role": role,
        "verdict_target": verdict_target,
    }


def _is_person_grounding_id(node_id: str) -> bool:
    return "dongfang_hospital" in node_id or "fuxing_park" in node_id


def _grounding_judge_stages(example: dict[str, Any]) -> list[JudgeStage]:
    gold = example["gold"]
    anchor = gold["anchor_places"][0]
    district = gold.get("district_id", "") or gold.get("district_name", "")
    target_type = example.get("grounding_target_type", "poi")
    granularity = example.get("acceptable_granularity", "exact_poi")
    place_id = gold.get("place_id", "")
    character_id = example.get("character_id", "")
    if gold.get("candidate_id"):
        candidate_id = gold["candidate_id"]
    elif place_id:
        candidate_id = place_id
    elif character_id:
        candidate_id = f"map_location:{character_id}"
    else:
        raise ValueError(
            f"Grounding example {example.get('example_id', '<unknown>')} must reference "
            "a real MapApp candidate via gold.place_id or character_id."
        )
    example_id = example["example_id"]
    select_checks = [
        CheckItem(
            check_type="action_executed",
            weight="hard",
        ),
        CheckItem(
            check_type="field_equals",
            field="args.candidate_id",
            expected=candidate_id,
            weight="hard" if granularity == "exact_poi" else "soft",
        ),
    ]
    radius = gold.get("acceptable_radius_m")
    if granularity != "exact_poi" and isinstance(radius, int) and radius > 0:
        select_checks.append(
            CheckItem(
                check_type="geo_within_radius",
                field="response.selected.location",
                expected=gold["coordinate"],
                tolerance={"radius_m": radius},
                weight="hard" if granularity != "exact_poi" or target_type == "observer_location" else "soft",
            )
        )
    select_checks.append(CheckItem("field_contains", "response.selected.name", anchor, weight="soft"))
    select_desc = (
        "最终关键节点：模型必须通过 MapApp 选择正确候选。exact_poi 任务以 candidate_id 命中为主判定；"
        "距离误差仅作为诊断信息。"
        if granularity == "exact_poi"
        else "最终关键节点：模型必须通过 MapApp 选择候选位置；区域/附近类任务可使用空间容忍度判定。"
    )
    final_mentions = [gold["place_name"], *example.get("evaluation", {}).get("must_mention", [])]
    final_mentions = list(dict.fromkeys([item for item in final_mentions if item]))
    return [
        JudgeStage(
            stage_id="retrieve_grounding_candidates",
            description="根据图文线索从 MapApp 统一候选索引中检索可能位置",
            depends_on=[],
            nodes=[
                JudgeNode(
                    node_id=f"jcp_{example_id}_search",
                    category="information_lookup",
                    agent_action=AgentAction(
                        app="MapApp",
                        function="search_grounding_candidates",
                        arg_slots={
                            "keyword": anchor,
                            "area_hint": district,
                            "entity_type_hint": "",
                            "limit": 8,
                        },
                    ),
                    checks=[
                        CheckItem(check_type="action_executed", weight="soft"),
                        CheckItem("field_not_empty", "response.candidates", weight="soft"),
                        CheckItem("field_contains", "response.candidates[].name", anchor, weight="soft"),
                    ],
                    description_zh="模型应先基于图片和文本线索检索候选位置；该节点用于诊断召回质量，不直接决定完成。",
                )
            ],
        ),
        JudgeStage(
            stage_id="select_grounded_location",
            description=f"从候选位置中选择最终 grounding 结果：{gold['place_name']}",
            depends_on=["retrieve_grounding_candidates"],
            nodes=[
                JudgeNode(
                    node_id=f"jcp_{example_id}_select",
                    category="information_lookup",
                    agent_action=AgentAction(
                        app="MapApp",
                        function="select_grounded_location",
                        arg_slots={
                            "candidate_id": candidate_id,
                            "rationale": "<grounding_rationale>",
                        },
                    ),
                    checks=select_checks,
                    description_zh=select_desc,
                )
            ],
        ),
        JudgeStage(
            stage_id="return_grounding_result",
            description="将定位结果、区域线索和必要的不确定性说明返回给用户",
            depends_on=["select_grounded_location"],
            nodes=[
                JudgeNode(
                    node_id=f"jcp_{example_id}_respond",
                    category="communication",
                    agent_action=AgentAction(
                        app="UserInterfaceApp",
                        function="send_message_to_user",
                        arg_slots={
                            "content": f"<grounding_result:{gold['place_name']}>",
                        },
                    ),
                    checks=[
                        CheckItem("action_executed", weight="hard"),
                        CheckItem("field_contains", "args.content", gold["place_name"], weight="hard"),
                        CheckItem(
                            "field_contains",
                            "args.content",
                            ",".join(final_mentions),
                            weight="soft",
                        ),
                    ],
                    description_zh="最终关键节点：模型需要把定位结果转化为用户可见回答，避免只在工具层选对但没有完成用户任务。",
                )
            ],
        ),
    ]


def _discovery_judge_stages(example: dict[str, Any]) -> list[JudgeStage]:
    service_type = example.get("service_type", "restaurant_reservation")
    start = example.get("start", {})
    ranking_criteria = example.get("ranking_criteria", [])
    gold_candidates = example.get("gold", {}).get("candidates", [])
    district_id = start.get("district_id", "")
    first_candidate = gold_candidates[0]
    gold_name = first_candidate["name"]
    gold_location = first_candidate.get("coordinate", {})
    example_id = example["example_id"]
    all_candidate_public = [
        {"name": candidate.get("name", ""), "location": candidate.get("coordinate", {})}
        for candidate in [*gold_candidates, *example.get("distractors", [])]
        if candidate.get("name")
    ]
    must_mentions = example.get("evaluation", {}).get("must_mention", [])
    soft_mentions = ",".join(must_mentions)
    final_expected = ",".join(
        [candidate.get("name", "") for candidate in gold_candidates if candidate.get("name")]
    ) or gold_name

    area_stage_id = "define_discovery_scope"
    search_stage_id = "search_candidate_services"
    detail_stage_id = "inspect_service_details"
    compare_stage_id = "compare_candidate_tradeoffs"
    rank_stage_id = "return_ranked_recommendation"
    area_desc = f"确认 {start.get('area_name', '用户指定区域')} 的搜索范围和服务类型"
    search_desc = "根据用户约束检索本地服务/商户候选"
    detail_desc = "查看候选商户详情，获取评分、价格、排队、营业状态等证据"
    compare_desc = "比较候选是否满足用户约束，说明推荐与排除理由"
    rank_desc = "返回排序后的推荐地点，并解释其满足用户约束的依据"

    stages = [
        JudgeStage(
            stage_id=area_stage_id,
            description=area_desc,
            depends_on=[],
            nodes=[
                JudgeNode(
                    node_id=f"jcp_{example_id}_area",
                    category="information_lookup",
                    agent_action=AgentAction(
                        app="MapApp",
                        function="search_places",
                        arg_slots={
                            "city_hint": example.get("candidate_scope", {}).get("city_id", ""),
                            "place_type": start.get("place_type", ""),
                            "keyword": start.get("area_name", ""),
                            "limit": 5,
                        },
                    ),
                    checks=[
                        CheckItem("action_executed", weight="hard"),
                        CheckItem("field_not_empty", "response.places", weight="soft"),
                    ],
                    description_zh=area_desc,
                )
            ],
        ),
        JudgeStage(
            stage_id=search_stage_id,
            description=search_desc,
            depends_on=[area_stage_id],
            nodes=[
                JudgeNode(
                    node_id=f"jcp_{example_id}_search",
                    category="service_operation",
                    agent_action=AgentAction(
                        app="LocalServiceApp",
                        function="search_services",
                        arg_slots={
                            "service_type": service_type,
                            "filters": {
                                "constraints": ranking_criteria,
                                "start": start.get("area_name", ""),
                            },
                            "limit": 10,
                        },
                    ),
                    checks=[
                        CheckItem("action_executed", weight="hard"),
                        CheckItem("field_not_empty", "response.results", weight="hard"),
                        CheckItem("field_contains", "response.results[].name", gold_name, weight="soft"),
                    ],
                    description_zh=search_desc,
                )
            ],
        ),
        JudgeStage(
            stage_id=detail_stage_id,
            description=detail_desc,
            depends_on=[search_stage_id],
            nodes=[
                JudgeNode(
                    node_id=f"jcp_{example_id}_detail",
                    category="information_lookup",
                    agent_action=AgentAction(
                        app="LocalServiceApp",
                        function="get_service_detail",
                        arg_slots={
                            "name": gold_name,
                            "location": gold_location,
                        },
                    ),
                    checks=[
                        CheckItem("action_executed", weight="hard"),
                        CheckItem("field_not_empty", "response.detail", weight="hard"),
                        CheckItem("field_contains", "response.detail.name", gold_name, weight="soft"),
                    ],
                    description_zh=detail_desc,
                )
            ],
        ),
        JudgeStage(
            stage_id=compare_stage_id,
            description=compare_desc,
            depends_on=[detail_stage_id],
            nodes=[
                JudgeNode(
                    node_id=f"jcp_{example_id}_compare",
                    category="information_lookup",
                    agent_action=AgentAction(
                        app="LocalServiceApp",
                        function="compare_services",
                        arg_slots={
                            "services": all_candidate_public or [{"name": gold_name, "location": gold_location}],
                            "constraints": ranking_criteria,
                        },
                    ),
                    checks=[
                        CheckItem("field_not_empty", "response.comparisons", weight="soft"),
                        CheckItem("field_contains", "response.comparisons[].name", gold_name, weight="soft"),
                    ],
                    description_zh=compare_desc,
                )
            ],
        ),
        JudgeStage(
            stage_id=rank_stage_id,
            description=rank_desc,
            depends_on=[compare_stage_id],
            nodes=[
                JudgeNode(
                    node_id=f"jcp_{example_id}_rank",
                    category="communication",
                    agent_action=AgentAction(
                        app="UserInterfaceApp",
                        function="send_message_to_user",
                        arg_slots={"content": f"<ranked_recommendation:{gold_name}>"},
                    ),
                    checks=[
                        CheckItem("action_executed", weight="hard"),
                        CheckItem("field_contains", "args.content", final_expected, weight="hard"),
                        CheckItem(
                            "field_contains",
                            "args.content",
                            soft_mentions,
                            weight="soft",
                        ),
                    ],
                    description_zh=rank_desc,
                )
            ],
        ),
    ]
    return stages


def _places_for_example(world: dict[str, Any], example: dict[str, Any]) -> list[dict[str, Any]]:
    ids: list[str] = []
    names: list[str] = []
    if "image_path" in example:
        gold = example.get("gold", {})
        ids.extend(_non_empty([gold.get("place_id")]))
        names.extend(gold.get("anchor_places", []))
    gold = example.get("gold", {})
    for candidate in gold.get("candidates", []):
        ids.extend(_non_empty([candidate.get("place_id")]))
        names.append(candidate.get("name", ""))
    for candidate in example.get("ambiguity_landscape", {}).get("confusable_candidates", []):
        ids.extend(_non_empty([candidate.get("place_id")]))
        names.append(candidate.get("name", ""))
    for candidate in example.get("search_landscape", {}).get("confusable_candidates", []):
        ids.extend(_non_empty([candidate.get("place_id")]))
        names.append(candidate.get("name", ""))

    places = []
    seen: set[str] = set()
    for place_id in ids:
        place = _find_place(world, place_id=place_id)
        if place and place.get("place_id") not in seen:
            places.append(place)
            seen.add(place.get("place_id", ""))
    for name in names:
        place = _find_place(world, name=name)
        pid = place.get("place_id", "") if place else ""
        if place and pid not in seen:
            places.append(place)
            seen.add(pid)
    return places


def _characters_for_example(characters: dict[str, Any], example: dict[str, Any]) -> list[dict[str, Any]]:
    ids = _non_empty([example.get("character_id")])
    names = _non_empty([example.get("character_name")])
    results = []
    for character in characters.get("characters", []):
        if character.get("character_id") in ids or character.get("name") in names:
            results.append(character)
    return results


def _world_slice_summary(
    world: dict[str, Any],
    characters: list[dict[str, Any]],
    places: list[dict[str, Any]],
    example: dict[str, Any],
) -> WorldSliceSummary:
    district_ids = {p.get("district_id", "") for p in places if p.get("district_id")}
    for key in ("district_id", "fallback_district_id"):
        value = example.get("gold", {}).get(key) or example.get("start", {}).get(key)
        if value:
            district_ids.add(value)
    districts = [_find_district(world, did) for did in district_ids]
    return WorldSliceSummary(
        characters=[_character_summary(c) for c in characters],
        places=[_place_summary(p) for p in places],
        districts=[d for d in districts if d],
        corridors=[],
        services=[],
    )


def _world_slice(characters: list[dict[str, Any]], places: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "character_ids": [c.get("character_id") for c in characters if c.get("character_id")],
        "place_ids": [p.get("place_id") for p in places if p.get("place_id")],
        "service_ids": [f"service_{p.get('place_id')}" for p in places if p.get("place_id")],
    }


def _gold_summary(task_type: str, example: dict[str, Any]) -> dict[str, Any]:
    if task_type == "place_grounding":
        gold = example["gold"]
        summary = {
            "candidate_id": gold.get("candidate_id") or gold.get("place_id", ""),
            "place_id": gold.get("place_id", ""),
            "place_name": gold["place_name"],
            "coordinate": gold["coordinate"],
            "anchor_places": gold["anchor_places"],
        }
        if example.get("acceptable_granularity") != "exact_poi":
            summary["acceptable_radius_m"] = gold.get("acceptable_radius_m")
        return summary
    return {
        "top_candidate": example["gold"]["candidates"][0]["name"],
        "candidate_count": len(example["gold"]["candidates"]),
        "distractor_count": len(example.get("distractors", [])),
    }


def _write_gold_file(task_dir: Path, task_type: str, example: dict[str, Any]) -> None:
    file_name = "grounding_gold.json" if task_type == "place_grounding" else "discovery_gold.json"
    write_json(task_dir / file_name, {
        "example_id": example["example_id"],
        "task_type": task_type,
        "service_type": example.get("service_type", ""),
        "start": example.get("start", {}),
        "gold": example.get("gold", {}),
        "evaluation": example.get("evaluation", {}),
        "ranking_criteria": example.get("ranking_criteria", []),
        "ambiguity_landscape": example.get("ambiguity_landscape", {}),
        "distractors": example.get("distractors", []),
    })


def _curated_source_payload(example: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version", ""),
        "source_path": payload.get("_source_path", ""),
        "example": example,
    }


def _find_place(world: dict[str, Any], place_id: str = "", name: str = "") -> dict[str, Any] | None:
    for place in world.get("places", []):
        if place_id and place.get("place_id") == place_id:
            return place
        if name and _normalize_name(place.get("name", "")) == _normalize_name(name):
            return place
    return None


def _find_district(world: dict[str, Any], district_id: str) -> dict[str, Any] | None:
    if not district_id:
        return None
    for district in world.get("districts", []):
        if district.get("district_id") == district_id:
            return {
                "district_id": district.get("district_id", ""),
                "name": district.get("name", ""),
                "tags": district.get("tags", []),
            }
    return None


def _place_summary(place: dict[str, Any]) -> dict[str, Any]:
    return {
        "place_id": place.get("place_id", ""),
        "name": place.get("name", ""),
        "type": place.get("primary_type", ""),
        "district_id": place.get("district_id", ""),
        "location": place.get("location", {}),
        "address": place.get("address", ""),
        "rating": place.get("map_projection", {}).get("rating"),
    }


def _character_summary(character: dict[str, Any]) -> dict[str, Any]:
    return {
        "character_id": character.get("character_id", ""),
        "name": character.get("name", ""),
        "occupation": character.get("occupation", ""),
        "preferences": character.get("preferences", {}),
        "mobility": character.get("mobility", {}),
    }


def _non_empty(values: list[Any]) -> list[str]:
    return [str(v) for v in values if isinstance(v, str) and v.strip()]


def _env_node_id(evt: Any) -> str:
    if isinstance(evt, dict):
        return str(evt.get("node_id", ""))
    return str(getattr(evt, "node_id", ""))


def _normalize_name(value: str) -> str:
    return value.casefold().replace(" ", "").replace("　", "")
