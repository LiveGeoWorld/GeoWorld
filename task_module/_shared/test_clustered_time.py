from datetime import date
from itertools import islice
from random import Random

import pytest

from .clustered_time import (
    assign_clustered_time,
    holiday_neighbor_dates,
    overlapping_events,
    remap_local_date,
)


def calendar():
    return {"year": 2026, "timezone": "Asia/Singapore", "core_holidays": [
        {"start": "2026-02-17", "end": "2026-02-18"},
        {"start": "2026-05-01", "end": "2026-05-05"},
    ]}


def choose(**overrides):
    args = dict(calendar=calendar(), previous_times=[], source_dates=None,
                available_minutes=lambda day: range(16 * 60, 23 * 60), rng=Random(42))
    args.update(overrides)
    return assign_clustered_time(**args)


def test_interval_boundaries_and_round_robin():
    assert [str(day) for day in islice(holiday_neighbor_dates(calendar()), 8)] == [
        "2026-02-16", "2026-04-30", "2026-02-19", "2026-05-06",
        "2026-02-15", "2026-04-29", "2026-02-20", "2026-05-07"]
    days = list(holiday_neighbor_dates(calendar()))
    assert len(days) == len(set(days))
    assert all(day.year == 2026 for day in days)


def test_reuse_date_and_clock_before_new_anchors():
    context, audit = choose(previous_times=["2026-07-02T18:12:00+08:00"])
    assert context["instruction_time"] == "2026-07-02T18:12:00+08:00"
    assert audit["date_origin"] == "reused_date"
    assert audit["clock_origin"] == "reused"
    assert "episode_end" not in context


def test_reuse_date_with_new_open_clock():
    context, audit = choose(previous_times=["2026-07-02T10:00:00+08:00"])
    assert context["instruction_time"].startswith("2026-07-02T")
    assert int(context["instruction_time"][11:13]) >= 16
    assert audit["clock_origin"] == "seeded_available"


def test_source_date_and_required_clock_override_history():
    context, _ = choose(source_dates=[date(2026, 12, 25)], required_minute=18 * 60,
                        previous_times=["2026-07-02T18:12:00+08:00"])
    assert context["instruction_time"] == "2026-12-25T18:00:00+08:00"


def test_required_weekday_is_preserved_when_reusing_clustered_dates():
    context, _ = choose(previous_times=["2026-07-02T18:12:00+08:00"], required_weekday=3)
    assert context["instruction_time"].startswith("2026-07-02T")


def test_rejected_candidate_moves_to_next_clustered_date():
    seen = []

    def check(candidate):
        seen.append(candidate["instruction_time"][:10])
        return {"status": "rejected", "reason": "event_overlap"} if len(seen) == 1 else {"status": "accepted"}

    context, audit = choose(candidate_checker=check)
    assert len(seen) == 2
    assert context["instruction_time"][:10] == seen[1]
    assert audit["skipped_dates"][0]["reason"] == "event_overlap"


def test_unresolved_candidate_is_not_auto_passed():
    with pytest.raises(ValueError, match="No source-compatible"):
        choose(candidate_checker=lambda candidate: {
            "status": "unresolved", "reason": "legacy_event_scope_missing",
        })


def test_holiday_start_preferred_but_real_source_can_override():
    start, end = date(2026, 2, 17), date(2026, 2, 18)
    context, _ = choose(source_dates=[start, end], preferred_source_date=start,
                        previous_times=["2026-02-18T18:00:00+08:00"])
    assert context["instruction_time"].startswith(str(start))
    context, _ = choose(source_dates=[end], preferred_source_date=end)
    assert context["instruction_time"].startswith(str(end))


def test_closed_date_advances_without_capacity_gate():
    context, audit = choose(available_minutes=lambda day: [] if day == date(2026, 2, 16) else [1080])
    assert context["instruction_time"] == "2026-04-30T18:00:00+08:00"
    assert audit["skipped_dates"] == [{"date": "2026-02-16", "reason": "no_available_clock"}]
    context, _ = choose(previous_times=[context["instruction_time"]] * 200)
    assert context["instruction_time"] == "2026-04-30T18:00:00+08:00"


def test_seed_replay():
    assert choose() == choose()


def test_unknown_hours_are_not_invented():
    with pytest.raises(ValueError, match="Unresolved"):
        choose(available_minutes=lambda day: None)


def test_empty_source_dates_do_not_fall_back_to_arbitrary_dates():
    with pytest.raises(ValueError, match="No source-compatible"):
        choose(source_dates=[])


def test_nonexistent_dst_clock_is_not_selected():
    london = {**calendar(), "timezone": "Europe/London"}
    with pytest.raises(ValueError, match="No source-compatible"):
        choose(calendar=london, source_dates=[date(2026, 3, 29)],
               available_minutes=lambda day: [90])


def test_remap_local_date_preserves_clock_and_timezone():
    assert remap_local_date(
        "2026-03-20T18:12:00+08:00", date(2026, 3, 27), "Asia/Shanghai",
    ) == "2026-03-27T18:12:00+08:00"


def test_overlapping_events_uses_half_open_windows_and_scope_filter():
    events = [
        {"event_id": "active", "scope": "route", "active_window": {
            "start": "2026-03-20T18:00:00+08:00",
            "end": "2026-03-20T20:00:00+08:00",
        }},
        {"event_id": "other-scope", "scope": "place", "active_window": {
            "start": "2026-03-20T18:00:00+08:00",
            "end": "2026-03-20T20:00:00+08:00",
        }},
    ]
    matches = overlapping_events(
        events,
        candidate_start="2026-03-20T19:00:00+08:00",
        scope_matches=lambda event: event.get("scope") == "route",
    )
    assert [event["event_id"] for event in matches] == ["active"]
    assert overlapping_events(
        events[:1], candidate_start="2026-03-20T20:00:00+08:00",
    ) == []


def test_overlapping_events_keeps_unknown_end_as_conflict():
    events = [{"event_id": "open-ended", "active_window": {
        "start": "2026-03-20T18:00:00+08:00",
    }}]
    assert overlapping_events(
        events, candidate_start="2026-03-20T20:00:00+08:00",
    )[0]["event_id"] == "open-ended"
