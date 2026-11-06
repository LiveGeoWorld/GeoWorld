"""LLM prompt templates for task composition.

Generates natural-language user_prompt that serves as the ONLY
input given to the benchmark agent.
"""

from __future__ import annotations

from typing import Any


SYSTEM_USER_PROMPT = """你是一个 benchmark 任务设计师。你的任务是为一个地理智能体 Agent 设计它需要执行的用户指令。

这个 Agent 生活在一个真实地理城市中，需要帮用户协调各种任务（聚餐、出行、购物等）。

你的输出就是 Agent 唯一能看到的用户输入（user_prompt），所以必须：
1. 只用自然语言，不要说"你需要做...步骤"
2. 包含任务的关键信息：参与者是谁、什么时间、什么地点/区域、有什么要求
3. 自然地融入参与者的偏好和约束（但不要直接说"某人的preference是..."，要自然表达）
4. 长度控制在 50-200 字
5. 语气自然，像真实的人发消息给助手

不要输出 JSON，只输出纯文本的 user_prompt。"""


def build_user_prompt_request(
    scenario_type: dict[str, Any],
    characters: list[dict[str, Any]],
    places: list[dict[str, Any]],
    env_events: list[dict[str, Any]],
) -> str:
    """Build the LLM prompt to generate a user_prompt for a task."""

    parts: list[str] = []
    scenario_label = scenario_type.get("label_zh", scenario_type.get("scenario_type", ""))
    scenario_desc = scenario_type.get("description", "")
    theme = scenario_type.get("user_prompt_theme", "general")

    parts.append(f"请为以下场景生成一个用户指令 (user_prompt):")
    parts.append(f"场景类型: {scenario_label} ({scenario_desc})")
    parts.append("")

    # Character summaries
    if characters:
        parts.append("## 参与者")
        for c in characters:
            name = c.get("name", "?")
            age = c.get("age", "?")
            occ = c.get("occupation", "?")
            prefs = c.get("preferences", {})
            prefer = prefs.get("prefer", [])
            avoid = prefs.get("avoid", [])
            dietary = prefs.get("dietary", "")
            mobility = c.get("mobility", {})
            home = mobility.get("home_district_name", "")
            constraints = c.get("constraints", [])
            relationships = c.get("relationships", [])

            parts.append(f"- {name}, {age}岁, {occ}, 住在{home}")
            if prefer:
                parts.append(f"  偏好: {', '.join(prefer[:3])}")
            if avoid:
                parts.append(f"  避讳: {', '.join(avoid[:3])}")
            if dietary:
                parts.append(f"  饮食: {dietary}")
            if constraints:
                for ct in constraints[:2]:
                    ct_content = ct.get("content", "")
                    if ct_content:
                        parts.append(f"  约束: {ct_content}")
            if relationships:
                parts.append(f"  关系: {', '.join(relationships[:2])}")
        parts.append("")

    # Places
    if places:
        parts.append("## 可选地点")
        for p in places:
            name = p.get("name", "?")
            ptype = p.get("type", "?")
            district = p.get("district", "")
            rating = p.get("rating", "")
            parts.append(f"- {name} ({ptype}) @ {district}"
                         + (f", 评分{rating}" if rating else ""))
        parts.append("")

    # Environment events (brief)
    if env_events:
        parts.append("## 可能的意外情况（Agent不知道，仅供设计师参考）")
        for e in env_events[:3]:
            desc = e.get("description_zh", "")
            if desc:
                parts.append(f"- {desc}")
        parts.append("")

    # Theme-specific guidance
    theme_guidance: dict[str, str] = {
        "dinner_meal": "这是一次聚餐场景。user_prompt 应包含：人数、区域、时间偏好、参与者的特殊需求。语气自然像邀请朋友帮忙。",
        "gathering_event": "这是一次多人聚会。user_prompt 应包含：活动类型、多个可能地点、每个人的时间约束。",
        "commute_travel": "这是一次出行规划。user_prompt 应包含：起点终点、时间要求、出行偏好。",
        "shopping_outing": "这是一次购物出行。user_prompt 应包含：购物目的地、用餐需求、时间安排。",
        "tourism_day_trip": "这是一次旅游规划。user_prompt 应包含：景点偏好、交通方式、用餐和休息需求、时间范围。",
    }
    guidance = theme_guidance.get(theme, "请生成一个自然语言的任务指令。")
    parts.append(f"## 设计指导")
    parts.append(guidance)
    parts.append("")
    parts.append("请直接输出 user_prompt 文本，不要加引号或前缀。")

    return "\n".join(parts)
