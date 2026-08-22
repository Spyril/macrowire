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
import os
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

# CNINFO addresses a company by BOTH its code and an opaque orgId, and the
# announcement query needs the pair. The orgId is NOT derivable from the
# code: Shanghai and Shenzhen listings mostly take the form gssh0600519 /
# gssz0000001, but CATL (300750) is GD165627. Constructing it would work for
# most tickers and silently return nothing for the rest - the failure mode
# this project keeps refusing, because a ticker that returns nothing forever
# looks exactly like a quiet company.
#
# So it is looked up, once, at add time, and cached. One entry per ticker
# rather than a bulk map: CNINFO publishes no downloadable list.
CNINFO_SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
ORGID_CACHE = db.REPO_ROOT / "data" / "cninfo_orgids.json"


def _orgid_path() -> Path:
    """Same guard the database has, for the same reason.

    A test that reaches this file would silently rewrite real, hand-verified
    lookups with fixture values. MACROWIRE_REFUSE_DEFAULT_DB is already set
    before macrowire is imported in the suite, so one env var covers every
    default path this project writes to rather than a second mechanism that
    can be forgotten.
    """
    if os.environ.get("MACROWIRE_REFUSE_DEFAULT_DB"):
        raise MacroWireError(
            "refusing to touch the default CNINFO orgId cache while "
            "MACROWIRE_REFUSE_DEFAULT_DB is set; pass an explicit cache dict"
        )
    return ORGID_CACHE


def load_orgid_cache() -> dict[str, dict]:
    path = _orgid_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MacroWireError(
            f"{path} is not readable JSON: {exc}. Delete it and re-add "
            f"your CN tickers, or repair it by hand."
        ) from exc


def _save_orgid_cache(cache: dict) -> None:
    path = _orgid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def resolve_cn(code: str, fetch=None, cache: dict | None = None,
               persist: bool = True) -> dict | None:
    """Code -> {orgId, name, category, delisted}, cached.

    `fetch` is a callable returning raw bytes, injected for the same reason
    as the SEC one: the caller owns the User-Agent policy and the test does
    not touch the network.

    Returns None when CNINFO does not know the code. An unknown code is a
    typo and the caller should refuse it, not store it.
    """
    code = code.strip().upper()
    cache = load_orgid_cache() if cache is None else cache
    if code in cache:
        return cache[code]
    if fetch is None:
        return None

    raw = fetch(CNINFO_SEARCH_URL, {"keyWord": code, "maxNum": 10})
    # Their own 404 page is GB2312 while this API is UTF-8. Decode strictly
    # rather than assuming a host is consistent with itself: mojibake stored
    # as a company name is not recoverable once the bytes are gone.
    text = raw.decode("utf-8")
    hits = json.loads(text)
    if not isinstance(hits, list):
        raise MacroWireError(
            f"CNINFO search for {code} returned {type(hits).__name__}, "
            f"expected a list. The endpoint has changed shape.")
    # An unknown code is an empty list and HTTP 200. Nothing about the
    # status distinguishes it from a hit, so the structure has to.
    exact = [h for h in hits if str(h.get("code", "")).upper() == code]
    if not exact:
        return None
    hit = exact[0]
    if not hit.get("orgId"):
        raise MacroWireError(
            f"CNINFO returned an entry for {code} with no orgId: {hit!r}. "
            f"The announcement query cannot be built without it.")
    entry = {"orgId": hit["orgId"], "name": hit.get("zwjc") or "",
             "category": hit.get("category") or "",
             "delisted": str(hit.get("delisted", "")).lower() == "true"}
    cache[code] = entry
    if persist:
        _save_orgid_cache(cache)
    return entry


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
        cik_map: dict | None = None, cn_fetch=None,
        cn_cache: dict | None = None) -> dict:
    """Add a ticker. An unmatched US ticker is a typo and fails now.

    Failing at add time matters: a mistyped ticker that is accepted returns
    nothing forever and looks exactly like a quiet company.
    """
    ticker = ticker.strip().upper()
    market = market.strip().upper()
    if not ticker:
        raise ConfigError("ticker cannot be empty")

    resolved = None
    if market == "CN":
        # Numeric six-digit codes only. Rejecting the shape before spending a
        # request keeps a typo from looking like a network problem.
        if not (len(ticker) == 6 and ticker.isdigit()):
            raise ConfigError(
                f"{ticker!r} is not a mainland China listing code. These are six "
                f"digits - 600519, 000001, 300750 - not letters."
            )
        found = resolve_cn(ticker, fetch=cn_fetch, cache=cn_cache,
                           persist=cn_cache is None)
        if found is None:
            raise ConfigError(
                f"{ticker} is not in CNINFO's listing index. Check the code - an "
                f"unmatched one would return nothing forever and look like a "
                f"quiet company."
            )
        resolved = {"cik": None, "title": found["name"], "org_id": found["orgId"]}
        if found["delisted"]:
            raise ConfigError(
                f"{ticker} ({found['name']}) is marked delisted by CNINFO. It "
                f"will not file again; adding it would be a permanently silent "
                f"row on the watchlist."
            )
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
            "cik": resolved.get("cik") if resolved else None,
            "org_id": resolved.get("org_id") if resolved else None,
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
