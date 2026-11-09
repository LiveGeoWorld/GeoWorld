"""Private itinerary clocks: preserve seconds without changing minute-only values."""
import math
import re


def clock_seconds(value: str) -> int:
    if value in ('24:00', '24:00:00'):
        return 86400
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?', value):
        raise ValueError('time must use HH:mm or HH:mm:ss')
    parts = [int(p) for p in value.split(':')]
    return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) == 3 else 0)


def clock_minutes(value: str):
    seconds = clock_seconds(value)
    return seconds // 60 if seconds % 60 == 0 else seconds / 60


def format_minutes(value):
    seconds = round(value * 60)
    if not math.isclose(seconds, value * 60, abs_tol=1e-7):
        raise ValueError('clock precision must be whole seconds')
    hour, remainder = divmod(seconds, 3600)
    minute, second = divmod(remainder, 60)
    return f'{hour:02d}:{minute:02d}' + (f':{second:02d}' if second else '')
