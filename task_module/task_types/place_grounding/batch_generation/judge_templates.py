"""Instance-specific judge flow templates for batch grounding tasks."""

from __future__ import annotations

from typing import Any


def build_grounding_judge_override(example: dict[str, Any]) -> dict[str, Any]:
    """Build a rich judge_sequence_override for a generated example."""
    gold = example["gold"]
    scope = example.get("candidate_scope", {})
    example_id = example["example_id"]
    granularity = example.get("acceptable_granularity", "exact_poi")
    candidate_id = gold.get("candidate_id") or gold.get("place_id", "")
    anchor = (gold.get("anchor_places") or [gold.get("place_name", "")])[0]
    search_args = _search_args(scope, anchor)
    select_checks = [
        _check("action_executed", weight="hard"),
        _check(
            "field_equals",
            field="args.candidate_id",
            expected=candidate_id,
            weight="hard" if granularity == "exact_poi" else "soft",
        ),
    ]
    radius = gold.get("acceptable_radius_m")
    if granularity != "exact_poi" and isinstance(radius, int) and radius > 0:
        select_checks.append(
            _check(
                "geo_within_radius",
                field="response.selected.location",
                expected=gold.get("coordinate", {}),
                tolerance={"radius_m": radius},
                weight="hard" if granularity != "exact_poi" else "soft",
            )
        )

    final_mentions = list(
        dict.fromkeys(
            [
                gold.get("place_name", ""),
                *(example.get("evaluation", {}).get("must_mention", []) or []),
            ]
        )
    )
    return {
        "schema_version": "curated.judge.v1",
        "completion_policy": "key_nodes",
        "stages": [
            {
                "stage_id": _stage_id("retrieve", search_args),
                "description": _search_description(search_args),
                "depends_on": [],
                "nodes": [
                    {
                        "node_id": f"jcp_{example_id}_search",
                        "category": "information_lookup",
                        "agent_action": "MapApp.search_grounding_candidates",
                        "arg_slots": search_args,
                        "description_zh": "模型应先把图文线索转化为地图字段检索；该节点用于诊断候选召回质量。",
                        "label_zh": "检索定位候选",
                        "role": "search",
                        "verdict_target": "candidate_retrieval",
                        "is_key": False,
                        "depends_on": [],
                        "hard_checks": [],
                        "soft_checks": [
                            _check("action_executed", weight="soft"),
                            _check("field_not_empty", field="response.candidates", weight="soft"),
                            _check("field_contains", field="response.candidates[].name", expected=anchor, weight="soft"),
                        ],
                    }
                ],
            },
            {
                "stage_id": "select_grounded_location",
                "description": f"提交最终 grounding 候选：{gold.get('place_name', '')}",
                "depends_on": [_stage_id("retrieve", search_args)],
                "nodes": [
                    {
                        "node_id": f"jcp_{example_id}_select",
                        "category": "information_lookup",
                        "agent_action": "MapApp.select_grounded_location",
                        "arg_slots": {
                            "candidate_id": candidate_id,
                            "rationale": "<grounding_rationale>",
                        },
                        "description_zh": _select_description(granularity),
                        "label_zh": "选择定位结果",
                        "role": "locate",
                        "verdict_target": "final_grounded_location",
                        "is_key": True,
                        "depends_on": [],
                        "hard_checks": [item for item in select_checks if item["weight"] == "hard"],
                        "soft_checks": [item for item in select_checks if item["weight"] == "soft"],
                    }
                ],
            },
            {
                "stage_id": "return_grounding_result",
                "description": "将定位结果、关键线索和不确定性返回给用户",
                "depends_on": ["select_grounded_location"],
                "nodes": [
                    {
                        "node_id": f"jcp_{example_id}_respond",
                        "category": "communication",
                        "agent_action": "UserInterfaceApp.send_message_to_user",
                        "arg_slots": {"content": f"<grounding_result:{gold.get('place_name', '')}>"},
                        "description_zh": "模型需要把工具层定位结果转化为用户可见回答。",
                        "label_zh": "最终用户回复",
                        "role": "respond",
                        "verdict_target": "final_user_response",
                        "is_key": True,
                        "depends_on": [],
                        "hard_checks": [
                            _check("action_executed", weight="hard"),
                            _check("field_contains", field="args.content", expected=gold.get("place_name", ""), weight="hard"),
                        ],
                        "soft_checks": [
                            _check(
                                "field_contains",
                                field="args.content",
                                expected=",".join(item for item in final_mentions if item),
                                weight="soft",
                            )
                        ],
                    }
                ],
            },
        ],
    }


def _search_args(scope: dict[str, Any], anchor: str) -> dict[str, Any]:
    args: dict[str, Any] = {
        "keyword": anchor,
        "city_id": scope.get("city_id", ""),
        "limit": 8,
    }
    allowed = scope.get("allowed_place_types") or []
    area_hints = scope.get("area_hints") or []
    address_hints = scope.get("address_hints") or []
    nearby = scope.get("nearby_anchors") or []
    if allowed:
        args["category"] = allowed[0]
    if area_hints:
        args["area_hint"] = area_hints[0]
    if address_hints:
        args["address_hint"] = address_hints[0]
    if nearby:
        args["near"] = nearby[0]
        args["near_radius_m"] = 1200
    return args


def _stage_id(prefix: str, args: dict[str, Any]) -> str:
    if args.get("near"):
        return f"{prefix}_near_anchor_candidates"
    if args.get("address_hint"):
        return f"{prefix}_address_candidates"
    if args.get("category"):
        return f"{prefix}_category_candidates"
    return f"{prefix}_grounding_candidates"


def _search_description(args: dict[str, Any]) -> str:
    fields = [key for key in ["category", "area_hint", "address_hint", "near"] if args.get(key)]
    return "根据图文线索使用地图字段检索候选：" + ("、".join(fields) if fields else "keyword")


def _select_description(granularity: str) -> str:
    if granularity == "exact_poi":
        return "exact_poi 任务以 candidate_id 命中为主判定，坐标距离仅作诊断。"
    if granularity == "district_area":
        return "区域级 grounding 允许使用空间/区域命中作为主判定。"
    return "附近/观察者位置 grounding 以候选位置和空间半径共同判定。"


def _check(
    check_type: str,
    field: str = "",
    expected: Any = None,
    tolerance: dict[str, Any] | None = None,
    weight: str = "hard",
) -> dict[str, Any]:
    return {
        "check_type": check_type,
        "field": field,
        "expected": expected,
        "tolerance": tolerance,
        "weight": weight,
    }
