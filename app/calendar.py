"""CalendarApp — schedule management."""

from app.base import AppSpec, AppFunction, AppArg, AppCheck

APP_CALENDAR = AppSpec(
    name="CalendarApp",
    label_zh="日历",
    description="提供日程查看、事件添加和冲突检测",
    version="v2.app.v1",
    world_projections=["Actor", "EventTemplate"],
    observes=["time_constraint", "schedule_change"],
    state_fields={
        "events": "dict[event_id, CalendarEvent] — 日历事件",
        "availability": "dict[character_id, AvailabilityWindow] — 人物空闲时间",
    },
    functions=[
        AppFunction(
            name="get_availability",
            label_zh="查看空闲时间",
            description_zh="查看指定人物在指定时间范围内的空闲窗口",
            access="read",
            args=[
                AppArg(name="character_id", desc="人物ID", required=True),
                AppArg(name="date", desc="日期 YYYY-MM-DD", required=False),
            ],
            returns_desc="空闲时间段列表 [{start, end, is_free}]",
            oracle_action_type="get_availability",
            hard_checks=[AppCheck(check_type="field_not_empty", field="response.windows")],
            soft_checks=[],
        ),
        AppFunction(
            name="add_event",
            label_zh="添加事件",
            description_zh="在日历中添加一个事件",
            access="write",
            args=[
                AppArg(name="title", desc="事件标题", required=True),
                AppArg(name="start_time", desc="开始时间 ISO格式", required=True),
                AppArg(name="end_time", desc="结束时间 ISO格式", required=True),
                AppArg(name="location", desc="地点名称", required=False),
                AppArg(name="attendee_ids", desc="参与者ID列表", required=False),
            ],
            returns_desc="添加确认 {event_id, status: created}",
            oracle_action_type="add_event",
            hard_checks=[
                AppCheck(check_type="action_executed"),
                AppCheck(check_type="field_equals", field="response.status", expected="created"),
            ],
            soft_checks=[],
        ),
        AppFunction(
            name="check_conflicts",
            label_zh="检测时间冲突",
            description_zh="检查多人之间的时间是否可用",
            access="read",
            args=[
                AppArg(name="character_ids", desc="人物ID列表", required=True),
                AppArg(name="start_time", desc="开始时间 ISO格式", required=True),
                AppArg(name="end_time", desc="结束时间 ISO格式", required=True),
            ],
            returns_desc="冲突检测结果 [{character_id, available, conflict_with}]",
            oracle_action_type="check_conflicts",
            hard_checks=[AppCheck(check_type="field_not_empty", field="response.result")],
            soft_checks=[],
        ),
    ],
)
