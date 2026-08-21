"""Versioned schema migrations.

The requirement this exists to satisfy: **a schema change must never
again mean deleting the database.** Some of what is stored cannot be
re-fetched at any price - `rba_media_releases` carries a one-item window
with no archive and no `since` parameter, so every release it collects
from now on exists only here.

Deliberately small: stdlib sqlite3, an ordered list, a `schema_version`
table. No Alembic, no autogeneration, no downgrades. A migration is a
version number, a name, and SQL.

Adding one: append to MIGRATIONS with the next integer. Never edit or
renumber a migration that has shipped - `applied_at` in an existing
database is the record that it already ran.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# --- 001 -------------------------------------------------------------------
# The schema as it stood when migrations were introduced, retrofitted as the
# baseline. Written with IF NOT EXISTS throughout so it is a no-op against a
# database that predates this mechanism.
BASELINE = """
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    config      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS items (
    id                  TEXT PRIMARY KEY,
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    external_id         TEXT,
    title               TEXT NOT NULL,
    url                 TEXT,
    summary             TEXT,
    content             TEXT,
    published_at        TEXT,
    fetched_at          TEXT NOT NULL,
    ticker              TEXT,
    is_price_sensitive  INTEGER,
    announcement_type   TEXT,
    institution_abbrev  TEXT,
    simple_title        TEXT,
    occurrence_date     TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_source_published
    ON items(source_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_ticker ON items(ticker);
CREATE INDEX IF NOT EXISTS idx_items_revision_chain
    ON items(source_id, external_id, published_at DESC);

CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    series          TEXT NOT NULL,
    period          TEXT NOT NULL,
    value           REAL NOT NULL,
    unit            TEXT,
    base_currency   TEXT,
    target_currency TEXT,
    rate_type       TEXT,
    frequency       TEXT,
    decimals        INTEGER,
    external_id     TEXT,
    observed_at     TEXT,
    fetched_at      TEXT NOT NULL,
    revised_at      TEXT,
    UNIQUE(source_id, series, period)
);
CREATE INDEX IF NOT EXISTS idx_observations_series_period
    ON observations(series, period DESC);

CREATE TABLE IF NOT EXISTS raw_responses (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    url          TEXT NOT NULL,
    status_code  INTEGER,
    fetched_at   TEXT NOT NULL,
    body         BLOB NOT NULL,
    encoding     TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_source_fetched
    ON raw_responses(source, fetched_at DESC);

CREATE TABLE IF NOT EXISTS fetch_log (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    status          TEXT NOT NULL,
    new_item_count  INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_source_ts
    ON fetch_log(source, timestamp DESC);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    email       TEXT UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlists (
    id       INTEGER PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id),
    ticker   TEXT NOT NULL,
    market   TEXT,
    UNIQUE(user_id, ticker, market)
);

CREATE TABLE IF NOT EXISTS item_state (
    user_id  INTEGER NOT NULL REFERENCES users(id),
    item_id  TEXT NOT NULL REFERENCES items(id),
    read_at  TEXT,
    flagged  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, item_id)
);
"""

# --- 002 -------------------------------------------------------------------
# Distinguish a transient network blip from a source that has genuinely
# gone. Without this they are the same row shape and read identically.
ERROR_KIND = """
ALTER TABLE fetch_log ADD COLUMN error_kind TEXT;
CREATE INDEX IF NOT EXISTS idx_fetch_log_kind ON fetch_log(source, error_kind);
"""

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "baseline", BASELINE),
    (2, "fetch_log.error_kind", ERROR_KIND),
]


def _current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_version (
               version    INTEGER PRIMARY KEY,
               name       TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    applied = row[0] if row and row[0] is not None else 0
    if applied:
        return applied

    # Retrofit: a database that predates this mechanism but already has the
    # baseline tables is at version 1, not 0. Re-running the baseline would
    # be harmless (IF NOT EXISTS throughout) but stamping it is honest.
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()
    if existing:
        conn.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (1, "baseline (retrofitted)", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
        return 1
    return 0


def migrate(conn: sqlite3.Connection, verbose: bool = False) -> list[tuple[int, str]]:
    """Apply every pending migration in order. Returns what was applied."""
    current = _current_version(conn)
    applied: list[tuple[int, str]] = []

    for version, name, script in MIGRATIONS:
        if version <= current:
            continue
        try:
            conn.executescript(script)
        except sqlite3.OperationalError as exc:
            # An ALTER that has already happened by other means should not
            # wedge the chain, but anything else must surface.
            if "duplicate column name" not in str(exc):
                raise
        conn.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
        applied.append((version, name))
        if verbose:
            print(f"  applied migration {version:03d}: {name}")
    return applied


def version(conn: sqlite3.Connection) -> int:
    """Applied schema version, or 0 for a database that has never migrated."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0
    return row[0] if row and row[0] is not None else 0
