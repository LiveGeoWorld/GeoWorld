from app.assets.safety.spacetime import TimeWindow, _instant

def moment(context):
    return context['task_time'] if 'task_time' in context else context['activity_window']

def applies(window, task):
    period = TimeWindow(**window)
    if isinstance(task, str):
        return _instant(period.start) <= _instant(task) < _instant(period.end)
    return period.overlaps(TimeWindow(**task))
