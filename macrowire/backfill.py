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
from .errors import ConfigError
from .parsers import get_parser


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

            response = wire._download(
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
                conn, source.name, status="backfill",
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
