"""Treasury tentative buyback calendar -> rows in `observations`.

The quarterly schedule of Treasury debt buyback operations, published as
XML alongside the refunding documents. A dated numeric series - a purchase
ceiling per maturity bucket per operation - so observations rather than
items, on the same argument as CFETS, CFTC and SSE Southbound.

Shape, confirmed against the live file:

    <BuyBackCalendar>
      <BuybackCalendarName>August 2026 Refunding Tentative Buyback Calendar V1</...>
      <StartDate>2026-08-18</StartDate>
      <EndDate>2026-11-05</EndDate>
      <BuybackCalendarDate>
        <PurchaseBucketName>Nominal Coupons 20Y to 30Y</PurchaseBucketName>
        <SecurityType>NOMINAL COUPONS</SecurityType>
        <OperationType>Liquidity Support</OperationType>
        <MinimumPurchaseAmountDollars>0</MinimumPurchaseAmountDollars>
        <MaximumPurchaseAmountDollars>2000000000</MaximumPurchaseAmountDollars>
        <MaturityDateRangeStart>2046-08-19</MaturityDateRangeStart>
        <MaturityDateRangeEnd>2056-08-18</MaturityDateRangeEnd>
        <AnnouncementDate>2026-08-17</AnnouncementDate>
        <OperationDate>2026-08-18</OperationDate>
        <SettlementDate>2026-08-19</SettlementDate>
        <OperationStartTimeEasternUS>13:40</OperationStartTimeEasternUS>
        <OperationEndTimeEasternUS>14:00</OperationEndTimeEasternUS>
      </BuybackCalendarDate>
      ...18 operations
    </BuyBackCalendar>

No namespace on anything, unlike the ECB's gesmes envelope. The root name
is the only shape check worth making, and it is made: a payload whose root
is not BuyBackCalendar is refused rather than walked hopefully.

One observation per operation per bucket, keyed (series, period) =
(bucket, OperationDate) - which is what _store_observations compares on,
so a ceiling that moves between quarters updates in place and writes a
revision row instead of being silently dropped.

WHAT THIS SOURCE DOES NOT DO
--------------------------------------------------------------------------
IT DID NOT CARRY THE 19 AUGUST 2026 ANNOUNCEMENT, AND WOULD NOT HAVE
CAUGHT IT. That announcement - increased sizes for nominal long-end
liquidity-support buybacks beginning 9 September - is the reason anyone
went looking for this file, so the limit belongs next to the code rather
than in a commit message.

Measured, not assumed. The file was re-uploaded on 19 August 2026 at
15:54 GMT, hours after the 08:30 release, which looks exactly like the
artefact you would want. Its contents did not move. Comparing every
bucket maximum across four consecutive refunding calendars:

    bucket              Nov 2025   Feb 2026   May 2026   Aug 2026
    Nominal 20Y to 30Y    $2.0B      $2.0B      $2.0B      $2.0B
    Nominal 10Y to 20Y    $2.0B      $2.0B      $2.0B      $2.0B

Unchanged in all four, including for operations scheduled after 9
September. Polling this file with full revision history would have
surfaced nothing that day.

So it exists for the change that DOES reach the file. When a ceiling
moves, the revision mechanism already makes it visible. Discretionary
changes announced by press release and never written into the calendar
are a documented blind spot - see the README - and not something this
parser can close. Anyone reading this later should find the measurement
above rather than repeat the search.
"""

from __future__ import annotations

from datetime import datetime, time
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from ..errors import MalformedEntryError, ParseError
from .base import ParsedFeed

ROOT = "BuyBackCalendar"
ENTRY = "BuybackCalendarDate"

# Operation times are published in US Eastern, per the element name. Resolved
# per instant so the EST/EDT switch lands correctly - never a stored offset,
# the same rule the ribbon follows.
OPERATION_ZONE = "America/New_York"

# The amounts are whole dollars as published. Stored as published: a
# maximum of 2000000000 is what the file says, and rescaling it to billions
# here would put a number in the database that nobody published.
UNIT = "USD"


def _required(entry, tag: str, source_name: str, context: str) -> str:
    """A field that must be present AND non-empty.

    Present-but-empty is the case worth naming: <MaximumPurchaseAmountDollars/>
    reads as a field the publisher deliberately left blank, and treating it
    as a zero ceiling would put a fabricated number in the series. It raises
    instead, like the ECB parser does for a Cube with no rate attribute.
    """
    node = entry.find(tag)
    if node is None:
        raise MalformedEntryError(
            f"{source_name}: {context} has no <{tag}>")
    value = (node.text or "").strip()
    if not value:
        raise MalformedEntryError(
            f"{source_name}: {context} has an empty <{tag}>; a blank ceiling "
            f"is not a zero ceiling and will not be stored as one")
    return value


def parse(source, body: str) -> ParsedFeed:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ParseError(
            f"{source.name}: payload is not well-formed XML: {exc}") from exc

    if root.tag != ROOT:
        raise ParseError(
            f"{source.name}: unexpected root <{root.tag}>; expected <{ROOT}>. "
            f"This host serves the homepage for unknown paths, so a wrong URL "
            f"arrives as HTML with status 200 rather than as a 404.")

    entries = root.findall(ENTRY)
    if not entries:
        raise ParseError(
            f"{source.name}: no <{ENTRY}> elements. Either the calendar is "
            f"empty or its shape has changed; refusing to guess.")

    calendar = (root.findtext("BuybackCalendarName") or "").strip() or None
    zone = ZoneInfo(source.config.get("timezone", OPERATION_ZONE))

    result = ParsedFeed()
    for position, entry in enumerate(entries):
        context = f"entry {position}"
        bucket = _required(entry, "PurchaseBucketName", source.name, context)
        context = f"{bucket} entry {position}"
        period = _required(entry, "OperationDate", source.name, context)
        if len(period) != 10 or period[4] != "-":
            raise MalformedEntryError(
                f"{source.name}: {context} OperationDate {period!r} is not "
                f"YYYY-MM-DD")

        raw = _required(entry, "MaximumPurchaseAmountDollars", source.name, context)
        try:
            value = float(raw)
        except ValueError as exc:
            raise MalformedEntryError(
                f"{source.name}: {context} MaximumPurchaseAmountDollars "
                f"{raw!r} is not numeric") from exc
        if value < 0:
            raise MalformedEntryError(
                f"{source.name}: {context} MaximumPurchaseAmountDollars "
                f"{value} is negative; a purchase ceiling cannot be")

        # The operation's own start time, published in the file. Not a
        # guessed publication hour: the element states it.
        start = (entry.findtext("OperationStartTimeEasternUS") or "").strip()
        hour, _, minute = start.partition(":")
        try:
            clock = time(int(hour), int(minute or 0))
        except ValueError as exc:
            raise MalformedEntryError(
                f"{source.name}: {context} OperationStartTimeEasternUS "
                f"{start!r} is not HH:MM") from exc
        observed_at = datetime.combine(
            datetime.strptime(period, "%Y-%m-%d").date(), clock, tzinfo=zone
        ).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")

        operation = (entry.findtext("OperationType") or "").strip() or None
        result.observations.append({
            # The publisher's own bucket name, verbatim. Slugging it here
            # would make the series a name this project invented for
            # something Treasury already named.
            "series": f"BUYBACK/{bucket}",
            "period": period,
            "value": value,
            "unit": UNIT,
            "base_currency": UNIT,
            "target_currency": None,
            # What the number IS, carried with it: a ceiling, not a
            # purchase, and which kind of operation it caps.
            "rate_type": " · ".join(x for x in (
                "maximum purchase amount", operation, calendar) if x),
            "frequency": "quarterly",
            "decimals": 0,
            "external_id": f"{period}#{bucket}",
            "observed_at": observed_at,
        })
    return result
