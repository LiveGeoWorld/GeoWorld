"""CityNewsApp — city events, festivals, area notices, traffic info."""

from app.base import AppSpec, AppFunction, AppArg, AppCheck

APP_CITY_NEWS = AppSpec(
    name="CityNewsApp",
    label_zh="城市资讯",
    description="提供城市活动、节日、本地新闻、区域公告等各类资讯查询",
    version="v3.app.v1",
    world_projections=["City", "District"],
    observes=["city_event", "local_news", "area_notice"],
    state_fields={
        "active_events": "list[dict] — 当前激活的城市资讯条目",
        "total_events": "int — 事件总数",
    },
    functions=[
        AppFunction(
            name="get_info",
            label_zh="查询城市资讯",
            description_zh="按类型查询城市资讯。info_type: events(活动), news(新闻/交通), notices(区域通告/天气影响)。返回匹配的资讯条目列表。",
            access="read",
            args=[
                AppArg(name="district_id", desc="区域ID（空字符串=不限）", required=False),
                AppArg(name="info_type", desc="类型: events / news / notices（空=全部）", required=False),
                AppArg(name="time_range", desc="时间范围 ISO格式如2025-07-05T15:00（空=全部时间）", required=False),
            ],
            returns_desc="资讯列表 [{type, category, severity, description, scope, timestamp}]",
            oracle_action_type="get_info",
            hard_checks=[AppCheck(check_type="field_not_empty", field="response")],
            soft_checks=[],
        ),
    ],
)
