"""CLI.

    python -m macrowire fetch    one poll cycle
    python -m macrowire status   per-source health, as information
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import backfill, backup as backup_mod, db, export as export_mod, migrations, wire
from .config import load_backup_settings, load_sources, load_web_settings
from .errors import MacroWireError


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h ago"


def cmd_fetch(args) -> int:
    sources = load_sources()
    conn = db.connect()
    db.initialise(conn)

    if args.source:
        sources = [s for s in sources if s.name in args.source]
        unknown = set(args.source) - {s.name for s in sources}
        if unknown:
            raise MacroWireError(f"unknown source(s): {', '.join(sorted(unknown))}")

    results, failures = wire.fetch_all(conn, sources)

    for result in results:
        if result["skipped"]:
            why = (
                result["reason"] if result.get("reason")
                else f"polled recently; {result['wait_seconds']}s until next allowed"
            )
            print(f"  {result['source']:<24} skipped  ({why})")
            continue
        parts = [f"{result['entries']} entries"]
        if result["new_items"]:
            parts.append(f"{result['new_items']} new item(s)")
        if result["new_observations"]:
            parts.append(f"{result['new_observations']} new observation(s)")
        if not result["new_items"] and not result["new_observations"]:
            parts.append("nothing new")
        print(f"  {result['source']:<24} ok       ({', '.join(parts)})")
        for note in result["revisions"]:
            print(f"  {'':<24} REVISED  {note}")

    stored_something = any(
        (r.get("new_items") or 0) + (r.get("new_observations") or 0)
        for r in results if not r["skipped"]
    )
    _maybe_backup(conn, stored_something)
    conn.close()

    if failures:
        print(f"\n{len(failures)} source(s) failed. Logged to fetch_log.", file=sys.stderr)
        if len(failures) == 1:
            raise failures[0]
        raise ExceptionGroup("one or more sources failed", failures)
    return 0


def cmd_backfill(args) -> int:
    from datetime import datetime, timezone, timedelta

    sources = {s.name: s for s in load_sources()}
    source = sources.get(args.source)
    if source is None:
        raise MacroWireError(
            f"unknown source {args.source!r}. Known: {', '.join(sorted(sources))}"
        )
    if not source.config.get("backfill_start"):
        raise MacroWireError(
            f"{source.name} has no backfill_start in sources.yaml - "
            f"this source has no retrievable history"
        )

    conn = db.connect()
    db.initialise(conn)
    # The API dates in the source's own timezone, not ours.
    offset = source.config.get("timezone", "+00:00")
    today = datetime.now(timezone(timedelta(
        hours=int(offset[:3]), minutes=int(offset[0] + offset[4:6])
    ))).date()

    print(f"backfill: {source.name}")
    result = backfill.run(conn, source, today, dry_run=args.dry_run)
    if not result["dry_run"]:
        print(f"\n  {result['requests']} request(s) made, "
              f"{result['stored']} observation(s) stored, "
              f"{result['skipped']} page(s) already had.")
    conn.close()
    return 0


def _maybe_backup(conn, stored_something: bool) -> None:
    """Take an automatic backup if the cycle produced anything worth keeping.

    Deliberately best-effort: a backup problem must not fail a fetch that
    already succeeded and already wrote its data.
    """
    settings = load_backup_settings()
    if not settings["enabled"] or not stored_something:
        return
    path = db.db_path()
    found = backup_mod.existing(path)
    if found:
        newest = found[-1].stat().st_mtime
        if (time.time() - newest) < settings["interval_seconds"]:
            return
    try:
        result = backup_mod.create(conn, path, keep=settings["keep"])
        print(f"  {'backup':<24} ok       ({result['path'].name}, "
              f"{result['bytes']/1024/1024:.1f} MB, verified)")
    except Exception as exc:
        print(f"  {'backup':<24} FAILED   ({exc})", file=sys.stderr)


def cmd_backup(args) -> int:
    conn = db.connect()
    db.initialise(conn)
    path = db.db_path()

    if args.list:
        found = backup_mod.existing(path)
        if not found:
            print("no backups yet")
        for f in found:
            print(f"  {f.name:<40} {f.stat().st_size/1024/1024:>7.2f} MB")
        conn.close()
        return 0

    result = backup_mod.create(conn, path, keep=args.keep)
    conn.close()
    print(f"backup verified: {result['path']}")
    print(f"  {result['bytes']/1024/1024:.2f} MB")
    print("  row counts match source: " +
          ", ".join(f"{k}={v:,}" for k, v in result["counts"].items() if v > 0))
    for pruned in result["pruned"]:
        print(f"  pruned old backup: {pruned.name}")
    return 0


def cmd_restore(args) -> int:
    path = db.db_path()
    chosen = Path(args.backup) if args.backup else None
    if chosen is None:
        found = backup_mod.existing(path)
        if not found:
            raise MacroWireError("no backups found")
        chosen = found[-1]

    print(f"restore {chosen.name}  ->  {path}")
    print("  the current database will be moved aside, not deleted.")
    if not args.yes:
        # Some of what is being replaced may be unrecoverable at any price.
        reply = input("  type 'restore' to proceed: ").strip()
        if reply != "restore":
            print("  aborted")
            return 1

    result = backup_mod.restore(chosen, path)
    print(f"  restored. rows: " +
          ", ".join(f"{k}={v:,}" for k, v in result["counts"].items() if v > 0))
    if result["displaced"]:
        print(f"  previous database kept at: {result['displaced'].name}")
    return 0


def cmd_migrate(args) -> int:
    conn = db.connect()
    print(f"schema version before: {migrations.version(conn)}")
    applied = db.initialise(conn, verbose=True)
    print(f"schema version after : {migrations.version(conn)}")
    if not applied:
        print("  already up to date")
    conn.close()
    return 0


def cmd_export(args) -> int:
    conn = db.connect()
    db.initialise(conn)
    result = export_mod.write(conn, load_sources())
    conn.close()

    c = result["counts"]
    print(f"export: {result['path']}")
    print(f"  {c['source']} source(s), {c['item']} item(s), {c['observation']} observation(s)")
    print(f"  {result['bytes']:,} bytes")
    if result["unchanged"]:
        print("  unchanged since last export - file not rewritten, git stays quiet")
    else:
        print("  CHANGED. Commit this file: it is what makes the irreplaceable")
        print("  rows survive a disk failure. Nothing else will do it for you.")
    return 0


def cmd_import(args) -> int:
    path = Path(args.file) if args.file else export_mod.DEFAULT_EXPORT_DIR / export_mod.EXPORT_NAME
    if not path.exists():
        raise MacroWireError(f"no export at {path}")
    conn = db.connect()
    db.initialise(conn)
    result = export_mod.load(conn, path)
    conn.close()
    a, p2 = result["added"], result["already_present"]
    print(f"import: {path}")
    print(f"  added          : {a['item']} item(s), {a['observation']} observation(s)")
    print(f"  already present: {p2['item']} item(s), {p2['observation']} observation(s)")
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    from .web import port as portlib

    settings = load_web_settings()
    host = args.host or settings["host"]
    want = args.port or settings["port"]

    # Bind the configured port or fail. No fallback: a server that quietly
    # moves is a server you lose track of, and then kill the wrong thing.
    if not portlib.is_free(want, host):
        held = portlib.holder(want) or {}
        print(f"port {want} is already in use.", file=sys.stderr)
        if held.get("pid"):
            print(f"  held by pid {held['pid']}: {held['cmdline']}", file=sys.stderr)
            if held.get("is_macrowire"):
                print(f"  stop it with:  python -m macrowire stop --port {want}",
                      file=sys.stderr)
            else:
                print("  that is not a macrowire server - stop it yourself, or use "
                      "--port for a one-off.", file=sys.stderr)
        else:
            print("  held by a process this user cannot inspect.", file=sys.stderr)
        return 1

    conn = db.connect()
    db.initialise(conn)
    conn.close()
    print(f"MacroWire on http://{host}:{want}  ({host} only, no auth)")
    print(f"  stop with:  python -m macrowire stop")
    uvicorn.run("macrowire.web.app:app", host=host, port=want,
                log_level="warning", reload=False)
    return 0


def cmd_stop(args) -> int:
    """Stop the server by PORT, never by command-line pattern.

    `pkill -f uvicorn` matches the shell running it and anything else that
    mentions the string, which is how you kill your own terminal while the
    server survives.
    """
    from .web import port as portlib

    want = args.port or load_web_settings()["port"]
    result = portlib.stop(want)
    if result["stopped"]:
        print(f"stopped pid {result['pid']} on port {want} ({result['signal']})")
        return 0
    print(f"not stopped: {result['reason']}", file=sys.stderr)
    if result.get("cmdline"):
        print(f"  pid {result['pid']}: {result['cmdline']}", file=sys.stderr)
    # Nothing listening is the state the caller wanted, not a failure.
    return 0 if "nothing is listening" in result["reason"] else 1


def cmd_status(args) -> int:
    conn = db.connect()
    db.initialise(conn)
    rows = wire.all_status(conn)

    for row in rows:
        flag = "  [STALE]" if row["stale"] else ""
        print(f"{row['name']}  ({row['kind']}){flag}")
        print(f"  last successful fetch : {_duration(row['seconds_since_success'])}"
              f"  {row['last_success'] or ''}")

        days = row["days_since_new_item"]
        age = f"{days:.1f} days ago" if days is not None else "never"
        print(f"  last new item stored  : {age}  {row['last_new_item'] or ''}")
        print(f"  latest content date   : {row['latest_content_at'] or '-'}")

        counts = f"{row['rows']} rows"
        if row["item_rows"] and row["observation_rows"]:
            counts += f" ({row['item_rows']} items, {row['observation_rows']} observations)"
        elif row["observation_rows"]:
            counts += " (observations)"
        elif row["item_rows"]:
            counts += " (items)"
        print(f"  stored                : {counts}, {row['raw_rows']} raw payloads")

        risk = {"NO": "our copy is the only one",
                "PARTIAL": "only the live window is re-fetchable",
                "YES": "fully re-fetchable from the source",
                "?": "unclassified"}[row["replaceable"]]
        line = f"  re-fetchable          : {row['replaceable']:<8} ({row['archive']}) - {risk}"
        if row["at_risk"]:
            line += f"\n  rows that exist ONLY here : {row['at_risk']:,}"
        print(line)

        if row["consecutive_failures"]:
            print(f"  consecutive failures  : {row['consecutive_failures']} "
                  f"since last success  [{', '.join(row['failure_kinds'])}]")

        threshold = row["staleness_days"]
        print(f"  staleness threshold   : "
              f"{str(threshold) + ' days' if threshold is not None else 'off'}"
              f"   (information only - never an error)")
        if row["revision_chains"]:
            print(f"  superseded versions   : {row['superseded_items']} item(s) "
                  f"across {row['revision_chains']} revision chain(s)")
        if row["revisions"]:
            print(f"  value revisions       : {row['revisions']}")
        if row["last_error"]:
            print(f"  last error            : {row['last_error']['timestamp']}  "
                  f"{row['last_error']['error']}")
        print()

    conn.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="macrowire", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="run one poll cycle")
    fetch.add_argument("--source", action="append", help="limit to named source(s)")
    fetch.set_defaults(func=cmd_fetch)

    seed = subparsers.add_parser("backfill", help="one-off historical seed for an API source")
    seed.add_argument("--source", required=True, help="source name from sources.yaml")
    seed.add_argument("--dry-run", action="store_true", help="show the plan, make no requests")
    seed.set_defaults(func=cmd_backfill)

    bk = subparsers.add_parser("backup", help="verified timestamped backup")
    bk.add_argument("--keep", type=int, default=7, help="how many backups to retain")
    bk.add_argument("--list", action="store_true", help="list existing backups")
    bk.set_defaults(func=cmd_backup)

    rs = subparsers.add_parser("restore", help="restore from a backup")
    rs.add_argument("--backup", help="path to a backup (default: newest)")
    rs.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    rs.set_defaults(func=cmd_restore)

    mg = subparsers.add_parser("migrate", help="apply pending schema migrations")
    mg.set_defaults(func=cmd_migrate)

    ex = subparsers.add_parser(
        "export", help="dump irreplaceable rows to a committable text file")
    ex.set_defaults(func=cmd_export)

    im = subparsers.add_parser("import", help="load an export back into the database")
    im.add_argument("--file", help="path to the export (default: export/irreplaceable.jsonl)")
    im.set_defaults(func=cmd_import)

    sv = subparsers.add_parser("serve", help="run the local web interface")
    sv.add_argument("--host", default=None, help="override sources.yaml web.host")
    sv.add_argument("--port", type=int, default=None, help="override sources.yaml web.port")
    sv.set_defaults(func=cmd_serve)

    st = subparsers.add_parser("stop", help="stop the server (resolved by port, not by name)")
    st.add_argument("--port", type=int, default=None, help="default: sources.yaml web.port")
    st.set_defaults(func=cmd_stop)

    status = subparsers.add_parser("status", help="per-source health")
    status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
