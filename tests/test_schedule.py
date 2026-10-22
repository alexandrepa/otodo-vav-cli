import pytest

from otodo_vav import schedule

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
        ("mon", [0]),
        ("mon-fri", [0, 1, 2, 3, 4]),
        ("sat,sun", [5, 6]),
        ("sat-tue", [5, 6, 0, 1]),
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
    assert described["mon"] == ["06:00 comfort", "09:00 eco", "18:00 comfort", "23:00 eco"]


def test_describe_shows_unknown_modes_raw():
    assert schedule.describe([{"d": 2, "mm": 9, "t": 2940}])["wed"] == ["01:00 mm=9"]


def test_replace_days_keeps_other_days():
    events = schedule.replace_days(DEFAULT, [5, 6], [(480, 2), (1380, 3)])
    described = schedule.describe(events)
    assert described["sat"] == described["sun"] == ["08:00 comfort", "23:00 eco"]
    assert described["fri"] == schedule.describe(DEFAULT)["fri"]
    assert [e["t"] for e in events] == sorted(e["t"] for e in events)


def test_mode_at_follows_last_event_and_wraps_round_the_week():
    assert schedule.mode_at(DEFAULT, 0 * 1440 + 420) == 2  # Monday 07:00
    assert schedule.mode_at(DEFAULT, 0 * 1440 + 600) == 3  # Monday 10:00
    assert schedule.mode_at(DEFAULT, 0 * 1440 + 360) == 2  # Monday 06:00 exactly
    assert schedule.mode_at(DEFAULT, 0 * 1440 + 60) == 3  # Monday 01:00, from Sunday 23:00
    assert schedule.mode_at([], 600) is None
