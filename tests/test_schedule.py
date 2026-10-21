import pytest

from vav_connect import schedule

# Default schedule observed on a real account: same program every day.
DEFAULT = [
    schedule.make_event(day, minute, mode)
    for day in range(7)
    for minute, mode in [(360, 2), (540, 3), (1080, 2), (1380, 3)]
]


def test_make_event_uses_minute_of_week():
    assert schedule.make_event(1, 360, 2) == {"d": 1, "mm": 2, "t": 1800}


def test_sort_events_rejects_t_outside_its_day():
    with pytest.raises(ValueError):
        schedule.sort_events([{"d": 0, "mm": 2, "t": 1500}])


@pytest.mark.parametrize(
    ("raw", "days"),
    [
        ("mon", [1]),
        ("mon-fri", [1, 2, 3, 4, 5]),
        ("sat,sun", [6, 0]),
        ("fri-mon", [5, 6, 0, 1]),
        ("all", [0, 1, 2, 3, 4, 5, 6]),
    ],
)
def test_parse_days(raw, days):
    assert schedule.parse_days(raw) == days


@pytest.mark.parametrize("raw", ["monday", "mon-xyz", ""])
def test_parse_days_rejects_unknown(raw):
    with pytest.raises(ValueError):
        schedule.parse_days(raw)


def test_parse_day_program():
    assert schedule.parse_day_program("06:30=comfort,22:00=eco") == [(390, 2), (1320, 3)]


@pytest.mark.parametrize("raw", ["06:00", "06:00=off", "6h=eco", "24:00=eco", "06:60=eco"])
def test_parse_day_program_rejects_bad_items(raw):
    with pytest.raises(ValueError):
        schedule.parse_day_program(raw)


def test_describe_default_schedule():
    described = schedule.describe(DEFAULT)
    assert list(described) == list(schedule.DAYS)
    assert described["sun"] == ["06:00 comfort", "09:00 eco", "18:00 comfort", "23:00 eco"]


def test_describe_shows_unknown_modes_raw():
    assert schedule.describe([{"d": 2, "mm": 9, "t": 2940}])["tue"] == ["01:00 mm=9"]


def test_replace_days_keeps_other_days():
    events = schedule.replace_days(DEFAULT, [6, 0], [(480, 2), (1380, 3)])
    described = schedule.describe(events)
    assert described["sat"] == described["sun"] == ["08:00 comfort", "23:00 eco"]
    assert described["mon"] == schedule.describe(DEFAULT)["mon"]
    assert [e["t"] for e in events] == sorted(e["t"] for e in events)
