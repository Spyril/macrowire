"""One poll cycle, and the status report.

Nothing in this module names a source. It reads sources.yaml, dispatches
to the parser each source declares, and writes to the tables that parser
produces.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .config import Source, load_sources
from .encoding import decode
from .errors import EmptyFeedError, FetchError, MacroWireError
from . import fx as fxmod
from .parsers import ParsedFeed, get_fetcher, get_parser


def _age_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def _download(source: Source, url: str | None = None, params: dict | None = None,
              headers: dict | None = None, data: dict | None = None) -> httpx.Response:
    """One request. `data` switches it to a form POST.

    CNINFO's announcement query is POST-only; everything else here is GET.
    The method is chosen by whether a body was supplied rather than by a
    separate flag, so there is no way to ask for a POST and forget the body.
    """
    url = url or source.url
    # A source may override the User-Agent entirely: the SEC requires its own
    # documented 'Name email' form and answers anything else with a 403.
    headers = dict(headers) if headers else {
        "User-Agent": source.user_agent,
        "Accept": "application/rss+xml, application/rdf+xml, application/xml, "
                  "text/xml, application/json",
    }
    try:
        if data is None:
            response = httpx.get(
                url, params=params, headers=headers,
                timeout=source.timeout_seconds, follow_redirects=True,
            )
        else:
            response = httpx.post(
                url, params=params, data=data, headers=headers,
                timeout=source.timeout_seconds, follow_redirects=True,
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
    except httpx.TimeoutException as exc:
        # Recorded apart from the rest. A timeout is the signature of a slow
        # or congested path, not of a source that has gone: on a slow
        # international link it is the expected failure, and reporting it as
        # a fault would mean the panel calls a working feed broken.
        raise FetchError(
            f"{source.name}: request to {url} timed out after "
            f"{source.timeout_seconds}s: {exc}", kind="timeout"
        ) from exc
    except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ProxyError) as exc:
        # DNS failure, refused connection, TLS handshake abort. Transient by
        # default: these say nothing about whether the source still exists.
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
    # Keyed by market rather than one list per market. A second hardcoded
    # `market = 'US'` query was the shape this project keeps having to undo:
    # the same fact written down twice, drifting the moment a third market
    # arrives.
    watch: dict[str, list[str]] = {}
    for ticker, market in conn.execute(
        """SELECT ticker, market FROM watchlists WHERE user_id = ?
           ORDER BY market, ticker""", (db.LOCAL_USER_ID,)
    ):
        watch.setdefault(market, []).append(ticker)
    return {"latest_period": row[0] if row and row[0] else None,
            "watchlist": watch}


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
            item.setdefault("type_primary", None)
            if not item.get("type_primary"):
                item["type_primary"] = item["announcement_type"]
            continue
        url = item.get("url") or ""
        for rule in source.categories:
            if rule["match"] in url:
                item["announcement_type"] = rule["name"]
                break
        else:
            item["announcement_type"] = fallback
        if not item.get("type_primary"):
            item["type_primary"] = item["announcement_type"]


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
                announcement_type, type_primary, type_tags, fx_state,
                institution_abbrev, simple_title, occurrence_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                item.get("type_primary"),
                item.get("type_tags"),
                item.get("fx_state") or fxmod.UNCLASSIFIED,
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
        db.log_fetch(conn, source.name, status=db.STATUS_REVISION, detail=note)

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
        # Never contacted. Says nothing about whether the source is alive.
        db.log_fetch(
            conn,
            source.name,
            status=db.STATUS_THROTTLED,
            detail=f"{wait}s below the {source.min_interval_seconds}s minimum interval",
        )
        return {"source": source.name, "skipped": True, "kind": "throttled",
                "wait_seconds": wait}

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
                lambda url, params=None, headers=None, data=None: _download(
                    source, url, params, headers, data),
                _source_state(conn, source, source_id),
            )
            if not responses:
                # The fetcher reached the source and it had nothing new. That
                # is a successful poll and it proves reachability.
                db.log_fetch(conn, source.name, status=db.STATUS_NO_CHANGE, detail=note)
                return {"source": source.name, "skipped": True,
                        "kind": "no_change", "reason": note}

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
            fxmod.classify_items(source, page.items)
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
        db.log_fetch(conn, source.name, status=db.STATUS_ERROR,
                     error=f"{type(exc).__name__}: {exc}", error_kind=exc.kind)
        raise
    except Exception as exc:  # unexpected, but still must reach fetch_log
        db.log_fetch(conn, source.name, status=db.STATUS_ERROR,
                     error=f"{type(exc).__name__}: {exc}", error_kind="internal")
        raise

    total_new = new_items + new_observations

    # Retention is applied only after a clean parse: if today's payload
    # could not be read, older ones are the fallback and must survive.
    pruned = 0
    if source.raw_retention_days:
        pruned = db.prune_raw_responses(conn, source.name, source.raw_retention_days)
        conn.commit()

    db.log_fetch(conn, source.name, status=db.STATUS_OK, new_item_count=total_new)

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
        if not source.enabled:
            # Not contacted, and NOT an error. Reported so a cycle that does
            # nothing says why it did nothing, rather than printing a blank.
            results.append({"source": source.name, "skipped": True,
                            "kind": "disabled", "reason": None})
            continue
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
    """Everything `status` prints. All of it is information, none of it raises.

    Health is derived from the DATA wherever the data can answer, and from
    fetch_log only where it cannot. The log is not a reliable witness: a
    restore drops rows that predate the backup, a gated source logs
    no_change rather than ok, and a source whose every poll dedupes never
    logs new_item_count > 0 again. Three separate false alarms came from
    trusting it, and a false alarm in a fail-loudly system is worse than no
    alarm because it teaches you to ignore the real ones.
    """
    row = conn.execute("SELECT id FROM sources WHERE name = ?", (source.name,)).fetchone()
    source_id = row["id"] if row else None

    def scalar(query, params=()):
        found = conn.execute(query, params).fetchone()
        return found[0] if found and found[0] is not None else None

    # ---- what the data itself says ---------------------------------------
    item_rows = observation_rows = 0
    latest_content = last_stored = None
    if source_id is not None:
        item_rows = scalar("SELECT COUNT(*) FROM items WHERE source_id = ?", (source_id,)) or 0
        observation_rows = scalar(
            "SELECT COUNT(*) FROM observations WHERE source_id = ?", (source_id,)) or 0

        # Newest thing the SOURCE published, whichever table it writes to.
        # Checking only `items` reported "never" for every observation
        # source, which is a query looking in the wrong place, not a fault.
        content_stamps = [
            scalar("SELECT MAX(published_at) FROM items WHERE source_id = ?", (source_id,)),
            scalar("SELECT MAX(observed_at) FROM observations WHERE source_id = ?", (source_id,)),
        ]
        latest_content = max([c for c in content_stamps if c], default=None)

        # When we last WROTE something new. fetched_at is stamped at insert,
        # and a deduped row is never re-inserted, so this is exactly it.
        stored_stamps = [
            scalar("SELECT MAX(fetched_at) FROM items WHERE source_id = ?", (source_id,)),
            scalar("SELECT MAX(fetched_at) FROM observations WHERE source_id = ?", (source_id,)),
        ]
        last_stored = max([c for c in stored_stamps if c], default=None)

    # ---- what the log says -----------------------------------------------
    # A successful CONTACT, not merely a successful store. Legacy 'skipped'
    # rows predate the distinction and are not counted as contact: they were
    # mostly throttles, and guessing would re-create the false alarm.
    last_contact = scalar(
        f"""SELECT MAX(timestamp) FROM fetch_log
            WHERE source = ? AND {db.contact_sql()}""",
        (source.name,),
    )
    last_ok = scalar(
        "SELECT MAX(timestamp) FROM fetch_log WHERE source = ? AND status = 'ok'",
        (source.name,),
    )
    last_throttled = scalar(
        "SELECT MAX(timestamp) FROM fetch_log WHERE source = ? AND status IN ('throttled','skipped')",
        (source.name,),
    )
    revisions = scalar(
        "SELECT COUNT(*) FROM fetch_log WHERE source = ? AND status = 'revision'",
        (source.name,),
    ) or 0
    raw_rows = scalar(
        "SELECT COUNT(*) FROM raw_responses WHERE source = ?", (source.name,)) or 0

    # ---- reconciling the two ---------------------------------------------
    # Data newer than the newest logged contact means the log is missing
    # rows - a restore from a backup taken before that fetch is the usual
    # cause. The data is fine and saying "never fetched" about it is a lie.
    log_incomplete = bool(
        last_stored and (last_contact is None or last_stored > last_contact))

    # Consecutive failures = errors since the most recent NON-error row, not
    # since the last 'ok'. A source that only ever legitimately skips has no
    # 'ok' row, so the old cutoff never advanced and one ancient blip read as
    # a permanent failure streak.
    #
    # Ordered by id, not timestamp. utc_now() has one-second resolution and a
    # recovery logged in the same second as the failure it recovered from
    # would otherwise be invisible - the same tiebreak the revision chains
    # needed for the same reason.
    last_non_error_id = scalar(
        """SELECT MAX(id) FROM fetch_log WHERE source = ? AND status <> 'error'""",
        (source.name,),
    ) or 0
    consecutive = scalar(
        """SELECT COUNT(*) FROM fetch_log
           WHERE source = ? AND status = 'error' AND id > ?""",
        (source.name, last_non_error_id),
    ) or 0
    kinds = []
    kind_counts: dict[str, int] = {}
    for kind, count in conn.execute(
        """SELECT error_kind, COUNT(*) FROM fetch_log
           WHERE source = ? AND status = 'error' AND id > ?
           GROUP BY error_kind ORDER BY COUNT(*) DESC""",
        (source.name, last_non_error_id),
    ):
        label = kind or "unclassified"
        kind_counts[label] = count
        kinds.append(f"{label}×{count}" if count > 1 else label)

    # Raw counts as well as labels: deciding whether a streak is the network
    # or the source must not mean parsing "network×4" back out of a display
    # string. Measured from the ACTUAL rows, never assumed.
    path_failures = sum(kind_counts.get(k, 0) for k in db.PATH_KINDS)

    earliest_held = scalar(
        """SELECT MIN(date(published_at)) FROM items WHERE source_id = ?""",
        (source_id,)) if source_id else None
    if earliest_held is None and source_id:
        earliest_held = scalar(
            "SELECT MIN(period) FROM observations WHERE source_id = ?", (source_id,))
    _cfg = source.config or {}
    _backfillable = bool(_cfg.get("backfill_start") or _cfg.get("backfill_url")
                         or _cfg.get("backfill_page_size")
                         or _cfg.get("backfill_per_date"))
    if source.archive == "none":
        coverage_state = "never"
    elif source.archive == "queryable":
        coverage_state = "recoverable" if _backfillable else "unwired"
    else:
        coverage_state = "lost"

    last_error_row = conn.execute(
        """SELECT id, timestamp, error FROM fetch_log
           WHERE source = ? AND status = 'error'
           ORDER BY id DESC LIMIT 1""",
        (source.name,),
    ).fetchone()
    last_error = dict(last_error_row) if last_error_row else None
    # An error a later contact superseded is history, not a live fault. By id
    # again, for the same one-second-resolution reason.
    if last_error:
        contact_id = scalar(
            f"""SELECT MAX(id) FROM fetch_log
                WHERE source = ? AND {db.contact_sql()}""",
            (source.name,),
        ) or 0
        last_error["resolved"] = contact_id > last_error["id"]

    # ---- staleness, from the CONTENT ------------------------------------
    # "How long since this source published anything new" is a question about
    # the source, so it is answered from the newest published/observed stamp.
    # Reading it off fetch_log made a source storing rows every single day
    # report STALE, because every poll after the first deduped.
    since_content = _age_seconds(latest_content)
    days_since_content = since_content / 86400 if since_content is not None else None
    stale = (
        source.staleness_days is not None
        and days_since_content is not None
        and days_since_content > source.staleness_days
    )

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

    # FX classification coverage, and whether it is DRIFTING.
    #
    # A vocabulary rots silently: when a source renames a committee, its
    # items stop matching and quietly drop out of the FX filter. Nothing
    # errors. So the unclassified rate is reported, and compared against
    # the rate before this window - a rising number is the rename.
    fx_counts = {"fx": 0, "not_fx": 0, "unclassified": 0}
    fx_recent = fx_older = None
    if source_id is not None:
        for row in conn.execute(
            """SELECT COALESCE(fx_state, 'unclassified') AS state, COUNT(*) AS n
               FROM items WHERE source_id = ? GROUP BY state""", (source_id,)
        ):
            if row["state"] in fx_counts:
                fx_counts[row["state"]] = row["n"]

        window = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        def rate(clause, params):
            row = conn.execute(
                f"""SELECT COUNT(*) AS n,
                           SUM(CASE WHEN COALESCE(fx_state,'unclassified')='unclassified'
                                    THEN 1 ELSE 0 END) AS u
                    FROM items WHERE source_id = ? AND {clause}""",
                (source_id, *params)).fetchone()
            return (row["u"] / row["n"]) if row and row["n"] else None
        fx_recent = rate("published_at >= ?", (window,))
        fx_older = rate("published_at < ?", (window,))

    fx_total = sum(fx_counts.values())
    fx_drift = (
        fx_recent is not None and fx_older is not None
        and fx_recent > fx_older + 0.15      # a real jump, not sampling noise
    )

    replaceable = {"none": "NO", "rolling": "PARTIAL",
                   "queryable": "YES", "unknown": "?"}[source.archive]
    at_risk = (item_rows + observation_rows) if source.archive == "none" else 0

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

        "last_contact": last_contact,
        "seconds_since_contact": _age_seconds(last_contact),
        "last_success": last_ok,
        "seconds_since_success": _age_seconds(last_ok),
        "last_throttled": last_throttled,

        "last_stored_at": last_stored,
        "seconds_since_stored": _age_seconds(last_stored),
        "latest_content_at": latest_content,
        "days_since_content": days_since_content,
        "log_incomplete": log_incomplete,

        "consecutive_failures": consecutive,
        "failure_kinds": kinds,
        "failure_kind_counts": kind_counts,
        # Every consecutive failure was the path, not an answer from the
        # source. The caller decides what to call that; this only measures it.
        "all_failures_are_path": bool(consecutive) and path_failures == consecutive,
        "path_failures": path_failures,
        "last_error": last_error,

        "fx_counts": fx_counts,
        "fx_unclassified_pct": (fx_counts["unclassified"] / fx_total * 100) if fx_total else None,
        "fx_recent_unclassified_pct": (fx_recent * 100) if fx_recent is not None else None,
        "fx_older_unclassified_pct": (fx_older * 100) if fx_older is not None else None,
        "fx_drift": fx_drift,
        "fx_has_vocabulary": bool(source.fx),
        "fx_unmeasured": (source.fx or {}).get("unmeasured"),

        "archive": source.archive,
        "replaceable": replaceable,
        "at_risk": at_risk,
        # WHERE THE RECORD BEGINS, and whether that is fixable. `re-fetchable`
        # said YES/NO without ever saying FROM WHEN, so a source with two
        # days of history and one with forty years read identically.
        "earliest": earliest_held,
        "coverage_state": coverage_state,
        "staleness_days": source.staleness_days,
        # A disabled source is never stale. Nothing is polling it, so "has
        # published nothing new" would be a fact about this tool, not about
        # the publisher - the same false alarm in a new costume.
        "stale": stale and source.enabled,
        "enabled": source.enabled,
    }


def all_status(conn: sqlite3.Connection) -> list[dict]:
    return [source_status(conn, source) for source in load_sources()]
