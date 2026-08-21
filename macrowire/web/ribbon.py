"""Session ribbon geometry.

Every local time here is computed PER INSTANT from an IANA zone. Nothing
stores a UTC offset, because offsets are a property of a moment, not of a
place - which is the whole point the ribbon exists to show.

Measured in the collected data: the Fed publishes at 14:00 New York with
an interquartile range of zero minutes, and that lands in Sydney at 06:00,
05:00 or 04:00 depending on which side of two different DST transitions
the date falls. A ribbon that cached an offset would show that as a fixed
mark and be wrong for five months of the year.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

VIEW = ZoneInfo("Australia/Sydney")

# Exchange trading hours in each venue's own local time.
# Continuous trading hours in each venue's own local time, EXCLUDING the
# pre-open and closing auctions.
#
# Two of these were wrong in the first pass and it showed:
#   Tokyo closed at 15:00. The TSE extended its close to 15:30 in November
#   2024. With that error and no lunch break, Tokyo projected to exactly
#   10:00-16:00 Sydney in winter - byte-identical to the ASX row, which is
#   why the two looked copy-pasted. They were, wrongly.
#   Tokyo and Hong Kong both break for lunch and neither break was drawn.
SESSIONS = [
    {"key": "sydney",   "label": "SYD", "tz": "Australia/Sydney",
     "spans": [("10:00", "16:00")]},
    {"key": "tokyo",    "label": "TYO", "tz": "Asia/Tokyo",
     "spans": [("09:00", "11:30"), ("12:30", "15:30")]},
    {"key": "hongkong", "label": "HKG", "tz": "Asia/Hong_Kong",
     "spans": [("09:30", "12:00"), ("13:00", "16:00")]},
    {"key": "london",   "label": "LON", "tz": "Europe/London",
     "spans": [("08:00", "16:30")]},
    {"key": "newyork",  "label": "NYC", "tz": "America/New_York",
     "spans": [("09:30", "16:00")]},
]


def _hhmm(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute))


def _fraction(moment: datetime, day: date) -> float:
    """Position within the viewed day, 0.0-1.0. May fall outside on wrap."""
    start = datetime.combine(day, time(0, 0), tzinfo=VIEW)
    return (moment - start).total_seconds() / 86400.0


def _clip(lo: float, hi: float) -> dict | None:
    """Clip one session instance to the visible day.

    No wrapping here: a session that straddles local midnight is two
    separate instances - yesterday's and today's - and the caller walks
    adjacent dates to find both. Wrapping as well would emit each segment
    twice.
    """
    start, end = max(lo, 0.0), min(hi, 1.0)
    if end <= start:
        return None
    return {
        "start": start,
        "end": end,
        # Which edge, if any, this segment runs off.
        "continues": ("both" if lo < 0 and hi > 1 else
                      "from" if lo < 0 else
                      "into" if hi > 1 else None),
    }


def sessions_for(day: date) -> list[dict]:
    """Each market's hours, resolved for this date and projected into Sydney."""
    result = []
    for spec in SESSIONS:
        tz = ZoneInfo(spec["tz"])
        # A session's calendar date is its own, not Sydney's; check the day
        # before and after so anything overlapping the viewed day is caught.
        spans = []
        for offset in (-1, 0, 1):
            local_day = day + timedelta(days=offset)
            for open_at, close_at in spec["spans"]:
                opens = datetime.combine(local_day, _hhmm(open_at), tzinfo=tz)
                closes = datetime.combine(local_day, _hhmm(close_at), tzinfo=tz)
                lo = _fraction(opens.astimezone(VIEW), day)
                hi = _fraction(closes.astimezone(VIEW), day)
                seg = _clip(lo, hi)
                if seg is None:
                    continue
                seg["opens_local"] = opens.astimezone(VIEW).strftime("%H:%M")
                seg["closes_local"] = closes.astimezone(VIEW).strftime("%H:%M")
                spans.append(seg)
        spans.sort(key=lambda x: x["start"])
        # Weekends: markets are shut, but showing the band greyed is more
        # useful than showing nothing - it still says where you are.
        result.append({
            "key": spec["key"], "label": spec["label"], "tz": spec["tz"],
            "segments": spans,
            "weekend": day.weekday() >= 5,
            "has_break": len(spec["spans"]) > 1,
        })
    return result


def marks_for(day: date, sources) -> list[dict]:
    """Publication marks, honest to each source's measured timing class.

    fixed/tight get a position. scattered and date_only get none: a mark
    for a source whose interquartile range is seven hours would be a
    decoration, not information.
    """
    out = []
    for source in sources:
        timing = source.timing or {}
        kind = timing.get("class", "scattered")
        if kind not in ("fixed", "tight"):
            out.append({
                "source": source.name, "class": kind, "position": None,
                "importance": source.importance,
                "reason": ("feed carries no time of day" if kind == "date_only"
                           else "no usable schedule (measured IQR 2-7h)"),
            })
            continue

        tz = ZoneInfo(timing["timezone"])
        # The source's publication date is its own, not Sydney's. 14:00 in
        # New York on a Wednesday is 04:00 in Sydney on the Thursday, so
        # walk adjacent origin dates and keep the instance that actually
        # lands inside the day being viewed.
        placed = None
        for offset in (-1, 0, 1):
            origin = datetime.combine(
                day + timedelta(days=offset), _hhmm(timing["at"]), tzinfo=tz)
            local = origin.astimezone(VIEW)
            position = _fraction(local, day)
            if 0 <= position <= 1:
                placed = (origin, local, position)
                break
        if placed is None:
            out.append({
                "source": source.name, "class": kind, "position": None,
                "importance": source.importance,
                "reason": "does not land on this date",
            })
            continue

        origin, local, position = placed
        out.append({
            "source": source.name, "class": kind,
            "position": position,
            "window": int(timing.get("window_minutes") or 0) / 1440.0,
            "local_time": local.strftime("%H:%M"),
            # tzname() of the ORIGIN instant, not the Sydney projection.
            "origin": f"{timing['at']} {origin.tzname()}",
            "origin_tz": timing["timezone"],
            "origin_date": origin.date().isoformat(),
            "crosses_date": origin.date() != local.date(),
            "importance": source.importance,
            "reason": None,
            "shifts": _dst_note(timing, day),
        })
    return out


def _dst_note(timing: dict, day: date) -> str | None:
    """What this mark's Sydney time is at the other end of the year.

    The single most important thing the ribbon shows: a source that never
    moves at origin still moves under you.
    """
    tz = ZoneInfo(timing["timezone"])
    seen = {}
    for month in range(1, 13):
        probe = date(day.year, month, 15)
        local = datetime.combine(probe, _hhmm(timing["at"]), tzinfo=tz).astimezone(VIEW)
        seen.setdefault(local.strftime("%H:%M"), []).append(probe.strftime("%b"))
    if len(seen) == 1:
        return None
    return " / ".join(f"{t} ({len(m)}mo)" for t, m in sorted(seen.items()))


def now_position(moment: datetime | None = None) -> dict:
    now = (moment or datetime.now(VIEW)).astimezone(VIEW)
    return {
        "position": _fraction(now, now.date()),
        "local": now.strftime("%H:%M:%S"),
        "zone": now.tzname(),
        "date": now.date().isoformat(),
        "offset": now.utcoffset().total_seconds() / 3600.0,
    }
