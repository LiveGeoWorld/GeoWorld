"""LocalServiceApp - source-backed service detail and operations.

MapApp discovers every frozen POI. LocalServiceApp receives that same POI
identity universe as a world-level projection, then serves merchant detail and
operational state only after an Agent identifies a place by name and location.
"""

from app.base import AppSpec, AppFunction, AppArg, AppCheck


_IDENTITY_ARGS = [
    AppArg(name="name", desc="MapApp 返回的公开地点名称", required=True),
    AppArg(name="location", desc="MapApp 返回的公开坐标 {lat, lng}", required=True),
]


APP_LOCAL_SERVICE = AppSpec(
    name="LocalServiceApp",
    label_zh="本地服务",
    description="为已由地图识别的地点提供来源支持的商户/服务详情、可用性和预约操作。",
    version="v2.app.v2",
    world_projections=["Place", "ServiceEndpoint"],
    observes=["service_state", "slot_inventory_changed", "reservation_status"],
    state_fields={
        "services": "dict[public name+location, ServiceState] - 与 MapApp POI 集一一对应",
        "merchant_information": "source-backed descriptions, menus, and service information",
        "reservations": "dict[reservation_id, ReservationState]",
    },
    functions=[
        AppFunction(
            name="get_service_detail",
            label_zh="查看服务详情",
            description_zh="查看已识别地点的来源支持商户介绍、菜单/服务内容和冻结运营信息。",
            access="read",
            args=_IDENTITY_ARGS,
            returns_desc="{found, name, location, merchant_information, opening_hours, availability, ...}",
            oracle_action_type="get_service_detail",
            hard_checks=[AppCheck(check_type="field_not_empty", field="response.name")],
            soft_checks=[],
        ),
        AppFunction(
            name="check_availability",
            label_zh="查看可用性",
            description_zh="查看已识别服务当前的营业和可用状态，不表示预约容量或库存。",
            access="read",
            args=[*_IDENTITY_ARGS, AppArg(name="date", desc="日期 YYYY-MM-DD", required=False)],
            returns_desc="{found, open_status, availability, source}；资料不足时为 unknown",
            oracle_action_type="check_availability",
            hard_checks=[],
            soft_checks=[],
        ),
        AppFunction(
            name="make_reservation",
            label_zh="预约服务",
            description_zh="预约已识别服务。",
            access="write",
            args=[
                *_IDENTITY_ARGS,
                AppArg(name="party_size", desc="人数", type_hint="int", required=True),
                AppArg(name="time", desc="预约时间 HH:MM", required=False),
                AppArg(name="guest_name", desc="预约人姓名", required=False),
                AppArg(name="special_requests", desc="特殊需求", required=False),
            ],
            returns_desc="预约确认 {reservation_id, status, service_name, time, party_size}",
            oracle_action_type="make_reservation",
            hard_checks=[
                AppCheck(check_type="action_executed"),
                AppCheck(check_type="field_equals", field="response.status", expected="confirmed"),
            ],
            soft_checks=[],
        ),
        AppFunction(
            name="cancel_reservation",
            label_zh="取消预约",
            description_zh="取消已提交的预约。",
            access="write",
            args=[AppArg(name="reservation_id", desc="预约确认返回的公开预约编号", required=True)],
            returns_desc="取消确认 {status: cancelled}",
            oracle_action_type="cancel_reservation",
            hard_checks=[AppCheck(check_type="field_equals", field="response.status", expected="cancelled")],
            soft_checks=[],
        ),
    ],
)
