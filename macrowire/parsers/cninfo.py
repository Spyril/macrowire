"""CNINFO company announcements -> rows in `items`.

CNINFO (巨潮资讯网) is the CSRC-designated disclosure portal for both
mainland exchanges, so ONE API covers Shanghai and Shenzhen. Watchlist
driven, per ticker: the unfiltered feed ran 2,492 announcements on
2026-08-20, roughly three and a half times the entire rest of the tape
per day.

ACCESS. SSE and SZSE run the same 法律声明 clause 三: 任何机构或者个人可基于
非商业目的浏览、下载本网站的内容 - any organisation or individual may browse
and download for non-commercial purposes - followed by a prohibition scoped
to 以向他人出售牟利为目的, use for the purpose of selling to others for
profit. CNINFO itself publishes no use restriction at all; its only notice
is a warranty disclaimer. robots.txt is absent on SSE and CNINFO and served
empty (HTTP 200, zero bytes) by SZSE. See README, "Chinese exchanges".

THREE THINGS THIS API DOES THAT LOOK LIKE SUCCESS AND ARE NOT
--------------------------------------------------------------------------
Each was measured, not guessed, and each has a guard below.

1. `column` DOES NOT IDENTIFY THE EXCHANGE. Asking for 2026-08-20 with
   column=sse and column=szse returned BYTE-IDENTICAL responses - 23,554
   bytes, every secCode beginning with 3, which is Shenzhen ChiNext. It is
   not inert: per ticker, column=sse and column=szse both give 1,284 while
   omitting it gives 1,684. So it filters something, just not the exchange.
   Nothing here derives a market from it. The exchange comes from the code
   itself and the code is checked against the one we asked for.

2. `pageSize` IS CAPPED AT 30, SILENTLY. Asking for 50 or 200 returns 30
   with no error and no field saying so. A loop advancing pageNum by one
   and assuming it received pageSize rows skips everything past the
   thirtieth. Paging here reads len(announcements) and `hasMore`.

3. AN UNKNOWN QUERY RETURNS AN EMPTY ENVELOPE, NOT AN ERROR. A code CNINFO
   has never heard of gives HTTP 200 and `[]`; a query that matches nothing
   gives 200 with `announcements: null` and `totalRecordNum: 0`. Status
   says nothing. Every check below is on structure.

TIMESTAMPS ARE DATE-ONLY IN PRACTICE. `announcementTime` is epoch ms, and
often it is midnight Beijing time - 30 of 30 sampled rows for 600519 and
000001, 17 of 30 for 300750. It is not a recency effect: everything CATL
filed on 2026-07-25 is midnight-stamped while its 2026-08-12 filings carry
19:22. Batch submissions appear to lose the time; individual ones keep it.
Since the field cannot be told apart from a genuine 00:00, the source is
declared `date_only` and gets no position on the ribbon. Same call as HKMA,
for the same reason: inventing a time would be a lie about the record.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..errors import MalformedEntryError, ParseError
from .base import ParsedFeed

QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
PDF_BASE = "http://static.cninfo.com.cn/"

# CNINFO reports every time in Beijing time and never says so in the payload.
CST = timezone(timedelta(hours=8))

# The server clamps pageSize to this. Named so the paging loop and the guard
# that notices a change both read the same number.
MAX_PAGE_SIZE = 30

# Which exchange a listing code belongs to. These prefixes are the exchanges'
# own allocation, not a guess: 60/68 Shanghai, 00/30 Shenzhen, 8/4 Beijing.
# A code matching none of them is not filed under a guessed venue - it raises,
# because a wrong venue is worse than a missing one.
VENUES = {
    "600": "SSE", "601": "SSE", "603": "SSE", "605": "SSE", "688": "SSE",
    "000": "SZSE", "001": "SZSE", "002": "SZSE", "003": "SZSE",
    "300": "SZSE", "301": "SZSE",
    "430": "BSE", "830": "BSE", "831": "BSE", "832": "BSE", "833": "BSE",
    "834": "BSE", "835": "BSE", "836": "BSE", "837": "BSE", "838": "BSE",
    "839": "BSE", "870": "BSE", "871": "BSE", "872": "BSE", "873": "BSE",
    "874": "BSE", "875": "BSE", "920": "BSE",
}


def venue(code: str) -> str:
    found = VENUES.get(code[:3])
    if found is None:
        raise MalformedEntryError(
            f"cninfo: listing code {code!r} matches no known exchange prefix. "
            f"Refusing to file it under a guessed venue."
        )
    return found


def _require(payload: dict, key: str, context: str):
    if key not in payload:
        raise ParseError(
            f"cninfo: response for {context} has no {key!r}. Keys present: "
            f"{sorted(payload)[:12]}. The endpoint has changed shape."
        )
    return payload[key]


def read_page(body: str, expect_code: str | None = None,
              expect_org: str | None = None) -> dict:
    """One response, checked before any of it is used.

    Returns {rows, total, has_more, code}. The page must describe exactly ONE
    company: that is what `stock` was for, and when a parameter here is
    ignored the reply is the firehose - many different codes, HTTP 200, no
    error anywhere. Measured, not imagined: column=sse and column=szse
    returned byte-identical firehose pages for the same date.

    `expect_code` is checked when the caller knows it. The single-company
    check does not depend on knowing it, so it survives a re-parse of stored
    bytes where the original request is no longer in hand.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ParseError(f"cninfo: response for {expect_code} is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParseError(
            f"cninfo: response for {expect_code} is a {type(payload).__name__}, "
            f"expected an object. An empty list here is the 'no such code' reply."
        )

    # Structure, not status. A miss is a 200 with these two fields set this
    # way, and there is no error field anywhere in the envelope to check.
    rows = _require(payload, "announcements", expect_code)
    total = _require(payload, "totalRecordNum", expect_code)
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ParseError(
            f"cninfo: 'announcements' for {expect_code} is a "
            f"{type(rows).__name__}, expected a list or null."
        )

    codes, orgs = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise MalformedEntryError(
                f"cninfo: non-object row in a page for {expect_code or 'a ticker'}: {row!r}")
        code = str(row.get("secCode") or "")
        if not code:
            raise MalformedEntryError(
                f"cninfo: row {row.get('announcementId')!r} carries no secCode, "
                f"so there is no way to tell whose filing it is.")
        codes.add(code)
        if row.get("orgId"):
            orgs.add(str(row["orgId"]))

    # THE GUARD THAT MATTERS, and it needs no knowledge of the request: a
    # per-ticker query that came back describing several companies was not
    # honoured, whatever the status code said.
    if len(codes) > 1:
        raise ParseError(
            f"cninfo: one ticker was requested and {len(codes)} came back "
            f"({', '.join(sorted(codes)[:5])}...). The 'stock' parameter was "
            f"not honoured; refusing to store other companies' filings."
        )
    code = next(iter(codes), None)

    # And when the caller does know what it asked for, check that too.
    if expect_code and code is not None and code != expect_code:
        raise ParseError(
            f"cninfo: asked for {expect_code} and got {code}. The 'stock' "
            f"parameter was not honoured."
        )
    if expect_org and orgs and expect_org not in orgs:
        raise ParseError(
            f"cninfo: expected orgId {expect_org!r}, page carries "
            f"{sorted(orgs)}. Same code, different entity."
        )
    if code is not None:
        # Raises on a prefix we do not recognise rather than filing it under
        # a guessed exchange.
        venue(code)

    return {"rows": rows, "total": total, "code": code,
            "has_more": bool(payload.get("hasMore"))}


def _published(row: dict, code: str) -> tuple[str, bool]:
    """(ISO UTC, whether the source supplied a real time of day)."""
    raw = row.get("announcementTime")
    if not isinstance(raw, (int, float)):
        raise MalformedEntryError(
            f"cninfo: {code} announcement {row.get('announcementId')!r} has "
            f"announcementTime={raw!r}, expected epoch milliseconds."
        )
    moment = datetime.fromtimestamp(raw / 1000, CST)
    # Midnight Beijing means the batch lost its time, not that anything was
    # filed at midnight. Recorded so the tape can decline to print 00:00 as
    # though it were an observation.
    timed = (moment.hour, moment.minute) != (0, 0)
    return moment.astimezone(timezone.utc).isoformat(), timed


def parse(source, body: str) -> ParsedFeed:
    """One page of one ticker's announcements.

    The ticker is read out of the page rather than passed in. read_page()
    has already established that the page describes exactly one company, so
    taking the code from the rows is not a guess - and it means a re-parse of
    stored bytes, months after the request is gone, checks the same thing.
    """
    page = read_page(body)
    expect_code = page["code"]
    feed = ParsedFeed()
    if expect_code is None:
        # A window with no filings. Legitimate, and the pipeline's own
        # empty-feed rule decides whether an empty CYCLE is a fault.
        return feed
    for row in page["rows"]:
        title = (row.get("announcementTitle") or "").strip()
        if not title:
            raise MalformedEntryError(
                f"cninfo: {expect_code} announcement "
                f"{row.get('announcementId')!r} has no title."
            )
        announcement_id = row.get("announcementId")
        if announcement_id in (None, ""):
            raise MalformedEntryError(
                f"cninfo: {expect_code} announcement {title!r} has no id, so "
                f"it cannot be deduplicated across pages."
            )
        published_at, _timed = _published(row, expect_code)
        adjunct = (row.get("adjunctUrl") or "").strip()

        feed.items.append({
            # The title is the record, in the language it was published in.
            # Nothing here translates it, now or later.
            "title": title,
            "url": (PDF_BASE + adjunct) if adjunct else None,
            # Stored exactly as published, midnight rows included. HKMA's
            # date-only feed already stores 00:00 HKT the same way and the
            # `date_only` timing class is what keeps both off the ribbon.
            # Whether a given row carried a time is derivable from this
            # value, so it is not stored a second time.
            "published_at": published_at,
            "external_id": f"cninfo:{announcement_id}",
            "ticker": expect_code,
            # The company's own name, as CNINFO publishes it. Chinese, and
            # it stays Chinese.
            "summary": (row.get("secName") or "").strip() or None,
            "content": None,
            "announcement_type": source.config.get("announcement_type"),
            # Which exchange, derived from the code by the exchanges' own
            # allocation. NOT from `column`, which returns the same answer
            # for sse and szse and cannot tell them apart.
            "type_primary": venue(expect_code),
            # CNINFO does carry its own announcementType codes - strings like
            # "01010503||010112" - but publishes no key to them. Putting
            # undecoded codes on a filter chip would be showing noise and
            # calling it a category, so they are left out until they can be
            # read. The raw payload keeps them either way.
            "type_tags": None,
            "institution_abbrev": "CNINFO",
            "simple_title": None,
            "occurrence_date": None,
            # Same restraint as SEC filings: nothing on the face of a title
            # supports asserting this, so it stays NULL rather than guessed.
            "is_price_sensitive": None,
        })
    return feed


def fetch(source, get, state):
    """One request per watchlisted CN ticker, oldest-safe and paged.

    Empty watchlist returns nothing, which is a skip and not an error - the
    same contract as sec_edgar, for the same reason.
    """
    import time as _time

    from .. import watchlist as wl

    codes = (state.get("watchlist") or {}).get("CN") or []
    if not codes:
        return [], "watchlist has no CN tickers - nothing to poll"

    headers = {"User-Agent": source.user_agent,
               "Accept": "application/json",
               "Content-Type": "application/x-www-form-urlencoded"}

    def post(url, code, org_id, form):
        # The query goes in the POST body because that is what the endpoint
        # accepts, and ALSO in the URL query string - which httpx appends and
        # the server ignores - purely so the stored raw_responses row records
        # which ticker each body belongs to. Without it the archive is a pile
        # of identical URLs and a re-parse cannot tell them apart.
        return get(url, params={"stock": f"{code},{org_id}"},
                   headers=headers, data=form)

    def search(url, form):
        return get(url, headers=headers, data=form)

    cache = wl.load_orgid_cache()
    window_days = int(source.config.get("window_days", 30))
    delay = float(source.config.get("request_interval_seconds", 2.0))
    max_pages = int(source.config.get("max_pages_per_ticker", 20))

    end = datetime.now(CST).date()
    start = end - timedelta(days=window_days)
    se_date = f"{start.isoformat()}~{end.isoformat()}"

    responses, unresolved, seen = [], [], []
    for position, code in enumerate(sorted(codes)):
        entry = cache.get(code)
        if entry is None:
            found = wl.resolve_cn(
                code, fetch=lambda url, form: search(url, form).content, cache=cache)
            if found is None:
                unresolved.append(code)
                continue
            entry = found
        org_id = entry["orgId"]

        page_no = 1
        while page_no <= max_pages:
            if position or page_no > 1:
                _time.sleep(delay)
            response = post(QUERY_URL, code, org_id, {
                "pageNum": page_no,
                # Asking for more is pointless: the server clamps to 30 and
                # says nothing. Asking for exactly the cap means there is
                # nothing left for it to clamp silently.
                "pageSize": MAX_PAGE_SIZE,
                "tabName": "fulltext",
                "stock": f"{code},{org_id}",
                "seDate": se_date,
            })
            responses.append(response)

            # Decode strictly. This endpoint declares UTF-8 and honours it,
            # but CNINFO's own 404 page is GB2312 - the host is not
            # consistent with itself, so an error page arriving with HTTP
            # 200 has to fail here rather than parse as something.
            try:
                envelope = json.loads(response.content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ParseError(
                    f"cninfo: page {page_no} for {code} did not decode as the "
                    f"UTF-8 JSON this endpoint declares: {exc}. CNINFO serves "
                    f"error pages as GB2312, so this may be one arriving with "
                    f"HTTP 200."
                ) from exc

            # The strongest form of the guard, available only here because
            # only here are both halves in hand: what was actually sent, and
            # what actually came back.
            page = read_page(response.content.decode("utf-8"), code, org_id)
            rows = page["rows"]

            # The cap, measured rather than trusted. If CNINFO ever returns
            # MORE than it clamps to, the paging arithmetic below is wrong
            # and should stop rather than skip records.
            if len(rows) > MAX_PAGE_SIZE:
                raise ParseError(
                    f"cninfo: page {page_no} for {code} returned {len(rows)} "
                    f"rows for a pageSize of {MAX_PAGE_SIZE}. The cap has "
                    f"changed; paging cannot be trusted until this is checked."
                )
            if not page["has_more"] or not rows:
                break
            page_no += 1
        else:
            raise ParseError(
                f"cninfo: {code} still reported more pages after "
                f"{max_pages} requests over {window_days} days. Refusing to "
                f"keep paging; raise max_pages_per_ticker if this is real."
            )
        seen.append(code)

    if unresolved and not responses:
        raise ParseError(
            f"cninfo: none of the watchlisted CN codes {unresolved} resolve "
            f"against CNINFO's listing index")
    note = f"{len(seen)} ticker(s), {len(responses)} page(s), {se_date}"
    if unresolved:
        note += f"; unresolved: {', '.join(unresolved)}"
    return responses, note
