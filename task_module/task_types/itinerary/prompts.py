"""LLM prompt templates for itinerary generation (menu-based).

LLM receives daily menus with candidate POI pools and creates:
  - user_prompt: natural Chinese scenario description
  - gt: structured itinerary (events chosen from candidates + times + constraints)
"""

SYSTEM_ITINERARY = """你是一个多日行程规划场景的设计师。

你会收到一个"每日事件菜单"。每天的菜单包含三类事件：
1. 固定事件 — 已有指定地点，你必须安排
2. 必选事件 — 你必须安排，但从候选地点列表中选一个最合理的
3. 可选事件 — 你决定是否安排，如果安排了就从候选地点中选一个

你需要完成以下工作：
1. 从每天的可选事件中决定要安排哪些（至少选 1 个可选事件，让每天的行程有变化）
2. 为每个事件从候选地点中选一个具体地点
3. 为每个事件填上 start/end 时间。要求：
   - 事件在 POI 营业时间内
   - 连续事件之间留足通勤时间（>= route_data 对应 pair 的最短耗时 + 5min buffer）
   - 事件按时间递增排列，不能重叠
   - 午餐 12:00-14:00，晚餐 17:00-19:00，两餐不能重叠
4. 判断整体行程是否可行（约80%可行，20%在某项约束上不可行）
5. 生成自然的 user_prompt（300-600字），用中文描述该旅行者需要完成的行程和约束
6. 输出结构性 GT：correct_itinerary（每天的事件序列） + constraints（预算上限等）

输出纯 JSON，包含 "user_prompt" 和 "gt" 两个顶级字段。"""


def build_llm_user_prompt(
    character: dict,
    hotel: dict,
    menus: list[dict],
    route_data: dict[str, dict],
    num_days: int,
) -> str:
    parts: list[str] = []

    parts.append("## 旅行者")
    parts.append(
        f"- {character.get('name','?')}, {character.get('age',0)}岁, "
        f"{character.get('occupation','')}, "
        f"偏好出行: {character.get('preferred_transit','TRANSIT')}, "
        f"每日预算: ¥{character.get('budget_daily',500)}"
    )
    parts.append(f"## 住宿 ({num_days}天)")
    parts.append(
        f"- 酒店: {hotel.get('name','?')}, {hotel.get('district_name','')}, "
        f"入住14:00 / 退房11:00, {hotel.get('price_level','MODERATE')}"
    )

    parts.append(f"\n## 每日事件菜单 ({num_days}天)")

    for m in menus:
        day = m["day"]
        parts.append(f"\n### Day {day}")
        parts.append("**固定事件**（地点已定，必须安排）:")
        for e in m.get("fixed_events", []):
            tinfo = ""
            if e.get("earliest") and e.get("latest"):
                tinfo = f", {e['earliest']}-{e['latest']}"
            elif e.get("earliest"):
                tinfo = f", 最早{e['earliest']}"
            elif e.get("latest"):
                tinfo = f", 最晚{e['latest']}"
            parts.append(
                f"  - {e['event_type']} @ {e['place_name']}"
                f"{tinfo}, {e.get('duration_min',60)}min, [{e.get('place_id','')}]"
            )

        if m.get("required_slots"):
            parts.append("**必选事件**（每个必须安排，从中选一个地点）:")
            for s in m["required_slots"]:
                tr = s.get("time_range", ("?", "?"))
                candidates_str = "; ".join(
                    f"[{c.get('place_id','')}] {c.get('name','?')} "
                    f"({c.get('opening_hours','?-?')})"
                    for c in s.get("candidates", [])
                )
                parts.append(
                    f"  - {s['event_type']}: 时间窗{tr[0]}-{tr[1]}, "
                    f"区域: {s.get('district_name','?')}\n"
                    f"    候选: {candidates_str}"
                )

        if m.get("optional_slots"):
            parts.append("**可选事件**（LLM 决定是否安排，最多每个类型选1个）:")
            for s in m["optional_slots"]:
                tr = s.get("time_range", ("?", "?"))
                candidates_str = "; ".join(
                    f"[{c.get('place_id','')}] {c.get('name','?')} "
                    f"({c.get('opening_hours','?-?')}, {c.get('duration_typical_min',60)}min)"
                    for c in s.get("candidates", [])
                )
                parts.append(
                    f"  - [{s['event_type']}]: 可选, 时间窗{tr[0]}-{tr[1]}, "
                    f"区域: {s.get('district_name','?')}\n"
                    f"    候选: {candidates_str}"
                )

    parts.append("\n## 通勤时间参考（分钟）")
    for key, modes in sorted(route_data.items()):
        details = ", ".join(f"{m}={t}min" for m, t in sorted(modes.items()))
        parts.append(f"  {key}: {details}")

    parts.append(f"\n## 生成要求")
    parts.append(f"为一个{num_days}天的行程生成 user_prompt 和结构化 GT。")
    parts.append("""输出 JSON 格式：
{
  "user_prompt": "自然的中文行程规划场景描述（300-600字）",
  "gt": {
    "expected_feasible": true,
    "constraints": {"max_daily_budget": 500, "max_event_gap_min": 120, "min_meal_interval_hours": 4},
    "correct_itinerary": {
      "days": [
        {"day": 1, "events": [
          {"event_type": "hotel_checkin", "place_id": "...", "place_name": "...",
           "start": "14:00", "end": "14:30", "commute_from_prev_event_id": null,
           "commute_mode": null, "commute_min": 0},
          {"event_type": "dinner", "place_id": "从候选里选", "place_name": "...",
           "start": "18:30", "end": "20:00", "commute_from_prev_event_id": "上一条的event_id或null",
           "commute_mode": "TRANSIT", "commute_min": 15}
        ]}
      ]
    },
    "acceptable_itineraries": []
  }
}""")

    return "\n".join(parts)


def parse_llm_response(text: str) -> dict | None:
    import json, re
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if m:
        text = m.group(1).strip()
    start = text.find('{')
    if start == -1:
        return None
    depth, end = 0, start
    for i in range(start, len(text)):
        if text[i] == '{': depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
