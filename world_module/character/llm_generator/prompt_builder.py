"""Prompt builders for the 4-step character generation pipeline.

Four independent prompts:
- build_region_prompt()                → Step 1: district allocation
- build_profile_prompt()               → Step 2: character profiles per district
- build_profile_prompt_incremental()   → Step 2 retry: fill missing profiles
- build_relationship_prompt()          → Step 4: relationship network (rules in SYSTEM_RELATIONSHIP)

All parameters are dynamic (no hardcoded values).
"""

from __future__ import annotations


def build_region_prompt(total_count: int, district_names: list[str]) -> str:
    """Step 1: ask LLM to allocate total_count characters across districts.

    Returns only the district-to-count mapping JSON.
    """
    min_regions = max(3, min(len(district_names), 5))
    max_regions = min(6, len(district_names))

    return f"""你是一个城市的居民分布规划师。

这座城市有 {len(district_names)} 个区域:
{_bullet_list(district_names)}

需要分配 {total_count} 个人物到这些区域中。

要求:
1. 选择 {min_regions} 到 {max_regions} 个区域（不是全部分配）
2. 不同区域的人数要有差异:
   - 繁华商业区/交通枢纽: 密集 ({total_count // 4}~{total_count // 2} 人)
   - 一般商圈/居住区: 适中 ({total_count // 5}~{total_count // 3} 人)
   - 边缘/度假区: 稀疏 ({max(1, total_count // 10)}~{max(2, total_count // 6)} 人)
3. 所有区域人数之和必须等于 {total_count}

输出严格 JSON，格式: {{"区域名A": N, "区域名B": M, ...}}
区域名必须与上方列表完全一致。
只输出 JSON，不要解释。"""


def build_profile_prompt(
    district_name: str,
    sub_count: int,
    city_name: str,
    country: str,
) -> str:
    """Step 2: generate exactly sub_count character profiles for one district.

    Returns a JSON array of characters (no relationships, no mobility).
    """
    return f"""你是 {city_name}, {country} 的居民角色设计师。

请为「{district_name}」这个区域生成 {sub_count} 个生活在此地的虚拟居民。

要求:
1. 名字符合 {country} 文化习惯，提供原名 + 罗马音/拼音
2. 年龄覆盖多样: 幼儿/青少年/青年/中年/老年，避免全是同一段
3. 职业贴合该区域特征
4. 偏好(perfer)和避讳(avoid)具体真实:
   好例: "辛辣食物"、"安静角落座位"、"宠物友好"、"夜景窗边"
   坏例: "near_transit"、"good_food"（太泛）
5. 饮食限制(dietary)仅在有需要的角色上标注: vegetarian/halal/seafood_allergy/low_sodium/无
6. 预算(budget): low/medium/high，贴合职业
7. 每个角色至少 1 个隐藏约束，触发条件为:
   after_first_plan_received / after_agent_checkin / 30min_before_deadline

输出 EXACTLY {sub_count} 个人物的 JSON 数组:
[
  {{
    "name": "...", "name_romanized": "...",
    "age": 0, "gender": "male|female",
    "occupation": "...",
    "personality_traits": ["...", "..."],
    "preferences": {{"prefer": [...], "avoid": [...], "dietary": "", "budget": "medium"}},
    "communication": {{"style": "...", "response_latency": "fast|medium|delayed"}},
    "constraints": [
      {{"constraint_type": "hidden", "content": "...", "reveal_trigger": "after_first_plan_received"}}
    ]
  }}
]

数组长度必须正好是 {sub_count}。只输出 JSON 数组。"""


def build_profile_prompt_incremental(
    district_name: str,
    existing_names: str,
    sub_count: int,
    city_name: str,
    country: str,
) -> str:
    """Step 2 retry: generate additional characters, avoiding duplicates.

    Used when a district got fewer profiles than requested.
    existing_names is a string like "佐藤恵美(42岁), 田中花(28岁)".
    """
    return f"""你是 {city_name}, {country} 的居民角色设计师。

「{district_name}」区域已有以下人物: {existing_names}
请为该区域再补充 {sub_count} 个新居民，与已有人物不重复。

要求:
1. 名字符合 {country} 文化习惯，不与已有人物重名
2. 优先填补已有集中缺少的年龄段
3. 职业贴合该区域特征
4. 偏好具体真实，不要千篇一律
5. 饮食限制仅在有需要的角色上标注
6. 预算贴合职业
7. 每个角色至少 1 个隐藏约束

输出 EXACTLY {sub_count} 个人物的 JSON 数组:
[
  {{
    "name": "...", "name_romanized": "...",
    "age": 0, "gender": "male|female",
    "occupation": "...",
    "personality_traits": ["...", "..."],
    "preferences": {{"prefer": [...], "avoid": [...], "dietary": "", "budget": "medium"}},
    "communication": {{"style": "...", "response_latency": "fast|medium|delayed"}},
    "constraints": [
      {{"constraint_type": "hidden", "content": "...", "reveal_trigger": "after_first_plan_received"}}
    ]
  }}
]

数组长度必须正好是 {sub_count}。只输出 JSON 数组。"""


# # Step 4: system prompt (rules + full JSON structure with keys)
# SYSTEM_RELATIONSHIP = """你是人物关系网络设计师，这些人物关系会用来构建benchmark，请保持多样性。
# 规则:
# 1. 优先家庭单元: 同姓+年龄差≥18→亲子(parent/child), 同姓+年龄差≤5+不同性别→夫妻(spouse), 年龄差≤5+同姓→兄弟姐妹(sibling)
# 2. 同区域→邻居(neighbor)、常客、本地朋友(friend)
# 3. 同龄(年龄差≤5)→同学(classmate)、朋友(friend)
# 4. 职业相关→同事(coworker)、上下级
# 5. 双向关系都要写 (A→B 和 B→A 各一条)
# 6. 每个人至少和另一人有直接关系，孤立不合格
# 7. 关系类型: spouse/parent/child/sibling/coworker/friend/neighbor/classmate
# 8. 避免仅出现单一类型关系，确保覆盖家庭、邻里、朋友、同事、同学等多种关系，提升关系网络的真实性和多样性。

# 每条关系的 JSON 结构 (严格使用以下 key 名):
# {
#   "character_id":            "char_1",           ← 关系一方序号
#   "related_id":              "char_2",           ← 关系另一方序号
#   "type":                    "spouse",           ← 关系类型
#   "subtype":                 "wife",             ← 子类型说明，无则空字符串 ""
#   "strength":                "very_strong",      ← 强度字符串: weak/medium/strong/very_strong
#   "trust_level":             "very_high",        ← 信任度字符串: low/medium/high/very_high
#   "communication_frequency": "daily",            ← 沟通频率字符串: daily/weekly/monthly/rarely
#   "context":                 "結婚5年"            ← 简短中文上下文
# }
# 引用时使用序号 char_1...char_N"""

# Step 4: system prompt (rules + full JSON structure with keys)
SYSTEM_RELATIONSHIP = """你是人物关系网络设计师，这些人物关系会用来构建benchmark，请保持多样性。
你需要考虑年龄，区域等因素进行构建，保证客观正确性。关系类型有[spouse/parent/child/sibling/coworker/friend/neighbor/classmate].
注意:避免仅出现单一关系类型，提升关系网络的真实性和多样性。

每条关系的 JSON 结构 (严格使用以下 key 名):
{
  "character_id":            "char_1",           ← 关系一方序号
  "related_id":              "char_2",           ← 关系另一方序号
  "type":                    "spouse",           ← 关系类型
  "subtype":                 "wife",             ← 子类型说明，无则空字符串 ""
  "strength":                "very_strong",      ← 强度字符串: weak/medium/strong/very_strong
  "trust_level":             "very_high",        ← 信任度字符串: low/medium/high/very_high
  "communication_frequency": "daily",            ← 沟通频率字符串: daily/weekly/monthly/rarely
  "context":                 "結婚5年"            ← 简短中文上下文
}
引用时使用序号 char_1...char_N"""



def build_relationship_prompt(characters_summary: list[str]) -> str:
    """Step 4 user prompt: summary + output format only. Rules in SYSTEM_RELATIONSHIP."""
    summary_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(characters_summary))
    total = len(characters_summary)
    min_rels = total * 2

    return f"""为以下 {total} 人构建关系网络:

{summary_text}

输出 JSON，引用序号 char_1...char_{total}:
{{"relationships": [...]}}

关系总数 >= {min_rels} 条。只输出 JSON。"""


def _bullet_list(items: list[str]) -> str:
    return "\n".join(f"  · {item}" for item in items)


# ============================================================
# Keep old prompt for backward compat (non-Azure providers)
# ============================================================
def build_character_prompt(
    city_name: str, country: str, timezone: str,
    districts: list, place_types: list[str], target_count: int,
) -> str:
    """Legacy single-pass prompt for Yunwu API."""
    lines = _bullet_list([d.get("name", "") for d in districts])
    types_str = ", ".join(place_types[:15])
    return f"""你是 GeoAgentBench 的城市生活角色设计师。
你正在为 {city_name}, {country} ({timezone} 时区) 设计虚拟居民。
已知城区 ({len(districts)} 个):
{lines}
可用商户类型: {types_str}
任务: 生成 {target_count} 个生活在该城市的虚拟人物... (legacy)"""


def build_character_messages(
    city_name: str, country: str, timezone: str,
    districts: list, place_types: list[str], target_count: int,
) -> list[dict[str, str]]:
    """Legacy Azure messages — kept for backward compat, redirect to new pipeline."""
    return [{"role": "user", "content": "use new pipeline instead"}]
