"""Writer contract for turning a fixed itinerary plan into a natural request."""

from __future__ import annotations

import json
from typing import Any


# Chinese review copy. Keep its contract aligned with the English runtime prompt below.
SYSTEM_ITINERARY_NATURAL_ZH = """你的职责是把输入中已经确定的旅行背景、固定事项和活动要求，写成一份自然、真实、可执行的请求。

输入中的每个活动组分为2部分：

1. 「活动意图」
   活动类型和日内时段用于组织自然语言。具体的地点、餐饮和出行要求以已核验的公开条件为准。

2. 「必须公开的地点限制」
   这些条件用于让被测规划助手找到目标地点，必须全部写入题面。
   你可以调整语序和措辞使得表述自然，但不得删除、放宽、加强或新增条件。

`private_gt_references` 只帮助你理解每组活动所对应的地点和行程衔接。其中的地点名称是答案，不得写入题面；只有 `fixed_events` 中的地点名称和时间可以直接公开。

写作要求：

- 用自然的英文写最终题面。将输入中的中文描述转写为英文，避免在同一词语或短语中混用中英文。保留有来源依据的人物和地点原名。
- 钟点使用 24 小时制 `HH:mm` 格式。
- 以真实旅行者的第一人称或自然委托口吻写作。
- 按日期和实际行程顺序组织。
- 同一活动的去程、活动目的、后续安排和返程应组成一段完整需求。
- 可以公开自然的时间锚点，例如“办理入住后”“晚上7点左右”“退房后”。
- 只有出现在「必须公开的地点限制」中的路线时间才是用户限制；可以把它写成清楚、可核验的自然近似值，例如“步行约15分钟”。
- 如果地点同时具有两种本题需要的服务能力，应将其写成一个自然的复合需求，例如“找一家可以买衣服、也能坐下来喝咖啡的店”。
- 如果公开条件指定了地点的主类型，明确写出该地点主要属于该类型。
- 个人活动只用于说明时间如何自然衔接，不得制造新的地点选择要求。
- 如果「必须公开的地点限制」包含金额，必须在对应活动中自然、准确地公开；输入未提供的每日总预算不得自行添加。

输出一个 JSON 对象：
{
  "task": "最终给被测规划助手的完整自然语言题面"
}"""


SYSTEM_ITINERARY_NATURAL = """Your responsibility is to turn the supplied, already-decided trip background, fixed events, and activity requirements into a natural, realistic, and executable request.

Each activity group in the input has two parts:

1. `activity_intent`
   Use the activity type and time of day to organize natural language. Take specific venue, food, and travel requirements from the verified public conditions.

2. `public_place_constraints`
   These conditions let the evaluated planning assistant identify the target place. Every supplied condition must appear in the request. You may change their order and phrasing for fluency, but you must not omit, relax, strengthen, or invent a condition.

`private_gt_references` only helps you understand which place corresponds to each activity group and how the trip fits together. Those place names are answers and must not appear in the request. Only place names and times in `fixed_events` may be disclosed directly.

Writing requirements:

- Write the final task in natural English. Render any Chinese descriptions from the input in English without mixing Chinese and English within a word or phrase. Keep the original spelling of source-backed person and place names.
- Use the 24-hour `HH:mm` format for clock times.
- Write in the first person as a real traveler, or as a natural request made on the traveler's behalf.
- Organize the request by date and actual itinerary order.
- Combine an activity's inbound travel, purpose, follow-on plans, and return travel into one coherent passage.
- You may disclose natural time anchors such as "after checking in", "around 7 p.m.", and "after checking out".
- A route duration is a user constraint only when it appears in `public_place_constraints`. Express such a duration as a clear, verifiable, natural approximation, such as "about a 15-minute walk".
- If a place must provide two services for this activity, express one natural combined need, such as "a place where we can shop for clothes and also sit down for coffee".
- If a public condition specifies a primary place type, state that the venue is primarily a place of that type.
- Personal activities only explain how time flows; do not turn them into new place-selection requirements.
- If `public_place_constraints` contains a monetary amount, disclose it naturally and accurately for that activity. Do not invent an overall daily budget when none is supplied.

Return exactly one JSON object with this shape:
{
  "task": "The complete natural-language task for the evaluated planning assistant"
}"""


def build_natural_user_input(
    character: dict[str, Any],
    hotel: dict[str, Any],
    menus: list[dict[str, Any]],
    num_days: int,
    scenario: dict[str, Any],
    public_requirements: list[dict[str, Any]],
) -> str:
    """Build the reusable English JSON input consumed by the writer."""
    dag = scenario.get("construction_dag") or {}
    days = list(dag.get("days") or [])
    weekday_by_day = {
        int(item.get("day") or 0): str(item.get("weekday") or "") for item in days
    }
    node_by_requirement: dict[str, dict[str, Any]] = {}
    for day_payload in days:
        for node in day_payload.get("timeline") or []:
            requirement_id = str(node.get("requirement_id") or "")
            if requirement_id and node.get("block_kind") == "poi_activity":
                node_by_requirement[requirement_id] = node

    payload = {
        "trip_context": {
            "traveler": {
                "name": character.get("name"),
                "age": character.get("age"),
                "occupation": character.get("occupation"),
            },
            "city": scenario.get("city_name") or character.get("city_name") or "",
            "number_of_days": num_days,
            "start_weekday": dag.get("start_weekday", ""),
            "daily_scenarios": [
                {
                    "day": int(item.get("day") or 0),
                    "weekday": item.get("weekday", ""),
                    "scenario": item.get("scenario_family", ""),
                }
                for item in days
            ],
            "lodging": {
                "name": hotel.get("name"),
                "district": hotel.get("district_name"),
            },
        },
        "fixed_events": _fixed_events(menus, weekday_by_day),
        "activity_groups": [
            _writer_activity_group(item)
            for item in public_requirements
        ],
        "private_gt_references": {
            "purpose": (
                "Private writing context only. Never disclose these ordinary place names in the task."
            ),
            "places": {
                requirement_id: node.get("place_name", "")
                for requirement_id, node in node_by_requirement.items()
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _fixed_events(
    menus: list[dict[str, Any]], weekday_by_day: dict[int, str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for menu in menus:
        day = int(menu.get("day") or 0)
        for event in menu.get("fixed_events") or []:
            result.append({
                "day": day,
                "weekday": weekday_by_day.get(day, ""),
                "event_type": event.get("event_type"),
                "place_name": event.get("place_name"),
                "time": {
                    "earliest": event.get("earliest"),
                    "latest": event.get("latest"),
                    "duration_minutes": event.get("duration_min"),
                },
            })
    return result


def _writer_activity_group(requirement: dict[str, Any]) -> dict[str, Any]:
    required_types = list(
        requirement.get("service_types")
        or (
            [requirement.get("primary_service_type")]
            if requirement.get("primary_service_type")
            else []
        )
    )
    group = requirement.get("activity_group") or {}
    return {
        "requirement_id": requirement.get("requirement_id"),
        "day": requirement.get("day"),
        "weekday": requirement.get("weekday"),
        "activity_intent": {
            "activity_direction": requirement.get("activity_direction"),
            "time_windows": list(group.get("time_windows") or []),
        },
        "public_place_constraints": {
            "required_service_types": required_types,
            "selected_constraints": [
                _writer_constraint(item) for item in requirement.get("constraints") or []
            ],
        },
    }


def _writer_constraint(constraint: dict[str, Any]) -> dict[str, Any]:
    kind = str(constraint.get("type") or "")
    result: dict[str, Any] = {
        "attribute": kind,
        "relation": constraint.get("operator"),
    }
    if kind in {"opens_by", "closes_at_or_after"}:
        value = int(constraint.get("value") or 0)
        result["value"] = f"{(value // 60) % 24:02d}:{value % 60:02d}"
    elif kind == "route_duration_range":
        result["value"] = {
            "minimum_minutes": constraint.get("minimum"),
            "maximum_minutes": constraint.get("maximum"),
            "actual_minutes": constraint.get("actual_minutes"),
            "transportation_mode": constraint.get("mode"),
            "anchor_relation": constraint.get("relation"),
        }
    else:
        result["value"] = constraint.get("value")
    for key in ("currency", "source_minimum", "source_maximum"):
        if constraint.get(key) is not None:
            result[key] = constraint[key]
    return result
