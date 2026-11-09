"""Bound Safety authoring to the confirmed rank-five comparison pool."""

from task_module.task_types.region_aware.generator import _stage3_local_candidate_pool
from task_module.task_types.safety_planning.activity_matching import match_activity


def local_activity(*, places, services, target_id, selected_service_type):
    universe = match_activity(places=places, services=services, target_id=target_id,
                              selected_service_type=selected_service_type)
    by_id = {p['place_id']: p for p in places}
    related = [by_id[pid] for pid in universe['activity_place_ids']]
    local = [by_id[target_id], *_stage3_local_candidate_pool(by_id[target_id], related)]
    result = match_activity(places=local, services=services, target_id=target_id,
                            selected_service_type=selected_service_type)
    result['comparison_scope'] = 'rank_5_local_only'
    result['universe_place_ids'] = universe['activity_place_ids']
    result['local_place_ids'] = [p['place_id'] for p in local]
    result['outside_pool_policy'] = 'manual_review_no_route_expansion'
    return result
