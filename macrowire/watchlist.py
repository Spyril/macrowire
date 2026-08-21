"""Watchlists, and the ticker->CIK map they validate against.

The `watchlists` table has existed since step 1 and been empty since step 1.
This is what it was for: company announcements are watchlist-filtered by
default, because the alternative is pulling a whole exchange's daily output
to keep a handful of rows.

Ships empty. There is no default watchlist - a default would be a guess
about what someone holds.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import db
from .errors import ConfigError, MacroWireError

CIK_URL = "https://www.sec.gov/files/company_tickers.json"
CIK_CACHE = db.REPO_ROOT / "data" / "sec_company_tickers.json"
# The map changes when companies list, delist or rename. Weekly is ample and
# keeps us off their servers.
CIK_MAX_AGE_DAYS = 7


def cik_cache_age_days() -> float | None:
    if not CIK_CACHE.exists():
        return None
    return (time.time() - CIK_CACHE.stat().st_mtime) / 86400


def load_cik_map(fetch=None, force: bool = False) -> dict[str, dict]:
    """Ticker -> {cik, title}, from cache, refreshed when stale.

    `fetch` is a callable returning raw bytes; injected so this is testable
    and so the caller owns the User-Agent policy.
    """
    age = cik_cache_age_days()
    stale = age is None or age > CIK_MAX_AGE_DAYS
    if (stale or force) and fetch is not None:
        raw = fetch(CIK_URL)
        # Validate before overwriting a good cache with a bad download.
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict) or len(parsed) < 1000:
            raise MacroWireError(
                f"SEC ticker map looks wrong ({type(parsed).__name__}, "
                f"{len(parsed) if hasattr(parsed, '__len__') else '?'} entries); "
                f"refusing to replace the cache"
            )
        CIK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CIK_CACHE.write_bytes(raw)

    if not CIK_CACHE.exists():
        raise MacroWireError(
            f"no SEC ticker map at {CIK_CACHE} and no way to fetch one. "
            f"Run `python -m macrowire watchlist refresh`."
        )

    entries = json.loads(CIK_CACHE.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for entry in entries.values():
        ticker = str(entry.get("ticker", "")).upper()
        if ticker:
            out[ticker] = {"cik": int(entry["cik_str"]), "title": entry.get("title", "")}
    return out


def add(conn: sqlite3.Connection, user_id: int, ticker: str, market: str,
        cik_map: dict | None = None) -> dict:
    """Add a ticker. An unmatched US ticker is a typo and fails now.

    Failing at add time matters: a mistyped ticker that is accepted returns
    nothing forever and looks exactly like a quiet company.
    """
    ticker = ticker.strip().upper()
    market = market.strip().upper()
    if not ticker:
        raise ConfigError("ticker cannot be empty")

    resolved = None
    if market == "US":
        cik_map = cik_map if cik_map is not None else load_cik_map()
        resolved = cik_map.get(ticker)
        if resolved is None:
            raise ConfigError(
                f"{ticker} is not in the SEC ticker map ({len(cik_map):,} entries). "
                f"Check the spelling - an unmatched ticker would return nothing "
                f"forever and look like a quiet company."
            )

    conn.execute(
        """INSERT OR IGNORE INTO watchlists (user_id, ticker, market)
           VALUES (?, ?, ?)""",
        (user_id, ticker, market),
    )
    conn.commit()
    return {"ticker": ticker, "market": market,
            "cik": resolved["cik"] if resolved else None,
            "name": resolved["title"] if resolved else None}


def remove(conn: sqlite3.Connection, user_id: int, ticker: str,
           market: str | None = None) -> int:
    ticker = ticker.strip().upper()
    if market:
        cursor = conn.execute(
            "DELETE FROM watchlists WHERE user_id = ? AND ticker = ? AND market = ?",
            (user_id, ticker, market.strip().upper()))
    else:
        cursor = conn.execute(
            "DELETE FROM watchlists WHERE user_id = ? AND ticker = ?", (user_id, ticker))
    conn.commit()
    return cursor.rowcount


def entries(conn: sqlite3.Connection, user_id: int, market: str | None = None) -> list[dict]:
    sql = "SELECT ticker, market FROM watchlists WHERE user_id = ?"
    params: list = [user_id]
    if market:
        sql += " AND market = ?"
        params.append(market.upper())
    sql += " ORDER BY market, ticker"
    return [{"ticker": r["ticker"], "market": r["market"]}
            for r in conn.execute(sql, params)]


def tickers(conn: sqlite3.Connection, user_id: int, market: str) -> list[str]:
    return [e["ticker"] for e in entries(conn, user_id, market)]
