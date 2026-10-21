"""Weekly schedule (`weeklyEvents`) followed by a heating service in Auto mode.

Each event switches the service to a mode at a minute of the week:
`{"d": day, "t": minute_of_week, "mm": mode}` with `t = d * 1440 + minute_of_day`.

Day 0 is Sunday (as in homebridge-otodo-vav). The `mm` values match the pilot-wire
values (2 = comfort, 3 = eco): the schedule the app creates by default reads
comfort 06:00-09:00 and 18:00-23:00, eco otherwise. Neither has been confirmed by
editing a schedule in the app.
"""

from __future__ import annotations

from typing import Any

MINUTES_PER_DAY = 1440
DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")

SCHEDULE_MODES = {"comfort": 2, "eco": 3}
_MODE_NAMES = {value: name for name, value in SCHEDULE_MODES.items()}


def make_event(day: int, minute_of_day: int, mode: int) -> dict[str, int]:
    if not 0 <= day < len(DAYS):
        raise ValueError(f"day must be 0-6, got {day}")
    if not 0 <= minute_of_day < MINUTES_PER_DAY:
        raise ValueError(f"minute of day must be 0-1439, got {minute_of_day}")
    return {"d": day, "mm": mode, "t": day * MINUTES_PER_DAY + minute_of_day}


def sort_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check that each event's `d` matches its `t` and order events by `t`."""
    for event in events:
        day, t = event["d"], event["t"]
        if not 0 <= t < len(DAYS) * MINUTES_PER_DAY or t // MINUTES_PER_DAY != day:
            raise ValueError(f"event {event} has t outside day {day}")
    return sorted(events, key=lambda e: e["t"])


def parse_time(raw: str) -> int:
    """'06:30' -> 390."""
    hours, sep, minutes = raw.partition(":")
    if not sep or not hours.isdigit() or not minutes.isdigit():
        raise ValueError(f"time must look like HH:MM, got {raw!r}")
    h, m = int(hours), int(minutes)
    if h > 23 or m > 59:
        raise ValueError(f"invalid time {raw!r}")
    return h * 60 + m


def format_time(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def parse_days(raw: str) -> list[int]:
    """'mon', 'mon-fri', 'sat,sun' or 'all' -> day indexes."""
    if raw == "all":
        return list(range(len(DAYS)))
    days: list[int] = []
    for part in raw.lower().split(","):
        first, sep, last = part.partition("-")
        try:
            start = DAYS.index(first)
            end = DAYS.index(last) if sep else start
        except ValueError:
            raise ValueError(f"unknown day in {raw!r}; use {', '.join(DAYS)} or all") from None
        day = start
        while True:
            if day not in days:
                days.append(day)
            if day == end:
                break
            day = (day + 1) % len(DAYS)
    return days


def parse_day_program(raw: str) -> list[tuple[int, int]]:
    """'06:00=comfort,09:00=eco' -> [(360, 2), (540, 3)]."""
    program = []
    for item in raw.split(","):
        at, sep, mode = item.partition("=")
        if not sep or mode not in SCHEDULE_MODES:
            raise ValueError(
                f"expected HH:MM=MODE with MODE in {', '.join(SCHEDULE_MODES)}, got {item!r}"
            )
        program.append((parse_time(at), SCHEDULE_MODES[mode]))
    return program


def replace_days(
    events: list[dict[str, Any]], days: list[int], program: list[tuple[int, int]]
) -> list[dict[str, Any]]:
    """Return `events` with the given days replaced by `program`; other days are kept."""
    kept = [e for e in events if e["d"] not in days]
    new = [make_event(day, minute, mode) for day in days for minute, mode in program]
    return sort_events(kept + new)


def describe(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    """{'sun': ['06:00 comfort', ...], ...} for every day, in week order."""
    out: dict[str, list[str]] = {day: [] for day in DAYS}
    for event in sort_events(events):
        mode = _MODE_NAMES.get(event["mm"], f"mm={event['mm']}")
        out[DAYS[event["d"]]].append(f"{format_time(event['t'] % MINUTES_PER_DAY)} {mode}")
    return out
