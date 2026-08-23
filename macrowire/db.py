"""SQLite storage.

Global tables (`sources`, `items`, `observations`, `raw_responses`,
`fetch_log`) carry no user column: an announcement is the same
announcement for everyone, so it is stored once. Everything personal
lives in the user-keyed tables. Single user today, id=1, but nothing here
assumes that.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import migrations
from .errors import MacroWireError

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "macrowire.db"

LOCAL_USER_ID = 1

# fetch_log.status. The distinction that matters is whether the SOURCE was
# actually contacted, because that is the only thing that proves it is alive.
#
#   ok         contacted, parsed, stored (possibly nothing new)
#   no_change  CONTACTED, and it said there is nothing new. A successful
#              poll: CFETS's publication gate reads ccpr.json, finds the
#              fix already stored, and stops. Proves reachability.
#   throttled  NOT contacted. The rate limiter blocked the attempt before
#              any request went out. Proves nothing either way.
#   error      contacted, and it failed
#
# Treating no_change and throttled alike is what made a healthy CFETS report
# "last successful fetch: never" forever - skipping is its normal outcome.
STATUS_OK = "ok"
STATUS_NO_CHANGE = "no_change"
STATUS_THROTTLED = "throttled"
STATUS_ERROR = "error"

STATUS_BACKFILL = "backfill"
STATUS_REVISION = "revision"

# Rows that mean WE REACHED THE SOURCE. Every one of these is written only
# after a real request got a real answer, so each is evidence of
# reachability. Health is derived from this set, not from a list repeated in
# a query somewhere - which is how it drifted three times:
#
#   no_change  a gated poll DID contact the source and was told nothing
#              was new. Missing it made CFETS report "never" forever.
#   backfill   paged seeding requests. Missing it made a freshly seeded
#              source report "log incomplete" about data it had just
#              fetched, minutes earlier.
#   revision   written only after a fetched payload parsed successfully.
#              Never independently reachable today, but a restore that
#              clipped the ok row while keeping this one would miscount -
#              which is exactly the shape of the other two.
#
# ADDING A STATUS? Decide here whether it means contact. That decision is
# the point of this set existing.
# Error kinds that describe the PATH to the source rather than the source.
# A run of these means the request never got an answer; it is not evidence
# that the feed has changed, moved or gone. Kept here beside the statuses
# because "which kinds mean unreachable" has to have exactly one definition -
# the contact-status drift this project already fixed four times.
PATH_KINDS = ("network", "timeout")

CONTACT_STATUSES = (STATUS_OK, STATUS_NO_CHANGE, STATUS_BACKFILL, STATUS_REVISION)

# Legacy: everything written before the no_change/throttled distinction
# existed used this. Mostly throttles, so it is NOT counted as contact -
# guessing otherwise would resurrect the false alarm it was meant to end.
LEGACY_SKIP = "skipped"

# Not contact: the attempt never reached the source.
NON_CONTACT_STATUSES = (STATUS_THROTTLED, LEGACY_SKIP)


def contact_sql(column: str = "status") -> str:
    """SQL fragment for `reached the source`, from the one authoritative set."""
    return f"{column} IN ({', '.join(repr(s) for s in CONTACT_STATUSES)})"


# The schema lives in macrowire/migrations.py as an ordered, versioned
# list. It is deliberately not duplicated here - two copies would drift.


def utc_now() -> str:
    """Single source of truth for timestamps. UTC, ISO 8601, seconds."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    override = os.environ.get("MACROWIRE_DB")
    return Path(override) if override else DEFAULT_DB_PATH


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the store. Pass an explicit path for anything throwaway.

    MACROWIRE_REFUSE_DEFAULT_DB makes an implicit connection to the real
    database a hard error. The test suite sets it, so a test that forgets
    to pass a temp path fails loudly instead of quietly writing fabricated
    rows into six years of collected history.
    """
    # Fires on the DEFAULT path, which is what the guard is named for. An
    # explicit path, or MACROWIRE_DB naming a throwaway, is a deliberate
    # choice of somewhere safe and is allowed - the app's own _conn() takes
    # no argument, so redirecting it by env is the only way to exercise an
    # endpoint at all. What stays impossible is reaching the real database
    # by forgetting to say where to write.
    if (path is None and os.environ.get("MACROWIRE_REFUSE_DEFAULT_DB")
            and not os.environ.get("MACROWIRE_DB")):
        raise MacroWireError(
            "refusing to open the default database: MACROWIRE_REFUSE_DEFAULT_DB "
            "is set. Pass an explicit path, or set MACROWIRE_DB to a throwaway "
            "- tests must never reach collected history."
        )
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def initialise(conn: sqlite3.Connection, verbose: bool = False) -> list:
    """Bring the schema up to date and make sure the local user exists.

    Never drops or recreates anything: see macrowire/migrations.py.
    """
    applied = migrations.migrate(conn, verbose=verbose)
    conn.execute(
        "INSERT OR IGNORE INTO users (id, email, created_at) VALUES (?, ?, ?)",
        (LOCAL_USER_ID, None, utc_now()),
    )
    conn.commit()
    return applied


def upsert_source(conn: sqlite3.Connection, name: str, kind: str, config: dict) -> int:
    payload = json.dumps(config, sort_keys=True)
    conn.execute(
        """
        INSERT INTO sources (name, kind, config) VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, config=excluded.config
        """,
        (name, kind, payload),
    )
    row = conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
    return row["id"]


def content_hash(*parts: object) -> str:
    """Stable PK for an item.

    Hashed over identity plus headline: a re-poll of the same entry
    collapses, while an upstream headline correction lands as a new row
    rather than silently overwriting what you already read. raw_responses
    keeps both payloads either way.
    """
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def store_raw_response(
    conn: sqlite3.Connection,
    source: str,
    url: str,
    status_code: int,
    body: bytes,
    encoding: str | None = None,
) -> int:
    """Persist the original bytes. Returns the row id so the resolved
    encoding can be recorded once decoding succeeds."""
    cursor = conn.execute(
        """INSERT INTO raw_responses (source, url, status_code, fetched_at, body, encoding)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (source, url, status_code, utc_now(), sqlite3.Binary(body), encoding),
    )
    return cursor.lastrowid


def record_raw_encoding(conn: sqlite3.Connection, raw_id: int, encoding: str) -> None:
    conn.execute("UPDATE raw_responses SET encoding = ? WHERE id = ?", (encoding, raw_id))


def log_fetch(
    conn: sqlite3.Connection,
    source: str,
    status: str,
    new_item_count: int = 0,
    error: str | None = None,
    detail: str | None = None,
    error_kind: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO fetch_log
               (source, timestamp, status, new_item_count, error, detail, error_kind)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (source, utc_now(), status, new_item_count, error, detail, error_kind),
    )
    conn.commit()


def prune_raw_responses(conn: sqlite3.Connection, source: str, keep_days: int) -> int:
    """Drop raw payloads for one source older than `keep_days`.

    Only ever used where the raw payload is genuinely re-fetchable. For a
    feed that carries one item and no archive, the stored bytes are the
    only copy in existence and retention must stay off - which is the
    default. See README, "raw_responses retention".
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=keep_days)
    ).isoformat(timespec="seconds")
    cursor = conn.execute(
        "DELETE FROM raw_responses WHERE source = ? AND fetched_at < ?", (source, cutoff)
    )
    return cursor.rowcount


def has_parsed_before(conn: sqlite3.Connection, source: str) -> bool:
    """True if this source has ever returned a usable feed.

    Gates the empty-feed alarm: zero entries from a source we have never
    seen populated is unproven, not broken.
    """
    row = conn.execute(
        "SELECT 1 FROM fetch_log WHERE source = ? AND status = 'ok' LIMIT 1", (source,)
    ).fetchone()
    return row is not None


def last_attempt_at(conn: sqlite3.Connection, source: str) -> str | None:
    """Most recent time we actually went out to the source, for rate limiting.

    Includes no_change: the publication gate DID make a request, so it counts
    against the interval. Excluding it meant a gated source was re-probed on
    every cycle regardless of its configured minimum.
    """
    row = conn.execute(
        """SELECT timestamp FROM fetch_log
           WHERE source = ? AND status IN ('ok', 'no_change', 'error')
           ORDER BY timestamp DESC LIMIT 1""",
        (source,),
    ).fetchone()
    return row["timestamp"] if row else None
