from app.apps.calendar import CalendarApp


def calendar():
    app = CalendarApp()
    app._sandbox = {"participants": [
        {"character_id": "private_1", "name": "Mpho Daniels", "available_from": "18:42", "must_leave_by": "20:30"},
        {"character_id": "private_2", "name": "Lwazi Ndlovu", "available_from": "18:43", "must_leave_by": "20:30"},
    ]}
    return app


def test_names_and_legacy_ids_resolve_without_exposing_ids():
    app = calendar()
    for reference in ("Mpho Daniels", "  mpho  DANIELS ", "private_1"):
        result = app.check_conflicts([reference], "18:43", "19:43")["result"][0]
        assert result["available"] is True
        assert result["status"] == "available"
        assert "character_id" not in result
        assert app.get_availability(reference)["status"] == "ok"


def test_missing_identity_is_unknown_not_a_conflict():
    app = calendar()
    result = app.check_conflicts(["Unknown", "Lwazi Ndlovu"], "18:43", "19:43")["result"]
    assert result[0]["status"] == "participant_not_found"
    assert result[0]["available"] is None
    assert "conflict_with" not in result[0]
    assert result[1]["available"] is True
    assert app.get_availability("Unknown")["status"] == "participant_not_found"


def test_duplicate_names_are_not_arbitrarily_selected():
    app = calendar()
    app._sandbox["participants"].append({"character_id": "private_3", "name": "Mpho Daniels"})
    assert app.get_availability("Mpho Daniels")["status"] == "participant_ambiguous"
    result = app.check_conflicts(["Mpho Daniels"])["result"][0]
    assert result["status"] == "participant_ambiguous"
    assert result["available"] is None


def test_internal_event_updates_remain_visible_by_public_name():
    app = calendar()
    app.inject_entry({"scope": {"id": "private_1"}, "must_leave_by": "19:00"})
    result = app.check_conflicts(["Mpho Daniels"], "18:43", "19:43")["result"][0]
    assert result["status"] == "conflict"
    assert result["available"] is False
    assert "19:00" in result["conflict_with"]
    assert app.get_availability("Mpho Daniels")["windows"][0]["end"] == "19:00"


def test_schema_advertises_public_names():
    for schema in calendar().get_tool_schemas():
        if schema["name"] not in {"get_availability", "check_conflicts"}:
            continue
        key = "character_id" if schema["name"] == "get_availability" else "character_ids"
        assert "public name" in schema["parameters"]["properties"][key]["description"]
