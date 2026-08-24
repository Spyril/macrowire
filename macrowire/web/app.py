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
from .. import i18n
from .. import preferences
from ..config import (DEFAULT_CONFIG_PATH, JURISDICTIONS, load_export_settings,
                      load_locale, load_sources, load_web_settings)
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

def _cninfo_fetch_for_ui(url: str, form: dict) -> bytes:
    """CN code validation from the UI, on the same terms as the CLI.

    A bad code must fail at add time in the browser exactly as it does in the
    terminal: an unmatched one returns nothing forever and reads as a quiet
    company.
    """
    import httpx

    sources = {s.name: s for s in load_sources()}
    source = sources.get("cninfo_announcements")
    if source is None:
        raise MacroWireError(
            "cninfo_announcements is not in sources.yaml, so CN codes cannot "
            "be validated.")
    response = httpx.post(url, data=form,
                          headers={"User-Agent": source.user_agent,
                                   "Accept": "application/json"},
                          timeout=60, follow_redirects=True)
    response.raise_for_status()
    return response.content


STATIC = Path(__file__).resolve().parent / "static"
USER_ID = db.LOCAL_USER_ID

# One translator for the process. The locale is a config decision, not a
# per-request one: there is a single local user and no Accept-Language to
# negotiate with.
def _window(days: int | None = None) -> int:
    """How far back to look. ONE definition.

    It was `days=30` in five places - three in app.js and two as endpoint
    defaults - so changing it meant finding all five and the client and the
    server could disagree. An explicit ?days= still wins, for a one-off.
    """
    if days is not None:
        return days
    conn = _conn()
    try:
        return int(preferences.resolve(conn)["window_days"])
    finally:
        conn.close()


def _translator() -> i18n.Translator:
    """Per request, not per process.

    `sources.yaml` is read fresh by _sources() on every request, so binding
    `defaults.locale` out of the SAME FILE at import gave one file two
    freshnesses in one process: disabling a source took effect immediately
    and changing the locale did not. The Translator itself caches on mtime,
    so this is a stat and a dict lookup unless something actually changed.
    """
    return i18n.Translator(ribbon._pref("locale", load_locale()))

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
    translator = _translator()
    payload = {
        "sources": queries.sources_meta(conn, sources),
        "now": ribbon.now_position(),
        "first_run": queries.first_run(conn, USER_ID),
        "unread": queries.unread_counts(conn, sources, USER_ID),
        "facets": queries.facets(conn, sources, USER_ID),
        "watchlist": wl.entries(conn, USER_ID),
        # The whole catalogue, once, on load. It is a few kilobytes and it
        # means the page never renders a label before its text arrives.
        "locale": translator.locale,
        # Everything the panel needs, resolved, with which level answered.
        "preferences": preferences.effective(conn),
        "window_days": _window(),
        # Without `cli`: ninety terminal strings the page can never render,
        # sent on every load.
        "strings": translator.merged(exclude=("cli",)),
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


def _install_config() -> list[dict]:
    """What sources.yaml says, resolved, for READ-ONLY display.

    Values, not just key names, and the fallback stated where a key is
    unset - "backup.path unset -> data/backups, same disk as the database"
    is the thing worth knowing, and it is not in the file to be read.
    """
    from ..config import (ABSOLUTE_MIN_INTERVAL, DEFAULT_CONFIG_PATH,
                          DEFAULT_TIMEOUT, load_backup_settings)
    import yaml as pyyaml

    document = pyyaml.safe_load(DEFAULT_CONFIG_PATH.read_text()) or {}
    declared = document.get("defaults") or {}
    rows = []

    def row(key, value, note=None, unset=False):
        rows.append({"key": key, "value": str(value), "note": note,
                     "unset": unset})

    sources = _sources()
    web = load_web_settings()
    row("web.host / web.port", f"{web['host']}:{web['port']}",
        "127.0.0.1 only; never bound to another interface")
    row("defaults.user_agent", declared.get("user_agent", ""),
        "sent to every source; they expect to know who is calling")
    row("defaults.min_interval_seconds",
        declared.get("min_interval_seconds", ABSOLUTE_MIN_INTERVAL),
        f"floor is {ABSOLUTE_MIN_INTERVAL}s and is not overridable downward")
    row("defaults.timeout_seconds",
        declared.get("timeout_seconds", DEFAULT_TIMEOUT),
        "raise it on a slow link; a timeout is reported as unreachable, "
        "not as a failing source")
    row("defaults.stagger_seconds", declared.get("stagger_seconds", 0))
    row("defaults.collapse_repeats", declared.get("collapse_repeats", True),
        "install-only for now: per source it states a fact about that "
        "source, not a preference")

    try:
        export = load_export_settings()
        row("export.path", export["path"],
            ("off this disk - a drive failure costs nothing"
             if export["external"] else
             "same disk as the database - protects a mistake, not a drive "
             "failure"),
            unset="path" not in (declared.get("export") or {}))
        row("export.auto", export["auto"])
    except Exception as exc:
        row("export.path", f"misconfigured: {exc}")

    backup = load_backup_settings()
    row("backup.enabled", backup["enabled"])
    row("backup.path", backup["path"],
        ("off this disk" if backup["external"] else
         "same disk as the database - protects a mistake, not a drive failure"),
        unset="path" not in (declared.get("backup") or {}))
    row("backup.interval_seconds", backup["interval_seconds"])
    row("backup.keep", backup["keep"])
    row("sources", f"{sum(1 for s in sources if s.enabled)} enabled "
                   f"of {len(sources)}",
        "enabled, intervals, vocabularies and thresholds are per source")
    return rows


@app.get("/api/settings")
def settings_get():
    conn = _conn()
    from .. import preferences as prefs
    payload = {
        "preferences": prefs.effective(conn),
        "settable": list(prefs.SETTABLE),
        "window_choices": list(prefs.WINDOW_CHOICES),
        "locales": [{"code": code,
                     "name": (i18n.load(code).get("_meta") or {}).get("name", code)}
                    for code in i18n.available()],
        "timezones": _timezone_choices(),
        "jurisdictions": sorted(JURISDICTIONS),
        "install": _install_config(),
        "config_path": str(DEFAULT_CONFIG_PATH),
    }
    conn.close()
    return payload


def _timezone_choices() -> dict:
    """Not a 400-entry dropdown.

    The detected zone, the five the band already draws, and UTC as quick
    picks; everything else through a text input backed by the full list as
    a <datalist>, which the browser filters as you type and never renders
    whole.
    """
    from ..config import system_timezone
    from . import ribbon as ribbon_mod

    quick = ["UTC"] + [s["tz"] for s in ribbon_mod.SESSIONS]
    detected = system_timezone()
    if detected not in quick:
        quick.insert(0, detected)
    try:
        from zoneinfo import available_timezones
        every = sorted(available_timezones())
    except Exception:
        every = quick
    return {"detected": detected, "quick": quick, "all": every}


class PreferenceRequest(BaseModel):
    key: str
    value: str | None = None


@app.post("/api/settings")
def settings_set(request: PreferenceRequest):
    """Set or clear ONE preference.

    Writes to the database and never to sources.yaml: the YAML is the
    floor and a viewer preference must not be able to edit the
    installation. A null value clears the override.
    """
    from .. import preferences as prefs
    conn = _conn()
    try:
        if request.value is None or request.value == "":
            prefs.clear(conn, request.key)
        else:
            prefs.set_one(conn, request.key, request.value)
    except ConfigError as exc:
        conn.close()
        raise HTTPException(status_code=400, detail=str(exc))
    payload = {"preferences": prefs.effective(conn)}
    conn.close()
    return payload


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
                       cik_map=_cik_map_for_ui(), cn_fetch=_cninfo_fetch_for_ui)
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
        target = (date.fromisoformat(day) if day
              else datetime.now(ribbon.view_zone()).date())
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
def tape(days: int | None = None, sources: str | None = None, jurisdictions: str | None = None,
         tickers: str | None = None, types: str | None = None,
         fx: str | None = None, collapse: bool = True, limit: int = 400):
    conn = _conn()
    only = [s for s in sources.split(",") if s] if sources else None
    juris = [j for j in jurisdictions.split(",") if j] if jurisdictions else None
    ticks = [t for t in tickers.split(",") if t] if tickers else None
    kinds = [t for t in types.split("|") if t] if types else None
    fx_states = [f for f in fx.split(",") if f] if fx else None
    rows = queries.tape(conn, _sources(), USER_ID, days=_window(days), only=only,
                        jurisdictions=juris, tickers=ticks, types=kinds,
                        fx_states=fx_states, collapse=collapse, limit=limit)
    # Boundaries travel WITH the tape: a coverage limit is a chronological
    # event and belongs in the same payload as the rows it sits between.
    cover = queries.coverage(conn, _sources(), _window(days))
    conn.close()
    return {"items": rows, "collapsed": collapse, "coverage": cover,
            "window_days": _window(days)}


@app.get("/api/rail")
def rail():
    conn = _conn()
    sources = _sources()
    payload = queries.rail(conn, sources, _translator())
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
def facets(days: int | None = None):
    conn = _conn()
    days = _window(days)
    payload = queries.facets(conn, _sources(), USER_ID, days=days)
    conn.close()
    return payload


@app.get("/api/unread")
def unread(days: int | None = None):
    conn = _conn()
    days = _window(days)
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
