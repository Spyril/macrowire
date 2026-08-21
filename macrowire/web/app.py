"""Local web interface. 127.0.0.1 only, no auth, single user.

Read-only against everything except `item_state`. The interface never
fetches a source, never parses a payload, and never writes collected data.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import db
from .. import export as export_mod
from .. import watchlist as wl
from ..config import load_export_settings, load_sources
from ..errors import ConfigError, MacroWireError
from . import queries, ribbon


def _cik_map_for_ui():
    """The ticker map, refreshed only if stale. Never blocks on the network
    when a usable cache exists."""
    import httpx

    sources = {s.name: s for s in load_sources()}
    contact = (sources["sec_edgar"].config.get("sec_contact")
               if "sec_edgar" in sources else None)

    def download(url: str) -> bytes:
        if not contact:
            raise MacroWireError(
                "SEC_CONTACT is not set; cannot fetch the ticker map.")
        response = httpx.get(url, headers={"User-Agent": contact,
                                           "Accept-Encoding": "gzip, deflate"},
                             timeout=60, follow_redirects=True)
        response.raise_for_status()
        return response.content

    age = wl.cik_cache_age_days()
    return wl.load_cik_map(
        fetch=download if (age is None or age > wl.CIK_MAX_AGE_DAYS) else None)

STATIC = Path(__file__).resolve().parent / "static"
USER_ID = db.LOCAL_USER_ID

app = FastAPI(title="MacroWire", docs_url=None, redoc_url=None)


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    return conn


def _sources():
    return load_sources()


class ReadRequest(BaseModel):
    ids: list[str]


class FlagRequest(BaseModel):
    id: str
    flagged: bool


class WatchlistRequest(BaseModel):
    ticker: str
    market: str = "US"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/bootstrap")
def bootstrap():
    """Everything the page needs on load, in one round trip. READ ONLY.

    It reports whether this is a first run but does not act on it. An
    earlier version performed the mark-everything-read sweep here, which
    made a GET mutate - a prefetch, a refresh or a crawler would have
    silently consumed the one chance to do it. The client POSTs to
    /api/first-run instead.
    """
    conn = _conn()
    sources = _sources()
    payload = {
        "sources": queries.sources_meta(conn, sources),
        "now": ribbon.now_position(),
        "first_run": queries.first_run(conn, USER_ID),
        "unread": queries.unread_counts(conn, sources, USER_ID),
        "facets": queries.facets(conn, sources, USER_ID),
        "watchlist": wl.entries(conn, USER_ID),
    }
    conn.close()
    return payload


@app.post("/api/first-run")
def first_run_sweep():
    """Mark everything read, once. With thousands of collected items and an
    empty item_state, every one would render unread - a wall, not a wire."""
    conn = _conn()
    swept = queries.mark_all_read(conn, USER_ID) if queries.first_run(conn, USER_ID) else 0
    conn.close()
    return {"marked": swept}


@app.get("/api/watchlist")
def watchlist_get():
    conn = _conn()
    payload = {"entries": wl.entries(conn, USER_ID)}
    conn.close()
    return payload


@app.post("/api/watchlist/add")
def watchlist_add(request: WatchlistRequest):
    """Same validation as the CLI, because it is the same code path.

    An unmatched US ticker is a typo and fails here exactly as it does in
    the terminal - a 400 carrying the identical message, never a silent
    accept that returns nothing forever.
    """
    conn = _conn()
    try:
        added = wl.add(conn, USER_ID, request.ticker, request.market,
                       cik_map=_cik_map_for_ui())
    except ConfigError as exc:
        conn.close()
        raise HTTPException(status_code=400, detail=str(exc))
    payload = {"added": added, "entries": wl.entries(conn, USER_ID)}
    conn.close()
    return payload


@app.post("/api/watchlist/remove")
def watchlist_remove(request: WatchlistRequest):
    conn = _conn()
    removed = wl.remove(conn, USER_ID, request.ticker, request.market or None)
    payload = {"removed": removed, "entries": wl.entries(conn, USER_ID)}
    conn.close()
    return payload


@app.get("/api/ribbon")
def ribbon_data(day: str | None = None):
    try:
        target = date.fromisoformat(day) if day else datetime.now(ribbon.VIEW).date()
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    sources = _sources()
    return {
        "day": target.isoformat(),
        "sessions": ribbon.sessions_for(target),
        "marks": ribbon.marks_for(target, sources),
        "now": ribbon.now_position(),
    }


@app.get("/api/tape")
def tape(days: int = 30, sources: str | None = None, jurisdictions: str | None = None,
         tickers: str | None = None, types: str | None = None,
         fx: str | None = None, collapse: bool = True, limit: int = 400):
    conn = _conn()
    only = [s for s in sources.split(",") if s] if sources else None
    juris = [j for j in jurisdictions.split(",") if j] if jurisdictions else None
    ticks = [t for t in tickers.split(",") if t] if tickers else None
    kinds = [t for t in types.split("|") if t] if types else None
    fx_states = [f for f in fx.split(",") if f] if fx else None
    rows = queries.tape(conn, _sources(), USER_ID, days=days, only=only,
                        jurisdictions=juris, tickers=ticks, types=kinds,
                        fx_states=fx_states, collapse=collapse, limit=limit)
    conn.close()
    return {"items": rows, "collapsed": collapse}


@app.get("/api/rail")
def rail():
    conn = _conn()
    sources = _sources()
    payload = queries.rail(conn, sources)
    # Measured, not assumed: whether the irreplaceable rows are actually
    # written somewhere, and whether that somewhere is off this disk.
    payload["export"] = _export_state(conn, sources)
    conn.close()
    return payload


def _export_state(conn, sources):
    try:
        settings = load_export_settings()
    except Exception as exc:
        return {"error": str(exc)}
    st = export_mod.state(conn, sources, settings)
    st["path"] = str(st["path"])
    st["directory"] = str(st["directory"])
    return st


@app.get("/api/facets")
def facets(days: int = 30):
    conn = _conn()
    payload = queries.facets(conn, _sources(), USER_ID, days=days)
    conn.close()
    return payload


@app.get("/api/unread")
def unread(days: int = 30):
    conn = _conn()
    payload = queries.unread_counts(conn, _sources(), USER_ID, days=days)
    conn.close()
    return payload


@app.post("/api/read")
def mark_read(request: ReadRequest):
    conn = _conn()
    n = queries.mark_read(conn, USER_ID, request.ids)
    conn.close()
    return {"marked": n}


@app.post("/api/flag")
def flag(request: FlagRequest):
    conn = _conn()
    queries.set_flag(conn, USER_ID, request.id, request.flagged)
    conn.close()
    return {"ok": True}


@app.exception_handler(Exception)
def unhandled(request, exc):
    # The wire should say when it is broken rather than render blank.
    return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
