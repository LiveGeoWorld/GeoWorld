"""Public category wording must resolve against frozen bus_station facts."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.runtime.map_app import MapAppRuntime
from test_runner.place.sandbox_adapter import PlaceGroundingSandbox


def make_runtime():
    places = [
        {'place_id': 'anchor', 'name': 'Corner Store', 'primary_type': 'store',
         'city_id': 'city', 'location': {'lat': 0, 'lng': 0}},
        {'place_id': 'stop', 'name': 'Central Stop', 'primary_type': 'bus_station',
         'city_id': 'city', 'location': {'lat': 0, 'lng': 0.001}},
        {'place_id': 'far', 'name': 'Distant Stop', 'primary_type': 'bus_station',
         'city_id': 'city', 'location': {'lat': 0, 'lng': 1}},
        {'place_id': 'cafe', 'name': 'Bus Stop Cafe', 'primary_type': 'cafe',
         'city_id': 'city', 'location': {'lat': 0, 'lng': 0.002}},
    ]
    return MapAppRuntime(places=places, cities=[{'city_id': 'city', 'name': 'London'}])


@pytest.mark.parametrize('query', [
    {'keyword': 'bus stop'}, {'keyword': 'bus station', 'category': 'bus stop'},
    {'keyword': 'bus stop', 'category': 'bus stop'},
    {'keyword': 'BUS_STOP', 'category': 'bus-stop'},
])
def test_public_bus_stop_query_matches_existing_bus_station_query(query):
    app = PlaceGroundingSandbox(SimpleNamespace(map_app=make_runtime()))
    city = app.search_city(city_name='London')['cities'][0]['city_handle']
    anchor = app.search_grounding_candidates(keyword='Corner Store', city_handle=city)['candidates'][0]['candidate_handle']
    context = {'city_handle': city, 'near_candidate_ref': anchor, 'near_radius_m': 500}
    expected = app.search_grounding_candidates(**context, keyword='bus station', category='bus station')
    before = deepcopy(query)
    result = app.search_grounding_candidates(**context, **query)
    assert [p['name'] for p in result['candidates']] == [p['name'] for p in expected['candidates']] == ['Central Stop']
    assert query == before


def test_full_place_name_is_not_rewritten():
    runtime = make_runtime()
    result = runtime.search_grounding_candidates({'keyword': 'Bus Stop Cafe'})
    assert result['candidates'][0]['name'] == 'Bus Stop Cafe'
