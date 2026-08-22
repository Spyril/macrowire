"""One-off historical seeding for paginated API sources.

Separate from `fetch` on purpose. A poll is a small, frequent, polite
request; a backfill is a bulk pull against a server that owes us nothing,
and the two should not share a code path or a rate limit.

Resumable by construction: every completed page is written to fetch_log
as a `backfill` row, and a resumed run skips any page already recorded.
An interruption costs one page, not the whole pull.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, timedelta

from . import db, wire
from .config import Source
from .encoding import decode
from .errors import BackfillInterrupted, ConfigError, FetchError, MacroWireError
from .parsers import get_parser

# A paced seed runs for tens of minutes against a public server over a link
# nobody controls. A dropped connection partway through is an expected
# event, not an exceptional one, so it is retried here rather than ending
# the run.
#
# ONLY the kinds that describe the PATH. db.PATH_KINDS is the one definition
# of which those are - the same list the health panel uses to decide
# "unreachable" rather than "failing" - so the two cannot drift apart. An
# http_404, a decode failure or a parse error is a statement about the
# source or the payload and will be exactly as wrong on the third attempt as
# on the first; retrying it would turn a clear failure into a slow one.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (5, 15)      # waits BETWEEN attempts, so len == attempts - 1


def download(source: Source, *args, attempts: int = RETRY_ATTEMPTS, **kwargs):
    """wire._download, retried on transport failures and nothing else.

    Returns the response, or re-raises the last error once the attempts are
    spent. Non-path failures re-raise immediately and untouched.
    """
    last: FetchError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return wire._download(source, *args, **kwargs)
        except FetchError as exc:
            if exc.kind not in db.PATH_KINDS:
                raise
            last = exc
            if attempt == attempts:
                break
            wait = RETRY_BACKOFF_SECONDS[min(attempt - 1,
                                             len(RETRY_BACKOFF_SECONDS) - 1)]
            print(f"    {exc.kind} on attempt {attempt}/{attempts}; "
                  f"retrying in {wait}s")
            time.sleep(wait)
    raise last


def windows(start: date, end: date, span_days: int) -> list[tuple[date, date]]:
    """Split a range into chunks the API will actually accept.

    CFETS rejects any window of 365 days or more with
    "只提供一年历史数据查询及下载"; 364 is verified to work.
    """
    out, cursor = [], start
    while cursor <= end:
        upper = min(cursor + timedelta(days=span_days - 1), end)
        out.append((cursor, upper))
        cursor = upper + timedelta(days=1)
    return out


def _marker(lo: date, hi: date, page: int) -> str:
    return f"{lo}..{hi} page {page}"


def _completed(conn: sqlite3.Connection, source: str) -> set[str]:
    rows = conn.execute(
        "SELECT detail FROM fetch_log WHERE source = ? AND status = 'backfill'",
        (source,),
    ).fetchall()
    return {r[0] for r in rows if r[0]}


def single_url(source: Source) -> str | None:
    """Some sources seed from ONE request rather than a paginated walk.

    The ECB publishes a 90-day file in the same shape as its daily one, so
    seeding is a single fetch and the window/page machinery would be
    ceremony around a single GET.
    """
    return source.config.get("backfill_url")


def run_single(conn: sqlite3.Connection, source: Source, dry_run: bool = False) -> dict:
    url = single_url(source)
    print(f"  single-request seed: {url}")
    if dry_run:
        print("\n  dry run - no requests made.")
        return {"requests": 0, "stored": 0, "skipped": 0, "dry_run": True}

    source_id = db.upsert_source(conn, source.name, source.kind, source.config)
    conn.commit()

    response = download(source, url)
    raw_id = db.store_raw_response(
        conn, source.name, str(response.url), response.status_code, response.content)
    conn.commit()
    body, encoding_used = decode(
        source.name, response.content, response.headers.get("content-type"))
    db.record_raw_encoding(conn, raw_id, encoding_used)

    parsed = get_parser(source.parser)(source, body)
    new, revisions = wire._store_observations(conn, source, source_id, parsed)
    db.log_fetch(conn, source.name, status=db.STATUS_BACKFILL, new_item_count=new,
                 detail=f"single-request seed from {url}")
    conn.commit()

    periods = sorted({o["period"] for o in parsed.observations})
    print(f"    {len(parsed.observations)} parsed over {len(periods)} date(s) "
          f"{periods[0]} .. {periods[-1]}" if periods else "    nothing parsed")
    print(f"    {new} new" + (f", {len(revisions)} REVISED" if revisions else ""))
    return {"requests": 1, "stored": new, "skipped": 0, "dry_run": False,
            "revisions": revisions}


def per_date(source: Source) -> bool:
    """Sources whose API answers ONE date and offers no range.

    SSE's southbound endpoint takes a single `tradeDate`, so the seed is a
    walk over dates rather than over windows or offsets. Slower per row than
    either, and there is no faster shape available - the monthly endpoint
    returns averages, which is different data, not the same data coarser.
    """
    return bool(source.config.get("backfill_per_date"))


def weekdays(start: date, end: date) -> list[date]:
    """Candidate dates. Weekends are skipped because no market is open.

    Public holidays are NOT skipped: this tool has no CN/HK holiday
    calendar, and the endpoint answers a holiday exactly as it answers a
    weekend - result [None], stored as nothing. Asking and being told
    nothing was published is honest; guessing a calendar would eventually
    skip a day that traded.
    """
    out, cursor = [], start
    while cursor <= end:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def run_dated(conn: sqlite3.Connection, source: Source, today: date,
              dry_run: bool = False) -> dict:
    """One request per candidate date, resumable, strictly sequential."""
    start = date.fromisoformat(source.config["backfill_start"])
    delay = float(source.config.get("backfill_delay_seconds", 3))
    candidates = weekdays(start, today)
    done = _completed(conn, source.name)
    remaining = [d for d in candidates if d.isoformat() not in done]

    print(f"  range          : {start} .. {today}")
    print(f"  requests       : {len(candidates)} weekday(s), one per date")
    print(f"  already done   : {len(done)} from a previous run")
    print(f"  to make now    : {len(remaining)}")
    print(f"  delay          : {delay}s between requests, sequential")
    print(f"  estimated time : {len(remaining) * delay / 60:.0f} min")
    print(f"  holidays are asked for and answer 'no data'; nothing is stored")
    if dry_run:
        print("\n  dry run - no requests made.")
        return {"requests": 0, "stored": 0, "skipped": len(done), "dry_run": True}

    source_id = db.upsert_source(conn, source.name, source.kind, source.config)
    conn.commit()
    parser = get_parser(source.parser)
    from .parsers import sse_southbound

    requests_made = stored = empty = 0
    for index, day in enumerate(remaining):
        if index:
            time.sleep(delay)
        try:
            response = download(
                source, source.url, {"tradeDate": day.strftime("%Y%m%d")},
                headers={"User-Agent": source.user_agent,
                         "Accept": "application/json",
                         "Referer": sse_southbound.REFERER})
        except MacroWireError as exc:
            # Retries are spent, or this was never retryable. Either way the
            # run stops HERE, with everything before it already committed
            # and logged, so resuming costs only the dates not yet reached.
            db.log_fetch(conn, source.name, status=db.STATUS_ERROR,
                         error=f"{type(exc).__name__}: {exc}",
                         error_kind=getattr(exc, "kind", "unknown"),
                         detail=f"backfill stopped at {day}")
            conn.commit()
            raise BackfillInterrupted(
                source.name, day, len(remaining) - index, exc) from exc
        requests_made += 1

        raw_id = db.store_raw_response(
            conn, source.name, str(response.url), response.status_code, response.content)
        conn.commit()
        body, encoding_used = decode(
            source.name, response.content, response.headers.get("content-type"))
        db.record_raw_encoding(conn, raw_id, encoding_used)

        parsed = parser(source, body)
        if not parsed.observations:
            # No figure published for that date. Logged as done so a resumed
            # run does not ask again, and counted so a run that finds nothing
            # for weeks is visible rather than quietly successful.
            empty += 1
            db.log_fetch(conn, source.name, status=db.STATUS_BACKFILL,
                         new_item_count=0, detail=day.isoformat())
            conn.commit()
            continue

        # The reply carries its own TRADE_DATE. Check it against the date we
        # asked for: this endpoint answers 200 for anything, and a row filed
        # under the wrong day is worse than a missing one.
        got = {o["period"] for o in parsed.observations}
        if got != {day.isoformat()}:
            raise ConfigError(
                f"{source.name}: asked for {day} and the reply carried "
                f"{sorted(got)}. Refusing to store a figure under a date it "
                f"does not belong to.")

        new, revisions = wire._store_observations(conn, source, source_id, parsed)
        stored += new
        db.log_fetch(conn, source.name, status=db.STATUS_BACKFILL,
                     new_item_count=new, detail=day.isoformat())
        conn.commit()
        net = next((o["value"] for o in parsed.observations
                    if o["series"].endswith("amount/net")), None)
        print(f"    {day}  {len(parsed.observations):>2} obs  "
              f"net {net:>9}  {new:>2} new"
              + (f", {len(revisions)} REVISED" if revisions else ""))

    print(f"\n  {empty} date(s) had no published figure (weekend or holiday).")
    return {"requests": requests_made, "stored": stored, "skipped": len(done),
            "dry_run": False}


def paged_query(source: Source):
    """Sources that seed by walking offsets rather than date windows.

    CFTC publishes one Socrata endpoint with the whole history behind it;
    the natural seed is `$limit`/`$offset` pages, not date ranges.
    """
    return source.config.get("backfill_page_size")


def run_paged(conn: sqlite3.Connection, source: Source, dry_run: bool = False) -> dict:
    from .parsers import cftc_cot

    page_size = int(paged_query(source))
    delay = float(source.config.get("request_interval_seconds", 1.0))
    print(f"  paged seed: {source.url}")
    print(f"  page size : {page_size}   delay: {delay}s between pages "
          f"(their robots.txt asks Crawl-delay: 1)")
    if dry_run:
        print("\n  dry run - no requests made.")
        return {"requests": 0, "stored": 0, "skipped": 0, "dry_run": True}

    source_id = db.upsert_source(conn, source.name, source.kind, source.config)
    conn.commit()
    parser = get_parser(source.parser)
    requests_made = stored = 0
    offset = 0
    first = True

    while True:
        if not first:
            time.sleep(delay)
        first = False
        response = download(
            source, source.url,
            cftc_cot.query(source, limit=page_size, offset=offset, order="ASC"))
        requests_made += 1

        raw_id = db.store_raw_response(
            conn, source.name, str(response.url), response.status_code, response.content)
        conn.commit()
        body, encoding_used = decode(
            source.name, response.content, response.headers.get("content-type"))
        db.record_raw_encoding(conn, raw_id, encoding_used)

        parsed = parser(source, body)
        if not parsed.observations:
            break
        new, revisions = wire._store_observations(conn, source, source_id, parsed)
        stored += new

        # One API row expands to several observations (one per metric), so
        # the page is counted in API rows - the thing $limit actually
        # bounds - not in rows written.
        api_rows = len({o["external_id"].split("#", 1)[0] for o in parsed.observations})
        periods = sorted({o["period"] for o in parsed.observations})
        db.log_fetch(conn, source.name, status=db.STATUS_BACKFILL, new_item_count=new,
                     detail=f"offset {offset} ({periods[0]}..{periods[-1]})")
        conn.commit()
        print(f"    offset {offset:>6}  {api_rows:>5} rows -> "
              f"{len(parsed.observations):>5} observations  "
              f"{periods[0]} .. {periods[-1]}  {new:>5} new"
              + (f", {len(revisions)} REVISED" if revisions else ""))

        offset += page_size
        if api_rows < page_size:      # a short page is the last page
            break

    return {"requests": requests_made, "stored": stored, "skipped": 0, "dry_run": False}


def plan(source: Source, today: date) -> dict:
    start = source.config.get("backfill_start")
    if not start:
        raise ConfigError(f"{source.name}: no backfill_start configured")
    span = int(source.config.get("window_days", 364))
    page_size = int(source.config.get("page_size", 50))
    spans = windows(date.fromisoformat(start), today, span)

    # A fixing happens on PRC trading days: weekdays less public holidays,
    # which run about 4.5% of weekdays across a year.
    estimated_pages = 0
    for lo, hi in spans:
        weekdays = sum(
            1 for i in range((hi - lo).days + 1) if (lo + timedelta(days=i)).weekday() < 5
        )
        fixings = int(weekdays * 0.955)
        estimated_pages += max(1, -(-fixings // page_size))

    return {
        "windows": spans,
        "page_size": page_size,
        "estimated_pages": estimated_pages,
        "delay": int(source.config.get("backfill_delay_seconds", 8)),
    }


def run(conn: sqlite3.Connection, source: Source, today: date, dry_run: bool = False) -> dict:
    """Walk every window and page, storing as it goes.

    Requests are strictly sequential with a fixed delay between them.
    There is no concurrency here by design.
    """
    if single_url(source):
        return run_single(conn, source, dry_run=dry_run)
    if per_date(source):
        return run_dated(conn, source, today, dry_run=dry_run)
    if paged_query(source):
        return run_paged(conn, source, dry_run=dry_run)

    outline = plan(source, today)
    done = _completed(conn, source.name)
    source_id = db.upsert_source(conn, source.name, source.kind, source.config)
    conn.commit()

    print(f"  range          : {outline['windows'][0][0]} .. {outline['windows'][-1][1]}")
    print(f"  windows        : {len(outline['windows'])}  "
          f"(max {source.config.get('window_days', 364)} days each)")
    print(f"  page size      : {outline['page_size']}  (server rejects more)")
    print(f"  estimated pages: {outline['estimated_pages']}")
    print(f"  delay          : {outline['delay']}s between requests, sequential")
    print(f"  already done   : {len(done)} page(s) from a previous run")
    if dry_run:
        print("\n  dry run - no requests made.")
        return {"requests": 0, "stored": 0, "skipped": len(done), "dry_run": True}

    parser = get_parser(source.parser)
    codes = ",".join(p["api_code"] for p in source.config["pairs"])
    requests_made = stored = skipped = 0
    first_request = True

    for lo, hi in outline["windows"]:
        page, page_total = 1, None
        while page_total is None or page <= page_total:
            marker = _marker(lo, hi, page)
            if marker in done:
                skipped += 1
                if page_total is None:
                    # Cannot learn pageTotal without asking; assume the
                    # recorded run got through this window and move on.
                    page_total = page
                page += 1
                continue

            if not first_request:
                time.sleep(outline["delay"])
            first_request = False

            response = download(
                source,
                source.url,
                {
                    "startDate": lo.isoformat(),
                    "endDate": hi.isoformat(),
                    "currency": codes,
                    "pageNum": page,
                    "pageSize": outline["page_size"],
                },
            )
            requests_made += 1

            raw_id = db.store_raw_response(
                conn, source.name, str(response.url), response.status_code, response.content
            )
            conn.commit()
            body, encoding_used = decode(
                source.name, response.content, response.headers.get("content-type")
            )
            db.record_raw_encoding(conn, raw_id, encoding_used)

            parsed = parser(source, body)
            new, revisions = wire._store_observations(conn, source, source_id, parsed)
            stored += new
            db.log_fetch(
                conn, source.name, status=db.STATUS_BACKFILL,
                new_item_count=new, detail=marker,
            )
            conn.commit()

            if page_total is None:
                import json
                page_total = int((json.loads(body).get("data") or {}).get("pageTotal") or 1)
            print(f"    {marker}/{page_total:<3} -> {len(parsed.observations):>3} parsed, "
                  f"{new:>3} new"
                  + (f", {len(revisions)} REVISED" if revisions else ""))
            page += 1

    return {"requests": requests_made, "stored": stored, "skipped": skipped, "dry_run": False}
