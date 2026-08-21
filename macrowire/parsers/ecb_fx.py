"""ECB euro foreign exchange reference rates -> rows in `observations`.

Published directly by the ECB as gesmes/XML - no wrapper needed. Frankfurter
and similar services are convenience layers over this exact file.

Shape, confirmed against the live feed:

    <gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
                     xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
      <Cube>
        <Cube time="2026-08-20">
          <Cube currency="USD" rate="1.1681"/>
          <Cube currency="JPY" rate="185.45"/>
          ...29 currencies
        </Cube>
      </Cube>
    </gesmes:Envelope>

Three nested elements all named Cube, distinguished only by which attributes
they carry: the outer is a container, the middle a date, the inner a rate.
The parser walks that structure explicitly rather than matching on tag name.

The daily file holds one date (1.5KB); the 90-day file holds ~64 trading
days in the same shape, so one parser reads both.

BASE IS EUR, always. Series are written EUR/USD, EUR/JPY - "target per one
EUR" - the same convention as the RBA (AUD base) and CFETS (CNY target).
"""

from __future__ import annotations

from datetime import datetime, time
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from ..errors import MalformedEntryError, ParseError
from .base import ParsedFeed

NS = {"gesmes": "http://www.gesmes.org/xml/2002-08-01",
      "fx": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}

BASE = "EUR"

# Sanity rails per pair, wide enough for a decade of real moves and tight
# enough to catch a misparse. Same principle as the CFETS bounds: they exist
# to turn a silent wrong number into a raised error.
DEFAULT_BOUNDS = {"USD": (0.5, 2.5), "JPY": (80.0, 300.0), "GBP": (0.5, 1.2),
                  "CHF": (0.6, 1.8), "AUD": (1.0, 2.5), "CNY": (5.0, 12.0),
                  "HKD": (5.0, 20.0), "CAD": (1.0, 2.5), "NZD": (1.2, 2.6),
                  "SGD": (1.2, 2.2)}


def parse(source, body: str) -> ParsedFeed:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ParseError(f"{source.name}: payload is not well-formed XML: {exc}") from exc

    if not root.tag.endswith("Envelope"):
        raise ParseError(
            f"{source.name}: unexpected root <{root.tag}>; expected a gesmes Envelope")

    # The date-bearing Cubes are the ones carrying a `time` attribute. Match
    # on that rather than on nesting depth, which differs between the daily
    # and 90-day files in ways the schema does not promise to keep.
    day_cubes = [c for c in root.iter() if c.tag.endswith("Cube") and "time" in c.attrib]
    if not day_cubes:
        raise ParseError(
            f"{source.name}: no dated Cube elements. Either the feed is empty "
            f"or its shape has changed; refusing to guess.")

    wanted = source.config.get("currencies")
    wanted = {c.upper() for c in wanted} if wanted else None
    bounds = dict(DEFAULT_BOUNDS)
    for code, pair in (source.config.get("bounds") or {}).items():
        bounds[code.upper()] = (float(pair[0]), float(pair[1]))

    publish_at = source.config.get("publication_time", "16:00")
    zone = ZoneInfo(source.config.get("timezone", "Europe/Berlin"))
    hour, _, minute = publish_at.partition(":")

    result = ParsedFeed()
    for cube in day_cubes:
        period = cube.attrib["time"]
        if len(period) != 10 or period[4] != "-":
            raise MalformedEntryError(
                f"{source.name}: Cube time {period!r} is not YYYY-MM-DD")

        # The file carries a date and no time. The ECB publishes around
        # 16:00 CET, so that is applied explicitly from config and resolved
        # per-instant so the CET/CEST switch lands correctly - never a
        # stored offset.
        stamped = datetime.combine(
            datetime.strptime(period, "%Y-%m-%d").date(),
            time(int(hour), int(minute)), tzinfo=zone)
        observed_at = stamped.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")

        rates = [c for c in cube if c.tag.endswith("Cube") and "currency" in c.attrib]
        if not rates:
            raise MalformedEntryError(
                f"{source.name}: {period} carries no currency Cubes")

        for entry in rates:
            code = entry.attrib["currency"].upper()
            if wanted and code not in wanted:
                continue
            raw = entry.attrib.get("rate")
            if raw is None:
                raise MalformedEntryError(
                    f"{source.name}: {period} {code} has no rate attribute")
            try:
                value = float(raw)
            except ValueError as exc:
                raise MalformedEntryError(
                    f"{source.name}: {period} {code} rate {raw!r} is not numeric"
                ) from exc

            low, high = bounds.get(code, (0.0, 1e9))
            if not low <= value <= high:
                raise MalformedEntryError(
                    f"{source.name}: {period} {BASE}/{code} = {value} is outside "
                    f"the sanity range {low}-{high}. Either the market moved "
                    f"further than these bounds allow, or the feed changed shape.")

            result.observations.append({
                "series": f"{BASE}/{code}",
                "period": period,
                "value": value,
                "unit": code,
                "base_currency": BASE,
                "target_currency": code,
                "rate_type": f"ECB euro reference rate ({publish_at} CET)",
                "frequency": "daily",
                "decimals": len(raw.partition(".")[2]) or None,
                "external_id": f"{source.url}#{code}",
                "observed_at": observed_at,
            })
    return result
