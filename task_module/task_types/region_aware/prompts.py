"""LLM prompt templates for region-aware planning generation.

LLM receives city cultural constraints + POIs and creates:
  user_prompt: natural Chinese scenario (does NOT leak constraint rules)
  gt: structured {constraint_ids, expected_plan, wrong_plans, explanation_keywords}
"""

SYSTEM_REGION = """你是一个地域感知规划场景的设计师。

你会收到某城市的文化约束清单和候选POI列表。你需要生成一个简洁的地域感知规划场景，并输出结构化GT。

**重要：不要在 user_prompt 中直接告诉 Agent 文化规则。**
Agent 需要靠自己的常识去推断该城市该场景下应该遵守什么规则。

要求：
1. 生成简洁的 user_prompt（80-150字），包含：
   - 用户在哪个城市、要做什么
   - 场景暗示（如"春节期间"、"圣诞前后"）
   - **不写**具体规则
2. 同时输出结构化 GT：
   - expected_plan: 正确规划，每个事件需标注 applied_constraint（约束ID）和 reasoning（为什么这个约束导致了这个决策）
   - wrong_plans: 1-2个典型错误，每个需标注 violates（违反的约束ID）
   - explanation_keywords: 文化关键词

输出纯 JSON，包含 "user_prompt" 和 "gt" 两个字段。"""


def build_llm_user_prompt(
    character: dict,
    city_id: str,
    kw_label: str,
    constraints_context: str,
    main_pois: list[dict],
    distractors: list[dict],
    route_data: dict[str, dict],
) -> str:
    parts: list[str] = []

    parts.append(f"## 城市与约束维度")
    parts.append(f"- 城市: {city_id}")
    parts.append(f"- 约束维度(keywords): {kw_label}")

    parts.append("\n## 文化约束清单（仅用于GT生成，不泄露给user_prompt）")
    parts.append(constraints_context)

    parts.append("\n## 访问者")
    parts.append(f"- {character.get('name','?')}, {character.get('age',0)}岁, "
                 f"{character.get('occupation','')}, 来自{character.get('home_place_name','?')}")

    parts.append("\n## 可用POI")
    for p in main_pois:
        parts.append(f"- [{p.get('place_id','')}] {p.get('name','?')} "
                     f"({p.get('category','?')}, {p.get('district_name','?')}, "
                     f"⭐{p.get('rating',0)}, {p.get('price_level','MODERATE')})")
    if distractors:
        parts.append("\n## 干扰项（不应选择的POI）")
        for d in distractors:
            parts.append(f"- [{d.get('place_id','')}] {d.get('name','?')} "
                         f"({d.get('category','?')})")

    parts.append("\n## 通勤时间参考（分钟）")
    for key, modes in sorted(route_data.items()):
        details = ", ".join(f"{m}={t}min" for m, t in sorted(modes.items()))
        parts.append(f"  {key}: {details}")

    parts.append("\n## 生成要求")
    parts.append("生成简洁的 region_aware 场景。user_prompt 80-150字，不泄露规则。GT 中每个事件标注 applied_constraint + reasoning。输出 JSON：")
    parts.append("""
{
  "user_prompt": "简洁的中文场景（80-150字）。暗示城市+场景但不列规则",
  "gt": {
    "feasible": true,
    "expected_plan": {
      "events": [
        {"event_type": "会议", "place_id": "...", "time": "10:00-12:00",
         "applied_constraint": "约束ID", "reasoning": "因为这个约束所以做了这个决策"}
      ]
    },
    "wrong_plans": [
      {"description": "错误方案", "violates": "违反的约束ID"}
    ],
    "explanation_keywords": ["文化关键词"]
  }
}""")
    return "\n".join(parts)


def parse_llm_response(text: str) -> dict | None:
    import json, re
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if m: text = m.group(1).strip()
    start = text.find('{')
    if start == -1: return None
    depth, end = 0, start
    for i in range(start, len(text)):
        if text[i] == '{': depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0: end = i + 1; break
    try: return json.loads(text[start:end])
    except json.JSONDecodeError: return None


# ── KB enrichment prompt ──

SYSTEM_REGION_ENRICH = """你是一个地理文化约束知识库的维护者。

给定一个城市、一个场景、以及该场景下已有的约束清单，判断该场景的文化约束是否足够全面。

要求：
1. 分析已有的约束覆盖了哪些维度（时间限制、饮食限制、场所限制、礼仪规范、节日活动等）
2. 如果某个维度明显缺失或约束太少（如只有时间约束没有饮食约束），建议新增 1-2 条
3. 新增的约束必须与已有约束不重复
4. 如果约束已经足够全面（覆盖了 3+ 个维度），返回空列表
5. 约束格式：{scene, type, rule, time: {mode}, severity}

输出 JSON: {"should_enrich": true/false, "new_constraints": [{...}]} 或 {"should_enrich": false, "new_constraints": []}"""


def build_enrich_prompt(enrich_context: str, scene_label: str) -> str:
    parts: list[str] = [
        f"## 场景: {scene_label}",
        "",
        enrich_context,
        "",
        "## 要求",
        "分析已有约束的维度覆盖，如有缺失则建议新增 1-2 条约束。",
        "输出 JSON: {should_enrich: bool, new_constraints: [{scene, type, rule, time: {mode}, severity}]}",
    ]
    return "\n".join(parts)
