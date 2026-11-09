"""Read-only projection of saved Region evidence; no candidate or semantic checks."""
import json
from pathlib import Path


def region_review_data(task_dir, sandbox, world, root):
    evidence = sandbox.get('stage3_private_evidence') or {}
    ids = set((evidence.get('local_candidate_pool') or {}).get('place_ids') or [])
    ids.add((evidence.get('target') or {}).get('place_id'))
    result = {'places': [p for p in world.get('places', []) if p.get('place_id') in ids],
              'warnings': [], 'place_evidence': {}}
    character = evidence.get('character') or sandbox.get('character') or {}
    if isinstance(character.get('location'), dict):
        result['start'] = {**character, 'place_id': 'character-start'}
    audit_path = task_dir.parent.parent / 'stage3_candidate_audit.json'
    if not audit_path.is_file():
        return result
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    root = Path(root).resolve()
    def asset(value):
        # Moved logs may carry a former machine's prefix. Only repository outputs.
        text = str(value or '').replace('\\', '/')
        if 'outputs/' not in text:
            return None
        path = (root / text[text.index('outputs/'):]).resolve()
        return path if path.is_relative_to(root / 'outputs') and path.is_file() else None
    rules = asset(audit.get('rules_path'))
    if rules:
        payload = json.loads(rules.read_text(encoding='utf-8'))
        rows = payload.get('rules') or payload.get('regional_rules') or []
        result['rule'] = next((r for r in rows if r.get('rule_id') == (evidence.get('selected_regional_rule') or {}).get('rule_id')), None)
    else:
        result['warnings'].append('绑定的地域来源文件未复制或不可读取；保留任务保存的准则。')
    # Do not substitute a newer service cache when the recorded asset is absent.
    cache = asset(audit.get('service_detail_cache_path'))
    if not cache:
        result['warnings'].append('绑定的官网详情缓存未复制或不可读取；不能展示原始官网片段。')
    else:
        from world_module.service_detail_snapshot import public_identity, identity_key
        payload = json.loads(cache.read_text(encoding='utf-8'))
        records = {identity_key(identity): row for row in payload.get('records', [])
                   if (identity := public_identity(row)) is not None}
        for place in result['places'] + [evidence.get('target') or {}]:
            identity = public_identity(place)
            record = records.get(identity_key(identity)) if identity else None
            if not record:
                continue
            documents = []
            for doc in record.get('source_documents') or []:
                text = str(doc.get('visible_text') or '')
                if not text:
                    continue
                documents.append({'source_url': doc.get('source_url'), 'excerpt': text[:800],
                                  'visible_text': text})
            result['place_evidence'][place['place_id']] = {'documents': documents,
                'matches': place.get('retrieval_matches'), 'collection_status': record.get('collection_status')}
    return result
