"""Viewer preferences: what belongs to whoever is looking.

THE LINE, and it is the whole design:

  VIEWER PREFERENCE   locale, timezone, session order, jurisdiction order,
                      tape window. Belongs to the reader. Editable from the
                      interface, stored per user, and it NEVER writes
                      sources.yaml.

  INSTALL CONFIG      poll intervals, timeouts, contacts, export and backup
                      paths, per-source vocabularies. Belongs to the
                      installation. Stays in YAML; the interface shows it
                      read-only so it can be seen without opening a file.

  DATA                the watchlist. Already in the database.

PRECEDENCE is preference -> sources.yaml -> built-in default, and every
level is visible: `effective()` reports which one answered, so a value can
always be traced and a preference can always be removed to fall back.

`collapse_repeats` is the one setting that does not sit cleanly on either
side and is deliberately install-only for now. Per source it states a fact
about that source - HKMA publishes "Scam alert related to banks" 207 times
- while "show me the collapsed view" is a reader's choice affecting no
stored row. It is the first candidate for promotion if the expanded view is
ever wanted; promoting it needs a precedence rule between a per-source
setting and a global override, which is complexity ahead of demand.
"""

from __future__ import annotations

import sqlite3

from . import db
from .config import (JURISDICTIONS, load_locale, load_ordering, load_timezone,
                     load_window_days)
from .errors import ConfigError

# Every preference the interface may set, with how to check it. A key not in
# here cannot be stored: an unknown preference is a typo, and a typo that
# persists silently is a setting that appears to work and does nothing.
SETTABLE = ("locale", "timezone", "session_order", "jurisdiction_order",
            "jurisdiction", "window_days")

# Bounds on the tape window. Not arbitrary: below a week the day headers
# outnumber the items on a quiet stretch, and above a year the collapsed
# tape stops being a wire and becomes an archive with no index.
WINDOW_CHOICES = (7, 30, 90, 365)


def _validate(key: str, value: str) -> str:
    """Reject at the door, reusing the config loaders' own rules.

    A preference and a config value must mean the same thing - one
    definition of "a valid timezone", not two that can drift.
    """
    from zoneinfo import ZoneInfo

    from . import i18n

    value = str(value).strip()
    if key == "locale":
        if value not in i18n.available():
            raise ConfigError(
                f"no locale {value!r} installed. Available: "
                f"{', '.join(i18n.available())}. Drop a JSON file into "
                f"macrowire/locales/ to add one.")
    elif key == "timezone":
        if value == "system":
            return value
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ConfigError(
                f"timezone {value!r} is not an IANA zone name (e.g. "
                f"Australia/Sydney, America/New_York, UTC). A fixed offset "
                f"is not accepted: it would be wrong across DST.") from exc
    elif key == "session_order":
        if value not in ("viewer", "fixed"):
            raise ConfigError(
                f"session_order {value!r} must be 'viewer' or 'fixed'. An "
                f"explicit list is a sources.yaml decision, not a per-view one.")
    elif key == "jurisdiction_order":
        if value not in ("viewer", "alphabetical"):
            raise ConfigError(
                f"jurisdiction_order {value!r} must be 'viewer' or "
                f"'alphabetical'.")
    elif key == "jurisdiction":
        if value and value not in JURISDICTIONS:
            raise ConfigError(
                f"jurisdiction {value!r} must be one of {sorted(JURISDICTIONS)}")
    elif key == "window_days":
        try:
            days = int(value)
        except ValueError as exc:
            raise ConfigError(f"window_days {value!r} is not a number") from exc
        if days not in WINDOW_CHOICES:
            raise ConfigError(
                f"window_days {days} must be one of {list(WINDOW_CHOICES)}")
        return str(days)
    else:
        raise ConfigError(
            f"{key!r} is not a viewer preference. Settable: "
            f"{', '.join(SETTABLE)}. Everything else lives in sources.yaml "
            f"and is shown read-only.")
    return value


def stored(conn: sqlite3.Connection, user_id: int = db.LOCAL_USER_ID) -> dict:
    """Only what has actually been overridden. Not the effective values."""
    return {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM preferences WHERE user_id = ?", (user_id,))}


def set_one(conn: sqlite3.Connection, key: str, value: str,
            user_id: int = db.LOCAL_USER_ID) -> str:
    value = _validate(key, value)
    conn.execute(
        """INSERT INTO preferences (user_id, key, value, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, key) DO UPDATE
             SET value = excluded.value, updated_at = excluded.updated_at""",
        (user_id, key, value, db.utc_now()))
    conn.commit()
    return value


def clear(conn: sqlite3.Connection, key: str,
          user_id: int = db.LOCAL_USER_ID) -> bool:
    """Remove an override so the config value applies again.

    Every preference must be removable. A setting you can change but not
    un-change is a one-way door, and the YAML has to stay the floor.
    """
    if key not in SETTABLE:
        raise ConfigError(f"{key!r} is not a viewer preference")
    cursor = conn.execute(
        "DELETE FROM preferences WHERE user_id = ? AND key = ?", (user_id, key))
    conn.commit()
    return cursor.rowcount > 0


def _from_config() -> dict:
    """What sources.yaml says, before any preference is applied."""
    ordering = load_ordering()
    return {
        "locale": load_locale(),
        "timezone": load_timezone(raw=True),
        "session_order": ordering["sessions"],
        "jurisdiction_order": ordering["jurisdictions"],
        "jurisdiction": ordering["viewer_jurisdiction"] or "",
        "window_days": str(load_window_days()),
    }


def effective(conn: sqlite3.Connection | None = None,
              user_id: int = db.LOCAL_USER_ID) -> dict:
    """Every preference, its value, and WHICH LEVEL answered.

    The level is returned rather than inferred so the interface can show it
    per row and offer "reset to config" only where there is something to
    reset. A value with no visible provenance is a value you cannot argue
    with.
    """
    config = _from_config()
    overrides = stored(conn, user_id) if conn is not None else {}
    out = {}
    for key in SETTABLE:
        configured = config.get(key, "")
        if key in overrides:
            out[key] = {"value": overrides[key], "source": "preference",
                        "config_value": configured}
        else:
            out[key] = {"value": configured, "source": "config",
                        "config_value": configured}
    return out


def resolve(conn: sqlite3.Connection | None = None,
            user_id: int = db.LOCAL_USER_ID) -> dict:
    """Just the values, for callers that do not care where they came from."""
    return {k: v["value"] for k, v in effective(conn, user_id).items()}
