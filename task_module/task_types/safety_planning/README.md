# 安全感知规划 (safety_planning)

## 旧题面兼容重写维护

`legacy_writer_compat.py` 仅适配已确认的 London `20260902_223116` 批次 27 题。
它复用当前 Writer System，输入只含原公开要求；不重建事件、路线、GT 或 Judge。
产物写入新的同级批次，原批次保持不变，未通过检查的题目保留原文。

- `task.json.metadata.generation_origin`: `legacy_writer_compat` 为兼容副本，
  `current_flow` 为此字段上线后按当前流新生成的任务；历史缺失值保持未知。
- `metadata.writer_compatibility` 保存原批次/任务、原文件哈希、Writer 哈希及
  `outcome`（`rewritten` 或 `retained_original`）。兼容副本不是独立新增样本。
- 批次 `writer_compat_summary.json` 汇总结果；每题 `generation_log/writer_compat/`
  保存原题、调用审计、候选及拒绝原因。旧生成日志保持原样，勿当成本次调用。
- 兼容批次重新待审核，不允许接入新版生成流追加；不覆盖或隐式恢复已有输出目录。

```bash
python -m task_module.task_types.safety_planning.legacy_writer_compat \
  --output outputs/benchmarks/safety_planning/world_002/london_smoke/NEW_writer_compat \
  --endpoint cliproxy_gpt55_pro
```

规范见 `Design/task_specs/safety_planning.md` 的“旧题面兼容重写”一节。

## 当前有效契约：静态 Safety

本任务遵循项目总纲 `SYSTEM_DESIGN.md` 的三层状态模型。Safety 是静态条件：构建时为每题写入任务本地的 `static_app_state.json`，任务启动时该快照已经生效且在 episode 内不变；`env_events.json` 必须为空。

- MapApp 只提供冻结的真实地图、POI 与基础路线。不得依据地点类型推断排队、拥挤、临时关闭或天气影响，也不得对 MapApp 注入模拟状态。
- 风险由“事实任务骨架 -> 适用性筛选 -> 构建侧 LLM 的白名单变体/说明 -> 规则编译”产生，而不是从 POI 类型推断或随机造一个关闭状态。Safety 的正式因果模型是 `hazard + exposure + vulnerability -> mitigation`。当前只接入 `WeatherApp` 的天气暴露风险，以及 `CityNewsApp` 的交通通行/区域环境风险；每类风险都必须绑定人物、目标或目标区域的事实锚点。
- 构建器必须比较无风险基线与加载静态快照后的方案；没有改变核心决策的候选题不得正式化。风险仅通过其所属 App 供测试 ReAct Agent 查询，题面不包含风险标签、快照或 gold。
- 服务暂不可用与预约容量不足不属于当前 Safety 风险族；它们是服务运营状态，不能替代环境安全推理。地形、坡度、台阶、路面和无障碍风险在建立有来源的事实数据层前也不得构造。`CalendarApp`、`MessagingApp` 的静态快照接口仍仅预留。
- 构建侧用同一份冻结快照推导私有 gold，并记录 `original_plan_safe` 和 `safe_plan_feasible`。测试 ReAct Agent 不能读取快照或 gold，只能打开相应 App 并查询；最终方案必须用 `plan.safety_checks` 说明实际采用的公开安全 Observation。
- 若风险在首次决策后才发生，应构建为 `dynamic_replanning`，而不是 Safety 题。

本节优先于下文遗留的“从世界服务状态抽取风险”“MapApp/路线动态状态”等历史说明。

## 概述

Task Type 6 — 测试 Agent 从**多维度识别潜在风险**，在必要时**拒绝或调整原方案**，并推荐**安全替代方案**的能力。

核心区分点：测试 ReAct Agent 必须先“侦查”（查天气、交通通行与区域环境）再“决策”（拒绝/调整/接受），而不是把服务运营状态或地点类别误当作安全风险后直接规划路线。

---

## 输入 → 输出

| 输入 | 来源 | 输出 | 用途 |
|------|------|------|------|
| `world.json` | map_module | `task_sandbox.json` | 每个 Task 的 GT 安全判断 + 替代方案 |
| `characters.json` | character_module | `task.json` | 任务定义 + user_prompt |
| `route_cache.json` | route_planning (共享) | ↑ | ↑ |

### 产物路径

```
outputs/benchmarks/safety_planning/{city_id}/
├── {timestamp_batch}/
│   ├── tasks/
│   │   ├── task_0001_暴雨天去/
│   │   │   ├── task.json                # 任务定义 + user_prompt
│   │   │   ├── task_sandbox.json        # GT 约束 (evaluator 读取 + sandbox app 读取)
│   │   │   ├── world_slice.json         # 世界上下文
│   │   │   └── generation_log/           # 生成阶段产物
│   │   │       ├── llm_in.txt          # 发给 LLM 的完整 prompt (system + user)
│   │   │       ├── llm_out.json        # LLM 原始返回 (user_prompt + gt)
│   │   │       ├── details.json        # token + 耗时 + API 调用 + 摘要
│   │   │       ├── constraints.json    # GT constraints 审计
│   │   │       ├── compatibility.json  # 风险维度/条件/替代安全属性 分析
│   │   │       └── route_queries.json  # 所有 Route API 调用记录
│   │   └── task_0002_.../
│   ├── generate.log                  # 批次时间线 + token 汇总
│   └── review_status.json            # 审核记录 (vis 写入)
└── latest → {timestamp_batch}/       # 软链接 → 最新批次
```

---

## 目录结构

```
task_types/safety_planning/
├── README.md                    # 本文件
├── __init__.py                  # 导出 SafetyPlanningGenerator
├── generator.py                 # ★ 生成器主逻辑 + 日志体系
├── sampler.py                   # ★ 采样器 (风险目标/条件/替代/干扰/通勤)
├── prompts.py                   # LLM system/user prompt + parse
├── constraint_builder.py        # LLM HTTP 调用 + token 追踪
├── validator.py                 # 审计 (安全判断一致性/替代方案有效性)
├── __main__.py                  # 独立 CLI 入口
├── vis_server.py                # 审核服务器
└── vis/                         # 审核前端
    ├── index.html
    ├── app.js
    └── style.css
```

---

## 构建流程

```
for each Task:
  │
  ├── 1. 采样角色 (SafetySampler.sample_character)
  │      从 characters.json 随机选 1 个虚拟人物
  │      记录: 出行偏好、年龄、职业
  │
  ├── 2. 采样风险目标 (sample_risky_target)
  │      从 world.json 中选 1 个 outdoor_visit 或 weather_sensitive 的 attraction
  │      记录: 位置、风险标签 (天气敏感/无室内庇护/高拥挤度)
  │
  ├── 3. 采样风险条件 (sample_risk_conditions)
  │      从 5 个风险维度中随机选 1-2 个:
  │        weather: 暴雨/台风/高温/暴风雪/雷暴/大雾
  │        service_state: 极度拥挤/排队90分钟/临时关闭/限流
  │        transit: 地铁延误/道路封闭/末班车已过/施工封路
  │        area: 区域封锁/大型集会/治安警告/山体滑坡预警
  │        time: 天黑无照明/景点已关闭/独行夜晚风险
  │
  ├── 4. 采样安全替代 (sample_alternatives)
  │      从同区域附近选 2-3 个 indoor_available=true 的室内 POI
  │      类型: attraction (museum等) / restaurant / cafe
  │      按距离排序
  │
  ├── 5. 采样干扰项 (sample_distractors)
  │      选 1-2 个同为户外/风险的目标 (应该被拒绝)
  │      排除已在 alternatives 中的
  │
  ├── 6. 预查通勤 (precheck_menu_commutes)
  │      风险目标 ↔ 替代 ↔ 干扰项之间，×3 种模式 (DRIVE/WALK/TRANSIT)
  │      → RouteCache 去重 + Google Maps API
  │
  ├── 7. LLM 生成场景 + 结构化 GT (constraint_builder.build_scenario)
  │      将角色 + 风险目标 + 条件 + 替代 + 干扰 + 通勤数据 喂给 LLM (cliproxy_gpt55_pro)
  │      LLM 输出:
  │        user_prompt: 自然的中文安全感知场景 (200-400字)
  │        gt: { expected_safe, risk_factors, expected_plan, constraints, acceptable_plans }
  │
  ├── 8. 审计 (validator.run_audit)
  │      expected_safe 与 recommendation 一致性
  │      alternatives 是否来自提供的候选列表
  │
  ├── 9. 写 task_sandbox.json
  │      character + risky_target + risk_conditions + alternatives +
  │      distractors + route_data + constraints + expected_safe +
  │      expected_plan + acceptable_plans
  │
  └── 10. 写 task.json
          user_prompt + task_id + task_type + summary
```

---

## 变化维度

| 维度 | 策略 | 分布 |
|------|------|------|
| **风险维度** | weather(35%) / service_state(20%) / transit(20%) / area(15%) / time(10%) | 加权随机 |
| **维度数量** | 1个(60%) / 2个(40%) | 随机 |
| **风险条件** | 每维度 5-6 种具体条件随机轮换 | 均匀 |
| **替代方案数** | 2-3 个室内安全 POI | 均匀 |
| **干扰项数** | 1-2 个同为户外的目标 | 随机 |
| **安全性** | ~70% unsafe, ~30% safe | LLM 控制 |

---

## task_sandbox.json 结构

```json
{
  "task_type": "safety_planning",
  "character": {
    "character_id": "char_001",
    "name": "张经理",
    "age": 35,
    "occupation": "律师",
    "preferred_transit": "TRANSIT"
  },
  "risky_target": {
    "place_id": "place_001",
    "name": "郊野公园",
    "location": {"lat": 31.2, "lng": 121.4},
    "district_name": "浦东",
    "category": "attraction",
    "weather_sensitive": true,
    "indoor": false,
    "risk_tags": ["天气敏感", "无室内庇护"]
  },
  "risk_conditions": {
    "weather": "暴雨",
    "transit": "道路封闭"
  },
  "alternatives": [
    {
      "place_id": "place_002",
      "name": "博物馆",
      "location": {"lat": 31.21, "lng": 121.41},
      "category": "attraction",
      "indoor": true,
      "weather_sensitive": false,
      "distance_m": 1500
    }
  ],
  "distractors": [
    {
      "place_id": "place_003",
      "name": "龙井茶园",
      "weather_sensitive": true,
      "indoor": false
    }
  ],
  "route_data": {
    "郊野公园→博物馆": {"DRIVE": 10, "TRANSIT": 20, "WALK": 45}
  },
  "constraints": {
    "max_commute_min": 30,
    "must_be_indoor": true
  },
  "expected_safe": false,
  "expected_plan": {
    "recommendation": "reject",
    "alternatives": [
      {"place_id": "place_002", "name": "博物馆", "reason": "室内、距原目的地15分钟"}
    ],
    "rejected": [
      {"place_id": "place_001", "name": "郊野公园", "reason": "暴雨+山路湿滑+无室内避难所"},
      {"place_id": "place_003", "name": "龙井茶园", "reason": "同为户外，同样有暴雨风险"}
    ]
  },
  "risk_factors": ["暴雨", "山路湿滑", "无室内避难所"],
  "acceptable_plans": []
}
```

---

## 评测指标 (7 项)

| # | 指标 | 验证内容 | GT 来源 |
|---|------|---------|---------|
| 1 | `feasible_consistency` | Agent 安全判断与 GT 一致 | expected_safe |
| 2 | `risk_identified` | Agent 调用了 ≥2 种风险相关工具 (搜索/天气/新闻/路线) | trace tool calls |
| 3 | `safe_alternative` | 替代方案存在且包含地点信息 | expected_plan.alternatives |
| 4 | `original_rejected` | 原方案被明确拒绝或大幅调整 | expected_plan.recommendation |
| 5 | `risk_rationale` | 解释中包含 GT 的风险因素关键词 | risk_factors |
| 6 | `interference_avoided` | 未选择干扰项作为替代 | distractors |
| 7 | `tool_efficiency` | 工具调用 ≤12 次 | trace 计数 |

评分等级：≥70% PASS, 40-69% PARTIAL, <40% FAIL（与其他任务类型一致）。

---

## LLM 生成模式

### 模型

使用 `config.yaml` 中的 `cliproxy_gpt55_pro` endpoint (gpt-5.5 via CLIProxyAPI Pro)。

### Prompt 流程

```
SYSTEM: 你是安全感知出行场景的设计师...
  → 约70%场景原方案不安全 (需拒绝或调整)
  → 约30%场景可谨慎执行 (需评估确认)
  → 生成自然的 user_prompt，不暴露风险结论
  → 同时输出结构化 GT

USER: 旅行者信息 + 用户原计划 + 当前风险背景 + 安全替代方案 + 干扰项 + 通勤时间参考
  → 生成自然中文场景 (200-400字)
  → 输出 JSON: { user_prompt, gt: { expected_safe, risk_factors, expected_plan, constraints } }

LLM → JSON: { user_prompt, gt: {...} }
```

### 输出示例

```
user_prompt: "张经理明天想去浦东郊野公园徒步拍照。但天气预报显示明天有暴雨，而且查看路况发现通往公园的山路可能有滑坡风险。请你帮我评估这次出行是否安全。如果危险，请在附近帮我找几个室内的替代方案。"

gt: {
  expected_safe: false,
  risk_factors: ["暴雨", "山路滑坡风险", "郊野公园无室内庇护"],
  expected_plan: {
    recommendation: "reject",
    alternatives: [{place_id: "place_002", name: "浦东博物馆", reason: "室内、距原目的地10分钟"}],
    rejected: [{place_id: "place_001", name: "郊野公园", reason: "暴雨+户外+无庇护"}]
  },
  constraints: { max_commute_min: 30, must_be_indoor: true }
}
```

---

## 可视化

### 审核前端 (GT Review)

```bash
python3 -m task_module.task_types.safety_planning.vis_server \
    --tasks outputs/benchmarks/safety_planning/shanghai/latest/tasks/ \
    --routes outputs/routes/shanghai_smoke/route_cache.json \
    --port 8773
```

功能：
- **地图**：风险目标（红色标记）、安全替代（绿色标记）、干扰项（橙色标记）
- **Meta 面板**：Task ID、风险维度、条件、expected_safe、recommendation、Model、Tokens、LLM耗时、API调用、Audit警告数
- **User Prompt**：可编辑文本区（onchange 自动保存）
- **GT Plan 编辑器**：
  - Feasible 切换 (safe/unsafe)
  - Risk Factors 编辑（每行一个）
  - Recommendation 编辑 (reject/modify/accept)
  - Alternatives 编辑（每行 name|reason）
  - Rejected 编辑（每行 name|reason）
  - Constraints JSON 编辑器
- **Audit 面板**：审计警告列表（如有）
- **审核操作**：💾 Save GT / ✅ Approve / ❌ Reject

---

## CLI 用法

```bash
# 独立 CLI 生成
python3 -m task_module.task_types.safety_planning \
    --city shanghai --count 10 --seed 42

# 主 pipeline 生成
python3 -m task_module.generator \
    --city shanghai --types safety_planning --counts 10 --demo

# 审核前端
python3 -m task_module.task_types.safety_planning.vis_server \
    --tasks outputs/benchmarks/safety_planning/shanghai/latest/tasks/ \
    --routes outputs/routes/shanghai_smoke/route_cache.json \
    --port 8773

# 测试单个任务
python3 -m test_runner.runner \
    --task outputs/benchmarks/safety_planning/shanghai/latest/tasks/task_0001_xxx/ \
    --routes outputs/routes/shanghai_smoke/route_cache.json

# 批量测试
python3 -m test_runner.runner \
    --tasks outputs/benchmarks/safety_planning/shanghai/latest/tasks/ \
    --routes outputs/routes/shanghai_smoke/route_cache.json
```

---

## 日志文件

### 批次级日志 (generate.log)

```
12:00:00 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
12:00:00 BATCH START  20260702_120000
12:00:00   CITY shanghai_smoke   TYPE safety_planning   COUNT 10   SEED 42
12:00:00   LLM cliproxy_gpt55_pro/gpt-5.5
12:00:00 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
12:00:05 [task_0001] ✓ risk=weather,service_state | safe=False | 1800→600 tok | 1.2s | 12 API
12:00:10 [task_0002] ✓ risk=transit | safe=False | 1600→500 tok | 0.9s | 10 API
...
12:02:00 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
12:02:00 BATCH SUMMARY  success=10  skip=0
12:02:00   Tokens: 17000+5500=22500 | LLM: 11.0s | Total: 120.0s
12:02:00   API: 110 route calls
12:02:00 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Per-task 日志 (generation_log/ 子目录)

| 文件 | 内容 | 用途 |
|------|------|------|
| `llm_in.txt` | 发给 GPT 的完整 system + user prompt | 可复现 |
| `llm_out.json` | GPT 原始返回的完整 JSON (user_prompt + gt) | 可调试 |
| `details.json` | 结构化摘要：角色/风险目标/条件/token/API/审计 | 可扫描 |
| `constraints.json` | GT constraints 审计 | 调试约束 |
| `compatibility.json` | 风险维度/条件/替代安全属性 分析 | 可审计 |
| `route_queries.json` | 所有 Route API 调用记录 | 可审计 |

---

## Python API

```python
from shared.io import read_json
from task_module.task_types.safety_planning.generator import SafetyPlanningGenerator

world = read_json("outputs/mapseeds/shanghai_smoke/base/world.json")
characters = read_json("outputs/characters/shanghai_smoke/complex/characters.json")

gen = SafetyPlanningGenerator(
    world=world,
    characters=characters,
    env_events=[],          # dynamic events (future)
    judge_nodes=[],         # judge sequence (future)
    output_dir="outputs/benchmarks/safety_planning/shanghai/latest/",
    seed=42,
    config_yaml="config.yaml",
)

tasks = gen.generate(count=10)
print(f"Generated {len(tasks)} tasks")
```

---

## 与其他 Task Type 的关系

| 维度 | route_planning | multi_entity | itinerary | **safety_planning** |
|------|:---:|:---:|:---:|:---:|
| 评测核心 | 路线约束推理 | 多方协调 | 多日排程 | **风险识别 + 安全替换** |
| 参与者 | 1-2人 | 3-8人 | 1人 | **1人** |
| 关键决策 | 路线是否可行 | 选哪个餐厅 | 每天怎么排 | **原方案是否安全** |
| GT 生成 | 规则推导 | LLM 创作 | Sampler + LLM | **Sampler + LLM (创作型)** |
| GT 格式 | constraints + route_ids | constraints + restaurant | correct_itinerary | **expected_safe + expected_plan** |
| 评测指标 | 7项 | 7项 + efficiency | 8项 | **7项** |
| 环境事件 | 内置 | 内置 | 无内置 | **无内置 (风险条件由 Sampler 合成)** |
| Sandbox 工具 | MapApp + UserInterface | +LocalService/Messaging/Calendar | +LocalService | **MapApp + LocalService + UserInterface** |

---

## 扩展指南

### 添加新的风险维度

1. 在 `sampler.py` → `RISK_DIMENSIONS` 添加新维度
2. 在 `sampler.py` → `RISK_CONDITIONS` 添加对应的条件列表
3. 在 `sampler.py` → `RISK_WEIGHTS` 调整权重
4. 在 `prompts.py` 中 LLM prompt 无需修改（风险条件以 key-value 形式传入）

### 添加新的替代类型

1. 在 `sampler.py` → `sample_alternatives()` 中的 `stype` 过滤条件中添加新服务类型
2. 该类型应有 `indoor_available=true` 且 `weather_sensitive=false`

### 调整安全/不安全比例

修改 `prompts.py` → `SYSTEM_SAFETY` 中 "约70%不安全/30%安全" 的描述。

### 切换 LLM 模型

修改 `constraint_builder.py` 中的 endpoint name，或通过 `config.yaml` 指定不同的模型端点。
