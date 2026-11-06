from app.assets.safety.spacetime import _instant
'Absolute event validity, using the same half-open window as Safety.'

def event_active_at(event, when):
    point = _instant(when.isoformat() if hasattr(when, 'isoformat') else when)
    start = event.get('activation_time') or event.get('timestep') or event.get('timestamp')
    end = event.get('end_time')
    return (not start or _instant(start) <= point) and (not end or point < _instant(end))
