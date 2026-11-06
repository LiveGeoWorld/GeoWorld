"""WeatherApp — weather-event queries, not forecasts."""
from app.base import AppSpec, AppFunction, AppArg, AppCheck

APP_WEATHER = AppSpec(
    name="WeatherApp", label_zh="天气事件",
    description="查询已公开的天气事件及解除通知，不提供日常天气或天气预报",
    version="v2.app.v1", world_projections=["City", "District"],
    observes=["weather", "weather_alert"],
    state_fields={"alerts": "list[WeatherAlert] — 保存的天气事件",
                  "current": "dict — 已保存天气事件的状态更新，不是日常天气"},
    functions=[AppFunction(
        name="get_alerts", label_zh="查询天气事件",
        description_zh="查询当前可见且有效的天气事件及解除通知；空结果不代表天气安全",
        access="read",
        args=[AppArg(name="district_id", desc="事件公开范围中的地区、城市或地点名称；不传查询全部", required=False)],
        returns_desc="保存的公开天气事件、范围、有效时间及解除通知；无匹配事件返回空列表",
        oracle_action_type="get_alerts",
        hard_checks=[AppCheck(check_type="action_executed")], soft_checks=[],
    )],
)
