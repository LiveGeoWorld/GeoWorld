"""MapApp — map search, route planning, transit, reviews.

Covers Google-Maps-like functionality: place search, navigation,
transit information, and user reviews/ratings.
"""

from app.base import AppSpec, AppFunction, AppArg, AppCheck

APP_MAP = AppSpec(
    name="MapApp",
    label_zh="地图",
    description="提供地点搜索、路线规划、交通查询、用户评论等功能。类比 Google Maps / 高德地图",
    version="v2.app.v1",
    world_projections=["Place", "District", "Corridor", "InformationItem"],
    observes=["place_location", "traffic", "transit", "mobility_alert"],
    state_fields={
        "places": "dict[place_id, PlaceInfo] — 地点基本信息",
        "routes": "dict[route_id, RouteInfo] — 路线信息",
        "reviews": "dict[place_id, list[Review]] — 用户评论",
        "traffic": "dict[corridor_id, TrafficState] — 实时交通",
    },
    functions=[
        # ── Place search ──
        AppFunction(
            name="search_places",
            label_zh="搜索地点",
            description_zh="搜索指定区域的地点/商户，支持类型筛选和关键词",
            access="read",
            args=[
                AppArg(name="district_id", desc="区域ID", required=False),
                AppArg(name="place_type", desc="地点类型（restaurant / hotel / attraction 等）", required=False),
                AppArg(name="keyword", desc="搜索关键词", required=False),
                AppArg(name="limit", desc="返回数量上限", type_hint="int", required=False),
            ],
            returns_desc="匹配的地点列表 [place_id, name, location, type, rating, address]",
            oracle_action_type="search_places",
            hard_checks=[
                AppCheck(check_type="action_executed"),
                AppCheck(check_type="field_not_empty", field="response.places"),
            ],
            soft_checks=[],
        ),
        AppFunction(
            name="get_place_detail",
            label_zh="查看地点详情",
            description_zh="查看地点的完整信息：地址、评分、营业时间、照片、周边信息",
            access="read",
            args=[AppArg(name="place_id", desc="地点ID", required=True)],
            returns_desc="地点详情 dict {name, address, rating, opening_hours, phone, website, district_name}",
            oracle_action_type="get_place_detail",
            hard_checks=[AppCheck(check_type="field_not_empty", field="response.name")],
            soft_checks=[],
        ),
        AppFunction(
            name="get_nearby",
            label_zh="搜索周边",
            description_zh="搜索某个地点周围的其他地点",
            access="read",
            args=[
                AppArg(name="place_id", desc="中心地点ID", required=True),
                AppArg(name="radius_m", desc="搜索半径（米）", type_hint="int", required=False),
                AppArg(name="place_type", desc="地点类型筛选", required=False),
            ],
            returns_desc="周边地点列表 [place_id, name, distance_m, type]",
            oracle_action_type="get_nearby",
            hard_checks=[AppCheck(check_type="field_not_empty", field="response.nearby")],
            soft_checks=[],
        ),
        AppFunction(
            name="search_grounding_candidates",
            label_zh="检索定位候选",
            description_zh="根据图片和文本中推断出的地图线索检索定位候选。仅使用地图字段和空间上下文，不注入 gold 答案。",
            access="read",
            args=[
                AppArg(name="keyword", desc="从用户描述/图片中抽取的地点、道路、区域或类型关键词", required=False),
                AppArg(name="city_id", desc="城市ID", required=False),
                AppArg(name="city_hint", desc="城市名称或提示", required=False),
                AppArg(name="district_id", desc="区域ID", required=False),
                AppArg(name="place_type", desc="地点类型", required=False),
                AppArg(name="nearby_anchor", desc="附近锚点名称", required=False),
                AppArg(name="limit", desc="返回数量上限", type_hint="int", required=False),
            ],
            returns_desc="定位候选列表 candidates，每项包含 candidate_id/place_id/name/type/location/match_explanation",
            oracle_action_type="search_grounding_candidates",
            hard_checks=[
                AppCheck(check_type="action_executed"),
                AppCheck(check_type="field_not_empty", field="response.candidates"),
            ],
            soft_checks=[],
        ),
        AppFunction(
            name="select_grounded_location",
            label_zh="选择定位结果",
            description_zh="从 MapApp 返回的候选中选择最终 grounded location，并给出简短理由。",
            access="read",
            args=[
                AppArg(name="candidate_id", desc="候选ID，通常为 place_id 或 map_location:<id>", required=True),
                AppArg(name="rationale", desc="选择理由", required=False),
            ],
            returns_desc="选择结果 {selected, candidate_id, place_id, location, rationale}",
            oracle_action_type="select_grounded_location",
            hard_checks=[
                AppCheck(check_type="action_executed"),
                AppCheck(check_type="field_not_empty", field="response.candidate_id"),
            ],
            soft_checks=[],
        ),

        # ── Route & transit ──
        AppFunction(
            name="plan_route",
            label_zh="规划路线",
            description_zh="规划两点之间的出行路线，支持步行、驾车、公共交通等多种模式",
            access="read",
            args=[
                AppArg(name="origin_place_id", desc="起点地点ID", required=True),
                AppArg(name="dest_place_id", desc="终点地点ID", required=True),
                AppArg(name="mode", desc="出行方式 (walking / driving / transit)", required=False),
                AppArg(name="waypoints", desc="途经点列表 [place_id, ...]", required=False),
            ],
            returns_desc="路线方案 {routes: [{distance_m, duration_min, mode, waypoints, polyline}]}",
            oracle_action_type="plan_route",
            hard_checks=[
                AppCheck(check_type="action_executed"),
                AppCheck(check_type="field_not_empty", field="response.routes"),
            ],
            soft_checks=[
                AppCheck(check_type="field_not_empty", field="response.routes[].eta", weight="soft"),
            ],
        ),
        AppFunction(
            name="check_transit",
            label_zh="查询交通状态",
            description_zh="查询公共交通或道路的实时状态，包括延误、拥挤、封路等",
            access="read",
            args=[
                AppArg(name="corridor_id", desc="交通走廊ID", required=False),
                AppArg(name="district_id", desc="区域ID（查询该区域所有交通）", required=False),
            ],
            returns_desc="交通状态 [{corridor_name, status, delay_min, congestion_level}]",
            oracle_action_type="check_transit",
            hard_checks=[AppCheck(check_type="action_executed")],
            soft_checks=[
                AppCheck(check_type="field_not_empty", field="response.status_list", weight="soft"),
            ],
        ),

        # ── Reviews ──
        AppFunction(
            name="get_reviews",
            label_zh="查看评论",
            description_zh="查看某个地点的用户评论和评分",
            access="read",
            args=[
                AppArg(name="place_id", desc="地点ID", required=True),
                AppArg(name="sort_by", desc="排序方式 (recent / rating / relevant)", required=False),
                AppArg(name="limit", desc="返回数量", type_hint="int", required=False),
            ],
            returns_desc="评论列表 [{author, rating, text, date, language}]",
            oracle_action_type="get_reviews",
            hard_checks=[AppCheck(check_type="field_not_empty", field="response.reviews")],
            soft_checks=[
                AppCheck(check_type="field_contains", field="response.reviews[].text", weight="soft"),
            ],
        ),
    ],
)
