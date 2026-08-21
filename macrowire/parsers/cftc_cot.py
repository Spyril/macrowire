"""CFTC Commitments of Traders, currency futures -> rows in `observations`.

A dated numeric series, so it lands in `observations` for the same reason
the RBA and CFETS rates do. What it adds is a kind of information the tape
cannot carry: how speculative money is positioned, and which way it moved.

THERE IS NO PUBLISHED NET FIELD - AND TWELVE FIELDS THAT LOOK LIKE ONE.
--------------------------------------------------------------------------
The payload has 133 fields. Twelve of them contain "net":

    conc_net_le_4_tdr_long_all, conc_net_le_8_tdr_short_all, ...

Every one is a CONCENTRATION RATIO - the net position of the four or eight
largest traders, as a share of open interest. None is the non-commercial
net. Reaching for `conc_net_le_4_tdr_long_all` because it matches on name
returns a plausible number measuring something else entirely, and nothing
downstream would catch it.

The net this stores is `noncomm_positions_long_all - noncomm_positions_
short_all`: two published fields, same row, same report. Both components
are stored alongside it so the arithmetic stays checkable rather than
becoming a number of unknown provenance.

CONTRACTS ARE PINNED BY CODE, NEVER BY NAME.
--------------------------------------------------------------------------
The dataset carries `JAPANESE YEN-dormant`, `SWISS FRANC-dormant`,
`POUND STERLING-OLD`, `MARK/YEN XRATE-OLD`, `AUSTRALIAN DOLLAR - SMALL`
and a shelf of cross-rate contracts. Matching on name would sweep those in
and produce a series that looks continuous while silently mixing
instruments. sources.yaml pins eight `cftc_contract_market_code` values,
and the parser ASSERTS the returned name matches the expected one - if the
CFTC ever reassigns a code, that raises rather than quietly changing what
is being tracked.
"""

from __future__ import annotations

import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

from ..errors import MalformedEntryError, ParseError
from .base import ParsedFeed

# Fields the parser needs. Requested explicitly so a 133-field payload does
# not travel over the wire 13,767 times.
SELECT = (
    "id", "report_date_as_yyyy_mm_dd", "cftc_contract_market_code",
    "contract_market_name", "open_interest_all",
    "noncomm_positions_long_all", "noncomm_positions_short_all",
    "change_in_noncomm_long_all", "change_in_noncomm_short_all",
)

# Metric -> whether it is published or derived from published fields.
DERIVED = {"net", "change_net"}


def contracts(source) -> dict[str, dict]:
    pinned = source.config.get("contracts") or []
    if not pinned:
        raise ParseError(f"{source.name}: no contracts pinned in sources.yaml")
    return {str(c["code"]): c for c in pinned}


def _number(row, field, source_name, context, required: bool = True):
    """Parse an integer field. `required=False` allows a genuine absence.

    The week-on-week change fields are absent on 29 of 13,767 historical
    rows: a contract's first report has no prior week, and there are gaps
    mid-history too (NZ Dollar has 9, the USD Index 5, so it is not simply
    one per contract). That is missing data, not a malformed row, and
    raising on it made the whole history unfetchable.
    """
    raw = row.get(field)
    if raw is None or raw == "":
        if required:
            raise MalformedEntryError(f"{source_name}: {context} has no {field}")
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError) as exc:
        raise MalformedEntryError(
            f"{source_name}: {context} {field}={raw!r} is not numeric") from exc


def parse(source, body: str) -> ParsedFeed:
    try:
        rows = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ParseError(f"{source.name}: response is not valid JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ParseError(
            f"{source.name}: expected a JSON array, got {type(rows).__name__}")

    pinned = contracts(source)
    zone = ZoneInfo(source.config.get("timezone", "America/New_York"))
    result = ParsedFeed()

    for index, row in enumerate(rows):
        code = str(row.get("cftc_contract_market_code") or "")
        spec = pinned.get(code)
        if spec is None:
            # Asked for eight codes; anything else means the query changed
            # underneath us, not that this row should be quietly dropped.
            raise ParseError(
                f"{source.name}: row {index} carries contract code {code!r}, "
                f"which is not among the pinned {sorted(pinned)}")

        # The assertion that stops a reassigned code changing what is
        # tracked without anybody noticing.
        name = (row.get("contract_market_name") or "").strip()
        if name != spec["name"]:
            raise ParseError(
                f"{source.name}: contract {code} returned {name!r} but "
                f"sources.yaml expects {spec['name']!r}. The CFTC may have "
                f"reassigned the code; refusing to store it as {spec['currency']}.")

        stamp = row.get("report_date_as_yyyy_mm_dd") or ""
        period = stamp[:10]
        if len(period) != 10 or period[4] != "-":
            raise MalformedEntryError(
                f"{source.name}: row {index} report date {stamp!r} is not "
                f"YYYY-MM-DD")

        context = f"{spec['currency']} {period}"
        long_all = _number(row, "noncomm_positions_long_all", source.name, context)
        short_all = _number(row, "noncomm_positions_short_all", source.name, context)
        change_long = _number(row, "change_in_noncomm_long_all", source.name,
                              context, required=False)
        change_short = _number(row, "change_in_noncomm_short_all", source.name,
                               context, required=False)

        # Arithmetic on two published fields of one row - not a cross of two
        # separate series, and both components are stored beside it.
        metrics = {
            "long": long_all,
            "short": short_all,
            "net": long_all - short_all,
        }
        # Omitted rather than stored as zero: "no prior week" and "no change
        # from last week" are different facts and must not look alike.
        if change_long is not None and change_short is not None:
            metrics["change_long"] = change_long
            metrics["change_short"] = change_short
            metrics["change_net"] = change_long - change_short

        # The report date is the TUESDAY the positions were held. The report
        # is released the following Friday at 15:30 ET. The payload carries a
        # date and no time, so no clock time is invented here.
        observed_at = datetime.combine(
            datetime.strptime(period, "%Y-%m-%d").date(), time(0, 0), tzinfo=zone
        ).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")

        currency = spec["currency"]
        for metric, value in metrics.items():
            result.observations.append({
                "series": f"COT/{currency}/{metric}",
                "period": period,
                "value": float(value),
                "unit": "contracts",
                "base_currency": currency,
                "target_currency": None,
                "rate_type": (
                    f"CFTC non-commercial {metric}"
                    f"{' (derived: long minus short)' if metric in DERIVED else ''}"
                    f", positions as of Tuesday close, released Friday 15:30 ET"),
                "frequency": "weekly",
                "decimals": 0,
                "external_id": f"{row.get('id') or code}#{metric}",
                "observed_at": observed_at,
            })
    return result


# --- fetching -------------------------------------------------------------

BASE = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"


def _codes_clause(source) -> str:
    quoted = ",".join(f"'{c}'" for c in contracts(source))
    return f"cftc_contract_market_code in({quoted})"


def query(source, *, limit: int, offset: int = 0, order: str = "ASC",
          latest_only: bool = False) -> dict:
    """Socrata query parameters. Only the fields the parser uses."""
    params = {
        "$select": ",".join(SELECT),
        "$where": _codes_clause(source),
        "$order": f"report_date_as_yyyy_mm_dd {order}",
        "$limit": limit,
    }
    if offset:
        params["$offset"] = offset
    if latest_only:
        params["$select"] = "report_date_as_yyyy_mm_dd"
    return params


def fetch(source, get, state):
    """Poll only once a newer report has actually been published.

    The report lands Friday 15:30 ET for the Tuesday positions, but the
    schedule is confirmed from the payload rather than assumed from the
    clock: one cheap query for the newest report date, compared against
    what is already stored. A holiday-delayed release then costs a skip
    rather than a false alarm.
    """
    import time as _time

    delay = float(source.config.get("request_interval_seconds", 1.0))

    probe = get(BASE, params=query(source, limit=1, order="DESC", latest_only=True))
    try:
        rows = json.loads(probe.content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseError(f"{source.name}: report-date probe is not JSON: {exc}") from exc
    if not rows:
        raise ParseError(f"{source.name}: report-date probe returned no rows")

    published = (rows[0].get("report_date_as_yyyy_mm_dd") or "")[:10]
    if not published:
        raise ParseError(f"{source.name}: report-date probe carries no date")

    held = state.get("latest_period")
    if held and published <= held:
        return [], f"no new report: CFTC latest is {published}, already stored"

    # One page is ample for a weekly release: eight contracts a week.
    _time.sleep(delay)
    page = get(BASE, params=query(source, limit=int(source.config.get("page_size", 200)),
                                  order="DESC"))
    return [page], f"report {published} published"
