"""SSE Southbound Stock Connect turnover -> rows in `observations`.

港股通成交概况 - mainland money trading Hong Kong-listed shares, published
by the Shanghai Stock Exchange on its own site under SSE's terms. A dated
numeric series, so observations rather than items, on the same argument as
CFETS and CFTC.

The signal is the DIRECTION, not the level: buy minus sell is what says
whether mainland money went into HK names that day. Net is derived here and
stored alongside the published buy and sell, so the derivation stays
checkable - the same treatment CFTC positioning gets.

THE ENDPOINT, OBSERVED
--------------------------------------------------------------------------
Traced from the page's own module rather than guessed. `/services/hkexsc/
ggtscsj/ggtcjgk/` loads `search_southboundStock_2021.js`, in which the
daily tab reads:

    hkexsctradingDayUrl_New: sseQueryURL + 'ggt/getQuatationInfo.do?jsonCallBack=?'
    hkexsctradingDayParam_New: { 'tradeDate': '' }

So the daily figures come from `ggt/getQuatationInfo.do` and carry NO sqlId
at all. The monthly and yearly tabs are different endpoints again
(`commonQuery.do` with COMMON_SSE_JYFW_HGT_GGTCJXX_*), and northbound is a
third (`commonSoaQuery.do` with FW_HGTZL_*). None of them is interchangeable
with this one.

`jsonCallBack` is a JSONP wrapper the page needs and we do not: omitting it
returns bare JSON.

WHY HISTORY STARTS AT 2024-08-19
--------------------------------------------------------------------------
See sources.yaml. Briefly: the amount fields change CURRENCY on that date,
with nothing in the payload marking it, and three of them do not exist
before it. Dates before the boundary are REFUSED below rather than left to
a comment somebody has to remember - a series that silently changes
denomination is worse than a short one.

FOUR REPLIES THAT LOOK LIKE SUCCESS
--------------------------------------------------------------------------
1. `result: [None]` - a list of length one containing null - for a
   non-trading day AND for a nonsense date. Truthy, so `if not result`
   passes. Checked as `result[0] is not None`.

2. `quatationInfo: "success"` IS NOT A SIGNAL. It says "success" for
   tradeDate=99999999. Nothing here reads it and nothing should; it is
   named only so that a later reader does not wire it in thinking it means
   something.

3. Numbers arrive as display-formatted strings with thousands separators
   ("1,258.47"). Separators are stripped explicitly and anything still
   unparseable raises - a bare try/except would swallow a genuinely
   malformed number as easily as a comma.

4. Three different null conventions across this host: `null` (a field that
   does not exist for that date), `"-"` (the monthly endpoint's empty
   marker), and `[None]` (no data for the date). Each is handled by name.

A note on the sibling endpoint, since only one guard is needed here but the
difference is worth knowing: a bad sqlId on `commonQuery.do` returns a
silent empty envelope (`result: null`, `total: 0`, no error field), while
`commonSoaQuery.do` returns HTTP 200 with `Content-Type: application/json`
and a body that is not JSON at all - a parenthesised JSONP wrapper with no
callback name, `\\n({"success":"false",...})`. Two failure modes on one
host; do not assume one guard covers both.

UNITS ARE NOT IN THE PAYLOAD
--------------------------------------------------------------------------
The scale appears only in the rendered column headers, never in the JSON,
so it is stored explicitly on every observation:

    当日买入成交金额（亿元）   BUY_AMOUNT   -> 100 million HKD
    当日买入成交笔数（万笔）   BUY_VOLUME   -> 10,000 TRADES

`*_VOLUME` is a COUNT OF TRADES, not a share count. The field name says
volume and the column header says 笔数. Storing it as shares would be a
silent factual error of exactly the kind a field name invites.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

from ..errors import MalformedEntryError, ParseError
from .base import ParsedFeed

QUERY_URL = "https://query.sse.com.cn/ggt/getQuatationInfo.do"

# The page renders under this path and the endpoint expects to be called
# from it. Sent so the request looks like what it is rather than anonymous.
REFERER = "https://www.sse.com.cn/services/hkexsc/ggtscsj/ggtcjgk/"

# 自2024年8月19日起，本页面港股通成交金额单位为港元。
# Before this date the amounts are CNY and TOTAL_AMOUNT, TOTAL_VOLUME and
# ETF_TOTAL_AMOUNT are null. Nothing in the payload marks either fact.
UNIT_BREAK = date(2024, 8, 19)

AMOUNT_UNIT = "100 million HKD"     # 亿元
TRADE_UNIT = "10,000 trades"        # 万笔

# Hong Kong close. Stock Connect settles to the HK session, and the payload
# carries a date with no time, so this is the instant the day's figure
# describes - declared here rather than implied by a bare date.
HKT = timezone(timedelta(hours=8))
HK_CLOSE = time(16, 0)

# Published field -> (series suffix, unit). Amounts and trade counts are
# deliberately in separate namespaces: they are different quantities and a
# shared prefix would invite summing them.
AMOUNTS = {
    "BUY_AMOUNT": "amount/buy",
    "SELL_AMOUNT": "amount/sell",
    "TOTAL_AMOUNT": "amount/total",
    "ETF_TOTAL_AMOUNT": "amount/etf",
}
TRADES = {
    "BUY_VOLUME": "trades/buy",
    "SELL_VOLUME": "trades/sell",
    "TOTAL_VOLUME": "trades/total",
}

# The monthly endpoint writes "-" where a value is absent. This parser does
# not read that endpoint, but the same host's conventions leak between
# payloads and an unrecognised marker must never become a number.
EMPTY_MARKERS = ("", "-", "--", "null", "None")


def number(raw, field: str, context: str) -> float | None:
    """A published figure, or None if the source published no value.

    Separators are stripped by name. Anything left that is not a number
    raises: catching broadly here would make a corrupted digit
    indistinguishable from a thousands separator.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        raise MalformedEntryError(
            f"sse_southbound: {context} {field} is a {type(raw).__name__}, "
            f"expected a number or a formatted string")
    text = raw.strip()
    if text in EMPTY_MARKERS:
        return None
    stripped = text.replace(",", "")
    try:
        return float(stripped)
    except ValueError as exc:
        raise MalformedEntryError(
            f"sse_southbound: {context} {field}={raw!r} is not a number once "
            f"thousands separators are removed ({stripped!r})") from exc


def read_row(body: str) -> dict | None:
    """The one row a daily reply carries, or None if there is no data.

    None means the source published nothing for that date - a weekend, a
    holiday, or a date that does not exist. It is not an error: all three
    are honestly "no figure was published", and this endpoint gives no way
    to tell them apart.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ParseError(
            f"sse_southbound: reply is not JSON: {exc}. This host also serves "
            f"a parenthesised JSONP wrapper as application/json on some error "
            f"paths, so a success status means nothing here.") from exc
    if not isinstance(payload, dict):
        raise ParseError(
            f"sse_southbound: reply is a {type(payload).__name__}, expected an "
            f"object")
    if "result" not in payload:
        raise ParseError(
            f"sse_southbound: reply has no 'result'. Keys present: "
            f"{sorted(payload)[:12]}. The endpoint has changed shape.")

    # NOT payload["quatationInfo"], which reads "success" for
    # tradeDate=99999999 and is therefore worth nothing as a check.
    rows = payload["result"]
    if rows is None:
        return None
    if not isinstance(rows, list):
        raise ParseError(
            f"sse_southbound: 'result' is a {type(rows).__name__}, expected a "
            f"list or null")
    if not rows:
        return None
    # THE GUARD. A missing date returns [None]: a list of length one holding
    # null, which is truthy and passes every emptiness check that does not
    # look inside it.
    if rows[0] is None:
        return None
    if not isinstance(rows[0], dict):
        raise MalformedEntryError(
            f"sse_southbound: result[0] is a {type(rows[0]).__name__}, "
            f"expected an object")
    if len(rows) > 1:
        raise ParseError(
            f"sse_southbound: a single-date query returned {len(rows)} rows. "
            f"The endpoint no longer answers one date at a time.")
    return rows[0]


def trade_date(row: dict) -> date:
    raw = (row.get("TRADE_DATE") or "").strip()
    if not raw:
        raise MalformedEntryError(
            "sse_southbound: row carries no TRADE_DATE, so there is no period "
            "to file it under")
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    raise MalformedEntryError(
        f"sse_southbound: TRADE_DATE={raw!r} is neither YYYY-MM-DD nor "
        f"YYYYMMDD. This host uses both, on the same day, across endpoints.")


def parse(source, body: str) -> ParsedFeed:
    feed = ParsedFeed()
    row = read_row(body)
    if row is None:
        return feed

    day = trade_date(row)
    # Machine-enforced, not a comment. Before this date BUY_AMOUNT and
    # SELL_AMOUNT are CNY rather than HKD and nothing in the payload says
    # so, so a row from the far side of the boundary must not reach the
    # same series under the same unit.
    if day < UNIT_BREAK:
        raise ParseError(
            f"sse_southbound: {day} is before the {UNIT_BREAK} currency "
            f"change (自2024年8月19日起，本页面港股通成交金额单位为港元). "
            f"Amounts before it are CNY, not HKD, and TOTAL_AMOUNT, "
            f"TOTAL_VOLUME and ETF_TOTAL_AMOUNT do not exist. Storing it "
            f"would splice two different series under one name.")

    period = day.isoformat()
    observed_at = datetime.combine(day, HK_CLOSE, tzinfo=HKT).astimezone(
        timezone.utc).isoformat()
    context = f"{period}"

    def emit(series: str, value: float, unit: str, currency: str | None,
             note: str, decimals: int) -> None:
        feed.observations.append({
            "series": f"SOUTHBOUND/{series}",
            "period": period,
            "value": value,
            "unit": unit,
            "base_currency": currency,
            "target_currency": None,
            "rate_type": note,
            "frequency": "daily",
            "decimals": decimals,
            "external_id": f"{period}#{series}",
            "observed_at": observed_at,
        })

    for field, series in AMOUNTS.items():
        value = number(row.get(field), field, context)
        if value is None:
            # A field the source did not publish for this date. Omitted
            # rather than stored as zero: a zero would be a number nobody
            # published, and it would average and chart as one.
            continue
        emit(series, value, AMOUNT_UNIT, "HKD",
             f"Southbound Stock Connect {series.split('/')[1]} turnover, "
             f"as published by SSE in 亿元 HKD", 2)

    for field, series in TRADES.items():
        value = number(row.get(field), field, context)
        if value is None:
            continue
        emit(series, value, TRADE_UNIT, None,
             f"Southbound Stock Connect {series.split('/')[1]} trade count, "
             f"as published by SSE in 万笔 (10,000 trades) - a count of "
             f"trades, NOT of shares", 2)

    # Net, derived, stored beside the two published figures it comes from so
    # the arithmetic stays checkable. Only when BOTH sides were published:
    # a net computed against a missing half is a number about our gaps.
    buy = number(row.get("BUY_AMOUNT"), "BUY_AMOUNT", context)
    sell = number(row.get("SELL_AMOUNT"), "SELL_AMOUNT", context)
    if buy is not None and sell is not None:
        emit("amount/net", round(buy - sell, 2), AMOUNT_UNIT, "HKD",
             "Southbound Stock Connect net flow in 亿元 HKD "
             "(derived: buy minus sell). Positive is mainland money into "
             "Hong Kong-listed shares.", 2)
    return feed


def fetch(source, get, state):
    """The most recent trading day. Backfill walks dates; this does not.

    An empty `tradeDate` asks for the latest published figure, which is what
    a poll wants: there is no way to ask "anything since X" and guessing
    yesterday would miss a day after any outage.
    """
    response = get(QUERY_URL, params={"tradeDate": ""},
                   headers={"User-Agent": source.user_agent,
                            "Accept": "application/json",
                            "Referer": REFERER})
    return [response], None
