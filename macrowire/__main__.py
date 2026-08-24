"""CLI.

    python -m macrowire fetch    one poll cycle
    python -m macrowire status   per-source health, as information
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
import time
from pathlib import Path

from . import (backfill, backup as backup_mod, db, export as export_mod, i18n,
               migrations, watchlist as wl, wire)
from .config import (load_backup_settings, load_export_settings, load_locale,
                     load_sources, load_web_settings)
from .errors import BackfillInterrupted, ConfigError, MacroWireError

def _cli_locale() -> str:
    """The stored preference, or the config value.

    Bound once at import, which is safe in a CLI and only in a CLI: the
    process reads and exits, so there is no window for the value to change
    underneath it. A long-running server must not do this - see config.py.

    Defensive because the database may not exist yet: `macrowire --help`
    before a first fetch must not fail on a missing table.
    """
    try:
        conn = db.connect()
    except Exception:
        return load_locale()
    try:
        row = conn.execute(
            "SELECT value FROM preferences WHERE user_id = ? AND key = 'locale'",
            (db.LOCAL_USER_ID,)).fetchone()
        return row[0] if row else load_locale()
    except Exception:
        return load_locale()
    finally:
        conn.close()


t = i18n.Translator(_cli_locale())


def _width(text: str) -> int:
    """Display columns, not characters.

    A CJK glyph occupies two terminal cells. Padding a translated label with
    len() lines the columns up in the string and not on the screen, which is
    the whole point of padding it.
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(width - _width(text), 0)


def _status_col() -> int:
    """Width of the fetch status column, measured rather than assumed.

    It was a literal 8 at ten call sites. `no change` was already 9 and
    无新内容 is 8, so the column was ragged in both locales before
    `within interval` at 15 put a whole row seven columns right of its
    neighbours. Same lesson cmd_status learned for its label column: a
    translated word has no width you can write down.
    """
    return max(_width(t(key)) for key in (
        "cli.fetch.ok", "cli.fetch.no_change", "cli.fetch.throttled",
        "cli.fetch.disabled", "cli.fetch.revised", "cli.backup.failed")) + 1


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return t("time.never")
    seconds = int(seconds)
    if seconds < 60:
        return t("cli.status.seconds_ago", n=seconds)
    if seconds < 3600:
        return t("time.minutes", n=seconds // 60)
    if seconds < 86400:
        return t("cli.status.hours_minutes_ago", h=seconds // 3600,
                 m=(seconds % 3600) // 60)
    return t("cli.status.days_hours_ago", d=seconds // 86400,
             h=(seconds % 86400) // 3600)


def cmd_fetch(args) -> int:
    sources = load_sources()
    conn = db.connect()
    db.initialise(conn)

    if args.source:
        sources = [s for s in sources if s.name in args.source]
        unknown = set(args.source) - {s.name for s in sources}
        if unknown:
            raise MacroWireError(
                t("cli.fetch.unknown_sources", names=", ".join(sorted(unknown))))

    _first_run_notice(conn, sources)
    results, failures = wire.fetch_all(conn, sources)

    for result in results:
        if result["skipped"]:
            # Two different skips, and conflating them is what produced a
            # false alarm: one reached the source, the other never tried.
            if result.get("kind") == "disabled":
                print(f"  {result['source']:<24} {_pad(t('cli.fetch.disabled'), _status_col())} "
                      f"({t('cli.fetch.disabled_detail')})")
            elif result.get("kind") == "no_change":
                print(f"  {result['source']:<24} {_pad(t('cli.fetch.no_change'), _status_col())} "
                      f"({result['reason']})")
            else:
                print(f"  {result['source']:<24} {_pad(t('cli.fetch.throttled'), _status_col())} "
                      f"({t('cli.fetch.throttled_detail', seconds=result['wait_seconds'])})")
            continue
        parts = [t("cli.fetch.entries", n=result["entries"])]
        if result["new_items"]:
            parts.append(t("cli.fetch.new_items", n=result["new_items"]))
        if result["new_observations"]:
            parts.append(t("cli.fetch.new_observations", n=result["new_observations"]))
        if not result["new_items"] and not result["new_observations"]:
            parts.append(t("cli.fetch.nothing_new"))
        print(f"  {result['source']:<24} {_pad(t('cli.fetch.ok'), _status_col())} ({', '.join(parts)})")
        for note in result["revisions"]:
            print(f"  {'':<24} {_pad(t('cli.fetch.revised'), _status_col())} {note}")

    # MEASURE. A cycle that contacted nothing must say so: without this the
    # output of "everything switched off" is indistinguishable from the
    # output of "everything up to date", which is a silence you would read
    # as success for weeks.
    disabled = [r for r in results if r.get("kind") == "disabled"]
    if disabled and len(disabled) == len(sources):
        _wrapped(t("cli.fetch.none_enabled"), indent="")
    elif disabled:
        print(t("cli.fetch.some_disabled", n=len(disabled), total=len(sources)))

    stored_something = any(
        (r.get("new_items") or 0) + (r.get("new_observations") or 0)
        for r in results if not r["skipped"]
    )
    _maybe_export(conn, sources, results)
    _maybe_backup(conn, stored_something)
    conn.close()

    if failures:
        print("\n" + t("cli.fetch.failed", n=len(failures)), file=sys.stderr)
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
            t("cli.errors.unknown_source", name=repr(args.source),
              known=", ".join(sorted(sources)))
        )
    if not (source.config.get("backfill_start") or source.config.get("backfill_url")
            or source.config.get("backfill_page_size")):
        raise MacroWireError(t("cli.backfill.no_history", source=source.name))

    conn = db.connect()
    db.initialise(conn)
    # The API dates in the source's own timezone, not ours. That may be a
    # fixed offset ("+08:00", as CFETS declares) or an IANA name
    # ("Europe/Berlin", which the ECB needs because CET observes DST).
    today = datetime.now(_source_zone(source)).date()

    print(t("cli.backfill.header", source=source.name))
    result = backfill.run(conn, source, today, dry_run=args.dry_run)
    if not result["dry_run"]:
        print("\n  " + t("cli.backfill.done", requests=result["requests"],
                             stored=result["stored"], skipped=result["skipped"]))
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
        print(f"  {'export':<24} {_pad(t('cli.backup.failed'), _status_col())} ({exc})", file=sys.stderr)
        return
    if not settings["auto"]:
        return
    try:
        result = export_mod.write(conn, sources, settings["path"])
    except Exception as exc:
        print(f"  {'export':<24} {_pad(t('cli.backup.failed'), _status_col())} ({exc})", file=sys.stderr)
        return
    if result["unchanged"]:
        return
    where = t("cli.fetch.where_external" if settings["external"] else "cli.fetch.where_local")
    detail = t("cli.fetch.export_ok", items=result["counts"]["item"],
               observations=result["counts"]["observation"],
               path=settings["path"], where=where)
    print(f"  {'export':<24} {_pad(t('cli.fetch.ok'), _status_col())} ({detail})")


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
        detail = t("cli.fetch.backup_ok", name=result["path"].name,
                   mb=f"{result['bytes']/1024/1024:.1f}")
        print(f"  {'backup':<24} {_pad(t('cli.fetch.ok'), _status_col())} ({detail})")
    except Exception as exc:
        print(f"  {'backup':<24} {_pad(t('cli.backup.failed'), _status_col())} ({exc})", file=sys.stderr)


def cmd_backup(args) -> int:
    conn = db.connect()
    db.initialise(conn)
    path = db.db_path()
    settings = load_backup_settings()

    if args.list:
        found = backup_mod.existing(path, settings["path"])
        if not found:
            print(t("cli.backup.none"))
        for f in found:
            print(f"  {f.name:<40} {f.stat().st_size/1024/1024:>7.2f} MB")
        conn.close()
        return 0

    result = backup_mod.create(conn, path, keep=args.keep, directory=settings["path"])
    conn.close()
    print(t("cli.backup.verified", path=result["path"]))
    if not settings["external"]:
        print("  " + t("cli.backup.same_disk"))
        print("  " + t("cli.backup.move_hint"))
    print("  " + t("cli.backup.size", mb=f"{result['bytes']/1024/1024:.2f}"))
    print("  " + t("cli.backup.counts",
                   counts=", ".join(f"{k}={v:,}" for k, v in result["counts"].items() if v > 0)))
    for pruned in result["pruned"]:
        print("  " + t("cli.backup.pruned", name=pruned.name))
    return 0


def cmd_restore(args) -> int:
    path = db.db_path()
    chosen = Path(args.backup) if args.backup else None
    if chosen is None:
        found = backup_mod.existing(path, load_backup_settings()["path"])
        if not found:
            raise MacroWireError(t("cli.restore.none"))
        chosen = found[-1]

    print(t("cli.restore.header", name=chosen.name, path=path))
    print("  " + t("cli.restore.warning"))
    if not args.yes:
        # Some of what is being replaced may be unrecoverable at any price.
        # The word typed back is NOT translated: it is a token compared
        # against a literal, and localising one side of that comparison
        # would lock the confirmation shut in every locale but English.
        word = "restore"
        reply = input("  " + t("cli.restore.prompt", word=word)).strip()
        if reply != word:
            print("  " + t("cli.restore.aborted"))
            return 1

    result = backup_mod.restore(chosen, path)
    print("  " + t("cli.restore.done",
                   counts=", ".join(f"{k}={v:,}" for k, v in result["counts"].items() if v > 0)))
    if result["displaced"]:
        print("  " + t("cli.restore.displaced", name=result["displaced"].name))
    return 0


def cmd_migrate(args) -> int:
    conn = db.connect()
    print(t("cli.migrate.before", version=migrations.version(conn)))
    applied = db.initialise(conn, verbose=True)
    print(t("cli.migrate.after", version=migrations.version(conn)))
    if not applied:
        print("  " + t("cli.migrate.up_to_date"))
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
    print(t("cli.export.header", path=result["path"]))
    print("  " + t("cli.export.counts", sources=c["source"], items=c["item"],
                   observations=c["observation"], bytes=f"{result['bytes']:,}"))
    if result["unchanged"]:
        print("  " + t("cli.export.unchanged"))

    # Measure where it landed rather than warning regardless.
    if settings["external"]:
        print("  " + t("cli.export.external", path=settings["path"]))
    else:
        print("  " + t("cli.export.local", path=settings["path"]))
        print("  " + t("cli.export.local_hint"))
        if _repo_present():
            # Only because a repo is actually here. Never the default advice.
            print("  " + t("cli.export.repo_hint"))
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
    _wrapped(t("cli.first_run.data_location", path=data_dir))
    try:
        data_dir.relative_to(db.REPO_ROOT)
        _wrapped(t("cli.first_run.gitignored"))
    except ValueError:
        _wrapped(t("cli.first_run.outside"))
    print()
    _wrapped(t("cli.first_run.refetchable"))
    if fragile:
        _wrapped(t("cli.first_run.irreplaceable", sources=", ".join(fragile)))
        if external:
            _wrapped(t("cli.first_run.export_auto", path=export_dir))
        else:
            _wrapped(t("cli.first_run.export_hint"))
    print()


def _wrapped(text: str, width: int = 76, indent: str = "  ", file=None) -> None:
    """Print a paragraph, wrapped here rather than hard-wrapped in the file.

    The English was laid out by hand across several print() calls. A
    translation has different line lengths, so the line breaks have to be
    computed from the text that is actually being shown.
    """
    line = indent
    for word in text.split():
        if _width(line) + _width(word) + 1 > width and line.strip():
            print(line.rstrip(), file=file)
            line = indent
        line += word + " "
    if line.strip():
        print(line.rstrip(), file=file)


def cmd_import(args) -> int:
    path = Path(args.file) if args.file else export_mod.DEFAULT_EXPORT_DIR / export_mod.EXPORT_NAME
    if not path.exists():
        raise MacroWireError(t("cli.import.missing", path=path))
    conn = db.connect()
    db.initialise(conn)
    result = export_mod.load(conn, path)
    conn.close()
    a, p2 = result["added"], result["already_present"]
    print(t("cli.import.header", path=path))
    print("  " + t("cli.import.added", items=a["item"], observations=a["observation"]))
    print("  " + t("cli.import.present", items=p2["item"], observations=p2["observation"]))
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
        print(t("cli.serve.port_in_use", port=want), file=sys.stderr)
        if held.get("pid"):
            print("  " + t("cli.serve.held_by", pid=held["pid"], cmdline=held["cmdline"]),
                  file=sys.stderr)
            if held.get("is_macrowire"):
                print("  " + t("cli.serve.stop_it", port=want), file=sys.stderr)
            else:
                print("  " + t("cli.serve.not_ours"), file=sys.stderr)
        elif held:
            print("  " + t("cli.serve.uninspectable"), file=sys.stderr)
        else:
            # Not bindable, yet nothing is LISTENing: a socket still closing.
            print("  " + t("cli.serve.closing"), file=sys.stderr)
        return 1

    conn = db.connect()
    db.initialise(conn)
    conn.close()
    print(t("cli.serve.running", host=host, port=want))
    print("  " + t("cli.serve.stop_hint"))
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
        print(t("cli.serve.stopped", pid=result["pid"], port=want, signal=result["signal"]))
        return 0

    code = result.get("code", "")
    reason = {
        "not_listening": lambda: t("cli.serve.stop_not_listening", port=want),
        "uninspectable": lambda: t("cli.serve.stop_uninspectable", port=want),
        "self": lambda: t("cli.serve.stop_self"),
        "not_ours": lambda: t("cli.serve.stop_not_ours", pid=result.get("pid"), port=want),
        "survived_sigkill": lambda: t("cli.serve.stop_survived", pid=result.get("pid"),
                                      port=want),
    }.get(code, lambda: result["reason"])()
    print(t("cli.serve.not_stopped", reason=reason), file=sys.stderr)
    if result.get("cmdline"):
        print("  " + t("cli.serve.pid_line", pid=result["pid"], cmdline=result["cmdline"]),
              file=sys.stderr)
    # Nothing listening is the state the caller wanted, not a failure. Decided
    # from the code, never by searching English inside the message.
    return 0 if code == "not_listening" else 1


def _sec_fetch(url: str) -> bytes:
    """Download for watchlist validation, using SEC's required UA form."""
    import httpx
    from .errors import ConfigError, MacroWireError
    sources = {s.name: s for s in load_sources()}
    contact = (sources.get("sec_edgar").config.get("sec_contact")
               if "sec_edgar" in sources else None)
    if not contact:
        raise MacroWireError(t("cli.errors.sec_contact"))
    response = httpx.get(url, headers={"User-Agent": contact,
                                       "Accept-Encoding": "gzip, deflate"},
                         timeout=60, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _cninfo_fetch(url: str, form: dict) -> bytes:
    """Download for CN watchlist validation.

    CNINFO's search is POST-only, so this cannot go through the generic GET
    helper. It carries the project's descriptive User-Agent - CNINFO states
    no rate limit, and silence is not permission.
    """
    import httpx

    sources = {s.name: s for s in load_sources()}
    source = sources.get("cninfo_announcements")
    agent = source.user_agent if source else None
    if not agent:
        raise MacroWireError(
            "cninfo_announcements is not in sources.yaml, so there is no "
            "User-Agent to identify this request with.")
    response = httpx.post(url, data=form,
                          headers={"User-Agent": agent,
                                   "Accept": "application/json"},
                          timeout=60, follow_redirects=True)
    response.raise_for_status()
    return response.content


def cmd_watchlist(args) -> int:
    conn = db.connect()
    db.initialise(conn)
    user = db.LOCAL_USER_ID

    if args.action == "refresh":
        cik = wl.load_cik_map(fetch=_sec_fetch, force=True)
        print(t("cli.watchlist.refreshed", n=f"{len(cik):,}", path=wl.CIK_CACHE))
        conn.close()
        return 0

    if args.action == "list":
        rows = wl.entries(conn, user)
        if not rows:
            print(t("cli.watchlist.empty"))
            print("  " + t("cli.watchlist.empty_hint"))
            _wrapped(t("cli.watchlist.empty_note"))
            _wrapped(t("cli.watchlist.empty_markets_cn"))
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
        added = wl.add(conn, user, args.ticker, args.market, cik_map=cik,
                       cn_fetch=_cninfo_fetch)
        # The company name is what the SEC published. It passes through as a
        # source fact; only the sentence around it is translated.
        # The company name is what the publisher published. It passes through
        # as a source fact - CNINFO's names are Chinese and stay Chinese.
        if added["cik"]:
            line = t("cli.watchlist.added_named", market=added["market"],
                     ticker=added["ticker"], name=added["name"],
                     cik=f"{added['cik']:010d}")
        elif added.get("name"):
            line = t("cli.watchlist.added_name_only", market=added["market"],
                     ticker=added["ticker"], name=added["name"])
        else:
            line = t("cli.watchlist.added", market=added["market"],
                     ticker=added["ticker"])
        print(line)
        conn.close()
        return 0

    if args.action == "remove":
        n = wl.remove(conn, user, args.ticker, args.market)
        print(t("cli.watchlist.removed", n=n, ticker=args.ticker.upper()) if n
              else t("cli.watchlist.not_present", ticker=args.ticker.upper()))
        conn.close()
        return 0 if n else 1

    conn.close()
    return 1


def cmd_prefs(args) -> int:
    """Viewer preferences and which level is answering for each.

    No hidden state: everything the settings panel writes is a row you can
    read here or with SQL, and every one of them can be cleared so
    sources.yaml applies again.
    """
    from . import preferences

    conn = db.connect()
    db.initialise(conn)
    if args.clear:
        removed = preferences.clear(conn, args.clear)
        print(t("cli.prefs.cleared" if removed else "cli.prefs.not_set",
                key=args.clear))
        conn.close()
        return 0

    rows = preferences.effective(conn)
    print(t("cli.prefs.header", user=db.LOCAL_USER_ID))
    width = max(len(k) for k in rows)
    for key, row in rows.items():
        origin = (t("cli.prefs.from_pref", config=row["config_value"] or "unset")
                  if row["source"] == "preference" else t("cli.prefs.from_config"))
        print("  " + t("cli.prefs.row", key=_pad(key, width),
                       value=_pad(row["value"] or "-", 22), source=origin))
    print()
    _wrapped(t("cli.prefs.note"))
    conn.close()
    return 0


def cmd_locales(args) -> int:
    """What languages exist and how complete each one is.

    Discovery is a directory listing, not a list in code: dropping a JSON
    file into macrowire/locales/ is the whole install step. This command
    exists so a contributor can see what is left to translate without
    diffing two JSON files by hand.
    """
    from . import i18n

    active = load_locale()
    print(t("cli.locales.header", path=i18n.LOCALES_DIR))
    reports = [i18n.coverage(loc) for loc in i18n.available()]
    width = max(len(r["locale"]) for r in reports)
    for report in reports:
        flags = ""
        if report["locale"] == i18n.DEFAULT_LOCALE:
            flags += t("cli.locales.source_of_truth")
        if report["locale"] == active:
            flags += t("cli.locales.active")
        print("  " + t("cli.locales.row",
                       locale=_pad(report["locale"], width),
                       name=_pad(report["name"], 14),
                       present=report["present"], total=report["total"],
                       percent=f"{report['percent']:.0f}", flags=flags))

    gaps = [r for r in reports if r["missing"] or r["orphaned"]]
    if not gaps:
        print()
        print("  " + t("cli.locales.complete"))
    for report in gaps:
        if report["missing"]:
            print()
            print("  " + t("cli.locales.missing_head", locale=report["locale"],
                           n=len(report["missing"])))
            shown = report["missing"] if args.all else report["missing"][:12]
            for key in shown:
                print(f"    {key}")
            if len(shown) < len(report["missing"]):
                print("    " + t("cli.locales.more",
                                 n=len(report["missing"]) - len(shown)))
        if report["orphaned"]:
            print()
            print("  " + t("cli.locales.orphaned_head", locale=report["locale"],
                           n=len(report["orphaned"]), default=i18n.DEFAULT_LOCALE))
            for key in (report["orphaned"] if args.all else report["orphaned"][:12]):
                print(f"    {key}")
            _wrapped(t("cli.locales.orphaned_note"), indent="    ")

    print()
    _wrapped(t("cli.locales.partial_note", default=i18n.DEFAULT_LOCALE))
    _wrapped(t("cli.locales.contribute", default=i18n.DEFAULT_LOCALE))
    return 0


def cmd_status(args) -> int:
    conn = db.connect()
    db.initialise(conn)
    rows = wire.all_status(conn)

    # Left-column labels are translated, so the column width is measured
    # from the widest one at runtime instead of being a literal 22.
    LABELS = ("last_contact", "last_stored", "newest_content", "stored",
              "coverage", "refetchable", "consecutive", "fx_label", "staleness",
              "superseded", "revisions", "last_error")
    label = {k: t(f"cli.status.{k}") for k in LABELS}
    col = max(_width(v) for v in label.values()) + 1

    for row in rows:
        flag = "  " + t("cli.status.disabled_flag") if not row["enabled"] else (
            "  " + t("cli.status.stale_flag") if row["stale"] else "")
        print(f"{row['name']}  ({row['kind']}){flag}")

        # Contact, not merely a store. A gated source that finds nothing new
        # HAS reached its source, and reporting that as "never" was a lie
        # about a healthy feed.
        contact = _duration(row["seconds_since_contact"])
        detail = ""
        if row["last_contact"] is None:
            detail = "  " + t("cli.status.no_contact")
        elif row["seconds_since_success"] is None:
            detail = "  " + t("cli.status.contacted_nothing_new")
        print(f"  {_pad(label['last_contact'], col)}: {contact}  "
              f"{row['last_contact'] or ''}{detail}")

        if row["log_incomplete"]:
            # The data is ahead of the log. Say so plainly rather than
            # implying a failure that did not happen.
            _wrapped(t("cli.status.log_incomplete",
                       when=_duration(row["seconds_since_stored"])),
                     indent=" " * (col + 4))

        stored = _duration(row["seconds_since_stored"])
        print(f"  {_pad(label['last_stored'], col)}: {stored}  "
              f"{row['last_stored_at'] or ''}")

        days = row["days_since_content"]
        age = (t("cli.status.days_ago", n=f"{days:.1f}") if days is not None
               else t("cli.status.no_content"))
        print(f"  {_pad(label['newest_content'], col)}: {age}  "
              f"{row['latest_content_at'] or '-'}")

        counts = t("cli.status.rows", n=row["rows"])
        if row["item_rows"] and row["observation_rows"]:
            counts += " " + t("cli.status.rows_split", items=row["item_rows"],
                              observations=row["observation_rows"])
        elif row["observation_rows"]:
            counts += " " + t("cli.status.rows_observations")
        elif row["item_rows"]:
            counts += " " + t("cli.status.rows_items")
        print(f"  {_pad(label['stored'], col)}: "
              + t("cli.status.stored_line", counts=counts,
                  raw=t("cli.status.raw_payloads", n=row["raw_rows"])))

        if row["earliest"]:
            print(f"  {_pad(label['coverage'], col)}: "
                  + t("cli.status.coverage_note", date=row["earliest"],
                      note=t(f"coverage.note_{row['coverage_state']}")))
        risk = t({"NO": "cli.status.risk_no",
                  "PARTIAL": "cli.status.risk_partial",
                  "YES": "cli.status.risk_yes",
                  "?": "cli.status.risk_unknown"}[row["replaceable"]])
        line = (f"  {_pad(label['refetchable'], col)}: {row['replaceable']:<8} "
                f"({row['archive']}) - {risk}")
        if row["at_risk"]:
            line += f"\n  {t('cli.status.only_here')} : {row['at_risk']:,}"
        print(line)

        if row["consecutive_failures"]:
            print(f"  {_pad(label['consecutive'], col)}: "
                  f"{t('cli.status.consecutive_detail', n=row['consecutive_failures'])}"
                  f"  [{', '.join(row['failure_kinds'])}]")
            # A streak made entirely of timeouts and refused connections is
            # a statement about the path, not the feed. Say which it is,
            # rather than letting a count of failures imply a broken source.
            if row["all_failures_are_path"]:
                _wrapped(t("cli.status.unreachable"), indent=" " * (col + 4))
                _wrapped(t("cli.status.unreachable_hint"), indent=" " * (col + 4))

        # Drift is the maintenance cost of a vocabulary, so it is reported
        # rather than left to be discovered by noticing an absence.
        fx = row["fx_counts"]
        if row["fx_unmeasured"]:
            # A declared absence reads differently from an omission, and the
            # difference is the whole point of declaring it.
            _wrapped(t("cli.status.fx_unmeasured", reason=row["fx_unmeasured"]),
                     indent=" " * (col + 4))
        elif not row["fx_has_vocabulary"]:
            print(f"  {_pad(label['fx_label'], col)}: {t('cli.status.fx_none')}")
        elif row["fx_unclassified_pct"] is not None:
            print(f"  {_pad(label['fx_label'], col)}: "
                  + t("cli.status.fx_counts", fx=fx["fx"], not_fx=fx["not_fx"],
                      unclassified=fx["unclassified"],
                      pct=f"{row['fx_unclassified_pct']:.0f}"))
            if row["fx_drift"]:
                _wrapped(t("cli.status.fx_drift",
                           recent=f"{row['fx_recent_unclassified_pct']:.0f}",
                           older=f"{row['fx_older_unclassified_pct']:.0f}"),
                         indent=" " * (col + 4))

        threshold = row["staleness_days"]
        shown = (t("cli.status.staleness_days", n=threshold) if threshold is not None
                 else t("cli.status.staleness_off"))
        print(f"  {_pad(label['staleness'], col)}: {shown}   "
              f"{t('cli.status.staleness_note')}")
        if row["revision_chains"]:
            print(f"  {_pad(label['superseded'], col)}: "
                  + t("cli.status.superseded_detail", items=row["superseded_items"],
                      chains=row["revision_chains"]))
        if row["revisions"]:
            print(f"  {_pad(label['revisions'], col)}: {row['revisions']}")
        if row["last_error"]:
            when = row["last_error"]["timestamp"]
            note = (t("cli.status.error_resolved") if row["last_error"].get("resolved")
                    else row["last_error"]["error"])
            print(f"  {_pad(label['last_error'], col)}: {when}  {note}")
        print()

    conn.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="macrowire",
                                     description=t("cli.help.description"))
    # Global, because the condition it exists for - a recoverable failure
    # printing a wall of stack instead of the one line that matters - is not
    # specific to any subcommand.
    parser.add_argument("--debug", action="store_true",
                        help=t("cli.help.debug"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help=t("cli.help.fetch"))
    fetch.add_argument("--source", action="append", help=t("cli.help.fetch_source"))
    fetch.set_defaults(func=cmd_fetch)

    seed = subparsers.add_parser("backfill", help=t("cli.help.backfill"))
    seed.add_argument("--source", required=True, help=t("cli.help.backfill_source"))
    seed.add_argument("--dry-run", action="store_true", help=t("cli.help.backfill_dry"))
    seed.set_defaults(func=cmd_backfill)

    bk = subparsers.add_parser("backup", help=t("cli.help.backup"))
    bk.add_argument("--keep", type=int, default=7, help=t("cli.help.backup_keep"))
    bk.add_argument("--list", action="store_true", help=t("cli.help.backup_list"))
    bk.set_defaults(func=cmd_backup)

    rs = subparsers.add_parser("restore", help=t("cli.help.restore"))
    rs.add_argument("--backup", help=t("cli.help.restore_backup"))
    rs.add_argument("--yes", action="store_true", help=t("cli.help.restore_yes"))
    rs.set_defaults(func=cmd_restore)

    mg = subparsers.add_parser("migrate", help=t("cli.help.migrate"))
    mg.set_defaults(func=cmd_migrate)

    ex = subparsers.add_parser(
        "export", help=t("cli.help.export"))
    ex.add_argument("--force", action="store_true",
                    help=t("cli.help.export_force"))
    ex.set_defaults(func=cmd_export)

    im = subparsers.add_parser("import", help=t("cli.help.import"))
    im.add_argument("--file", help=t("cli.help.import_file"))
    im.set_defaults(func=cmd_import)

    sv = subparsers.add_parser("serve", help=t("cli.help.serve"))
    sv.add_argument("--host", default=None, help=t("cli.help.serve_host"))
    sv.add_argument("--port", type=int, default=None, help=t("cli.help.serve_port"))
    sv.set_defaults(func=cmd_serve)

    st = subparsers.add_parser("stop", help=t("cli.help.stop"))
    st.add_argument("--port", type=int, default=None, help=t("cli.help.stop_port"))
    st.set_defaults(func=cmd_stop)

    wlp = subparsers.add_parser("watchlist", help=t("cli.help.watchlist"))
    wsub = wlp.add_subparsers(dest="action", required=True)
    wl_add = wsub.add_parser("add", help=t("cli.help.watchlist_add"))
    wl_add.add_argument("ticker")
    wl_add.add_argument("--market", default="US", help=t("cli.help.watchlist_market"))
    wl_rm = wsub.add_parser("remove", help=t("cli.help.watchlist_remove"))
    wl_rm.add_argument("ticker")
    wl_rm.add_argument("--market", default=None)
    wsub.add_parser("list", help=t("cli.help.watchlist_list"))
    wsub.add_parser("refresh", help=t("cli.help.watchlist_refresh"))
    wlp.set_defaults(func=cmd_watchlist)

    pref = subparsers.add_parser("prefs", help=t("cli.help.prefs"))
    pref.add_argument("--clear", metavar="KEY", help=t("cli.help.prefs_clear"))
    pref.set_defaults(func=cmd_prefs)

    loc = subparsers.add_parser("locales", help=t("cli.help.locales"))
    loc.add_argument("--all", action="store_true", help=t("cli.help.locales_all"))
    loc.set_defaults(func=cmd_locales)

    status = subparsers.add_parser("status", help=t("cli.help.status"))
    status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BackfillInterrupted as exc:
        # A twenty-minute paced run meeting a network blip is an expected
        # condition with a known remedy. The remedy is the output; the
        # traceback is available but is not what the operator needs to read
        # first. It is in fetch_log either way.
        if args.debug:
            raise
        # Progress went to stdout and this goes to stderr. When either is a
        # pipe they are buffered independently, so without this the summary
        # lands above the run it summarises.
        sys.stdout.flush()
        kind = getattr(exc.cause, "kind", "unknown")
        described = {
            "network": t("cli.errors.kind_network"),
            "timeout": t("cli.errors.kind_timeout"),
        }.get(kind, t("cli.errors.kind_other", kind=kind))
        print(t("cli.backfill.interrupted", source=exc.source, kind=described,
                date=exc.reached, remaining=exc.remaining), file=sys.stderr)
        _wrapped(t("cli.backfill.kept"), indent="  ", file=sys.stderr)
        print("  " + t("cli.backfill.resume",
                       command=f"python -m macrowire backfill --source {exc.source}"),
              file=sys.stderr)
        print("  " + t("cli.backfill.debug_hint"), file=sys.stderr)
        return 1
    except ConfigError as exc:
        # A configuration or input problem is not a system fault, and a
        # traceback for a mistyped ticker is noise. Everything else keeps
        # its traceback - that is the project's stance and it stands.
        print(t("cli.errors.prefix", message=exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
