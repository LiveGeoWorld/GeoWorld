# 地点发现模块

本目录实现 `place_discovery` 的构建与任务合同投影。研究设计的唯一规范来源是
[`Design/task_specs/place_discovery.md`](../../../Design/task_specs/place_discovery.md)，跨任务
的世界绑定、三类角色隔离、正式 batch 和成本规则见
[`SYSTEM_DESIGN.md`](../../../SYSTEM_DESIGN.md)。

## 当前正式链路

```text
冻结 world / city / active version 中的真实 POI 与公开详情
-> 私有侧建立可区分的约束、可接受候选与干扰候选
-> 私有求解器证明至少一条公开可执行的检索-比较-选择路径
-> 构建 Agent 仅把已验证的公开事实写成自然题面
-> 逐题合同、provenance 与泄露检查
-> formal task
```

构建 Agent 可以接触私有候选和事实来生成题目，但不能将 Gold、内部 POI ID、候选全集、
城市/世界路径或私有可行性结论写入题面、App Observation 或公开日志。测试 ReAct Agent
从统一 App 目录主动打开 MapApp；只可依据公开的名称、类型、地址和详情做检索与比较。
无命中不包含纠错、城市推荐或 Gold 线索。

Judge 在运行结束后才读取私有约束和可接受集合，核验最终选择、公开证据路径与 trace；
它不要求 Agent 猜测内部 ID，也不会在运行时修复选择或补写回答。当前历史 batch 若缺少
逐题 provenance 或公开 App/Judge 身份契约，则保留审计但不得直接视为可评测正式任务。

地点发现是静态发现任务。任何首次决策后才出现的服务、天气、交通或人员变化，应通过
`dynamic_replanning` 的固定事件层评估，而不能直接混入本任务。
