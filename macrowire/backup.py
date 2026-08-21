"""Backups, using SQLite's online backup API.

Not a file copy. In WAL mode the database file on disk is not, on its
own, a consistent snapshot - recent commits live in the `-wal` sidecar
until a checkpoint. Copying `macrowire.db` while anything is writing
produces a file that may open cleanly and be silently short of data,
which is the worst possible property in a backup.

`sqlite3.Connection.backup()` takes a consistent snapshot under a read
lock and works correctly with WAL.

Every backup is verified before it counts as one: the file is reopened,
integrity-checked, and its row counts compared against the source. An
unverified backup is a guess.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .errors import MacroWireError

# Counted and compared on every backup. If these do not match, the backup
# is deleted rather than kept and trusted.
VERIFIED_TABLES = (
    "sources", "items", "observations", "raw_responses",
    "fetch_log", "users", "watchlists", "item_state",
)

STAMP = "%Y%m%dT%H%M%SZ"


def backup_dir(db_file: Path, directory: Path | None = None) -> Path:
    """Where backups live. Configurable, same as export - a backup on the
    same disk as the database protects against a mistake but not a drive
    failure, and the config should let you say so."""
    return directory or (db_file.parent / "backups")


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for table in VERIFIED_TABLES:
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            out[table] = -1        # table absent; recorded, not silently skipped
    return out


def create(source_conn: sqlite3.Connection, db_file: Path, keep: int = 7,
           now: datetime | None = None, directory: Path | None = None) -> dict:
    """Take a verified backup and prune older ones to `keep`."""
    target_dir = backup_dir(db_file, directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime(STAMP)
    target = target_dir / f"{db_file.stem}-{stamp}.db"

    if target.exists():
        raise MacroWireError(f"backup already exists: {target}")

    expected = _counts(source_conn)

    # The online backup API: consistent under WAL, unlike a file copy.
    dest = sqlite3.connect(target)
    try:
        source_conn.backup(dest)
        # A backup should be ONE self-contained file. The destination
        # inherits WAL from the source, which leaves -wal/-shm sidecars
        # beside it; anyone copying just the .db would silently take an
        # incomplete archive. Rolling it back to a non-WAL journal folds
        # everything into the single file.
        dest.execute("PRAGMA journal_mode=DELETE")
        dest.commit()
    finally:
        dest.close()

    for suffix in ("-wal", "-shm"):
        side = Path(str(target) + suffix)
        if side.exists():
            side.unlink()

    # Verify before declaring success.
    check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        actual = _counts(check)
    finally:
        check.close()

    if integrity != "ok":
        target.unlink(missing_ok=True)
        raise MacroWireError(f"backup failed integrity_check ({integrity}); discarded")
    if actual != expected:
        differing = {t: (expected[t], actual[t]) for t in expected if expected[t] != actual[t]}
        target.unlink(missing_ok=True)
        raise MacroWireError(
            f"backup row counts do not match source {differing}; discarded"
        )

    pruned = prune(db_file, keep, directory)
    return {
        "path": target,
        "bytes": target.stat().st_size,
        "counts": actual,
        "pruned": pruned,
    }


def existing(db_file: Path, directory: Path | None = None) -> list[Path]:
    d = backup_dir(db_file, directory)
    if not d.exists():
        return []
    return sorted(d.glob(f"{db_file.stem}-*.db"))


def prune(db_file: Path, keep: int, directory: Path | None = None) -> list[Path]:
    """Delete all but the newest `keep` backups. Filenames sort chronologically."""
    if keep < 1:
        raise MacroWireError("keep must be at least 1")
    found = existing(db_file, directory)
    doomed = found[:-keep] if len(found) > keep else []
    for path in doomed:
        path.unlink()
    return doomed


def restore(backup_file: Path, db_file: Path) -> dict:
    """Replace the live database with a backup.

    The current database is moved aside rather than overwritten - if the
    restore was a mistake, the thing being replaced may be the only copy
    of something that cannot be re-fetched.
    """
    if not backup_file.exists():
        raise MacroWireError(f"no such backup: {backup_file}")

    # Validate before touching the live database. A file that is not a
    # database at all raises on the first query, before integrity_check
    # ever runs, so that has to be caught too.
    check = None
    try:
        check = sqlite3.connect(f"file:{backup_file}?mode=ro", uri=True)
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MacroWireError(f"{backup_file} fails integrity_check; refusing to restore")
        counts = _counts(check)
    except sqlite3.DatabaseError as exc:
        raise MacroWireError(
            f"{backup_file} is not a readable SQLite database ({exc}); refusing to restore"
        ) from exc
    finally:
        if check is not None:
            check.close()

    displaced = None
    if db_file.exists():
        stamp = datetime.now(timezone.utc).strftime(STAMP)
        displaced = db_file.with_name(f"{db_file.stem}-replaced-{stamp}.db")
        os.replace(db_file, displaced)
        # WAL sidecars belong to the file being replaced, not the new one.
        for suffix in ("-wal", "-shm"):
            side = Path(str(db_file) + suffix)
            if side.exists():
                side.unlink()

    source = sqlite3.connect(f"file:{backup_file}?mode=ro", uri=True)
    dest = sqlite3.connect(db_file)
    try:
        source.backup(dest)
    finally:
        source.close()
        dest.close()

    return {"restored_from": backup_file, "counts": counts, "displaced": displaced}
