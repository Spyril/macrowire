"""CFETS CNY central parity fix -> rows in `observations`.

Not a feed. A JSON API, shape confirmed by probing:

    {"head": {"rep_code": "200", ...},
     "data": {"head": ["USD/CNY","EUR/CNY","100JPY/CNY","HKD/CNY",...25 pairs],
              "currency": "USD/CNY,AUD/CNY,HKD/CNY",
              "total": 242, "pageTotal": 5, "pageNum": 1, "pageSize": 50,
              "flagMessage": ""},
     "records": [{"date": "2026-08-21", "values": ["6.7817", "0.86466", "4.8058"]}]}

Note `records` sits at the TOP level, a sibling of `data`, not inside it.

POSITIONAL ALIGNMENT
--------------------
`values` is a bare array with no labels, and this is the one genuinely
dangerous thing about the endpoint. Measured behaviour:

  * values are NOT ordered by the `currency` parameter we send. The
    server emits them in its own canonical order - the order of
    `data.head` - filtered to the pairs requested. Asking for
    USD/CNY,AUD/CNY,HKD/CNY returns [USD, HKD, AUD].
  * `data.currency` is a verbatim echo of the request string. It comes
    back unchanged even when the server silently drops a pair, so it
    cannot detect anything and is not used for validation.
  * unknown, misspelled, duplicated and empty pairs are dropped in
    silence: the array simply comes back shorter.

Verified against single-pair requests (which are unambiguous), aligning
by request order mis-filed 4 of 5 pairs, every value plausible. Aligning
by `data.head` got 5 of 5 right.

So alignment is by `data.head` only, and every assumption behind it is
asserted rather than trusted.
"""

from __future__ import annotations

import json

from ..errors import MalformedEntryError, ParseError
from .base import ParsedFeed


def _pairs(source) -> list[dict]:
    configured = source.config.get("pairs") or []
    if not configured:
        raise ParseError(f"{source.name}: no pairs configured in sources.yaml")
    return configured


def requested_codes(source) -> list[str]:
    return [p["api_code"] for p in _pairs(source)]


def parse(source, body: str) -> ParsedFeed:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ParseError(f"{source.name}: response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParseError(f"{source.name}: expected a JSON object, got {type(payload).__name__}")

    envelope = payload.get("head") or {}
    rep_code = str(envelope.get("rep_code", ""))
    if rep_code != "200":
        raise ParseError(
            f"{source.name}: API reported rep_code={rep_code!r} "
            f"({envelope.get('rep_message')!r})"
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ParseError(f"{source.name}: response has no `data` object")

    # The server explains its own refusals here - an over-long date window
    # comes back as a 200 with an empty result and a note in this field.
    flag = (data.get("flagMessage") or "").strip()
    if flag:
        raise ParseError(f"{source.name}: API refused the query: {flag}")

    canonical = data.get("head")
    if not isinstance(canonical, list) or not canonical:
        raise ParseError(
            f"{source.name}: response carries no `data.head` currency list. "
            f"That list is the only reliable way to align `values`; refusing "
            f"to fall back to request order, which is measurably wrong."
        )

    wanted = _pairs(source)
    wanted_codes = [p["api_code"] for p in wanted]
    bounds = {p["api_code"]: (float(p["min"]), float(p["max"])) for p in wanted}

    missing = [c for c in wanted_codes if c not in canonical]
    if missing:
        raise ParseError(
            f"{source.name}: requested pair(s) {missing} are absent from "
            f"data.head {canonical}. CFETS drops unknown pairs silently, which "
            f"would shorten `values` and shift every later pair by one."
        )

    # The exact order the server will have used for `values`.
    expected = [code for code in canonical if code in set(wanted_codes)]
    if len(expected) != len(wanted_codes):
        raise ParseError(
            f"{source.name}: {len(wanted_codes)} pairs requested but only "
            f"{len(expected)} resolve against data.head - duplicates in config?"
        )

    records = payload.get("records")
    if records is None:
        raise ParseError(f"{source.name}: response has no `records` array")
    if not isinstance(records, list):
        raise ParseError(f"{source.name}: `records` is not an array")

    result = ParsedFeed()
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise MalformedEntryError(f"{source.name}: record {position} is not an object")

        period = record.get("date")
        values = record.get("values")
        if not period:
            raise MalformedEntryError(f"{source.name}: record {position} has no date")
        if not isinstance(values, list):
            raise MalformedEntryError(
                f"{source.name}: record {position} ({period}) has no values array"
            )

        # The assertion that matters. A short array means the server
        # dropped a pair, and every value after the gap belongs to a
        # different currency than the one it would land under.
        if len(values) != len(expected):
            raise MalformedEntryError(
                f"{source.name}: record {position} ({period}) returned "
                f"{len(values)} values for {len(expected)} requested pairs "
                f"{expected}. Refusing to guess which pair is missing."
            )

        for code, raw in zip(expected, values):
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise MalformedEntryError(
                    f"{source.name}: {period} {code} has a non-numeric value {raw!r}"
                ) from exc

            low, high = bounds[code]
            if not low <= value <= high:
                raise MalformedEntryError(
                    f"{source.name}: {period} {code} = {value} is outside the "
                    f"configured sanity range {low}-{high}. Either the market "
                    f"moved further than these bounds allow, or `values` has "
                    f"shifted position and this is another pair's rate."
                )

            base, _, target = code.partition("/")
            result.observations.append(
                {
                    "series": code,
                    "period": period,
                    "value": value,
                    "unit": target,
                    "base_currency": base,
                    "target_currency": target,
                    "rate_type": "CNY central parity fix (09:15 CST)",
                    "frequency": "daily",
                    "decimals": len(raw.partition(".")[2]) or None,
                    "external_id": f"{source.url}#{code}",
                    "observed_at": f"{period}T09:15:00{source.config.get('timezone', '+08:00')}",
                }
            )
    return result


# --- fetching -------------------------------------------------------------
#
# This source needs more than one GET, so it supplies its own fetcher.
# The pipeline uses it in place of the default single request.

def _publication_date(source, get) -> tuple[str, str]:
    """Read the fix timestamp CFETS advertises. Returns (date, raw stamp).

    This is the "has it actually published" check. Polling on clock time
    alone would fire on PRC public holidays and on any day the fix is
    late, and would look identical to a successful no-op.
    """
    url = source.config.get("publication_url")
    if not url:
        raise ParseError(f"{source.name}: no publication_url configured")

    response = get(url)
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseError(f"{source.name}: publication stamp is not valid JSON: {exc}") from exc

    stamp = ((payload.get("data") or {}).get("lastDate") or "").strip()
    if not stamp:
        raise ParseError(
            f"{source.name}: publication stamp carries no data.lastDate. "
            f"Refusing to fall back to clock time."
        )
    day = stamp.split()[0]
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        raise ParseError(
            f"{source.name}: publication stamp {stamp!r} is not the expected "
            f"'YYYY-MM-DD HH:MM' shape"
        )
    return day, stamp


def fetch(source, get, state):
    """Poll for a newly published fix.

    Returns a list of responses for the pipeline to store and parse, or
    an empty list with a reason when there is nothing new - which is a
    skip, not an error. A quiet CFETS is a weekend or a PRC holiday.
    """
    published_day, stamp = _publication_date(source, get)
    latest_held = state.get("latest_period")

    if latest_held and published_day <= latest_held:
        return [], f"no new fix: CFETS last published {stamp}, already stored"

    # Empty dates ask for the most recent records, which also closes any
    # gap left by a machine that was off for a few days.
    response = get(
        source.url,
        params={
            "startDate": "",
            "endDate": "",
            "currency": ",".join(requested_codes(source)),
            "pageNum": 1,
            "pageSize": int(source.config.get("page_size", 50)),
        },
    )
    return [response], f"fix published {stamp}"
