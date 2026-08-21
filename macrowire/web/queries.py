"""Read-only queries for the interface.

The UI writes exactly one table - `item_state` - and reads everything
else. Nothing here fetches, parses or mutates collected data.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .. import db
from ..wire import source_status

SYDNEY = "Australia/Sydney"

# Official Form 8-K captions, for labelling item tags in the filter panel.
ITEM_LABELS = {
    "1.01": "material agreement", "1.02": "agreement terminated",
    "1.05": "cybersecurity incident", "2.01": "acquisition or disposal",
    "2.02": "results of operations", "2.03": "financial obligation",
    "2.05": "exit or disposal costs", "2.06": "material impairment",
    "3.01": "delisting notice", "3.02": "unregistered equity sales",
    "4.01": "auditor change", "4.02": "non-reliance on financials",
    "5.01": "change in control", "5.02": "officer or director change",
    "5.03": "articles or bylaws amended", "5.07": "shareholder vote",
    "7.01": "Regulation FD", "8.01": "other events",
    "9.01": "exhibits",
}


def sources_meta(conn: sqlite3.Connection, sources) -> list[dict]:
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM sources")}
    out = []
    for s in sources:
        out.append({
            "name": s.name,
            "id": ids.get(s.name),
            "institution": s.config.get("institution"),
            "jurisdiction": s.jurisdiction,
            "importance": s.importance,
            "archive": s.archive,
            "timing_class": (s.timing or {}).get("class", "scattered"),
        })
    return out


def first_run(conn: sqlite3.Connection, user_id: int) -> bool:
    """True when this user has never had any item state recorded.

    On a first launch there are 1,825 items and none of them are news to
    anybody - marking them all read is the only usable behaviour.
    """
    row = conn.execute(
        "SELECT 1 FROM item_state WHERE user_id = ? LIMIT 1", (user_id,)
    ).fetchone()
    return row is None


def mark_all_read(conn: sqlite3.Connection, user_id: int) -> int:
    now = db.utc_now()
    cursor = conn.execute(
        """INSERT OR IGNORE INTO item_state (user_id, item_id, read_at, flagged)
           SELECT ?, id, ?, 0 FROM items""",
        (user_id, now),
    )
    conn.commit()
    return cursor.rowcount


def mark_read(conn: sqlite3.Connection, user_id: int, item_ids: list[str]) -> int:
    if not item_ids:
        return 0
    now = db.utc_now()
    conn.executemany(
        """INSERT INTO item_state (user_id, item_id, read_at, flagged)
           VALUES (?, ?, ?, 0)
           ON CONFLICT(user_id, item_id) DO UPDATE
             SET read_at = COALESCE(item_state.read_at, excluded.read_at)""",
        [(user_id, i, now) for i in item_ids],
    )
    conn.commit()
    return len(item_ids)


def set_flag(conn: sqlite3.Connection, user_id: int, item_id: str, flagged: bool) -> None:
    conn.execute(
        """INSERT INTO item_state (user_id, item_id, read_at, flagged)
           VALUES (?, ?, NULL, ?)
           ON CONFLICT(user_id, item_id) DO UPDATE SET flagged = excluded.flagged""",
        (user_id, item_id, 1 if flagged else 0),
    )
    conn.commit()


def tape(conn: sqlite3.Connection, sources, user_id: int, *, days: int = 30,
         only: list[str] | None = None, jurisdictions: list[str] | None = None,
         tickers: list[str] | None = None, types: list[str] | None = None,
         collapse: bool = True, limit: int = 400) -> list[dict]:
    """Reverse-chronological items, optionally collapsing repeated titles.

    Collapsing groups identical (source, title) pairs into one row carrying
    a count and the ids of every member. HKMA publishes "Scam alert related
    to banks" 207 times - 30% of its entire feed is that one string - and a
    tape that lists it 207 times is unusable. The group is anchored at its
    most recent occurrence, so a recurring notice reads as recurring rather
    than as 207 separate events.
    """
    by_name = {s.name: s for s in sources}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    params: list = [cutoff]
    clause = ""
    if only:
        clause += f" AND s.name IN ({', '.join('?' * len(only))})"
        params.extend(only)
    if tickers:
        clause += f" AND i.ticker IN ({', '.join('?' * len(tickers))})"
        params.extend([t.upper() for t in tickers])
    if types:
        # A type token is "source:primary" or "source:primary:tag". Scoped to
        # its source, because 9 of 10 sources have exactly one type and a
        # global type axis would just duplicate the source axis.
        parts, extra = [], []
        for token in types:
            bits = token.split(":")
            if len(bits) == 2:
                parts.append("(s.name = ? AND i.type_primary = ?)")
                extra.extend(bits)
            elif len(bits) == 3:
                parts.append("(s.name = ? AND i.type_primary = ? "
                             "AND (',' || i.type_tags || ',') LIKE ?)")
                extra.extend([bits[0], bits[1], f"%,{bits[2]},%"])
        if parts:
            clause += f" AND ({' OR '.join(parts)})"
            params.extend(extra)
    if jurisdictions:
        wanted = [s.name for s in sources if s.jurisdiction in jurisdictions]
        if not wanted:
            return []
        clause += f" AND s.name IN ({', '.join('?' * len(wanted))})"
        params.extend(wanted)

    rows = conn.execute(
        f"""SELECT i.id, i.title, i.url, i.summary, i.published_at,
                   i.announcement_type, i.type_primary, i.type_tags,
                   i.ticker, i.is_price_sensitive, s.name AS source,
                   (st.read_at IS NOT NULL) AS is_read,
                   COALESCE(st.flagged, 0) AS flagged
            FROM items i
            JOIN sources s ON s.id = i.source_id
            LEFT JOIN item_state st ON st.item_id = i.id AND st.user_id = ?
            WHERE i.published_at >= ?{clause}
            ORDER BY i.published_at DESC""",
        [user_id, *params],
    ).fetchall()

    groups: dict = {}
    ordered: list[dict] = []
    for r in rows:
        source = by_name.get(r["source"])
        key = (r["source"], r["title"]) if collapse else (r["id"],)
        entry = groups.get(key)
        if entry is None:
            entry = {
                "key": "|".join(str(k) for k in key),
                "ids": [],
                "title": r["title"],
                "url": r["url"],
                "summary": r["summary"],
                "published_at": r["published_at"],
                "source": r["source"],
                "institution": source.config.get("institution") if source else None,
                "jurisdiction": source.jurisdiction if source else None,
                "importance": source.importance if source else 3,
                "announcement_type": _display_category(r["announcement_type"]),
                "type_primary": r["type_primary"],
                "type_tags": r["type_tags"],
                "ticker": r["ticker"],
                "price_sensitive": bool(r["is_price_sensitive"]) if r["is_price_sensitive"] is not None else None,
                "count": 0,
                "unread": False,
                "flagged": False,
                "occurrences": [],
            }
            groups[key] = entry
            ordered.append(entry)
        entry["ids"].append(r["id"])
        entry["count"] += 1
        entry["occurrences"].append(r["published_at"])
        # A group is unread if ANY member is: 207 identical notices are
        # one thing you have not seen, not 207 things.
        if not r["is_read"]:
            entry["unread"] = True
        if r["flagged"]:
            entry["flagged"] = True

    return ordered[:limit]


def _display_category(value: str | None) -> str | None:
    """RBA stores a full cbwiki URI; only its fragment carries meaning.

    The stored value is never altered - this is display only.
    """
    if not value:
        return None
    if value.startswith("http") and "#" in value:
        return value.rsplit("#", 1)[1].replace("-", " ")
    return value


def watchlist_axis(conn: sqlite3.Connection, user_id: int, days: int = 30) -> dict:
    """Which watchlisted tickers actually have items in the window.

    Only tickers with something to show become chips: a chip that filters to
    nothing is a chip you press once and never trust again.
    """
    from .. import watchlist as wl
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    held = wl.entries(conn, user_id)
    counts = {
        r["ticker"]: r["n"] for r in conn.execute(
            """SELECT ticker, COUNT(*) AS n FROM items
               WHERE ticker IS NOT NULL AND published_at >= ?
               GROUP BY ticker""", (cutoff,))
    }
    return {"entries": held,
            "with_items": [e for e in held if counts.get(e["ticker"])],
            "counts": counts}


def facets(conn: sqlite3.Connection, sources, user_id: int, days: int = 30) -> dict:
    """Every filterable value that would actually return rows, right now.

    Populated-only on every axis. A chip that filters to nothing is one you
    press once and never trust again - and its ABSENCE is information: no UK
    chip means the Bank of England has published nothing this window.
    """
    from datetime import datetime, timedelta, timezone

    from .. import watchlist as wl

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    by_name = {s.name: s for s in sources}

    per_source = {
        r["name"]: r["n"] for r in conn.execute(
            """SELECT s.name, COUNT(*) AS n FROM items i
               JOIN sources s ON s.id = i.source_id
               WHERE i.published_at >= ? GROUP BY s.name""", (cutoff,))
    }
    per_jurisdiction: dict[str, int] = {}
    for name, count in per_source.items():
        source = by_name.get(name)
        if source:
            per_jurisdiction[source.jurisdiction] = (
                per_jurisdiction.get(source.jurisdiction, 0) + count)

    per_ticker = {
        r["ticker"]: r["n"] for r in conn.execute(
            """SELECT ticker, COUNT(*) AS n FROM items
               WHERE ticker IS NOT NULL AND published_at >= ?
               GROUP BY ticker""", (cutoff,))
    }
    held = {e["ticker"] for e in wl.entries(conn, user_id)}

    # Type, grouped under the source that owns it.
    groups: dict[str, dict] = {}
    for row in conn.execute(
        """SELECT s.name AS source, i.type_primary AS primary_type,
                  i.type_tags AS tags, COUNT(*) AS n
           FROM items i JOIN sources s ON s.id = i.source_id
           WHERE i.published_at >= ? AND i.type_primary IS NOT NULL
           GROUP BY s.name, i.type_primary, i.type_tags""", (cutoff,)
    ):
        group = groups.setdefault(row["source"], {"primary": {}, "tags": {}})
        group["primary"][row["primary_type"]] = (
            group["primary"].get(row["primary_type"], 0) + row["n"])
        for tag in (row["tags"] or "").split(","):
            tag = tag.strip()
            if tag:
                key = f"{row['primary_type']}:{tag}"
                group["tags"][key] = group["tags"].get(key, 0) + row["n"]

    type_axis = []
    for name, group in sorted(groups.items()):
        # A source with a single type adds nothing the source axis does not
        # already say, so it is not offered as a type filter at all.
        if len(group["primary"]) < 2 and not group["tags"]:
            continue
        type_axis.append({
            "source": name,
            "primary": sorted(({"value": v, "count": c} for v, c in group["primary"].items()),
                              key=lambda x: (-x["count"], x["value"])),
            "tags": sorted(({"value": v, "count": c,
                             "label": ITEM_LABELS.get(v.split(":")[-1], v)}
                            for v, c in group["tags"].items()),
                           key=lambda x: x["value"]),
        })

    return {
        "window_days": days,
        "jurisdiction": sorted(({"value": v, "count": c}
                                for v, c in per_jurisdiction.items()),
                               key=lambda x: x["value"]),
        "source": sorted(({"value": v, "count": c} for v, c in per_source.items()),
                         key=lambda x: x["value"]),
        "ticker": sorted(({"value": v, "count": c} for v, c in per_ticker.items()
                          if v in held), key=lambda x: x["value"]),
        "type": type_axis,
    }


def unread_counts(conn: sqlite3.Connection, sources, user_id: int, days: int = 30) -> dict:
    rows = tape(conn, sources, user_id, days=days, collapse=True, limit=100000)
    per: dict[str, int] = {}
    per_j: dict[str, int] = {}
    for row in rows:
        if row["unread"]:
            per[row["source"]] = per.get(row["source"], 0) + 1
            j = row["jurisdiction"]
            if j:
                per_j[j] = per_j.get(j, 0) + 1
    return {"total": sum(per.values()), "per_source": per, "per_jurisdiction": per_j}


# Every health state a source can be in, with what it MEANS and what to do
# about it. "no contact logged" was truthful and read like an error; it was
# a new source nobody had polled yet. A state name nobody can decode is a
# state that gets ignored.
HEALTH_STATES = {
    "never_polled": {
        "label": "not polled yet",
        "meaning": "This source has never been fetched. Nothing is wrong.",
        "action": "Run `python -m macrowire fetch` to collect from it.",
        "severity": "info",
    },
    "healthy": {
        "label": "polled",
        "meaning": "Last poll succeeded and stored what it found.",
        "action": None,
        "severity": "ok",
    },
    "no_change": {
        "label": "polled, nothing new",
        "meaning": "The source was contacted and reported nothing new. "
                   "A successful poll - this is the normal outcome for a "
                   "source that publishes once a day.",
        "action": None,
        "severity": "ok",
    },
    "throttled": {
        "label": "waiting on interval",
        "meaning": "The rate limiter blocked this cycle before any request "
                   "went out, to stay inside the source's polite interval.",
        "action": None,
        "severity": "ok",
    },
    "log_incomplete": {
        "label": "data current, log incomplete",
        "meaning": "This source holds data newer than any fetch it has "
                   "logged - usually a restore from a backup taken before "
                   "that fetch. The data is fine; only the log is missing.",
        "action": "Harmless. The next successful fetch clears it.",
        "severity": "info",
    },
    "failing": {
        "label": "failing",
        "meaning": "Consecutive failures since the last good cycle.",
        "action": "Check the error kind: `network` is usually transient, "
                  "`http_404` suggests the feed has moved or been withdrawn.",
        "severity": "bad",
    },
    "stale": {
        "label": "stale",
        "meaning": "Polling works, but the source has published nothing new "
                   "for longer than its configured threshold.",
        "action": "Usually the source being quiet. Confirm on its website "
                  "before assuming a fault.",
        "severity": "warn",
    },
}


def health_state(st: dict) -> str:
    """One state per source, most serious first."""
    if st["consecutive_failures"]:
        return "failing"
    if st["stale"]:
        return "stale"
    if st["log_incomplete"]:
        return "log_incomplete"
    if st["last_contact"] is None:
        # Never reached. Distinguish "nobody has run fetch" from a fault:
        # a source with no contact AND no data is simply new.
        return "never_polled"
    if st["last_success"] is None:
        return "no_change"
    return "healthy"


def rail(conn: sqlite3.Connection, sources) -> dict:
    """Right-rail data: latest fixes with change, and source health."""
    fx = []
    for series in ("USD/CNY", "AUD/CNY", "HKD/CNY", "EUR/CNY", "100JPY/CNY"):
        rows = conn.execute(
            """SELECT o.period, o.value FROM observations o
               JOIN sources s ON s.id = o.source_id
               WHERE s.name = 'cfets_ccpr' AND o.series = ?
               ORDER BY o.period DESC LIMIT 2""",
            (series,),
        ).fetchall()
        if not rows:
            continue
        latest = rows[0]
        prior = rows[1] if len(rows) > 1 else None
        change = (latest["value"] - prior["value"]) if prior else None
        fx.append({
            "series": series, "period": latest["period"], "value": latest["value"],
            "change": change,
            "change_pct": (change / prior["value"] * 100) if prior and prior["value"] else None,
            "prior_period": prior["period"] if prior else None,
        })

    # Positioning. The CHANGE matters more than the level, so both are
    # shown and the change is not buried.
    cot = []
    latest_cot = conn.execute(
        """SELECT MAX(period) FROM observations o JOIN sources s ON s.id = o.source_id
           WHERE s.name = 'cftc_cot'""").fetchone()[0]
    if latest_cot:
        for row in conn.execute(
            """SELECT o.series, o.value FROM observations o
               JOIN sources s ON s.id = o.source_id
               WHERE s.name = 'cftc_cot' AND o.period = ?""", (latest_cot,)
        ):
            _, currency, metric = row["series"].split("/")
            entry = next((c for c in cot if c["currency"] == currency), None)
            if entry is None:
                entry = {"currency": currency, "period": latest_cot}
                cot.append(entry)
            entry[metric] = row["value"]
        cot.sort(key=lambda c: c["currency"])

    ecb = []
    for row in conn.execute(
        """SELECT o.series, o.period, o.value FROM observations o
           JOIN sources s ON s.id = o.source_id
           WHERE s.name = 'ecb_fx'
             AND o.period = (SELECT MAX(period) FROM observations o2
                             JOIN sources s2 ON s2.id = o2.source_id
                             WHERE s2.name = 'ecb_fx')
           ORDER BY o.series"""
    ):
        prior = conn.execute(
            """SELECT value FROM observations o JOIN sources s ON s.id = o.source_id
               WHERE s.name = 'ecb_fx' AND o.series = ? AND o.period < ?
               ORDER BY o.period DESC LIMIT 1""",
            (row["series"], row["period"]),
        ).fetchone()
        change = (row["value"] - prior["value"]) if prior else None
        ecb.append({
            "series": row["series"], "period": row["period"], "value": row["value"],
            "change": change,
            "change_pct": (change / prior["value"] * 100) if prior and prior["value"] else None,
        })

    rba = []
    for row in conn.execute(
        """SELECT o.series, o.period, o.value FROM observations o
           JOIN sources s ON s.id = o.source_id
           WHERE s.name = 'rba_exchange_rates'
             AND o.period = (SELECT MAX(period) FROM observations o2
                             JOIN sources s2 ON s2.id = o2.source_id
                             WHERE s2.name = 'rba_exchange_rates')
           ORDER BY o.series"""
    ):
        rba.append({"series": row["series"], "period": row["period"], "value": row["value"]})

    health = []
    for source in sources:
        st = source_status(conn, source)
        state_key = health_state(st)
        health.append({
            "name": st["name"],
            "jurisdiction": source.jurisdiction,
            "state": state_key,
            **{f"state_{k}": v for k, v in HEALTH_STATES[state_key].items()},
            # Contact, not store: a gated source that finds nothing new has
            # still reached its source and is demonstrably alive.
            "seconds_since_contact": st["seconds_since_contact"],
            "seconds_since_stored": st["seconds_since_stored"],
            "days_since_content": st["days_since_content"],
            "log_incomplete": st["log_incomplete"],
            "stale": st["stale"],
            "rows": st["rows"],
            "replaceable": st["replaceable"],
            "at_risk": st["at_risk"],
            "consecutive_failures": st["consecutive_failures"],
            "failure_kinds": st["failure_kinds"],
        })
    return {"fx": fx, "ecb": ecb, "cot": cot, "rba": rba, "health": health,
            "states": HEALTH_STATES}
