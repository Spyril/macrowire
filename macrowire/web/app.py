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
from ..config import load_sources
from . import queries, ribbon

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


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/bootstrap")
def bootstrap():
    """Everything the page needs on load, in one round trip.

    Includes the first-run sweep: with 1,825 collected items and an empty
    item_state, every one of them would render unread. That is not a wire,
    it is a wall. First launch marks the lot read; only what arrives after
    is news.
    """
    conn = _conn()
    sources = _sources()
    swept = 0
    if queries.first_run(conn, USER_ID):
        swept = queries.mark_all_read(conn, USER_ID)
    payload = {
        "sources": queries.sources_meta(conn, sources),
        "now": ribbon.now_position(),
        "first_run_marked_read": swept,
        "unread": queries.unread_counts(conn, sources, USER_ID),
    }
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
         collapse: bool = True, limit: int = 400):
    conn = _conn()
    only = [s for s in sources.split(",") if s] if sources else None
    juris = [j for j in jurisdictions.split(",") if j] if jurisdictions else None
    rows = queries.tape(conn, _sources(), USER_ID, days=days, only=only,
                        jurisdictions=juris, collapse=collapse, limit=limit)
    conn.close()
    return {"items": rows, "collapsed": collapse}


@app.get("/api/rail")
def rail():
    conn = _conn()
    payload = queries.rail(conn, _sources())
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
