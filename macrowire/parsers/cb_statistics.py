"""cb:statistics feeds -> rows in `observations`.

Shape confirmed against the live RBA exchange rates feed. 21 entries,
one per currency plus the trade-weighted index and the SDR:

    <item rdf:about="https://www.rba.gov.au/statistics/frequency/exchange-rates.html#USD">
      <title xml:lang="en">AU: 0.7116 USD = 1 AUD 2026-08-20 RBA 4.00 pm ...</title>
      <dc:date>2026-08-20T16:35:00+10:00</dc:date>
      <cb:statistics rdf:parseType="Resource">
        <cb:country>AU</cb:country>
        <cb:institutionAbbrev>RBA</cb:institutionAbbrev>
        <cb:exchangeRate rdf:parseType="Resource">
          <cb:observation rdf:parseType="Resource">
            <cb:value>0.7116</cb:value><cb:unit>AUD</cb:unit><cb:decimals>4</cb:decimals>
          </cb:observation>
          <cb:baseCurrency>AUD</cb:baseCurrency>
          <cb:targetCurrency>USD</cb:targetCurrency>
          <cb:rateType>4.00 pm foreign exchange rates</cb:rateType>
          <cb:observationPeriod rdf:parseType="Resource">
            <cb:frequency>daily</cb:frequency><cb:period>2026-08-20</cb:period>
          </cb:observationPeriod>
        </cb:exchangeRate>
      </cb:statistics>
    </item>

The rdf:about is a permanent anchor - #USD is the same string every day,
with only cb:value and cb:period changing underneath it. That is why
these are observations keyed by (source, series, period) and not items
keyed by a content hash: hashing the entry would make every day's rate
collide with yesterday's, and hashing the value would push 21 rows a day
into a tape meant for sporadic announcements.
"""

from __future__ import annotations

from ..errors import MalformedEntryError
from .base import ParsedFeed, parse_document
from .rdfcb import NS, RDF_ABOUT, cb_text as text, to_utc

# ISO 4217 "no currency". The RBA uses it for the trade-weighted index,
# which is an index level rather than a rate against a real currency.
NOT_A_CURRENCY = "XXX"


def _series_name(base: str, target: str, external_id: str | None) -> str:
    if target and target != NOT_A_CURRENCY:
        return f"{base}/{target}"
    # Fall back to the anchor fragment, e.g. AUD/TWI_4pm.
    fragment = (external_id or "").rsplit("#", 1)[-1] or "UNKNOWN"
    return f"{base}/{fragment}"


def parse(source, body: str) -> ParsedFeed:
    _root, entries = parse_document(source.name, body)
    result = ParsedFeed()

    for position, entry in enumerate(entries):
        external_id = entry.get(RDF_ABOUT) or text(entry, "rss:link")
        stats = entry.find("cb:statistics", NS)
        if stats is None:
            raise MalformedEntryError(
                f"{source.name}: entry {position} ({external_id!r}) has no "
                f"cb:statistics block"
            )
        rate = stats.find("cb:exchangeRate", NS)
        if rate is None:
            raise MalformedEntryError(
                f"{source.name}: entry {position} ({external_id!r}) has no "
                f"cb:exchangeRate block"
            )
        observation = rate.find("cb:observation", NS)
        period_block = rate.find("cb:observationPeriod", NS)
        if observation is None or period_block is None:
            raise MalformedEntryError(
                f"{source.name}: entry {position} ({external_id!r}) is missing "
                f"cb:observation or cb:observationPeriod"
            )

        raw_value = text(observation, "cb:value")
        period = text(period_block, "cb:period")
        base = text(rate, "cb:baseCurrency")
        target = text(rate, "cb:targetCurrency")

        for label, value in (
            ("cb:value", raw_value),
            ("cb:period", period),
            ("cb:baseCurrency", base),
            ("cb:targetCurrency", target),
        ):
            if not value:
                raise MalformedEntryError(
                    f"{source.name}: entry {position} ({external_id!r}) is "
                    f"missing {label}"
                )

        try:
            value = float(raw_value)
        except ValueError as exc:
            raise MalformedEntryError(
                f"{source.name}: entry {position} ({external_id!r}) has a "
                f"non-numeric cb:value {raw_value!r}"
            ) from exc

        decimals = text(observation, "cb:decimals")
        result.observations.append(
            {
                "series": _series_name(base, target, external_id),
                "period": period,
                "value": value,
                "unit": text(observation, "cb:unit"),
                "base_currency": base,
                "target_currency": target,
                "rate_type": text(rate, "cb:rateType"),
                "frequency": text(period_block, "cb:frequency"),
                "decimals": int(decimals) if decimals and decimals.isdigit() else None,
                "external_id": external_id,
                "observed_at": to_utc(
                    text(entry, "dc:date"), source.name, f"entry {position} dc:date"
                ),
            }
        )
    return result
