def events(value):
    return value['events'] if 'events' in value else [value]
