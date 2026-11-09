"""Route-specific guidance within the shared final-answer tool."""


def add_mobility_submission_guidance(schema: dict) -> None:
    instructions = (
        "  * legs (Mobility Planning): If feasible is true, list each leg in travel order, "
        "from the origin through all required stops to the destination. For each leg, copy "
        "the public origin and destination name and coordinates used in MapApp.query_routes, "
        "and set the leg's mode to the observed WALK, TRANSIT, or DRIVE. A direct trip has "
        "one leg. If feasible is false, omit plan and explain the blocking requirement.\n"
    )
    schema["description"] = schema["description"].replace(
        "- route_ids: 路线ID列表（可选，route_planning任务使用）\n", ""
    ).replace(
        "- plan: 结构化方案字典（可选），其中可包含：\n",
        "- plan: 结构化方案字典（可选），其中可包含：\n" + instructions,
    )

    properties = schema["parameters"]["properties"]
    properties.pop("route_ids")
    plan = properties["plan"]
    plan["description"] = "结构化方案（可选），可包含逐段路线或行程数据"
    place = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "location": {
                "type": "object",
                "properties": {"lat": {"type": "number"}, "lng": {"type": "number"}},
                "required": ["lat", "lng"],
            },
        },
        "required": ["name", "location"],
    }
    plan["properties"]["legs"] = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "properties": {
                "origin": place,
                "destination": place,
                "mode": {"type": "string", "enum": ["WALK", "TRANSIT", "DRIVE"]},
            },
            "required": ["origin", "destination", "mode"],
        },
    }
