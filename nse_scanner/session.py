"""NSE session clock (IST). Matches Pine: marketOpenMinutes = hour*60+minute-555."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN = time(9, 15)
CLOSE = time(15, 30)
FIRST_BAR = time(9, 15)
OPEN_MINUTES = 9 * 60 + 15  # 555

# Opening range of the persistent engine. The entry-time guards that used to sit
# beside it (no entries before 09:45 / after 15:00, freeze after 15:25) were
# dead code: nothing called them, and the master scanner's own windows
# (BUY_W1 09:16-09:59 ... FIN_BUY 09:26-11:15) are the rules that actually
# bound an entry. They are gone rather than left looking like a safeguard.
OR_MINUTES = 30              # opening range 09:15-09:45 — the BUY side
# The opening range is not one number: the buy side keeps the full thirty
# minutes, the sell side reads a fifteen-minute one (09:15-09:30). A short is
# the side that gets there first — 18 Nov fired DIXON and TITAN at 09:25 and
# KALYANKJIL at 09:20, all before the 09:45 buy-side range was complete — so
# the sell side is allowed to be committed from 09:30 while the buy side still
# waits for 09:45. Nothing else about the two sides changes.
OR_MINUTES_SHORT = 15        # opening range 09:15-09:30 — the SELL side

# Master scanner aggressive windows (bar OPEN time, IST)
BUY_W1 = ((9, 16), (9, 59))
BUY_W2 = ((10, 0), (11, 44))
BUY_W3 = ((11, 45), (15, 15))
SELL_W1 = ((9, 16), (9, 59))
SELL_W2 = ((10, 0), (11, 44))
SELL_W3 = ((11, 45), (15, 15))
FIN_BUY = ((9, 26), (11, 15))
FIN_SELL = ((9, 26), (12, 0))
IGNORE_PDH = ((9, 0), (9, 35))


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(ts) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=IST)
        return ts.astimezone(IST)
    return pd_timestamp_to_ist(ts)


def pd_timestamp_to_ist(ts) -> datetime:
    t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if t.tzinfo is None:
        return t.replace(tzinfo=IST)
    return t.astimezone(IST)


def session_date(dt: datetime):
    dt = dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)
    return dt.date()


def market_open_minutes(dt: datetime) -> int:
    dt = dt.astimezone(IST)
    return dt.hour * 60 + dt.minute - OPEN_MINUTES


def in_window(dt: datetime, start: tuple[int, int], end: tuple[int, int]) -> bool:
    h, m = dt.astimezone(IST).hour, dt.astimezone(IST).minute
    sh, sm = start
    eh, em = end
    after = (h > sh) or (h == sh and m >= sm)
    before = (h < eh) or (h == eh and m <= em)
    return after and before


def is_weekday(dt: datetime) -> bool:
    return dt.astimezone(IST).weekday() < 5


def next_5m_close(dt: datetime | None = None) -> datetime:
    """Next 5-minute wall-clock boundary in IST (bar close)."""
    dt = (dt or now_ist()).astimezone(IST).replace(second=0, microsecond=0)
    extra = 5 - (dt.minute % 5)
    if extra == 5 and dt.second == 0:
        extra = 5
    if dt.minute % 5 == 0:
        extra = 5
    return dt + timedelta(minutes=extra)


