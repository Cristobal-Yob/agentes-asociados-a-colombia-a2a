"""Reglas de agenda puras, sin transporte HTTP ni proveedores."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from agenda_agent.contracts import AgendaEvent


def local_time(day, hour, timezone):
    zone = ZoneInfo(timezone)
    naive = datetime.combine(day, time.fromisoformat(hour))
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        raise ValueError("La hora local no existe por un cambio de horario.")
    if first.utcoffset() != second.utcoffset():
        raise ValueError("La hora local es ambigua por un cambio de horario.")
    return first


def missing_field(spec):
    for key in ("date", "timezone", "start_time", "sessions"):
        if not getattr(spec, key):
            return key
    if any(session.duration_minutes is None for session in spec.sessions):
        return "duration_minutes"
    return None


def plan_events(spec):
    day = date.fromisoformat(spec.date)
    cursors = {}
    events = []
    for session in spec.sessions:
        start = cursors.get(session.day_offset)
        if start is None:
            start = local_time(
                day + timedelta(days=session.day_offset), spec.start_time, spec.timezone
            )
        end = (start.astimezone(UTC) + timedelta(minutes=session.duration_minutes)).astimezone(
            start.tzinfo
        )
        events.append(
            AgendaEvent(title=session.title, start=start, end=end, timezone=spec.timezone)
        )
        cursors[session.day_offset] = end
    return events


def overlaps(events, busy):
    return any(
        event.start.astimezone(UTC) < end.astimezone(UTC)
        and start.astimezone(UTC) < event.end.astimezone(UTC)
        for event in events
        for start, end in busy
    )
