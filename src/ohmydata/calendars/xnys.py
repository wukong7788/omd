"""Bounded XNYS trading sessions sourced from ``exchange-calendars``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_NEW_YORK = ZoneInfo("America/New_York")
_MAX_RANGE = timedelta(days=366 * 5)


@dataclass(frozen=True, slots=True)
class XNYSCalendarSession:
    """One XNYS session with source-derived, timezone-aware session times."""

    session_date: date
    open_at: datetime
    close_at: datetime


def _as_date(value: date | str, name: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{name} must be a date or ISO YYYY-MM-DD string, not datetime")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a date or ISO YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a real ISO YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use ISO YYYY-MM-DD format")
    return parsed


def get_xnys_sessions(
    start_date: date | str,
    end_date: date | str,
) -> tuple[XNYSCalendarSession, ...]:
    """Return XNYS sessions in the inclusive bounded date range.

    The ``exchange-calendars`` package is required through OMD's ``calendars``
    extra. The lookup is local and deterministic: no network or consumer policy
    is involved. Closed dates are omitted. Session times are returned in
    ``America/New_York`` and include exchange early closes.

    Args:
        start_date: First calendar date to include, as ``date`` or ISO text.
        end_date: Last calendar date to include, as ``date`` or ISO text.

    Raises:
        ImportError: If OMD's optional ``calendars`` extra is not installed.
        ValueError: If dates are invalid, reversed, or span more than five years.
    """
    start = _as_date(start_date, "start_date")
    end = _as_date(end_date, "end_date")
    if end < start:
        raise ValueError("end_date must be on or after start_date")
    if end - start > _MAX_RANGE:
        raise ValueError("date range must not exceed five years")

    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise ImportError(
            "XNYS sessions require the optional dependency; install ohmydata[calendars]"
        ) from exc

    # exchange-calendars requires a strict start < end even when callers ask
    # about one inclusive calendar date. Expand only the source lookup; the
    # returned rows are still filtered to the caller's exact bounds below.
    calendar_end = end + timedelta(days=1) if start == end else end
    calendar = xcals.get_calendar("XNYS", start=start.isoformat(), end=calendar_end.isoformat())
    schedule = calendar.schedule
    sessions: list[XNYSCalendarSession] = []
    for label, row in schedule.iterrows():
        session_day = date.fromisoformat(str(label)[:10])
        if not start <= session_day <= end:
            continue
        open_at = row["open"].to_pydatetime().astimezone(_NEW_YORK)
        close_at = row["close"].to_pydatetime().astimezone(_NEW_YORK)
        sessions.append(XNYSCalendarSession(session_day, open_at, close_at))
    return tuple(sessions)
