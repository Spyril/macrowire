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
    if jurisdictions:
        wanted = [s.name for s in sources if s.jurisdiction in jurisdictions]
        if not wanted:
            return []
        clause += f" AND s.name IN ({', '.join('?' * len(wanted))})"
        params.extend(wanted)

    rows = conn.execute(
        f"""SELECT i.id, i.title, i.url, i.summary, i.published_at,
                   i.announcement_type, s.name AS source,
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
        health.append({
            "name": st["name"],
            "jurisdiction": source.jurisdiction,
            "seconds_since_success": st["seconds_since_success"],
            "days_since_new_item": st["days_since_new_item"],
            "stale": st["stale"],
            "rows": st["rows"],
            "replaceable": st["replaceable"],
            "at_risk": st["at_risk"],
            "consecutive_failures": st["consecutive_failures"],
            "failure_kinds": st["failure_kinds"],
        })
    return {"fx": fx, "rba": rba, "health": health}
