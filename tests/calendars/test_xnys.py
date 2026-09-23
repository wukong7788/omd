from datetime import date
from zoneinfo import ZoneInfo

import pytest

from ohmydata.calendars import get_xnys_sessions


def test_xnys_schedule_includes_holidays_regular_and_early_closes() -> None:
    sessions = get_xnys_sessions("2026-02-16", "2026-11-27")
    by_date = {session.session_date: session for session in sessions}

    assert date(2026, 2, 16) not in by_date
    assert date(2026, 7, 3) not in by_date
    assert by_date[date(2026, 7, 2)].close_at.isoformat() == "2026-07-02T16:00:00-04:00"
    assert by_date[date(2026, 11, 27)].close_at.isoformat() == "2026-11-27T13:00:00-05:00"
    assert by_date[date(2026, 7, 2)].close_at.tzinfo == ZoneInfo("America/New_York")


def test_xnys_single_day_range_preserves_closed_and_open_dates() -> None:
    assert get_xnys_sessions("2026-02-16", "2026-02-16") == ()
    sessions = get_xnys_sessions("2026-07-02", "2026-07-02")
    assert len(sessions) == 1
    assert sessions[0].session_date == date(2026, 7, 2)


@pytest.mark.parametrize(
    ("start", "end"),
    [("2026-07-04", "2026-07-03"), ("2026-01-01", "2031-01-10")],
)
def test_xnys_schedule_rejects_invalid_ranges(start: str, end: str) -> None:
    with pytest.raises(ValueError):
        get_xnys_sessions(start, end)
