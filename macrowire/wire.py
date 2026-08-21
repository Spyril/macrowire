"""One poll cycle, and the status report.

Nothing in this module names a source. It reads sources.yaml, dispatches
to the parser each source declares, and writes to the tables that parser
produces.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import httpx

from . import db
from .config import Source, load_sources
from .encoding import decode
from .errors import EmptyFeedError, FetchError, MacroWireError
from .parsers import ParsedFeed, get_fetcher, get_parser


def _age_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def _download(source: Source, url: str | None = None, params: dict | None = None) -> httpx.Response:
    url = url or source.url
    headers = {
        "User-Agent": source.user_agent,
        "Accept": "application/rss+xml, application/rdf+xml, application/xml, "
                  "text/xml, application/json",
    }
    try:
        response = httpx.get(
            url,
            params=params,
            headers=headers,
            timeout=source.timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # A 404/410 is the shape of a feed that has been withdrawn; a 5xx
        # or 429 is the shape of a server having a bad day. Recorded
        # separately so `status` can tell them apart later.
        code = exc.response.status_code
        raise FetchError(
            f"{source.name}: HTTP {code} from {url}", kind=f"http_{code}"
        ) from exc
    except httpx.DecodingError as exc:
        raise FetchError(
            f"{source.name}: response body failed content-decoding: {exc}",
            kind="transport",
        ) from exc
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
            httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError,
            httpx.ProxyError) as exc:
        # DNS failure, refused connection, TLS handshake abort, timeout.
        # Transient by default: these say nothing about whether the source
        # still exists.
        raise FetchError(
            f"{source.name}: request to {url} failed: {exc}", kind="network"
        ) from exc
    except httpx.HTTPError as exc:
        raise FetchError(
            f"{source.name}: request to {url} failed: {exc}", kind="network"
        ) from exc

    # A truncated body is the dangerous case: it still parses, just with
    # the tail missing, and a cut mid-element derails encoding detection
    # downstream. Verify the server sent what it promised.
    #
    # Only meaningful on an identity-encoded response: with gzip in play,
    # Content-Length describes the COMPRESSED stream while response.content
    # is the decompressed body, so comparing them is nonsense. httpx already
    # enforces the compressed stream's own length and raises on a short read,
    # so the compressed case is covered - just not here.
    if not response.headers.get("content-encoding"):
        _ = url  # the message below names the source, not the sub-request
        declared_length = response.headers.get("content-length")
        if declared_length is not None:
            try:
                expected = int(declared_length)
            except ValueError:
                expected = None
            if expected is not None and expected != len(response.content):
                raise FetchError(
                    f"{source.name}: truncated response - Content-Length said "
                    f"{expected} bytes, received {len(response.content)}. "
                    f"Raise timeout_seconds for this source if it is a large feed."
                )
    return response


def _source_state(conn: sqlite3.Connection, source: Source, source_id: int) -> dict:
    """What a custom fetcher needs to know about what we already hold."""
    row = conn.execute(
        "SELECT MAX(period) FROM observations WHERE source_id = ?", (source_id,)
    ).fetchone()
    return {"latest_period": row[0] if row and row[0] else None}


def classify(source: Source, parsed: ParsedFeed) -> None:
    """Fill in announcement_type from configuration, in place.

    Precedence: whatever the feed declared for itself, then the first
    matching `categories:` URL rule, then the flat `announcement_type`
    default. Kept here rather than in a parser so that a feed which does
    not classify its own entries - the ECB press feed mixes releases,
    speeches and Governing Council decisions on one URL space - can be
    classified from sources.yaml without any parser learning its name.
    """
    fallback = source.config.get("announcement_type")
    for item in parsed.items:
        if item.get("announcement_type"):
            continue
        url = item.get("url") or ""
        for rule in source.categories:
            if rule["match"] in url:
                item["announcement_type"] = rule["name"]
                break
        else:
            item["announcement_type"] = fallback


def _store_items(conn, source_id: int, parsed: ParsedFeed) -> int:
    fetched_at = db.utc_now()
    stored = 0
    for item in parsed.items:
        item_id = db.content_hash(
            source_id,
            item["external_id"],
            item["url"],
            item["title"],
            item["published_at"],
        )
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO items (
                id, source_id, external_id, title, url, summary, content,
                published_at, fetched_at, ticker, is_price_sensitive,
                announcement_type, institution_abbrev, simple_title, occurrence_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                source_id,
                item["external_id"],
                item["title"],
                item["url"],
                item["summary"],
                item.get("content"),
                item["published_at"],
                fetched_at,
                item["ticker"],
                item["is_price_sensitive"],
                item["announcement_type"],
                item["institution_abbrev"],
                item["simple_title"],
                item["occurrence_date"],
            ),
        )
        stored += cursor.rowcount
    return stored


def _store_observations(conn, source: Source, source_id: int, parsed: ParsedFeed) -> tuple[int, list[str]]:
    """Insert new observations; surface corrections rather than swallowing them.

    (source_id, series, period) is unique, but the upstream value under a
    given key is mutable - the RBA does occasionally revise. INSERT OR
    IGNORE would drop the correction silently, which is the one failure
    mode worth being noisy about. So: compare, no-op if identical, update
    and record a revision row in fetch_log if not.
    """
    fetched_at = db.utc_now()
    stored = 0
    revisions: list[str] = []

    for observation in parsed.observations:
        key = (source_id, observation["series"], observation["period"])
        existing = conn.execute(
            """SELECT id, value FROM observations
               WHERE source_id = ? AND series = ? AND period = ?""",
            key,
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO observations (
                    source_id, series, period, value, unit, base_currency,
                    target_currency, rate_type, frequency, decimals,
                    external_id, observed_at, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    observation["series"],
                    observation["period"],
                    observation["value"],
                    observation["unit"],
                    observation["base_currency"],
                    observation["target_currency"],
                    observation["rate_type"],
                    observation["frequency"],
                    observation["decimals"],
                    observation["external_id"],
                    observation["observed_at"],
                    fetched_at,
                ),
            )
            stored += 1
            continue

        if existing["value"] == observation["value"]:
            continue

        note = (
            f"{observation['series']} {observation['period']}: "
            f"{existing['value']} -> {observation['value']}"
        )
        revisions.append(note)
        conn.execute(
            """UPDATE observations
               SET value = ?, unit = ?, decimals = ?, observed_at = ?,
                   fetched_at = ?, revised_at = ?
               WHERE id = ?""",
            (
                observation["value"],
                observation["unit"],
                observation["decimals"],
                observation["observed_at"],
                fetched_at,
                fetched_at,
                existing["id"],
            ),
        )

    for note in revisions:
        db.log_fetch(conn, source.name, status="revision", detail=note)

    return stored, revisions


def fetch_source(conn: sqlite3.Connection, source: Source, stagger: bool = False) -> dict:
    """Poll one source. Logs every failure to fetch_log, then re-raises.

    `stagger` is set by fetch_all once an earlier source in this cycle has
    actually gone out to the network, so the pause spaces real requests
    rather than padding a run where everything was rate-limit skipped.
    """
    source_id = db.upsert_source(conn, source.name, source.kind, source.config)
    conn.commit()

    age = _age_seconds(db.last_attempt_at(conn, source.name))
    if age is not None and age < source.min_interval_seconds:
        wait = int(source.min_interval_seconds - age)
        db.log_fetch(
            conn,
            source.name,
            status="skipped",
            detail=f"{wait}s below the {source.min_interval_seconds}s minimum interval",
        )
        return {"source": source.name, "skipped": True, "wait_seconds": wait}

    if stagger and source.stagger_seconds:
        time.sleep(source.stagger_seconds)

    try:
        fetcher = get_fetcher(source.parser)
        if fetcher is None:
            responses, note = [_download(source)], None
        else:
            # A source that needs several requests - a publication gate, a
            # paginated API - supplies its own fetcher and may legitimately
            # decide there is nothing to collect.
            responses, note = fetcher(
                source,
                lambda url, params=None: _download(source, url, params),
                _source_state(conn, source, source_id),
            )
            if not responses:
                db.log_fetch(conn, source.name, status="skipped", detail=note)
                return {"source": source.name, "skipped": True, "reason": note}

        parser = get_parser(source.parser)
        parsed = ParsedFeed()
        for response in responses:
            # Store the raw bytes before decoding, let alone parsing. A
            # decode or parser failure today must not cost us a body we
            # cannot re-fetch - and it must not cost us the ORIGINAL bytes,
            # which are the only thing that makes an encoding mistake
            # recoverable.
            raw_id = db.store_raw_response(
                conn, source.name, str(response.url), response.status_code, response.content
            )
            conn.commit()

            # Decode explicitly rather than trusting httpx, which
            # substitutes U+FFFD instead of raising on a wrong encoding.
            body, encoding_used = decode(
                source.name, response.content, response.headers.get("content-type")
            )
            db.record_raw_encoding(conn, raw_id, encoding_used)

            page = parser(source, body)
            classify(source, page)
            parsed.items.extend(page.items)
            parsed.observations.extend(page.observations)

        if parsed.entry_count == 0 and db.has_parsed_before(conn, source.name):
            raise EmptyFeedError(
                f"{source.name}: returned zero entries but has parsed "
                f"successfully before. Treating as a fault, not a quiet day."
            )

        new_items = _store_items(conn, source_id, parsed)
        new_observations, revisions = _store_observations(conn, source, source_id, parsed)
        conn.commit()

    except MacroWireError as exc:
        db.log_fetch(conn, source.name, status="error",
                     error=f"{type(exc).__name__}: {exc}", error_kind=exc.kind)
        raise
    except Exception as exc:  # unexpected, but still must reach fetch_log
        db.log_fetch(conn, source.name, status="error",
                     error=f"{type(exc).__name__}: {exc}", error_kind="internal")
        raise

    total_new = new_items + new_observations

    # Retention is applied only after a clean parse: if today's payload
    # could not be read, older ones are the fallback and must survive.
    pruned = 0
    if source.raw_retention_days:
        pruned = db.prune_raw_responses(conn, source.name, source.raw_retention_days)
        conn.commit()

    db.log_fetch(conn, source.name, status="ok", new_item_count=total_new)

    return {
        "source": source.name,
        "skipped": False,
        "entries": parsed.entry_count,
        "new_items": new_items,
        "new_observations": new_observations,
        "pruned_raw": pruned,
        "revisions": revisions,
    }


def fetch_all(conn: sqlite3.Connection, sources: list[Source]) -> tuple[list[dict], list[Exception]]:
    """Poll every source. A dead feed must not hide the others' results."""
    results, failures = [], []
    fetched_any = False
    for source in sources:
        # Space out the cycle. Fetching is already sequential, so servers
        # are never hit simultaneously; this stops nine requests going out
        # inside a second across five different governments.
        try:
            result = fetch_source(conn, source, stagger=fetched_any)
            fetched_any = fetched_any or not result["skipped"]
            results.append(result)
        except Exception as exc:
            fetched_any = True  # it reached the network before failing
            failures.append(exc)
    return results, failures


def source_status(conn: sqlite3.Connection, source: Source) -> dict:
    """Everything `status` prints. All of it is information, none of it raises."""
    row = conn.execute("SELECT id FROM sources WHERE name = ?", (source.name,)).fetchone()
    source_id = row["id"] if row else None

    def scalar(query, params=()):
        found = conn.execute(query, params).fetchone()
        return found[0] if found and found[0] is not None else None

    last_success = scalar(
        "SELECT MAX(timestamp) FROM fetch_log WHERE source = ? AND status = 'ok'",
        (source.name,),
    )
    last_error_row = conn.execute(
        """SELECT timestamp, error FROM fetch_log
           WHERE source = ? AND status = 'error'
           ORDER BY timestamp DESC LIMIT 1""",
        (source.name,),
    ).fetchone()
    last_new = scalar(
        """SELECT MAX(timestamp) FROM fetch_log
           WHERE source = ? AND status = 'ok' AND new_item_count > 0""",
        (source.name,),
    )
    revisions = scalar(
        "SELECT COUNT(*) FROM fetch_log WHERE source = ? AND status = 'revision'",
        (source.name,),
    ) or 0

    item_rows = observation_rows = 0
    latest_content = None
    if source_id is not None:
        item_rows = scalar("SELECT COUNT(*) FROM items WHERE source_id = ?", (source_id,)) or 0
        observation_rows = (
            scalar("SELECT COUNT(*) FROM observations WHERE source_id = ?", (source_id,)) or 0
        )
        latest_content = scalar(
            "SELECT MAX(published_at) FROM items WHERE source_id = ?", (source_id,)
        ) or scalar(
            "SELECT MAX(observed_at) FROM observations WHERE source_id = ?", (source_id,)
        )

    raw_rows = scalar(
        "SELECT COUNT(*) FROM raw_responses WHERE source = ?", (source.name,)
    ) or 0

    # A corrected headline upstream lands as a new row sharing the same
    # external_id rather than overwriting the one you may already have
    # read. Count those chains so the near-duplicates are visible and a
    # UI can collapse them.
    chains = superseded = 0
    if source_id is not None:
        row = conn.execute(
            """SELECT COUNT(*) AS chains, COALESCE(SUM(n - 1), 0) AS superseded
               FROM (SELECT COUNT(*) AS n FROM items
                     WHERE source_id = ? AND external_id IS NOT NULL
                     GROUP BY external_id HAVING n > 1)""",
            (source_id,),
        ).fetchone()
        chains, superseded = row["chains"], row["superseded"]

    since_success = _age_seconds(last_success)
    since_new = _age_seconds(last_new)
    days_since_new = since_new / 86400 if since_new is not None else None

    # How much of this source would be lost with the database.
    replaceable = {"none": "NO", "rolling": "PARTIAL",
                   "queryable": "YES", "unknown": "?"}[source.archive]
    at_risk = (item_rows + observation_rows) if source.archive == "none" else 0

    # Consecutive failures since the last success: one is a blip, twelve is
    # a source that has gone.
    last_ok = scalar(
        "SELECT MAX(timestamp) FROM fetch_log WHERE source = ? AND status = 'ok'",
        (source.name,),
    )
    consecutive = scalar(
        """SELECT COUNT(*) FROM fetch_log
           WHERE source = ? AND status = 'error' AND timestamp > COALESCE(?, '')""",
        (source.name, last_ok),
    ) or 0
    # error_kind is NULL for any failure logged before migration 002 added
    # the column. Formatting that as "Nonex1" is how a null reaches the
    # screen dressed as a value.
    kinds = []
    for kind, count in conn.execute(
        """SELECT error_kind, COUNT(*) FROM fetch_log
           WHERE source = ? AND status = 'error' AND timestamp > COALESCE(?, '')
           GROUP BY error_kind ORDER BY COUNT(*) DESC""",
        (source.name, last_ok),
    ):
        label = kind or "unclassified"
        kinds.append(f"{label}×{count}" if count > 1 else label)

    stale = (
        source.staleness_days is not None
        and days_since_new is not None
        and days_since_new > source.staleness_days
    )

    return {
        "name": source.name,
        "kind": source.kind,
        "rows": item_rows + observation_rows,
        "item_rows": item_rows,
        "observation_rows": observation_rows,
        "raw_rows": raw_rows,
        "revisions": revisions,
        "revision_chains": chains,
        "superseded_items": superseded,
        "last_success": last_success,
        "seconds_since_success": since_success,
        "last_new_item": last_new,
        "days_since_new_item": days_since_new,
        "latest_content_at": latest_content,
        "archive": source.archive,
        "replaceable": replaceable,
        "at_risk": at_risk,
        "consecutive_failures": consecutive,
        "failure_kinds": kinds,
        "staleness_days": source.staleness_days,
        "stale": stale,
        "last_error": dict(last_error_row) if last_error_row else None,
    }


def all_status(conn: sqlite3.Connection) -> list[dict]:
    return [source_status(conn, source) for source in load_sources()]
