from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path
'Project recorded person identities into messages; never infer names from prose.'

def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}

def resolve_message_senders(events: list[dict], folders: list[Path], binding: str='') -> dict:
    """Use task-local frozen identity records for the loaded world version only.

    Identity records stay private. Only a resolved public name enters the App
    patch; a missing/ambiguous identity does not reject an event or guess a user.
    """
    people = defaultdict(set)
    requests = defaultdict(set)
    event_requests = defaultdict(list)
    for folder in folders:
        if binding:
            provenance = _read(folder / 'task_provenance.json')
            context = provenance.get('generation_context') or provenance
            city = provenance.get('city_id') or context.get('city_id')
            versions = context.get('world_city_versions') or context.get('required_city_versions') or {}
            version = versions.get(city, context.get('city_version'))
            if version is None:
                continue
            candidate_binding = f"{provenance.get('world_id')}/{city}/v{str(version).lstrip('vV')}"
            if candidate_binding != binding:
                continue
        sandbox = _read(folder / 'task_sandbox.json')
        source = sandbox.get('source_task') or {}
        character = sandbox.get('character') or (sandbox.get('safety_choice') or {}).get('character') or {}
        slice_people = _read(folder / 'world_slice.json').get('characters') or []
        records = [character, *slice_people, *(sandbox.get('participants') or [])]
        for person in records:
            if person.get('character_id') and person.get('name'):
                people[str(person['character_id'])].add(str(person['name']))
        actors = sandbox.get('participants') or [] if source.get('task_type') == 'multi_entity' else [character] if character.get('character_id') else slice_people
        actor_ids = {str(p['character_id']) for p in actors if p.get('character_id')}
        if source.get('task_id'):
            requests[str(source['task_id'])].update(actor_ids)
            payload = _read(folder / 'env_events.json')
            saved_events = payload if isinstance(payload, list) else payload.get('events', payload.get('env_events', []))
            local_names = defaultdict(set)
            for person in records:
                if person.get('character_id') and person.get('name'):
                    local_names[str(person['character_id'])].add(str(person['name']))
            ordered_ids = list(dict.fromkeys((str(p['character_id']) for p in actors if p.get('character_id'))))
            complete = bool(ordered_ids) and all((p.get('character_id') for p in actors))
            complete = complete and all((len(local_names.get(actor, set())) == 1 for actor in ordered_ids))
            display_name = '、'.join((next(iter(local_names[actor])) for actor in ordered_ids)) if complete else ''
            for saved in saved_events:
                if isinstance(saved, dict) and saved.get('event_id'):
                    event_requests[str(saved['event_id']), str(source['task_id'])].append((tuple(ordered_ids), display_name))
    (resolved, unresolved) = ([], [])
    for event in events:
        if event.get('app') != 'MessagingApp':
            continue
        scope = event.get('scope') or {}
        actor_ids = {str(scope.get('id'))} if scope.get('type') == 'participant' else requests.get(str(scope.get('id')), set())
        names = people.get(next(iter(actor_ids)), set()) if len(actor_ids) == 1 else set()
        name = next(iter(names)) if len(names) == 1 else ''
        if scope.get('type') != 'participant':
            owners = event_requests.get((str(event.get('event_id')), str(scope.get('id'))), [])
            if owners:
                name = owners[0][1] if all((owner == owners[0] for owner in owners)) else ''
        event['state_patch']['sender_name'] = name
        if name:
            resolved.append(event['event_id'])
        else:
            unresolved.append(event['event_id'])
    return {'resolved_count': len(resolved), 'unresolved_event_ids': unresolved}
