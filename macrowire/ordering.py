"""Where the viewer sits, and what that changes about order.

Two orderings in this interface depended on the author being in Sydney.
Neither is arbitrary and neither should simply be sorted:

  THE SESSION BAND is the trading day in sequence - Sydney opens, then
  Tokyo, then Hong Kong, then London, then New York. That sequence is a
  fact about the world and it is what the band is FOR: you learn where
  Tokyo sits and read the shape without reading the labels. Sorting the
  rows by distance from the reader would destroy exactly that. So the
  sequence is preserved and only the STARTING POINT rotates, which drops
  the parochialism and keeps the spatial memory.

  JURISDICTION CHIPS are not a sequence. They are things you click, and a
  control that moves is a control you misclick - so nothing here is
  volume-ordered or otherwise self-organising. The reader's own market
  goes first because that is where the eye starts, and the rest is
  alphabetical because alphabetical is stable and learnable.

Both fall back to something defensible when the viewer cannot be placed:
the band to its canonical order, the chips to plain alphabetical.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# IANA zone -> jurisdiction code, for the seven this tool carries sources
# for. NOT a general timezone-to-country database and not trying to be:
# the only question is "is the reader in one of these places", and a miss
# is answered honestly by falling back to alphabetical rather than by
# guessing. Override with `defaults.jurisdiction` in sources.yaml.
ZONE_JURISDICTION = {
    "AU": ("Australia/",),
    "CN": ("Asia/Shanghai", "Asia/Urumqi", "Asia/Chongqing", "Asia/Harbin",
           "Asia/Macau"),
    "HK": ("Asia/Hong_Kong",),
    "JP": ("Asia/Tokyo",),
    "UK": ("Europe/London", "Europe/Belfast"),
    "US": ("America/New_York", "America/Chicago", "America/Denver",
           "America/Los_Angeles", "America/Phoenix", "America/Anchorage",
           "America/Detroit", "America/Indiana/", "America/Kentucky/",
           "America/North_Dakota/", "America/Boise", "America/Juneau",
           "America/Sitka", "America/Nome", "America/Adak",
           "Pacific/Honolulu"),
    # The euro area and the rest of the EU. Ireland is EU, not UK.
    "EU": ("Europe/Paris", "Europe/Berlin", "Europe/Madrid", "Europe/Rome",
           "Europe/Amsterdam", "Europe/Brussels", "Europe/Vienna",
           "Europe/Dublin", "Europe/Lisbon", "Europe/Helsinki",
           "Europe/Athens", "Europe/Warsaw", "Europe/Prague",
           "Europe/Budapest", "Europe/Stockholm", "Europe/Copenhagen",
           "Europe/Bucharest", "Europe/Sofia", "Europe/Zagreb",
           "Europe/Bratislava", "Europe/Ljubljana", "Europe/Tallinn",
           "Europe/Riga", "Europe/Vilnius", "Europe/Luxembourg",
           "Europe/Malta", "Asia/Nicosia", "Europe/Nicosia"),
}


def jurisdiction_for_zone(zone: str) -> str | None:
    """Which of the seven the reader is in, or None.

    None is a real answer - a reader in Zurich is in none of them - and it
    is what makes the chip order degrade to plain alphabetical rather than
    to a guess.
    """
    for code, patterns in ZONE_JURISDICTION.items():
        for pattern in patterns:
            if zone == pattern or (pattern.endswith("/") and zone.startswith(pattern)):
                return code
    return None


def rotate_sessions(sessions: list, zone: str, mode="viewer") -> list:
    """The trading day, started at the reader's own market.

    `mode` is `viewer` (rotate), `fixed` (leave the canonical order alone)
    or an explicit list of session keys.
    """
    if isinstance(mode, (list, tuple)):
        wanted = [str(k) for k in mode]
        by_key = {s["key"]: s for s in sessions}
        # An explicit list that names something unknown is a typo, and a
        # silently dropped row is worse than a loud one - but an ordering
        # is not worth failing a page load over, so unknown names are
        # ignored and anything unlisted keeps its place at the end.
        ordered = [by_key[k] for k in wanted if k in by_key]
        ordered += [s for s in sessions if s["key"] not in set(wanted)]
        return ordered
    if mode == "fixed":
        return list(sessions)

    start = _nearest_session(sessions, zone)
    if start is None:
        return list(sessions)
    return sessions[start:] + sessions[:start]


def _nearest_session(sessions: list, zone: str) -> int | None:
    """Index of the session closest to the reader.

    An exact zone match first, because "I am in Tokyo" should start at
    Tokyo and not at whatever else shares +09:00. Otherwise the smallest
    current UTC-offset difference, resolved per instant so the answer is
    right on both sides of a DST switch.
    """
    for index, session in enumerate(sessions):
        if session["tz"] == zone:
            return index
    try:
        here = datetime.now(ZoneInfo(zone)).utcoffset()
    except Exception:
        return None
    if here is None:
        return None

    best, best_gap = None, None
    for index, session in enumerate(sessions):
        theirs = datetime.now(ZoneInfo(session["tz"])).utcoffset()
        if theirs is None:
            continue
        # Compare around the clock: +11 and -12 are an hour apart, not 23.
        raw = abs((here - theirs).total_seconds()) / 3600.0
        gap = min(raw, 24.0 - raw)
        if best_gap is None or gap < best_gap:
            best, best_gap = index, gap
    return best


def order_jurisdictions(codes, viewer: str | None, mode="viewer") -> list:
    """The reader's own market first, then alphabetical.

    `mode` is `viewer`, `alphabetical`, or an explicit list of codes.
    Stable under any window: nothing here depends on how many items a
    jurisdiction happens to have today.
    """
    codes = list(codes)
    if isinstance(mode, (list, tuple)):
        wanted = [str(c) for c in mode]
        rank = {c: i for i, c in enumerate(wanted)}
        return sorted(codes, key=lambda c: (rank.get(c, len(rank)), c))
    if mode == "alphabetical" or viewer is None or viewer not in codes:
        return sorted(codes)
    return [viewer] + sorted(c for c in codes if c != viewer)
