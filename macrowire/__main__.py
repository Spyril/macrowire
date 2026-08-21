"""CLI.

    python -m macrowire fetch    one poll cycle
    python -m macrowire status   per-source health, as information
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import backfill, backup as backup_mod, db, export as export_mod, migrations, watchlist as wl, wire
from .config import (load_backup_settings, load_export_settings, load_sources,
                     load_web_settings)
from .errors import ConfigError, MacroWireError


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

    _first_run_notice(conn, sources)
    results, failures = wire.fetch_all(conn, sources)

    for result in results:
        if result["skipped"]:
            # Two different skips, and conflating them is what produced a
            # false alarm: one reached the source, the other never tried.
            if result.get("kind") == "no_change":
                print(f"  {result['source']:<24} no change ({result['reason']})")
            else:
                print(f"  {result['source']:<24} throttled "
                      f"(not contacted; {result['wait_seconds']}s until next allowed)")
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
    _maybe_export(conn, sources, results)
    _maybe_backup(conn, stored_something)
    conn.close()

    if failures:
        print(f"\n{len(failures)} source(s) failed. Logged to fetch_log.", file=sys.stderr)
        if len(failures) == 1:
            raise failures[0]
        raise ExceptionGroup("one or more sources failed", failures)
    return 0


def _source_zone(source):
    """A source's own timezone, accepting a fixed offset or an IANA name."""
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo

    declared = source.config.get("timezone", "+00:00")
    if declared.startswith(("+", "-")):
        try:
            return _dt.fromisoformat(f"2000-01-01T00:00:00{declared}").tzinfo
        except ValueError:
            return _tz.utc
    try:
        return ZoneInfo(declared)
    except Exception:
        return _tz.utc


def cmd_backfill(args) -> int:
    from datetime import datetime, timezone, timedelta

    sources = {s.name: s for s in load_sources()}
    source = sources.get(args.source)
    if source is None:
        raise MacroWireError(
            f"unknown source {args.source!r}. Known: {', '.join(sorted(sources))}"
        )
    if not (source.config.get("backfill_start") or source.config.get("backfill_url")
            or source.config.get("backfill_page_size")):
        raise MacroWireError(
            f"{source.name} has no backfill_start, backfill_url or backfill_page_size in "
            f"sources.yaml - this source has no retrievable history"
        )

    conn = db.connect()
    db.initialise(conn)
    # The API dates in the source's own timezone, not ours. That may be a
    # fixed offset ("+08:00", as CFETS declares) or an IANA name
    # ("Europe/Berlin", which the ECB needs because CET observes DST).
    today = datetime.now(_source_zone(source)).date()

    print(f"backfill: {source.name}")
    result = backfill.run(conn, source, today, dry_run=args.dry_run)
    if not result["dry_run"]:
        print(f"\n  {result['requests']} request(s) made, "
              f"{result['stored']} observation(s) stored, "
              f"{result['skipped']} page(s) already had.")
    conn.close()
    return 0


def _maybe_export(conn, sources, results) -> None:
    """Write the irreplaceable rows out when any of them changed.

    Writes a file. Does not commit it, push it, or touch a credential - the
    tool's job ends at the path, and where that path goes is the user's
    arrangement. The export is deterministic, so an unchanged database
    produces a byte-identical file and nothing is rewritten.
    """
    protected = {s.name for s in sources if s.archive == "none"}
    touched = any(
        r["source"] in protected and not r["skipped"]
        and (r.get("new_items") or 0) + (r.get("new_observations") or 0)
        for r in results
    )
    if not touched:
        return
    try:
        settings = load_export_settings()
    except Exception as exc:
        print(f"  {'export':<24} FAILED   ({exc})", file=sys.stderr)
        return
    if not settings["auto"]:
        return
    try:
        result = export_mod.write(conn, sources, settings["path"])
    except Exception as exc:
        print(f"  {'export':<24} FAILED   ({exc})", file=sys.stderr)
        return
    if result["unchanged"]:
        return
    where = "off this disk" if settings["external"] else "on this disk"
    print(f"  {'export':<24} ok       ({result['counts']['item']} item(s), "
          f"{result['counts']['observation']} observation(s) -> "
          f"{settings['path']}, {where})")


def _maybe_backup(conn, stored_something: bool) -> None:
    """Take an automatic backup if the cycle produced anything worth keeping.

    Deliberately best-effort: a backup problem must not fail a fetch that
    already succeeded and already wrote its data.
    """
    settings = load_backup_settings()
    if not settings["enabled"] or not stored_something:
        return
    path = db.db_path()
    found = backup_mod.existing(path, settings["path"])
    if found:
        newest = found[-1].stat().st_mtime
        if (time.time() - newest) < settings["interval_seconds"]:
            return
    try:
        result = backup_mod.create(conn, path, keep=settings["keep"],
                                   directory=settings["path"])
        print(f"  {'backup':<24} ok       ({result['path'].name}, "
              f"{result['bytes']/1024/1024:.1f} MB, verified)")
    except Exception as exc:
        print(f"  {'backup':<24} FAILED   ({exc})", file=sys.stderr)


def cmd_backup(args) -> int:
    conn = db.connect()
    db.initialise(conn)
    path = db.db_path()
    settings = load_backup_settings()

    if args.list:
        found = backup_mod.existing(path, settings["path"])
        if not found:
            print("no backups yet")
        for f in found:
            print(f"  {f.name:<40} {f.stat().st_size/1024/1024:>7.2f} MB")
        conn.close()
        return 0

    result = backup_mod.create(conn, path, keep=args.keep, directory=settings["path"])
    conn.close()
    print(f"backup verified: {result['path']}")
    if not settings["external"]:
        print(f"  same disk as the database - protects against a mistake, not a "
              f"drive failure.")
        print(f"  Set backup.path in sources.yaml to move them off it.")
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
        found = backup_mod.existing(path, load_backup_settings()["path"])
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
    sources = load_sources()
    settings = load_export_settings()
    result = export_mod.write(conn, sources, settings["path"], force=args.force)
    conn.close()

    c = result["counts"]
    print(f"export: {result['path']}")
    print(f"  {c['source']} source(s), {c['item']} item(s), "
          f"{c['observation']} observation(s), {result['bytes']:,} bytes")
    if result["unchanged"]:
        print("  unchanged - file not rewritten")

    # Measure where it landed rather than warning regardless.
    if settings["external"]:
        print(f"  written outside the project to {settings['path']} - "
              f"a drive failure costs nothing")
    else:
        print(f"  written to {settings['path']}, the same disk as the database.")
        print(f"  Set export.path in sources.yaml to a synced folder and these "
              f"rows are backed up automatically.")
        if _repo_present():
            # Only because a repo is actually here. Never the default advice.
            print(f"  (this directory is a git repo, so committing the file "
                  f"is one way to get it off the disk)")
    return 0


def _repo_present() -> bool:
    return (db.REPO_ROOT / ".git").exists()


def _first_run_notice(conn, sources) -> None:
    """Shown once, when the database has collected nothing yet.

    Proportionate on purpose. "Back everything up" is advice people ignore,
    because most of what is here can be re-fetched by waiting. Naming the
    small part that genuinely cannot is what makes the sentence worth
    reading.
    """
    collected = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    collected += conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    if collected:
        return

    fragile = [s.name for s in sources if s.archive == "none"]
    try:
        settings = load_export_settings()
        export_dir, external = settings["path"], settings["external"]
    except Exception:
        export_dir, external = db.REPO_ROOT / "export", False

    print()
    data_dir = db.db_path().parent.resolve()
    print(f"  Your data lives in {data_dir}")
    try:
        data_dir.relative_to(db.REPO_ROOT)
        print(f"  That directory is gitignored: cloning this repo gives you the")
        print(f"  code, not the data. Every install builds its own history by polling.")
    except ValueError:
        print(f"  That is outside the project directory, so it is not affected by")
        print(f"  anything the repository does. Every install builds its own history.")
    print()
    print(f"  Most of what accumulates is re-fetchable - SEC, NBS, CFETS, ECB and")
    print(f"  the news feeds all serve their recent history on request, so losing")
    print(f"  the database costs polling time rather than data.")
    if fragile:
        print(f"  The exception is {', '.join(fragile)}: those feeds carry one item")
        print(f"  and no archive, so what you collect is the only copy in existence.")
        if external:
            print(f"  Those rows export automatically to {export_dir}.")
        else:
            print(f"  Set export.path in sources.yaml to a synced folder and your")
            print(f"  irreplaceable rows are backed up automatically.")
    print()


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
        elif held:
            print("  held by a process this user cannot inspect.", file=sys.stderr)
        else:
            # Not bindable, yet nothing is LISTENing: a socket still closing.
            print("  nothing is listening on it - the socket is probably still",
                  file=sys.stderr)
            print("  closing from a previous run. Try again in a moment.",
                  file=sys.stderr)
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


def _sec_fetch(url: str) -> bytes:
    """Download for watchlist validation, using SEC's required UA form."""
    import httpx
    from .errors import ConfigError, MacroWireError
    sources = {s.name: s for s in load_sources()}
    contact = (sources.get("sec_edgar").config.get("sec_contact")
               if "sec_edgar" in sources else None)
    if not contact:
        raise MacroWireError(
            "SEC_CONTACT is not set. The SEC requires a User-Agent of the form "
            "'Name email' and enforces it with a 403.")
    response = httpx.get(url, headers={"User-Agent": contact,
                                       "Accept-Encoding": "gzip, deflate"},
                         timeout=60, follow_redirects=True)
    response.raise_for_status()
    return response.content


def cmd_watchlist(args) -> int:
    conn = db.connect()
    db.initialise(conn)
    user = db.LOCAL_USER_ID

    if args.action == "refresh":
        cik = wl.load_cik_map(fetch=_sec_fetch, force=True)
        print(f"SEC ticker map refreshed: {len(cik):,} tickers -> {wl.CIK_CACHE}")
        conn.close()
        return 0

    if args.action == "list":
        rows = wl.entries(conn, user)
        if not rows:
            print("watchlist is empty")
            print("  add one with:  python -m macrowire watchlist add AAPL")
            print("  Company filings poll nothing until you do.")
            print("  Only US tickers are collected today, via SEC EDGAR, and they are")
            print("  validated against its ticker map on add. Other markets can be")
            print("  recorded with --market but nothing polls them yet: ASX and HKEX")
            print("  both prohibit automated access, so neither is a source.")
        for r in rows:
            print(f"  {r['market']:<4} {r['ticker']}")
        conn.close()
        return 0

    if args.action == "add":
        cik = None
        if args.market.upper() == "US":
            age = wl.cik_cache_age_days()
            cik = wl.load_cik_map(
                fetch=_sec_fetch if (age is None or age > wl.CIK_MAX_AGE_DAYS) else None)
        added = wl.add(conn, user, args.ticker, args.market, cik_map=cik)
        label = f" — {added['name']} (CIK {added['cik']:010d})" if added["cik"] else ""
        print(f"added {added['market']} {added['ticker']}{label}")
        conn.close()
        return 0

    if args.action == "remove":
        n = wl.remove(conn, user, args.ticker, args.market)
        print(f"removed {n} entry(ies) for {args.ticker.upper()}"
              if n else f"{args.ticker.upper()} was not on the watchlist")
        conn.close()
        return 0 if n else 1

    conn.close()
    return 1


def cmd_status(args) -> int:
    conn = db.connect()
    db.initialise(conn)
    rows = wire.all_status(conn)

    for row in rows:
        flag = "  [STALE]" if row["stale"] else ""
        print(f"{row['name']}  ({row['kind']}){flag}")

        # Contact, not merely a store. A gated source that finds nothing new
        # HAS reached its source, and reporting that as "never" was a lie
        # about a healthy feed.
        contact = _duration(row["seconds_since_contact"])
        detail = ""
        if row["last_contact"] is None:
            detail = "  (no contact logged)"
        elif row["seconds_since_success"] is None:
            detail = "  (contacted; nothing new, so no store logged)"
        print(f"  last contact          : {contact}  {row['last_contact'] or ''}{detail}")

        if row["log_incomplete"]:
            # The data is ahead of the log. Say so plainly rather than
            # implying a failure that did not happen.
            print(f"  {'':22}  DATA CURRENT, LOG INCOMPLETE \u2014 stored "
                  f"{_duration(row['seconds_since_stored'])}, newer than any logged")
            print(f"  {'':22}  contact. A restore from a backup predating that "
                  f"fetch is the usual cause.")

        stored = _duration(row["seconds_since_stored"])
        print(f"  last stored new       : {stored}  {row['last_stored_at'] or ''}")

        days = row["days_since_content"]
        age = f"{days:.1f} days ago" if days is not None else "no content"
        print(f"  newest content        : {age}  {row['latest_content_at'] or '-'}")

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
                  f"since the last good cycle  [{', '.join(row['failure_kinds'])}]")

        # Drift is the maintenance cost of a vocabulary, so it is reported
        # rather than left to be discovered by noticing an absence.
        fx = row["fx_counts"]
        if not row["fx_has_vocabulary"]:
            print(f"  fx classification     : none - every item unclassified")
        elif row["fx_unclassified_pct"] is not None:
            line = (f"  fx classification     : {fx['fx']} fx / {fx['not_fx']} not / "
                    f"{fx['unclassified']} unclassified "
                    f"({row['fx_unclassified_pct']:.0f}%)")
            if row["fx_drift"]:
                line += (f"\n  {'':22}  DRIFT: {row['fx_recent_unclassified_pct']:.0f}% "
                         f"unclassified in the last 30d vs "
                         f"{row['fx_older_unclassified_pct']:.0f}% before - the source "
                         f"may have renamed something the vocabulary matches on.")
            print(line)

        threshold = row["staleness_days"]
        print(f"  staleness threshold   : "
              f"{str(threshold) + ' days' if threshold is not None else 'off'}"
              f"   (measured on published content - information only)")
        if row["revision_chains"]:
            print(f"  superseded versions   : {row['superseded_items']} item(s) "
                  f"across {row['revision_chains']} revision chain(s)")
        if row["revisions"]:
            print(f"  value revisions       : {row['revisions']}")
        if row["last_error"]:
            when = row["last_error"]["timestamp"]
            if row["last_error"].get("resolved"):
                print(f"  last error            : {when}  (RESOLVED - a successful "
                      f"contact followed)")
            else:
                print(f"  last error            : {when}  {row['last_error']['error']}")
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
        "export", help="write irreplaceable rows to export.path")
    ex.add_argument("--force", action="store_true",
                    help="overwrite even if this database has fewer rows than the file")
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

    wlp = subparsers.add_parser("watchlist", help="tickers to follow for company filings")
    wsub = wlp.add_subparsers(dest="action", required=True)
    wl_add = wsub.add_parser("add", help="add a ticker (validated against the SEC map for US)")
    wl_add.add_argument("ticker")
    wl_add.add_argument("--market", default="US", help="US, AU, HK ... (default US)")
    wl_rm = wsub.add_parser("remove", help="remove a ticker")
    wl_rm.add_argument("ticker")
    wl_rm.add_argument("--market", default=None)
    wsub.add_parser("list", help="show the watchlist")
    wsub.add_parser("refresh", help="re-download the SEC ticker map")
    wlp.set_defaults(func=cmd_watchlist)

    status = subparsers.add_parser("status", help="per-source health")
    status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        # A configuration or input problem is not a system fault, and a
        # traceback for a mistyped ticker is noise. Everything else keeps
        # its traceback - that is the project's stance and it stands.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
