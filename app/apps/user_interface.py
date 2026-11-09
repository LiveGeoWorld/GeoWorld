from __future__ import annotations
from typing import Any
from app.apps.base import BaseApp
'UserInterfaceApp sandbox — agent submits final answer through this.'

class UserInterfaceApp(BaseApp):
    """Sandboxed UserInterfaceApp for agent final answer submission.

    The agent submits a plan as a list of route_ids forming legs
    (e.g. ["route_001"] for direct, ["route_001", "route_002"] for waypoint).
    """
    name = 'UserInterfaceApp'

    def __init__(self, *, task_type: str = ""):
        super().__init__()
        self.task_type = task_type
        self.initial_plan_data: dict[str, Any] | None = None
        self.final_answer_data: dict[str, Any] | None = None

    def submit_initial_plan(self, feasible: bool, route_ids: list[str] | None=None, mode: str='', explanation: str='', plan: dict | None=None) -> dict[str, Any]:
        """Record the initial dynamic plan and advance to the next virtual checkpoint."""
        self.initial_plan_data = self._build_plan_payload(feasible=feasible, route_ids=route_ids, mode=mode, explanation=explanation, plan=plan)
        if self.env is None:
            return {'status': 'initial_plan_recorded', 'advanced': False}
        self.env.record_initial_plan(self.initial_plan_data)
        checkpoint = self.env.advance_to_next_checkpoint()
        return {'status': 'initial_plan_recorded', **checkpoint}

    def final_answer(self, feasible: bool, route_ids: list[str] | None=None, mode: str='', explanation: str='', plan: dict | None=None) -> dict[str, Any]:
        """Submit the final route plan / coordination plan / itinerary.

        For route_planning:
            feasible, route_ids, mode, explanation

        For multi_entity / itinerary (plan dict):
            {feasible, selection: {name, location}, assignments, days, ...}
        """
        if self.env is not None and self.env.disruptions and (not self.env.initial_plan_recorded):
            return {'status': 'initial_plan_required'}
        self.final_answer_data = self._build_plan_payload(feasible=feasible, route_ids=route_ids, mode=mode, explanation=explanation, plan=plan)
        return {'status': 'submitted', 'route_count': len(route_ids or [])}

    def advance_simulation(self, reason: str='agent_replanning_checkpoint') -> dict[str, Any]:
        """Advance virtual time instantly to the next causal-ready checkpoint."""
        if self.env is None:
            return {'advanced': False, 'virtual_time': None, 'activated_event_ids': [], 'notifications': []}
        return self.env.advance_to_next_checkpoint(reason=reason)

    @staticmethod
    def _build_plan_payload(feasible: bool, route_ids: list[str] | None, mode: str, explanation: str, plan: dict | None) -> dict[str, Any]:
        payload = {'feasible': feasible, 'route_ids': route_ids or [], 'mode': mode, 'explanation': explanation}
        if plan and isinstance(plan, dict):
            payload['plan'] = plan
        return payload

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        schemas = [{'name': 'submit_initial_plan', 'app': 'UserInterfaceApp', 'description': 'For a dynamic task, submit the initial feasible plan before the simulation advances. Continue after the returned checkpoint and submit the revised final answer only with final_answer.', 'parameters': {'type': 'object', 'properties': {'feasible': {'type': 'boolean'}, 'route_ids': {'type': 'array', 'items': {'type': 'string'}}, 'mode': {'type': 'string'}, 'plan': {'type': 'object'}, 'explanation': {'type': 'string'}}, 'required': ['feasible', 'explanation']}}, {'name': 'final_answer', 'app': 'UserInterfaceApp', 'description': 'For selected services and places, use only the public name and coordinates returned by an App; do not submit internal IDs.\n提交最终方案。调用后任务结束。\n- feasible: 是否找到了满足所有约束的方案（true/false）\n- route_ids: 路线ID列表（可选，route_planning任务使用）\n- mode: 出行方式（可选）\n- plan: 结构化方案字典（可选），其中可包含：\n  * selection: 选中的公开地点 {name, location}\n  * pickup_order: 接送顺序\n  * days: 每日行程列表 [{day, events: [{event_type, place_id, place_name, start, end}]}]\n  * daily_budgets: 每日预算列表\n  * recommendation: Safety 决策 accept/modify/postpone/reject\n  * rationale: 基于实际 Observation 的 Safety 决策理由\n  * alternatives: 安全替代地点 [{name, location: {lat, lng}}]，不得提交内部 ID\n  * safety_checks: 安全核查 [{app, tool, finding}]，说明采用的公开风险信息\n  * event_information: 动态任务中实际从所属 APP 读到并用于决策的公开事件消息文本列表\n  * cultural_actions: 行程中实际采取的现场行动（自然语言或 action 名称列表）\n  * selection: 单项地点/服务选择 {name, location}；name 和坐标必须来自公开 App 返回\n- explanation: 方案说明', 'parameters': {'type': 'object', 'properties': {'feasible': {'type': 'boolean', 'description': '是否找到可行方案'}, 'route_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': '路线ID列表（可选）'}, 'mode': {'type': 'string', 'description': '出行方式（可选）'}, 'plan': {'type': 'object', 'description': '结构化方案（可选），包含 days 等比 route_ids 更丰富的行程数据', 'properties': {'recommendation': {'type': 'string', 'enum': ['accept', 'modify', 'postpone', 'reject']}, 'rationale': {'type': 'string'}, 'alternatives': {'type': 'array', 'items': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'location': {'type': 'object', 'properties': {'lat': {'type': 'number'}, 'lng': {'type': 'number'}}, 'required': ['lat', 'lng']}}, 'required': ['name', 'location']}}, 'safety_checks': {'type': 'array', 'items': {'type': 'object', 'properties': {'app': {'type': 'string'}, 'tool': {'type': 'string'}, 'finding': {'type': 'string'}}, 'required': ['app', 'tool', 'finding']}}}}, 'explanation': {'type': 'string', 'description': '方案说明'}}, 'required': ['feasible', 'explanation']}}, {'name': 'advance_simulation', 'app': 'UserInterfaceApp', 'description': "Advance the benchmark's virtual city time to the next ready event checkpoint. This is instantaneous and never waits for wall-clock time. Use it after an initial plan or a replan when the episode has more scheduled changes.", 'parameters': {'type': 'object', 'properties': {'reason': {'type': 'string'}}, 'required': []}}]

        if self.task_type == "route_planning":
            from shared.mobility_submission import add_mobility_submission_guidance

            add_mobility_submission_guidance(schemas[1])
        return schemas
