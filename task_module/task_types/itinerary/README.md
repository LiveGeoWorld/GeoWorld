# 长时行程规划 (itinerary)

> **当前正式契约（2026-08-02）**：本 README 以下保留的是早期实现与历史产物说明，
> 其中的“LLM 直接生成 GT”“全量预查 POI 对”“约 20% 不可完成”等文字不能作为
> 当前正式构建依据。任务研究目标、三类角色隔离、公开 App 边界、路线成本和正式化
> 门禁以 [`Design/task_specs/itinerary.md`](../../../Design/task_specs/itinerary.md) 为准；
> 跨任务世界/provenance/成本规则以 [`SYSTEM_DESIGN.md`](../../../SYSTEM_DESIGN.md) 为准。
>
> 当前链路是：冻结 `world/city/version` 与其绑定路线缓存 -> 先选本题实际时间段与
> 有限候选 -> 只物化相邻选定时段及固定依赖所需的真实多方式路线 -> 私有求解器验证
> 全局可行性 -> 构建 Agent 仅把已验证的公开投影写成题面 -> 每题合同校验后才正式化。
> 测试 ReAct Agent 只从统一 App 目录及公开 Observation 自行查证；Judge 在运行后读取
> 私有证据，绝不在运行中纠正或补写答案。启动后变化不属于 itinerary，而应由
> `dynamic_replanning` 的固定事件时间线处理。

## 概述

Task Type 5 — 测试 Agent 在**长时间跨度下维护多地点、多事件与多时间窗之间的依赖关系**，并动态生成完整行程。

核心挑战：Agent 需要同时处理搜索多种类型的服务（酒店、餐厅、景点、交通枢纽）、计算通勤成本、匹配营业时间、安排事件顺序、遵守用餐时间约束、控制预算，并在复杂的日程依赖关系中找到可行的排序方案。

---

## 输入 → 输出

| 输入 | 来源 | 输出 | 用途 |
|------|------|------|------|
| `world.json` | map_module | `task_sandbox.json` | 每个 Task 的 GT 行程 + 约束 |
| `characters.json` | character_module | `task.json` | 任务定义 + user_prompt |
| `route_cache.json` | route_planning (共享) | ↑ | ↑ |

### 产物路径

```
outputs/benchmarks/itinerary/{city_id}/
├── {timestamp_batch}/                ← 批次目录
│   ├── tasks/
│   │   ├── task_0001_我要去东京/
│   │   │   ├── task.json                # 任务定义 + user_prompt
│   │   │   ├── task_sandbox.json        # GT 约束 (evaluator 读取 + sandbox app 读取)
│   │   │   ├── world_slice.json         # 世界上下文
│   │   │   └── generation_log/           # 生成阶段产物
│   │   │       ├── llm_in.txt          # 发给 LLM 的完整 prompt (system + user)
│   │   │       ├── llm_out.json        # LLM 原始返回 (含 user_prompt + gt)
│   │   │       ├── details.json        # token + 耗时 + API 调用 + 摘要
│   │   │       ├── constraints.json    # GT constraints 审计
│   │   │       ├── compatibility.json  # 酒店/POI/预算/通勤 分析
│   │   │       └── route_queries.json  # 所有 Route API 调用记录
│   │   └── task_0002_.../
│   ├── generate.log                  # 批次时间线 + token 汇总
│   └── review_status.json            # 审核记录 (vis 写入)
└── latest → {timestamp_batch}/       # 软链接 → 最新批次
```

> 每次生成一个"批次"。默认继续最新批次，可指定 `--new-batch` 新建或 `--batch` 指定。

---

## 目录结构

```
task_types/itinerary/
├── README.md                    # 本文件
├── __init__.py                  # 导出 ItineraryGenerator
├── generator.py                 # ★ 生成器主逻辑 + 日志体系
├── sampler.py                   # ★ 采样器 (角色/酒店/POI/事件模板/通勤预查)
├── prompts.py                   # LLM system/user prompt + parse
├── constraint_builder.py        # LLM HTTP 调用 + token 追踪
├── validator.py                 # 规则验证 (时间窗/事件顺序/地理相干/用餐间隔)
├── __main__.py                  # 独立 CLI 入口
├── vis_server.py                # GT 审核服务器
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
  ├── 1. 采样角色 (ItinerarySampler.sample_character)
  │      从 characters.json 中随机选 1 个虚拟人物
  │      记录: 出行偏好、预算、家庭位置
  │
  ├── 2. 确定行程天数
  │      2天(10%) / 3天(45%) / 4天(30%) / 5天(15%)
  │
  ├── 3. 采样酒店 (sample_hotel)
  │      从 world.json 的 services 中选 1 个 hotel_booking 类型
  │      记录: 位置、评分、入住/退房时间
  │
  ├── 4. 采样多类别 POI (sample_pois_by_category)
  │      restaurant: 3-5个/天    (restaurant_reservation 类型)
  │      attraction: 2-3个/天    (attraction_visit: museum, tourist_attraction 等)
  │      cafe: 1-2个/天          (local_visit: cafe, coffee_shop 等)
  │      transit: 1-2个          (train_station 用于到达/出发)
  │      → 确保地理多样性 (≥2 个不同 district)
  │      → 每个 POI 附带: 营业时间、预估停留时长、价格等级、评分
  │
  ├── 5. 构建事件模板 (build_event_catalog)
  │      Day 1:   arrival → (lunch) → hotel_checkin → dinner
  │      Day 2..N-1: breakfast → morning_activity → lunch → afternoon_activity → dinner
  │      Day N:   breakfast → (morning_activity) → checkout → lunch → departure
  │      → 固定事件 (is_fixed=True): checkin, checkout, arrival, departure
  │      → 灵活事件: 用餐和活动由 Agent 自由安排顺序和时间
  │      → 约25%任务注入合成固定事件 (synthetic_fixed_event):
  │        business_meeting / appointment 等，迫使 Agent 在特定时间窗内安排
  │
  ├── 6. 预查所有通勤时间 (precheck_all_commutes)
  │      对所有 POI 对 × 3 种模式 (DRIVE/WALK/TRANSIT) 调用 RouteCache
  │      → 返回 route_data dict + query_log list
  │      后期任务因缓存命中率高，API 成本逐步降低
  │
  ├── 7. LLM 生成场景 + 结构化 GT (constraint_builder.build_scenario)
  │      将角色信息 + 酒店 + POI + 事件模板 + 通勤数据 喂给 LLM (cliproxy_gpt55_pro)
  │      LLM 输出:
  │        user_prompt: 自然的中文行程规划场景描述 (300-600字)
  │        gt: { expected_feasible, constraints, correct_itinerary, acceptable_itineraries }
  │      → 最多重试 3 次，每次验证通过才接受
  │
  ├── 8. 规则验证 (validator.validate_result)
  │      时间窗口: 事件在 POI 营业时间内
  │      事件顺序: 每天事件时间严格递增
  │      地理相干: 连续事件间通勤时间 ≤ 实际 route_data + buffer
  │      用餐间隔: lunch/dinner 间隔 ≥ 4 小时
  │
  ├── 9. 写 task_sandbox.json (generator)
  │      character + hotel + all_pois + event_catalog + route_data +
  │      constraints + correct_itinerary + acceptable_itineraries
  │
  └── 10. 写 task.json
          user_prompt + task_id + task_type + summary
```

---

## 变化维度

| 维度 | 策略 | 分布 |
|------|------|------|
| **天数** | 2天 / 3天 / 4天 / 5天 | 10/45/30/15% |
| **事件数** | 每天 4-5 个事件 (含固定) | 总事件 8-25 个 |
| **POI 数量** | restaurant 3-5/天 + attraction 2-3/天 + cafe 1-2/天 + transit 1-2 | 总 POI 10-40 个 |
| **固定事件** | checkin/checkout/到达/出发 (必有) + 合成固定事件 (25%概率) | 必有 + 可选 |
| **可行性** | ~80% feasible, ~20% infeasible | LLM 控制 |
| **预算** | daily ¥300/500/800/1000 | 随机 |
| **出行方式** | TRANSIT / DRIVE 偏好 | 随机 |
| **地理多样性** | POI 覆盖 ≥2 个 district | 采样保证 |

---

## task_sandbox.json 结构

```json
{
  "task_type": "itinerary",
  "num_days": 3,
  "character": {
    "character_id": "char_001",
    "name": "张经理",
    "age": 35,
    "occupation": "律师",
    "home_place_id": "place_A",
    "home_place_name": "上海",
    "preferred_transit": "TRANSIT",
    "budget_daily": 500,
    "preferences": {}
  },
  "hotel": {
    "service_id": "svc_H1",
    "place_id": "place_H1",
    "name": "希尔顿",
    "location": {"lat": 35.7, "lng": 139.7},
    "district_name": "新宿",
    "rating": 4.2,
    "price_level": "EXPENSIVE"
  },
  "all_pois": {
    "restaurant": [
      {
        "place_id": "place_R1",
        "name": "寿司大",
        "location": {"lat": 35.69, "lng": 139.78},
        "category": "restaurant",
        "district_name": "银座",
        "rating": 4.5,
        "price_level": "MODERATE",
        "opening_hours": "11:00-22:00",
        "duration_typical_min": 90
      }
    ],
    "attraction": [...],
    "cafe": [...],
    "transit": [...]
  },
  "event_catalog": [
    {
      "event_id": "evt_arrival_p_001_1",
      "event_type": "arrival",
      "place_id": "place_T1",
      "place_name": "东京站",
      "category": "transit",
      "day": 1,
      "is_fixed": true,
      "duration_min": 60
    },
    {
      "event_id": "evt_lunch_p_R1_1",
      "event_type": "lunch",
      "place_id": "place_R1",
      "place_name": "寿司大",
      "category": "restaurant",
      "day": 1,
      "is_fixed": false,
      "duration_min": 90,
      "earliest": "11:30",
      "latest": "13:30"
    }
  ],
  "route_data": {
    "希尔顿→寿司大": {"DRIVE": 15, "TRANSIT": 25, "WALK": 45},
    "东京站→希尔顿": {"DRIVE": 20, "TRANSIT": 30, "WALK": 90}
  },
  "constraints": {
    "max_daily_budget": 500,
    "max_event_gap_min": 120,
    "min_meal_interval_hours": 4
  },
  "expected_feasible": true,
  "correct_itinerary": {
    "days": [
      {
        "day": 1,
        "events": [
          {
            "event_id": "evt_arrival_p_001_1",
            "event_type": "arrival",
            "place_id": "place_T1",
            "place_name": "东京站",
            "start": "09:00",
            "end": "10:00",
            "transit_from_event_id": null,
            "commute_mode": "TRANSIT",
            "commute_min": 30
          },
          {
            "event_id": "evt_lunch_p_R1_1",
            "event_type": "lunch",
            "place_id": "place_R1",
            "place_name": "寿司大",
            "start": "11:00",
            "end": "12:30",
            "transit_from_event_id": "evt_arrival_p_001_1",
            "commute_mode": "TRANSIT",
            "commute_min": 25
          }
        ]
      }
    ]
  },
  "acceptable_itineraries": [
    {
      "days": [...]
    }
  ]
}
```

---

## Agent 可用工具

Agent 在测试时通过以下 sandbox 调用工具：

| App | 工具 | 说明 |
|-----|------|------|
| **MapApp** | `search_places(query)` | 搜索地点 |
| | `query_routes(origin_id, dest_id, mode)` | 查询两点间路线 (WALK/DRIVE/TRANSIT/ALL) |
| | `get_place_detail(place_id)` | 查看地点详情 |
| **LocalServiceApp** | `search_services(district_id, service_type, filters)` | 搜索可用服务 (restaurant/hotel/attraction/cafe) |
| | `get_service_detail(service_id)` | 查看服务详情（价格、评分、营业时间、位置） |
| | `make_reservation(service_id, party_size, time)` | 预订服务 (餐厅/酒店/景点) |
| | `check_availability(service_id)` | 查看可用时间段 |
| **MessagingApp** | `send_message(recipient_id, content)` | 向参与者发送消息 |
| | `get_contact(contact_id)` | 查看联系人信息 |
| | `create_group(name, member_ids)` | 创建群组 |
| **CalendarApp** | `get_availability(character_id)` | 查看某人的空闲时间窗口 |
| | `check_conflicts(character_ids, start_time, end_time)` | 检测多人时间冲突 |
| **UserInterfaceApp** | `final_answer(feasible, route_ids, mode, explanation, plan)` | 提交最终行程方案 |

### final_answer 格式

对于 itinerary 任务，agent 通过 `plan` 参数提交行程方案：

```json
{
  "feasible": true,
  "plan": {
    "days": [
      {
        "day": 1,
        "events": [
          {
            "event_type": "arrival",
            "place_id": "place_T1",
            "place_name": "东京站",
            "start": "09:00",
            "end": "10:00",
            "commute_mode": "TRANSIT",
            "commute_min": 30
          },
          {
            "event_type": "lunch",
            "place_id": "place_R1",
            "place_name": "寿司大",
            "start": "11:00",
            "end": "12:30",
            "commute_mode": "TRANSIT",
            "commute_min": 25
          }
        ]
      }
    ],
    "daily_budgets": [450, 500, 480],
    "total_budget": 1430
  },
  "explanation": "Day1先到东京站，乘地铁去银座寿司大午餐。14:00入住新宿希尔顿..."
}
```

---

## 评测指标 (8 项)

| # | 指标 | 验证内容 | GT 来源 |
|---|------|---------|---------|
| 1 | `feasible_consistency` | Agent 可行性判断与 GT 一致 | expected_feasible |
| 2 | `itinerary_completeness` | 覆盖 ≥70% 的 GT 事件类型 | correct_itinerary.days[].events[].event_type |
| 3 | `time_window_valid` | 事件在 POI 营业时间内 | all_pois[].opening_hours |
| 4 | `event_order_valid` | 每天事件时间严格递增 | (事件自身 start/end) |
| 5 | `spatial_coherence` | 连续事件间通勤时间 ≤ route_data 记录值 | route_data |
| 6 | `budget_valid` | 每日预算合规 | character.budget_daily |
| 7 | `constraint_satisfaction` | max_event_gap / min_meal_interval 满足 | constraints |
| 8 | `tool_efficiency` | 工具调用 ≤15 次 | (trace 计数) |

评分等级：≥70% PASS, 40-69% PARTIAL, <40% FAIL（与 route_planning / multi_entity 一致）。

---

## LLM 生成模式

### 模型

使用 `config.yaml` 中的 `cliproxy_gpt55_pro` endpoint (gpt-5.5 via CLIProxyAPI Pro)。

`max_completion_tokens=4096`（比 multi_entity 的 2048 更大，因为行程计划输出较长）。

### Prompt 流程

```
SYSTEM: 你是多日行程规划场景的设计师...
  → 约80%可行/20%不可行
  → 确保事件在营业时间内、通勤时间留有 buffer
  → 用餐对齐(午餐11:30-13:30, 晚餐18:00-20:30, 间隔≥4h)
  → 同半天内事件在邻近区域(通勤≤30min)

USER: 旅行者信息 + 住宿 + 每日事件需求 + POI 详情 + 通勤时间参考
  → 生成自然的中文场景 (300-600字)
  → 输出结构化 GT: { expected_feasible, constraints, correct_itinerary, acceptable_itineraries }

LLM → JSON: { user_prompt, gt: {...} }
```

### 输出示例

```
user_prompt: "张经理将在东京停留3天，需要参加一场商务会议、参观2个博物馆、品尝当地美食。酒店预订了新宿希尔顿。第一天到达东京站后需要入住酒店，晚上安排了晚餐。第二天上午有商务会议在丸之内（需Agent确认具体会议室位置并安排在附近），下午和晚上可以自由安排。请结合营业时间、交通成本、区域分布和日程依赖，生成完整的3天行程方案。"

gt: {
  expected_feasible: true,
  constraints: { max_daily_budget: 500, max_event_gap_min: 120, min_meal_interval_hours: 4 },
  correct_itinerary: {
    days: [...]
  },
  acceptable_itineraries: [
    { days: [...] }
  ]
}
```

---

## 约束推导与 GT 生成

### mixed 模式: Sampler + LLM + Validator

与 route_planning (纯规则推导) 和 multi_entity (纯 LLM 生成) 不同，itinerary 采用混合模式：

1. **Sampler**: 采样真实 POI + 预查真实通勤时间 → 提供事实基础
2. **LLM**: 在事实基础上生成自然场景 + 结构化 GT (temperature=0.8，保证多样性)
3. **Validator**: 规则验证 LLM 输出:
   - 事件是否在 POI 营业时间内
   - 连续事件间通勤时间是否足够 (≥ route_data 最短值)
   - 事件是否按时间顺序排列
   - 用餐间隔是否 ≥4 小时
4. **Retry 机制**: 最多重试 3 次，validator 未通过则重新请求 LLM

### 评测方式

- **feasible 任务**: agent 提交每天的完整 schedule，evaluator 逐项校验 8 项指标
- **infeasible 任务**: agent 必须输出 `feasible=false` 并说明违反的约束
- evaluator 读取 `task_sandbox.json`（Agent 不可见）进行 GT 比对

---

## 可视化

### 审核前端 (GT Review)

```bash
python -m task_module.task_types.itinerary.vis_server \
    --tasks outputs/benchmarks/itinerary/tokyo_complex/latest/tasks/ \
    --world outputs/mapseeds/tokyo_complex/base/world.json \
    --port 8772
```

功能：
- **地图**: 酒店（红点）、所有 POI 按类别着色 (restaurant=橙, attraction=蓝, cafe=绿, transit=紫)
- **Timeline**: 按天折叠的 GT 行程展示（时间+事件类型+地点名称）
- **Generation 信息**: 展示 token 消耗、API 调用次数、重试次数
- **审核标记**: Approve/Reject + Reviewer 姓名 + 备注 + 时间戳

### 审核流程

```
1. 启动 vis_server
2. 逐一点击 task，观察地图上 POI 分布和 GT Timeline 是否合理
3. 如果 GT 有问题，前端可直接编辑
4. 审核通过点 Approve，有疑问点 Reject 并填写备注
5. review_status.json 记录所有审核状态，可被下游 test_runner 读取
```

---

## CLI 用法

```bash
# 独立 CLI 生成
python3 -m task_module.task_types.itinerary \
    --city shanghai --count 10 --seed 42

# 主 pipeline 生成
python -m task_module.generator \
    --city tokyo_complex --types itinerary --counts 10

python3 -m task_module.generator \
    --city shanghai --types itinerary --counts 10
# 新建批次
python -m task_module.generator \
    --city tokyo_complex --types itinerary --counts 10 --new-batch

# 审核前端
python3 -m task_module.task_types.itinerary.vis_server \
    --tasks outputs/benchmarks/itinerary/tokyo_complex/latest/tasks/ \
    --world outputs/mapseeds/tokyo_complex/base/world.json \
    --port 17033

# 测试单个任务
python -m test_runner.runner \
    --task outputs/benchmarks/itinerary/tokyo_complex/latest/tasks/task_0001_xxx/ \
    --routes outputs/routes/tokyo_complex/route_cache.json

# 批量测试
python -m test_runner.runner \
    --tasks outputs/benchmarks/itinerary/tokyo_complex/latest/tasks/ \
    --routes outputs/routes/tokyo_complex/route_cache.json
```

---

## 日志文件

### 批次级日志 (generate.log)

每个批次目录下有独立的 `generate.log`，记录本批次的时间线：

```
12:00:00 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
12:00:00 BATCH START  20260625_120000
12:00:00   CITY tokyo_complex   TYPE itinerary   COUNT 10   SEED 42
12:00:00   LLM cliproxy_gpt55_pro/gpt-5.5
12:00:00 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
12:00:05 [task_0001] ✓ 3d 12e | feasible=True | 2500→800 tok | 1.2s | 15 API
12:00:10 [task_0002] ✓ 4d 15e | feasible=False | 2800→900 tok | 1.5s | 18 API
...
12:02:00 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
12:02:00 BATCH SUMMARY  success=10  skip=0
12:02:00   Tokens: 25000+8000=33000 | LLM: 12.0s | Total: 120.0s
12:02:00   API: 150 route calls
12:02:00 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Per-task 日志 (generation_log/ 子目录)

| 文件 | 内容 | 用途 |
|------|------|------|
| `llm_in.txt` | 发给 GPT 的完整 system + user prompt (含角色/酒店/POI/事件/通勤全部数据) | 可复现 |
| `llm_out.json` | GPT 原始返回的完整 JSON (user_prompt + gt) | 可调试 |
| `details.json` | 结构化摘要：角色/酒店/POI数量/GT摘要/token/API调用/重试 | 可扫描 |
| `constraints.json` | GT constraints 审计 | 调试约束 |
| `compatibility.json` | 酒店/POI通勤矩阵/预算总览 | 可审计 |
| `route_queries.json` | 所有 Route API 调用记录 (source/duration/error) | 可审计 |

---

## Python API

```python
from shared.io import read_json
from task_module.task_types.itinerary.generator import ItineraryGenerator

world = read_json("outputs/mapseeds/tokyo_complex/complex/world.json")
characters = read_json("outputs/characters/tokyo_complex/complex/characters.json")

gen = ItineraryGenerator(
    world=world,
    characters=characters,
    env_events=[],          # dynamic events from weather/news app (future)
    judge_nodes=[],         # judge sequence (future)
    output_dir="outputs/benchmarks/itinerary/tokyo_complex/latest/",
    seed=42,
    config_yaml="config.yaml",
)

tasks = gen.generate(count=10)
print(f"Generated {len(tasks)} tasks")
```

---

## 与其他 Task Type 的关系

| 维度 | route_planning | multi_entity | **itinerary** |
|------|:---:|:---:|:---:|
| 评测核心 | 单次路线约束推理 | 多方聚餐协调 | **多日事件排序 + 时间窗调度** |
| 参与者 | 1-2人 | 3-8人 | **1人** |
| 时间跨度 | 单次出行 (分钟) | 单次聚餐 (小时) | **多日行程 (天)** |
| 服务类型 | 2种 (search/route) | 5种 (含餐厅/消息/日历) | **5种 (含酒店/景点/交通枢纽)** |
| GT 生成 | 规则推导 (route_cache) | LLM 生成 | **Sampler + LLM + Validator (混合)** |
| GT 格式 | constraints + acceptable_plan_ids | constraints + acceptable_plans + restaurant | **constraints + correct_itinerary + acceptable_itineraries** |
| 评测指标 | 7项 | 7项 | **8项** |
| 环境事件 | 内置 (weather/transit) | 内置 (actor/transit/service) | **无内置 (留给 weather/news app)** |

---

## 扩展指南

### 添加新的事件类型

1. `sampler.py` → `DAY_TEMPLATES` 添加新 slot
2. `prompts.py` → `build_llm_user_prompt` 中传递新事件到 LLM
3. `validator.py` → 添加新的规则检查（如有必要）

### 添加新的服务类别

1. `sampler.py` → `SERVICE_CATEGORIES` 添加映射
2. `sampler.py` → `_collect_candidates` 在 `raw` dict 中声明新 key
3. `local_service.py` → `_get_items` 添加新分支

### 调整不可行任务比例

修改 `prompts.py` → `SYSTEM_ITINERARY` 中的描述 (当前为 "约80%概率可行")。

### 切换 LLM 模型

修改 `constraint_builder.py` 中的 endpoint_name，或通过 `config_yaml` 参数指定不同的模型端点。

### 增加天数分布权重

修改 `sampler.py` → `determine_duration()` 中的 `weights` 参数。
