"""Off-machine durability for the rows that cannot be re-fetched.

Local backups do not survive a dead disk. A handful of sources carry no
archive at all - `rba_media_releases` is a one-item window with no paging
and no `since` parameter - so every row they collect exists nowhere else
in the world once this machine is gone.

This writes those rows, and only those rows, to a plain-text file meant
to be committed to the repository. Everything else the project stores is
re-fetchable and is deliberately excluded: an export that also carried
8,050 CFETS observations and 1,800 news items would be large, would churn
on every poll, and would be protecting things that need no protection.

FORMAT: JSONL, one record per line.

Chosen over a SQL dump because the entire point is that this file lives
in git. Line-oriented data diffs per row: adding a media release appends
one line and touches nothing else, so history stays readable and a
conflict is resolvable by eye. A SQL dump rewrites multi-row INSERT
batches and re-emits schema DDL, producing diffs that are large and
uninformative for a one-row change. JSONL is also schema-loose - it
survives the schema evolving underneath it, which matters now that
migrations exist - and is readable without a database.

DETERMINISM: rows are emitted in a fixed sort order with sorted keys and
no timestamps anywhere. Re-exporting unchanged data produces a
byte-identical file, so `git status` stays quiet unless something
genuinely new was collected.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPORT_DIR = REPO_ROOT / "export"
EXPORT_NAME = "irreplaceable.jsonl"

# Bumped only if the record shape changes incompatibly. Not a timestamp -
# nothing in this file may vary between runs over identical data.
FORMAT_VERSION = 1

ITEM_COLUMNS = (
    "id", "external_id", "title", "url", "summary", "content", "published_at",
    "fetched_at", "ticker", "is_price_sensitive", "announcement_type",
    "institution_abbrev", "simple_title", "occurrence_date",
)
OBSERVATION_COLUMNS = (
    "series", "period", "value", "unit", "base_currency", "target_currency",
    "rate_type", "frequency", "decimals", "external_id", "observed_at",
    "fetched_at", "revised_at",
)


def _line(record: dict) -> str:
    # sort_keys for stable key order; ensure_ascii=False so Chinese text
    # stays readable in a diff rather than becoming \uXXXX noise.
    return json.dumps(record, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")) + "\n"


def irreplaceable_sources(sources) -> list:
    return sorted((s for s in sources if s.archive == "none"), key=lambda s: s.name)


def build(conn: sqlite3.Connection, sources) -> str:
    """Render the export as a string. Pure - no clock, no filesystem."""
    conn.row_factory = sqlite3.Row
    targets = irreplaceable_sources(sources)

    lines = [_line({"type": "header", "format_version": FORMAT_VERSION,
                    "sources": [s.name for s in targets]})]

    for source in targets:
        row = conn.execute(
            "SELECT id, name, kind FROM sources WHERE name = ?", (source.name,)
        ).fetchone()
        if row is None:
            continue
        lines.append(_line({"type": "source", "name": row["name"], "kind": row["kind"],
                            "archive": source.archive}))

        # Deterministic order: the natural key, never rowid or insertion order.
        for item in conn.execute(
            f"SELECT {', '.join(ITEM_COLUMNS)} FROM items WHERE source_id = ? "
            f"ORDER BY published_at, id", (row["id"],)
        ):
            record = {"type": "item", "source": source.name}
            record.update({c: item[c] for c in ITEM_COLUMNS})
            lines.append(_line(record))

        for obs in conn.execute(
            f"SELECT {', '.join(OBSERVATION_COLUMNS)} FROM observations "
            f"WHERE source_id = ? ORDER BY period, series", (row["id"],)
        ):
            record = {"type": "observation", "source": source.name}
            record.update({c: obs[c] for c in OBSERVATION_COLUMNS})
            lines.append(_line(record))

    return "".join(lines)


def state(conn: sqlite3.Connection, sources, settings) -> dict:
    """MEASURE whether the irreplaceable rows are actually protected.

    Never warn unconditionally. If someone has pointed export.path at a
    synced folder and it is up to date, the honest report is that the
    problem is solved - and a panel that keeps nagging anyway is one you
    learn to ignore, which costs you the warning that mattered.
    """
    target = settings["path"] / EXPORT_NAME
    payload = build(conn, sources)
    on_disk = target.read_text(encoding="utf-8") if target.exists() else None

    rows = sum(1 for line in payload.splitlines()
               if json.loads(line).get("type") in ("item", "observation"))

    return {
        "path": target,
        "directory": settings["path"],
        "external": settings["external"],
        "exists": on_disk is not None,
        "current": on_disk == payload,
        "rows": rows,
        "written_at": (datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
                       .isoformat(timespec="seconds") if target.exists() else None),
        "sources": [s.name for s in irreplaceable_sources(sources)],
    }


def _row_count(payload: str) -> int:
    return sum(1 for line in payload.splitlines()
               if json.loads(line).get("type") in ("item", "observation"))


def write(conn: sqlite3.Connection, sources, directory: Path | None = None,
          force: bool = False) -> dict:
    directory = directory or DEFAULT_EXPORT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / EXPORT_NAME

    payload = build(conn, sources)
    previous = target.read_text(encoding="utf-8") if target.exists() else None

    # Refuse to shrink. The export path is global config while the database
    # path can be overridden by MACROWIRE_DB, so a second instance or a test
    # run pointed at a scratch database would otherwise overwrite the only
    # off-disk copy of the irreplaceable rows with a nearly-empty file.
    # That is not hypothetical - it happened while this guard was being
    # written, from a temp database in /tmp.
    if previous is not None and not force:
        had, have = _row_count(previous), _row_count(payload)
        if have < had:
            from .errors import MacroWireError
            raise MacroWireError(
                f"refusing to overwrite {target}: it holds {had} irreplaceable "
                f"row(s) and this database offers only {have}. If the database "
                f"is genuinely the right one, pass force=True. Check MACROWIRE_DB "
                f"first - a scratch database is the usual cause.")

    unchanged = previous == payload
    if not unchanged:
        target.write_text(payload, encoding="utf-8")

    counts = {"item": 0, "observation": 0, "source": 0}
    for line in payload.splitlines():
        kind = json.loads(line).get("type")
        if kind in counts:
            counts[kind] += 1

    return {"path": target, "bytes": len(payload.encode("utf-8")),
            "counts": counts, "unchanged": unchanged}


def read(path: Path) -> tuple[dict, list[dict]]:
    """Parse an export. Returns (header, records)."""
    from .errors import MacroWireError

    header, records = None, []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MacroWireError(f"{path}:{number} is not valid JSON: {exc}") from exc
        if record.get("type") == "header":
            header = record
        else:
            records.append(record)

    if header is None:
        raise MacroWireError(f"{path} has no header record; refusing to import")
    if header.get("format_version") != FORMAT_VERSION:
        raise MacroWireError(
            f"{path} is format_version {header.get('format_version')}, "
            f"this build reads {FORMAT_VERSION}"
        )
    return header, records


def load(conn: sqlite3.Connection, path: Path) -> dict:
    """Import an export into a database. Existing rows are left alone.

    INSERT OR IGNORE throughout: this restores what is missing and never
    overwrites what is present. A local row is at least as trustworthy as
    a committed copy of itself.
    """
    from . import db

    header, records = read(path)
    source_ids: dict[str, int] = {}
    added = {"item": 0, "observation": 0}
    skipped = {"item": 0, "observation": 0}

    for record in records:
        kind = record.get("type")
        if kind == "source":
            source_ids[record["name"]] = db.upsert_source(
                conn, record["name"], record["kind"], {}
            )
            continue

        source_id = source_ids.get(record.get("source"))
        if source_id is None:
            continue

        if kind == "item":
            cursor = conn.execute(
                f"""INSERT OR IGNORE INTO items
                    (source_id, {', '.join(ITEM_COLUMNS)})
                    VALUES (?, {', '.join('?' * len(ITEM_COLUMNS))})""",
                (source_id, *(record.get(c) for c in ITEM_COLUMNS)),
            )
        elif kind == "observation":
            cursor = conn.execute(
                f"""INSERT OR IGNORE INTO observations
                    (source_id, {', '.join(OBSERVATION_COLUMNS)})
                    VALUES (?, {', '.join('?' * len(OBSERVATION_COLUMNS))})""",
                (source_id, *(record.get(c) for c in OBSERVATION_COLUMNS)),
            )
        else:
            continue

        if cursor.rowcount:
            added[kind] += 1
        else:
            skipped[kind] += 1

    conn.commit()
    return {"header": header, "added": added, "already_present": skipped}
