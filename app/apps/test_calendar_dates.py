import pytest
from app.apps.calendar import CalendarApp


@pytest.mark.parametrize("city", ["Shanghai", "Tokyo", "Singapore", "London", "New York", "Cape Town"])
def test_six_cities(city):
    app = CalendarApp()
    assert app.get_festivals(city, "2026-01-01")["festivals"]
    assert app.get_holidays(city, "2026-01-01")["is_public_holiday"]


def test_dates_and_periods_are_distinct():
    app = CalendarApp()
    assert app.get_weekday("2026-02-17")["weekday"] == "Tuesday"
    festivals = app.get_festivals("上海", "2026-02-17")["festivals"]
    assert any(r["name"] == "Chinese New Year" for r in festivals)
    assert app.get_holidays("Shanghai", "2026-02-17")["holidays"][0]["start_date"] == "2026-02-15"
    assert app.get_holidays("Shanghai", "2026-02-14")["is_makeup_workday"]
    assert not app.get_holidays("Shanghai", "2026-02-14")["is_public_holiday"]


def test_range_inclusive_and_original_period():
    app = CalendarApp()
    result = app.get_holidays("Shanghai", start_date="2026-02-17", end_date="2026-02-17")
    assert result["holidays"][0]["end_date"] == "2026-02-23"
    result = app.get_festivals("Tokyo", start_date="2026-05-03", end_date="2026-05-05")
    assert {r["date"] for r in result["festivals"]} == {"2026-05-03", "2026-05-04", "2026-05-05"}
    assert app.get_holidays("London", "2026-12-28")["is_public_holiday"]


def test_empty_is_success_and_pending_not_public():
    app = CalendarApp()
    result = app.get_holidays("London", "2026-02-02")
    assert result["status"] == "ok" and not result["is_public_holiday"]
    result = app.get_festivals("New York", start_date="2026-01-01", end_date="2026-12-31")
    assert not any(r["category"] in {"United Nations observance", "Worldwide observance", "Other Observance"} for r in result["festivals"])


@pytest.mark.parametrize("args", [{}, {"date": "2025-01-01"}, {"date": "2026-02-30"},
    {"date": "2026-2-01"}, {"start_date": "2026-02-02"},
    {"start_date": "2026-02-02", "end_date": "2026-02-01"},
    {"date": "2026-01-01", "start_date": "2026-01-01", "end_date": "2026-01-02"}])
def test_invalid_inputs(args):
    assert CalendarApp().get_festivals("Shanghai", **args)["status"] == "invalid_date"


def test_unknown_city_and_missing_source(tmp_path):
    app = CalendarApp()
    assert app.get_holidays("Unknown", "2026-01-01")["status"] == "city_not_found"
    app._date_root = tmp_path
    assert app.get_holidays("Shanghai", "2026-01-01")["status"] == "calendar_data_unavailable"


def test_schema_registration():
    names = {s["name"] for s in CalendarApp().get_tool_schemas()}
    assert names == {"get_weekday", "get_festivals", "get_holidays", "get_availability", "check_conflicts"}


def test_environment_open_and_dispatch():
    from app.runtime.environment import Environment
    env = Environment()
    env.register_app(CalendarApp())
    env.configure_application_catalog({"CalendarApp": "Calendar queries."})
    assert len(env.get_tool_schemas()) == 1
    opened = env.execute_tool("SystemApp__open_app", {"app": "CalendarApp"})
    assert "CalendarApp__get_holidays" in {r["name"] for r in opened["tools"]}
    assert env.execute_tool("CalendarApp__get_weekday", {"date": "2026-02-17"})["weekday"] == "Tuesday"
    assert env.execute_tool("CalendarApp__get_holidays", {"city_name": "Shanghai", "date": "2026-02-17"})["is_public_holiday"]
