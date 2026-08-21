"""Regression suite. Uses stdlib unittest - no new dependency.

    python -m unittest discover -s tests -v

Every test writes to a temporary database. MACROWIRE_REFUSE_DEFAULT_DB is
set before macrowire is imported, so a test that forgets to pass a path
fails loudly rather than writing fabricated rows into collected history.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["MACROWIRE_REFUSE_DEFAULT_DB"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrowire import backup, db, export, wire                    # noqa: E402
from macrowire.config import load_sources                         # noqa: E402
from macrowire.encoding import decode                             # noqa: E402
from macrowire.errors import (                                    # noqa: E402
    ConfigError, DecodeError, EmptyFeedError, MacroWireError, MalformedEntryError,
    ParseError,
)
from macrowire.parsers import get_parser                          # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCES = {s.name: s for s in load_sources()}


def fixture(name):
    return (FIXTURES / name).read_bytes()


class TempDB(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._dir.name) / "test.db")
        db.initialise(self.conn)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()


class TestLiveDBProtection(unittest.TestCase):
    def test_default_connect_refused(self):
        """The guard that stops this suite touching data/macrowire.db."""
        with self.assertRaises(MacroWireError):
            db.connect()


class TestDecoding(unittest.TestCase):
    def test_strict_decode_rejects_wrong_encoding(self):
        with self.assertRaises(DecodeError):
            decode("t", "国家统计局".encode("gb18030"), "text/html")

    def test_rejects_replacement_characters(self):
        with self.assertRaises(DecodeError):
            decode("t", "abc�".encode("utf-8"), "text/html")

    def test_uses_document_declaration_over_header(self):
        body = '<?xml version="1.0" encoding="gb18030"?>'.encode() + "国家".encode("gb18030")
        text, enc = decode("t", body, "text/xml; charset=utf-8")
        self.assertEqual(enc, "gb18030")
        self.assertTrue(text.endswith("国家"))


class TestFeedGate(unittest.TestCase):
    """The defect that let 313 mojibake entries through."""

    def setUp(self):
        self.src = SOURCES["ecb_press"]
        self.body = fixture("ecb_press.xml").decode("utf-8")

    def test_good_feed_parses(self):
        self.assertEqual(len(get_parser("rss_news")(self.src, self.body).items), 15)

    def test_bozo_with_entries_is_fatal(self):
        truncated = self.body[: int(len(self.body) * 0.6)]
        with self.assertRaises(ParseError):
            get_parser("rss_news")(self.src, truncated)

    def test_replacement_character_in_feed_is_fatal(self):
        with self.assertRaises(ParseError):
            get_parser("rss_news")(self.src, self.body.replace("ECB", "E�CB", 1))

    def test_html_error_page_is_not_a_feed(self):
        with self.assertRaises(ParseError):
            get_parser("rss_news")(self.src, "<html><body>503</body></html>")

    def test_stale_encoding_declaration_is_rewritten(self):
        lied = '<?xml version="1.0" encoding="gb2312"?>' + self.body.split("?>", 1)[1]
        self.assertEqual(len(get_parser("rss_news")(self.src, lied).items), 15)


class TestRdfCbParser(unittest.TestCase):
    def setUp(self):
        self.src = SOURCES["rba_media_releases"]
        self.body = fixture("rba_media_releases.xml").decode("utf-8")

    def test_parses_and_captures_cb_namespace(self):
        item = get_parser("cb_news")(self.src, self.body).items[0]
        self.assertEqual(item["institution_abbrev"], "RBA")
        self.assertIn("Media-Releases", item["announcement_type"])
        self.assertTrue(item["published_at"].endswith("+00:00"))

    def test_naive_timestamp_refused(self):
        naive = self.body.replace("2026-08-19T19:00:00+10:00", "2026-08-19T19:00:00")
        with self.assertRaises(MalformedEntryError):
            get_parser("cb_news")(self.src, naive)


class TestCfetsAlignment(unittest.TestCase):
    """The failure this source exists to defend against: `values` is a bare
    array ordered by data.head, NOT by the currency parameter sent."""

    def setUp(self):
        self.src = SOURCES["cfets_ccpr"]
        self.payload = json.loads(fixture("cfets_ccpr.json").decode("utf-8"))

    def parse(self, payload):
        return get_parser("cfets_ccpr")(self.src, json.dumps(payload))

    def test_aligns_against_data_head(self):
        obs = {o["series"]: o["value"] for o in self.parse(self.payload).observations
               if o["period"] == self.payload["records"][0]["date"]}
        head = [h for h in self.payload["data"]["head"]
                if h in {p["api_code"] for p in self.src.config["pairs"]}]
        values = [float(v) for v in self.payload["records"][0]["values"]]
        self.assertEqual(obs, dict(zip(head, values)))

    def test_short_values_array_raises(self):
        p = json.loads(json.dumps(self.payload))
        p["records"][0]["values"].pop(2)
        with self.assertRaises(MalformedEntryError):
            self.parse(p)

    def test_missing_head_raises(self):
        p = json.loads(json.dumps(self.payload))
        p["data"].pop("head")
        with self.assertRaises(ParseError):
            self.parse(p)

    def test_requested_pair_absent_from_head_raises(self):
        p = json.loads(json.dumps(self.payload))
        p["data"]["head"] = [h for h in p["data"]["head"] if h != "AUD/CNY"]
        with self.assertRaises(ParseError):
            self.parse(p)

    def test_flag_message_raises(self):
        p = json.loads(json.dumps(self.payload))
        p["data"]["flagMessage"] = "只提供一年历史数据查询及下载"
        with self.assertRaises(ParseError):
            self.parse(p)

    def test_positional_shift_caught_by_bounds(self):
        """A rotated values array must not pass as plausible data.

        Note: sources.yaml happens to list the pairs in data.head order, so
        a request-order bug would NOT manifest with today's config. That is
        luck, not safety - reordering the YAML would expose it. Hence the
        explicit rotation here, and test_alignment_is_independent_of_config
        _order below.
        """
        p = json.loads(json.dumps(self.payload))
        values = p["records"][0]["values"]
        rotated = values[1:] + values[:1]
        p["records"] = [{"date": p["records"][0]["date"], "values": rotated}]
        with self.assertRaises(MalformedEntryError):
            self.parse(p)

    def test_alignment_is_independent_of_config_order(self):
        """Reversing the configured pair order must not change any value.

        This is the property that makes head-alignment correct: the server
        ignores the order we ask in, so the parser must too.
        """
        from dataclasses import replace
        forward = {o["series"]: o["value"] for o in self.parse(self.payload).observations}

        reversed_config = dict(self.src.config)
        reversed_config["pairs"] = list(reversed(self.src.config["pairs"]))
        shuffled_src = replace(self.src, config=reversed_config)
        backward = {
            o["series"]: o["value"]
            for o in get_parser("cfets_ccpr")(shuffled_src, json.dumps(self.payload)).observations
        }
        self.assertEqual(forward, backward)


class TestObservationRevisions(TempDB):
    def setUp(self):
        super().setUp()
        self.src = SOURCES["cfets_ccpr"]
        self.sid = db.upsert_source(self.conn, self.src.name, self.src.kind, self.src.config)
        self.conn.commit()
        self.payload = json.loads(fixture("cfets_ccpr.json").decode("utf-8"))

    def parsed(self, payload=None):
        return get_parser("cfets_ccpr")(self.src, json.dumps(payload or self.payload))

    def test_insert_then_dedupe(self):
        first, _ = wire._store_observations(self.conn, self.src, self.sid, self.parsed())
        again, _ = wire._store_observations(self.conn, self.src, self.sid, self.parsed())
        self.assertGreater(first, 0)
        self.assertEqual(again, 0)

    def test_changed_value_is_reported_not_swallowed(self):
        wire._store_observations(self.conn, self.src, self.sid, self.parsed())
        p = json.loads(json.dumps(self.payload))
        original = p["records"][0]["values"][0]
        p["records"][0]["values"][0] = f"{float(original) + 0.02:.4f}"
        new, revisions = wire._store_observations(self.conn, self.src, self.sid, self.parsed(p))
        self.assertEqual(new, 0)
        self.assertEqual(len(revisions), 1)
        logged = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE status='revision'").fetchone()[0]
        self.assertEqual(logged, 1)


class TestEmptyFeedAlarm(TempDB):
    EMPTY = ('<?xml version="1.0"?><rdf:RDF '
             'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
             'xmlns="http://purl.org/rss/1.0/"><channel rdf:about="x"><title>t</title>'
             '<link>l</link><description>d</description></channel></rdf:RDF>')

    def test_empty_is_unproven_until_a_success_is_recorded(self):
        src = SOURCES["rba_media_releases"]
        self.assertFalse(db.has_parsed_before(self.conn, src.name))
        parsed = get_parser("cb_news")(src, self.EMPTY)
        self.assertEqual(parsed.entry_count, 0)      # parses fine, no alarm yet

        db.log_fetch(self.conn, src.name, status="ok", new_item_count=1)
        self.assertTrue(db.has_parsed_before(self.conn, src.name))

    def test_alarm_fires_once_proven(self):
        src = SOURCES["rba_media_releases"]
        db.log_fetch(self.conn, src.name, status="ok", new_item_count=1)
        parsed = get_parser("cb_news")(src, self.EMPTY)
        if parsed.entry_count == 0 and db.has_parsed_before(self.conn, src.name):
            with self.assertRaises(EmptyFeedError):
                raise EmptyFeedError("zero entries from a proven source")


class TestClassification(unittest.TestCase):
    def test_url_rules_classify_an_unlabelled_feed(self):
        src = SOURCES["ecb_press"]
        parsed = get_parser("rss_news")(src, fixture("ecb_press.xml").decode("utf-8"))
        self.assertTrue(all(i["announcement_type"] is None for i in parsed.items))
        wire.classify(src, parsed)
        kinds = {i["announcement_type"] for i in parsed.items}
        self.assertIn("Press Release", kinds)
        self.assertIn("Speech", kinds)

    def test_feed_category_wins_over_config(self):
        src = SOURCES["fed_press_monetary"]
        parsed = get_parser("rss_news")(src, fixture("fed_press_monetary.xml").decode("utf-8"))
        wire.classify(src, parsed)
        self.assertEqual({i["announcement_type"] for i in parsed.items}, {"Monetary Policy"})


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMigrations(unittest.TestCase):
    """A schema change must never mean deleting the database."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "m.db"

    def tearDown(self):
        self._dir.cleanup()

    def test_fresh_database_reaches_latest_version(self):
        from macrowire import migrations
        conn = db.connect(self.path)
        db.initialise(conn)
        self.assertEqual(migrations.version(conn), max(m[0] for m in migrations.MIGRATIONS))
        conn.close()

    def test_migration_is_idempotent(self):
        conn = db.connect(self.path)
        db.initialise(conn)
        self.assertEqual(db.initialise(conn), [])   # nothing left to apply
        conn.close()

    def test_pre_migration_database_is_retrofitted_not_wiped(self):
        """The incident this exists to prevent: a schema change losing data."""
        from macrowire import migrations
        conn = db.connect(self.path)
        conn.executescript(migrations.BASELINE)     # old-style DB, no schema_version
        conn.execute("INSERT INTO sources (name, kind) VALUES ('legacy', 'rss_news')")
        conn.execute(
            """INSERT INTO items (id, source_id, title, fetched_at)
               VALUES ('abc', 1, 'irreplaceable', '2026-01-01T00:00:00+00:00')""")
        conn.commit()
        self.assertEqual(migrations.version(conn), 0)

        db.initialise(conn)

        self.assertGreaterEqual(migrations.version(conn), 2)
        self.assertEqual(
            conn.execute("SELECT title FROM items WHERE id='abc'").fetchone()[0],
            "irreplaceable")
        cols = [c[1] for c in conn.execute("PRAGMA table_info(fetch_log)")]
        self.assertIn("error_kind", cols)
        conn.close()


class TestBackupRoundTrip(unittest.TestCase):
    """Untested backups are not backups."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "live.db"
        self.conn = db.connect(self.path)
        db.initialise(self.conn)
        db.upsert_source(self.conn, "s", "rss_news", {})
        for n in range(25):
            self.conn.execute(
                """INSERT INTO items (id, source_id, title, fetched_at)
                   VALUES (?, 1, ?, '2026-01-01T00:00:00+00:00')""", (f"id{n}", f"title {n}"))
        db.store_raw_response(self.conn, "s", "http://x", 200, b"<rss/>")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self._dir.cleanup()

    def counts(self, conn):
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in backup.VERIFIED_TABLES}

    def test_backup_verifies_and_round_trips(self):
        original = self.counts(self.conn)
        result = backup.create(self.conn, self.path, keep=3)
        self.assertTrue(result["path"].exists())
        self.assertEqual(result["counts"], original)

        # Lose data the way a bad rm would.
        self.conn.execute("DELETE FROM items")
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
        self.conn.close()

        backup.restore(result["path"], self.path)

        restored = db.connect(self.path)
        self.assertEqual(self.counts(restored), original)
        self.assertEqual(
            restored.execute("SELECT title FROM items WHERE id='id7'").fetchone()[0],
            "title 7")
        self.assertEqual(
            restored.execute("SELECT body FROM raw_responses").fetchone()[0], b"<rss/>")
        restored.close()

    def test_restore_moves_the_old_database_aside(self):
        result = backup.create(self.conn, self.path, keep=3)
        self.conn.close()
        outcome = backup.restore(result["path"], self.path)
        self.assertIsNotNone(outcome["displaced"])
        self.assertTrue(outcome["displaced"].exists())

    def test_corrupt_backup_is_refused(self):
        result = backup.create(self.conn, self.path, keep=3)
        result["path"].write_bytes(b"this is not a database")
        with self.assertRaises(MacroWireError):
            backup.restore(result["path"], self.path)

    def test_keep_prunes_oldest_first(self):
        from datetime import datetime, timezone, timedelta
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for n in range(5):
            backup.create(self.conn, self.path, keep=3, now=base + timedelta(days=n))
        kept = [p.name for p in backup.existing(self.path)]
        self.assertEqual(len(kept), 3)
        self.assertTrue(kept[0].endswith("20260103T000000Z.db"))


class TestIrreplaceability(unittest.TestCase):
    def test_every_source_is_classified(self):
        for src in SOURCES.values():
            self.assertIn(src.archive, {"none", "rolling", "queryable"},
                          f"{src.name} is unclassified")

    def test_archive_none_forbids_raw_pruning(self):
        for src in SOURCES.values():
            if src.archive == "none":
                self.assertIsNone(src.raw_retention_days,
                                  f"{src.name} would prune its only copy")


class TestErrorClassification(unittest.TestCase):
    def test_kinds_are_distinct(self):
        from macrowire.errors import FetchError, DecodeError, ParseError, EmptyFeedError
        self.assertEqual(FetchError("x").kind, "network")
        self.assertEqual(FetchError("x", kind="http_404").kind, "http_404")
        self.assertEqual(DecodeError("x").kind, "decode")
        self.assertEqual(ParseError("x").kind, "parse")
        self.assertEqual(EmptyFeedError("x").kind, "empty")
        self.assertNotEqual(FetchError("x").kind, ParseError("x").kind)


class TestExportRoundTrip(TempDB):
    """Off-machine durability for rows that exist nowhere else."""

    def setUp(self):
        super().setUp()
        self.sources = [s for s in SOURCES.values()]
        self.irreplaceable = [s for s in self.sources if s.archive == "none"]
        self.assertTrue(self.irreplaceable, "no archive:none source to test with")

        # Populate from a real payload for the archive:none news source.
        news = SOURCES["rba_media_releases"]
        sid = db.upsert_source(self.conn, news.name, news.kind, news.config)
        parsed = get_parser("cb_news")(news, fixture("rba_media_releases.xml").decode("utf-8"))
        wire.classify(news, parsed)
        wire._store_items(self.conn, sid, parsed)

        # And a handful of observations for the archive:none rates source.
        rates = SOURCES["rba_exchange_rates"]
        rid = db.upsert_source(self.conn, rates.name, rates.kind, rates.config)
        for n, code in enumerate(("USD", "JPY", "EUR")):
            self.conn.execute(
                """INSERT INTO observations
                   (source_id, series, period, value, unit, fetched_at)
                   VALUES (?, ?, '2026-08-20', ?, 'AUD', '2026-08-21T00:00:00+00:00')""",
                (rid, f"AUD/{code}", 0.5 + n))
        self.conn.commit()

    def test_export_is_byte_identical_on_rerun(self):
        first = export.build(self.conn, self.sources)
        second = export.build(self.conn, self.sources)
        self.assertEqual(first, second)
        self.assertNotIn('"fetched_at":null', first)

    def test_export_contains_only_irreplaceable_sources(self):
        payload = export.build(self.conn, self.sources)
        names = {json.loads(l).get("source") for l in payload.splitlines()}
        names.discard(None)
        self.assertEqual(names, {s.name for s in self.irreplaceable})
        for src in self.sources:
            if src.archive != "none":
                self.assertNotIn(f'"{src.name}"', payload)

    def test_round_trip_into_an_empty_database(self):
        payload = export.build(self.conn, self.sources)
        original_items = self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        original_obs = self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        titles = {r[0] for r in self.conn.execute("SELECT title FROM items")}

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "e.jsonl"
            path.write_text(payload, encoding="utf-8")

            fresh_dir = tempfile.TemporaryDirectory()
            fresh = db.connect(Path(fresh_dir.name) / "fresh.db")
            db.initialise(fresh)
            result = export.load(fresh, path)

            self.assertEqual(result["added"]["item"], original_items)
            self.assertEqual(result["added"]["observation"], original_obs)
            self.assertEqual(
                {r[0] for r in fresh.execute("SELECT title FROM items")}, titles)
            self.assertEqual(
                fresh.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
                original_obs)

            # Re-importing must not duplicate.
            again = export.load(fresh, path)
            self.assertEqual(again["added"]["item"], 0)
            self.assertEqual(again["already_present"]["item"], original_items)

            # And the re-export from the restored DB matches the original.
            self.assertEqual(export.build(fresh, self.sources), payload)
            fresh.close()
            fresh_dir.cleanup()

    def test_import_never_overwrites_a_local_row(self):
        payload = export.build(self.conn, self.sources)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "e.jsonl"
            path.write_text(payload, encoding="utf-8")
            self.conn.execute("UPDATE items SET title = 'locally corrected'")
            self.conn.commit()
            export.load(self.conn, path)
            self.assertEqual(
                self.conn.execute("SELECT title FROM items").fetchone()[0],
                "locally corrected")

    def test_missing_header_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "e.jsonl"
            path.write_text('{"type":"item","source":"x"}\n', encoding="utf-8")
            with self.assertRaises(MacroWireError):
                export.read(path)

    def test_wrong_format_version_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "e.jsonl"
            path.write_text('{"type":"header","format_version":999}\n', encoding="utf-8")
            with self.assertRaises(MacroWireError):
                export.read(path)


class TestRibbonGeometry(unittest.TestCase):
    """The ribbon's whole job is being honest about time."""

    def test_offsets_are_computed_per_instant_not_cached(self):
        """A source that never moves at origin still moves under Sydney."""
        from macrowire.web import ribbon
        from datetime import date
        fed = [s for s in SOURCES.values() if s.name == "fed_press_monetary"][0]
        seen = set()
        for month in (1, 3, 7, 10):
            marks = ribbon.marks_for(date(2026, month, 15), [fed])
            placed = [m for m in marks if m["position"] is not None]
            self.assertEqual(len(placed), 1, f"Fed unplaced in month {month}")
            seen.add(placed[0]["local_time"])
        # 14:00 New York lands at three different Sydney hours across a year.
        self.assertGreaterEqual(len(seen), 2, f"expected DST spread, got {seen}")
        self.assertIn("04:00", seen)

    def test_scattered_and_date_only_sources_get_no_mark(self):
        from macrowire.web import ribbon
        from datetime import date
        marks = {m["source"]: m for m in ribbon.marks_for(date(2026, 7, 15), list(SOURCES.values()))}
        for name, src in SOURCES.items():
            kind = (src.timing or {}).get("class", "scattered")
            if kind in ("scattered", "date_only"):
                self.assertIsNone(marks[name]["position"],
                                  f"{name} is {kind} and must not be given a time position")
                self.assertTrue(marks[name]["reason"])

    def test_hkma_never_gets_a_time(self):
        """The feed stamps every item 00:00 HKT. Inventing a time would lie."""
        from macrowire.web import ribbon
        from datetime import date
        marks = {m["source"]: m for m in ribbon.marks_for(date(2026, 7, 15), list(SOURCES.values()))}
        self.assertIsNone(marks["hkma_press"]["position"])
        self.assertIn("no time of day", marks["hkma_press"]["reason"])

    def test_sessions_crossing_midnight_split_into_two_segments(self):
        from macrowire.web import ribbon
        from datetime import date
        by_key = {s["key"]: s for s in ribbon.sessions_for(date(2026, 7, 15))}
        london = by_key["london"]
        self.assertGreaterEqual(len(london["segments"]), 2)
        self.assertTrue(any(s["continues"] for s in london["segments"]))
        for s in by_key.values():
            for seg in s["segments"]:
                self.assertGreaterEqual(seg["start"], 0.0)
                self.assertLessEqual(seg["end"], 1.0)
                self.assertLess(seg["start"], seg["end"])

    def test_session_hours_match_real_exchanges(self):
        """Tokyo closes 15:30 (extended Nov 2024), and both TYO and HKG break."""
        from macrowire.web import ribbon
        spec = {s["key"]: s for s in ribbon.SESSIONS}
        self.assertEqual(spec["tokyo"]["spans"], [("09:00", "11:30"), ("12:30", "15:30")])
        self.assertEqual(spec["hongkong"]["spans"], [("09:30", "12:00"), ("13:00", "16:00")])
        self.assertEqual(spec["sydney"]["spans"], [("10:00", "16:00")])
        self.assertEqual(spec["newyork"]["spans"], [("09:30", "16:00")])

    def test_sydney_and_tokyo_are_not_identical(self):
        """They looked copy-pasted because a wrong Tokyo close made them so."""
        from macrowire.web import ribbon
        from datetime import date
        for day in (date(2026, 1, 15), date(2026, 7, 15)):
            by_key = {s["key"]: s for s in ribbon.sessions_for(day)}
            syd = [(s["start"], s["end"]) for s in by_key["sydney"]["segments"]]
            tyo = [(s["start"], s["end"]) for s in by_key["tokyo"]["segments"]]
            self.assertNotEqual(syd, tyo, f"SYD and TYO identical on {day}")
            self.assertEqual(len(tyo), 2, "Tokyo must show its lunch break")

    def test_no_duplicate_segments(self):
        from macrowire.web import ribbon
        from datetime import date
        for day in (date(2026, 1, 15), date(2026, 7, 15)):
            for s in ribbon.sessions_for(day):
                spans = [(round(x["start"], 6), round(x["end"], 6)) for x in s["segments"]]
                self.assertEqual(len(spans), len(set(spans)), f"{s['key']} on {day}")


class TestServePort(unittest.TestCase):
    """The port must come from config and must never silently move."""

    def test_port_comes_from_config_not_a_literal(self):
        from macrowire.config import load_web_settings
        settings = load_web_settings()
        self.assertEqual(settings["port"], 8917)
        self.assertEqual(settings["host"], "127.0.0.1")

    def test_cli_default_is_none_so_config_wins(self):
        """--port defaulting to a literal would silently outrank sources.yaml."""
        import re
        main = (Path(__file__).resolve().parent.parent
                / "macrowire/__main__.py").read_text()
        block = main[main.index('subparsers.add_parser("serve"'):]
        block = block[:block.index("set_defaults")]
        for arg in ("--host", "--port"):
            m = re.search(rf'add_argument\("{arg}"[^)]*\)', block)
            self.assertIn("default=None", m.group(0), f"{arg} has a literal default")

    def test_invalid_configured_port_is_rejected(self):
        import yaml
        from macrowire.config import load_web_settings
        from macrowire.errors import ConfigError
        doc = yaml.safe_load(Path("sources.yaml").read_text())
        doc["defaults"]["web"] = {"host": "127.0.0.1", "port": 99999}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.yaml"
            p.write_text(yaml.safe_dump(doc, allow_unicode=True))
            with self.assertRaises(ConfigError):
                load_web_settings(p)

    def test_is_free_reflects_reality(self):
        import socket
        from macrowire.web import port as portlib
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        held = s.getsockname()[1]
        try:
            self.assertFalse(portlib.is_free(held))
            found = portlib.holder(held)
            self.assertIsNotNone(found)
            self.assertEqual(found["pid"], os.getpid())
            self.assertTrue(found["is_self"])
        finally:
            s.close()

    def test_stop_refuses_a_process_that_is_not_ours(self):
        """Resolving by port must not become a licence to kill anything."""
        import socket
        from macrowire.web import port as portlib
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        held = s.getsockname()[1]
        try:
            result = portlib.stop(held)
            self.assertFalse(result["stopped"])
            self.assertIn("this process", result["reason"])
        finally:
            s.close()

    def test_stop_on_a_free_port_is_not_an_error(self):
        import socket
        from macrowire.web import port as portlib
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()
        result = portlib.stop(free)
        self.assertFalse(result["stopped"])
        self.assertIn("nothing is listening", result["reason"])


class TestSecEdgar(unittest.TestCase):
    """SEC's own vocabulary, their enforced User-Agent, and NULL where a
    price-sensitive judgement is not defensible."""

    def setUp(self):
        from macrowire.parsers import sec_edgar
        self.mod = sec_edgar
        self.src = SOURCES["sec_edgar"]

    def payload(self, **over):
        base = {
            "cik": 320193, "name": "Apple Inc.", "tickers": ["AAPL"],
            "filings": {"recent": {
                "accessionNumber": ["0000320193-26-000018", "0000320193-26-000017"],
                "filingDate": ["2026-07-30", "2026-07-01"],
                "reportDate": ["2026-07-30", ""],
                "acceptanceDateTime": ["2026-07-30T20:30:28.000Z", "2026-07-01T12:00:00.000Z"],
                "form": ["8-K", "4"],
                "items": ["2.02,9.01", ""],
                "primaryDocument": ["aapl-20260730.htm", "form4.xml"],
                "primaryDocDescription": ["8-K", "FORM 4"],
            }},
        }
        base["filings"]["recent"].update(over)
        return base

    def parse(self, payload):
        return self.mod.parse(self.src, json.dumps(payload))

    def test_skip_forms_is_exact_not_a_prefix(self):
        """'4' must not also drop 424B2, an unrelated prospectus form."""
        p = self.payload(form=["4", "424B2", "8-K"],
                         accessionNumber=["a", "b", "c"], filingDate=["2026-07-01"] * 3,
                         reportDate=[""] * 3,
                         acceptanceDateTime=["2026-07-01T12:00:00.000Z"] * 3,
                         items=["", "", ""], primaryDocument=["x"] * 3,
                         primaryDocDescription=[""] * 3)
        types = {i["announcement_type"] for i in self.parse(p).items}
        self.assertNotIn("4", types)
        self.assertIn("424B2", types)
        self.assertIn("8-K", types)

    def test_price_sensitive_only_where_the_item_says_so(self):
        self.assertTrue(self.mod.price_sensitive("8-K", ["2.02", "9.01"]))
        self.assertTrue(self.mod.price_sensitive("8-K", ["5.02"]))
        self.assertTrue(self.mod.price_sensitive("8-K", ["7.01"]))

    def test_ambiguous_items_stay_null_not_false(self):
        """8.01 'Other Events' and 9.01 exhibits are not a judgement we can
        defend, and the column has been nullable since step 1."""
        self.assertIsNone(self.mod.price_sensitive("8-K", ["8.01", "9.01"]))
        self.assertIsNone(self.mod.price_sensitive("8-K", ["5.07"]))
        self.assertIsNone(self.mod.price_sensitive("8-K", []))
        self.assertIsNone(self.mod.price_sensitive("10-Q", []))
        self.assertIsNone(self.mod.price_sensitive("144", []))

    def test_announcement_type_is_the_sec_vocabulary(self):
        self.assertEqual(self.mod.describe("8-K", ["2.02", "9.01"]), "8-K [2.02, 9.01]")
        self.assertEqual(self.mod.describe("10-Q", []), "10-Q")

    def test_parallel_arrays_must_agree_in_length(self):
        """Positional zip, same risk class as the CFETS values array."""
        p = self.payload()
        p["filings"]["recent"]["form"] = ["8-K"]
        with self.assertRaises(ParseError):
            self.parse(p)

    def test_missing_fields_raise(self):
        p = self.payload()
        p["filings"]["recent"].pop("acceptanceDateTime")
        with self.assertRaises(ParseError):
            self.parse(p)

    def test_uses_acceptance_time_not_just_the_date(self):
        item = self.parse(self.payload()).items[0]
        self.assertEqual(item["published_at"], "2026-07-30T20:30:28+00:00")

    def test_accession_number_is_the_external_id(self):
        item = self.parse(self.payload()).items[0]
        self.assertEqual(item["external_id"], "0000320193-26-000018")
        self.assertIn("/Archives/edgar/data/320193/000032019326000018/", item["url"])

    def test_missing_contact_is_a_hard_failure(self):
        """Their edge answers a wrong UA with 403, so guessing is worse."""
        from dataclasses import replace
        for bad in ("", "   ", "MacroWire/0.1", "noatsign"):
            cfg = dict(self.src.config)
            cfg["sec_contact"] = bad
            with self.assertRaises(ParseError, msg=f"accepted {bad!r}"):
                self.mod.sec_user_agent(replace(self.src, config=cfg))

    def test_valid_contact_is_accepted(self):
        from dataclasses import replace
        cfg = dict(self.src.config)
        cfg["sec_contact"] = "Jane Doe jane@example.com"
        self.assertEqual(self.mod.sec_user_agent(replace(self.src, config=cfg)),
                         "Jane Doe jane@example.com")


class TestWatchlist(TempDB):
    """Schema-only since step 1. This is what it was for."""

    CIK = {"AAPL": {"cik": 320193, "title": "Apple Inc."},
           "MSFT": {"cik": 789019, "title": "MICROSOFT CORP"}}

    def test_ships_empty(self):
        from macrowire import watchlist as wl
        self.assertEqual(wl.entries(self.conn, 1), [])

    def test_add_and_list(self):
        from macrowire import watchlist as wl
        wl.add(self.conn, 1, "aapl", "us", cik_map=self.CIK)
        rows = wl.entries(self.conn, 1)
        self.assertEqual(rows, [{"ticker": "AAPL", "market": "US"}])

    def test_unmatched_us_ticker_fails_immediately(self):
        """A typo that is accepted returns nothing forever and looks like a
        quiet company."""
        from macrowire import watchlist as wl
        with self.assertRaises(ConfigError):
            wl.add(self.conn, 1, "APPL", "US", cik_map=self.CIK)
        self.assertEqual(wl.entries(self.conn, 1), [])

    def test_non_us_market_is_not_validated_against_the_sec_map(self):
        from macrowire import watchlist as wl
        wl.add(self.conn, 1, "BHP", "AU", cik_map=self.CIK)
        self.assertEqual(wl.entries(self.conn, 1), [{"ticker": "BHP", "market": "AU"}])

    def test_adding_twice_does_not_duplicate(self):
        from macrowire import watchlist as wl
        wl.add(self.conn, 1, "AAPL", "US", cik_map=self.CIK)
        wl.add(self.conn, 1, "AAPL", "US", cik_map=self.CIK)
        self.assertEqual(len(wl.entries(self.conn, 1)), 1)

    def test_remove(self):
        from macrowire import watchlist as wl
        wl.add(self.conn, 1, "AAPL", "US", cik_map=self.CIK)
        self.assertEqual(wl.remove(self.conn, 1, "aapl"), 1)
        self.assertEqual(wl.entries(self.conn, 1), [])

    def test_empty_watchlist_makes_sec_skip_rather_than_error(self):
        from macrowire.parsers import sec_edgar
        responses, note = sec_edgar.fetch(SOURCES["sec_edgar"], None,
                                          {"watchlist_us": []})
        self.assertEqual(responses, [])
        self.assertIn("empty", note)


class TestJurisdiction(unittest.TestCase):
    """A fact about the publisher, fixed at config time — no rules to rot."""

    def test_every_source_declares_one(self):
        from macrowire.config import JURISDICTIONS
        for src in SOURCES.values():
            self.assertIn(src.jurisdiction, JURISDICTIONS,
                          f"{src.name} has jurisdiction {src.jurisdiction!r}")

    def test_missing_jurisdiction_is_rejected(self):
        import yaml
        from macrowire.config import load_sources
        from macrowire.errors import ConfigError
        doc = yaml.safe_load(Path("sources.yaml").read_text())
        doc["sources"] = [dict(doc["sources"][0])]
        doc["sources"][0]["config"] = dict(doc["sources"][0]["config"])
        doc["sources"][0]["config"].pop("jurisdiction", None)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.yaml"
            p.write_text(yaml.safe_dump(doc, allow_unicode=True))
            with self.assertRaises(ConfigError):
                load_sources(p)

    def test_invalid_jurisdiction_is_rejected(self):
        import yaml
        from macrowire.config import load_sources
        from macrowire.errors import ConfigError
        doc = yaml.safe_load(Path("sources.yaml").read_text())
        doc["sources"] = [dict(doc["sources"][0])]
        doc["sources"][0]["config"] = dict(doc["sources"][0]["config"])
        doc["sources"][0]["config"]["jurisdiction"] = "ZZ"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.yaml"
            p.write_text(yaml.safe_dump(doc, allow_unicode=True))
            with self.assertRaises(ConfigError):
                load_sources(p)

    def test_china_groups_the_three_expected_sources(self):
        cn = {s.name for s in SOURCES.values() if s.jurisdiction == "CN"}
        self.assertEqual(cn, {"cfets_ccpr", "nbs_releases", "nbs_interpretation"})

    def test_jurisdiction_carries_no_colour(self):
        """Seven hues would compete with the one accent that means unread."""
        import re
        css = (Path(__file__).resolve().parent.parent
               / "macrowire/web/static/style.css").read_text()
        block = re.search(r"\.item \.meta \.jur \{([^}]*)\}", css).group(1)
        self.assertNotIn("--accent", block)
        self.assertNotIn("--fault", block)
        self.assertIn("--chrome", block)


class TestJurisdictionFilter(TempDB):
    def setUp(self):
        super().setUp()
        from macrowire.web import queries
        self.queries = queries
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for name in ("nbs_releases", "fed_press_monetary", "rba_media_releases"):
            src = SOURCES[name]
            sid = db.upsert_source(self.conn, src.name, src.kind, src.config)
            self.conn.execute(
                """INSERT INTO items (id, source_id, title, fetched_at, published_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (f"i-{name}", sid, f"item from {name}", now, now))
        self.conn.commit()

    def test_filtering_by_jurisdiction(self):
        rows = self.queries.tape(self.conn, list(SOURCES.values()), 1,
                                 days=30, jurisdictions=["CN"])
        self.assertEqual({r["source"] for r in rows}, {"nbs_releases"})
        self.assertEqual({r["jurisdiction"] for r in rows}, {"CN"})

    def test_multiple_jurisdictions_are_ored(self):
        rows = self.queries.tape(self.conn, list(SOURCES.values()), 1,
                                 days=30, jurisdictions=["AU", "US"])
        self.assertEqual({r["source"] for r in rows},
                         {"rba_media_releases", "fed_press_monetary"})

    def test_axes_are_anded(self):
        """CN plus a US source is legitimately empty."""
        rows = self.queries.tape(self.conn, list(SOURCES.values()), 1, days=30,
                                 jurisdictions=["CN"], only=["fed_press_monetary"])
        self.assertEqual(rows, [])

    def test_items_carry_their_jurisdiction(self):
        rows = self.queries.tape(self.conn, list(SOURCES.values()), 1, days=30)
        by_source = {r["source"]: r["jurisdiction"] for r in rows}
        self.assertEqual(by_source["nbs_releases"], "CN")
        self.assertEqual(by_source["fed_press_monetary"], "US")
        self.assertEqual(by_source["rba_media_releases"], "AU")

    def test_unread_counts_break_down_by_jurisdiction(self):
        counts = self.queries.unread_counts(self.conn, list(SOURCES.values()), 1)
        self.assertEqual(counts["per_jurisdiction"], {"CN": 1, "US": 1, "AU": 1})


class TestTypeDecomposition(TempDB):
    """The composite string cannot express 'results announcements'."""

    def setUp(self):
        super().setUp()
        from macrowire.web import queries
        self.queries = queries
        src = SOURCES["sec_edgar"]
        sid = db.upsert_source(self.conn, src.name, src.kind, src.config)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            ("a", "8-K", "2.02,9.01", "8-K [2.02, 9.01]"),
            ("b", "8-K", "8.01,9.01", "8-K [8.01, 9.01]"),
            ("c", "10-Q", None, "10-Q"),
            ("d", "424B2", None, "424B2"),
        ]
        for key, primary, tags, composite in rows:
            self.conn.execute(
                """INSERT INTO items (id, source_id, title, fetched_at, published_at,
                                      announcement_type, type_primary, type_tags, ticker)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'AAPL')""",
                (key, sid, f"filing {key}", now, now, composite, primary, tags))
        self.conn.commit()

    def tape(self, **kw):
        return self.queries.tape(self.conn, list(SOURCES.values()), 1, days=30, **kw)

    def test_results_announcements_spans_form_and_item(self):
        rows = self.tape(types=["sec_edgar:8-K:2.02", "sec_edgar:10-Q"])
        self.assertEqual({r["title"] for r in rows}, {"filing a", "filing c"})

    def test_item_tag_does_not_match_a_different_item(self):
        rows = self.tape(types=["sec_edgar:8-K:2.02"])
        self.assertEqual([r["title"] for r in rows], ["filing a"])

    def test_tag_match_is_not_a_substring_match(self):
        """9.01 must not match inside '2.02,9.01' by accident, and 2.0 must
        not match 2.02."""
        self.assertEqual([r["title"] for r in self.tape(types=["sec_edgar:8-K:9.01"])],
                         ["filing a", "filing b"])
        self.assertEqual(self.tape(types=["sec_edgar:8-K:2.0"]), [])

    def test_primary_only_token_matches_every_item_of_that_form(self):
        rows = self.tape(types=["sec_edgar:8-K"])
        self.assertEqual({r["title"] for r in rows}, {"filing a", "filing b"})

    def test_type_is_scoped_to_its_source(self):
        self.assertEqual(self.tape(types=["ecb_press:8-K"]), [])

    def test_axes_and_together(self):
        self.assertEqual(self.tape(types=["sec_edgar:10-Q"], tickers=["MSFT"]), [])
        self.assertEqual(len(self.tape(types=["sec_edgar:10-Q"], tickers=["AAPL"])), 1)


class TestFacets(TempDB):
    """Populated-only on every axis: a chip's absence is information."""

    def setUp(self):
        super().setUp()
        from macrowire.web import queries
        self.queries = queries
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for name, primary, tags in (("hkma_press", "Press Release", None),
                                    ("ecb_press", "Speech", None),
                                    ("ecb_press", "Press Release", None),
                                    ("sec_edgar", "8-K", "2.02,9.01")):
            src = SOURCES[name]
            sid = db.upsert_source(self.conn, src.name, src.kind, src.config)
            self.conn.execute(
                """INSERT INTO items (id, source_id, title, fetched_at, published_at,
                                      announcement_type, type_primary, type_tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"{name}-{primary}", sid, "t", now, now, primary, primary, tags))
        self.conn.commit()

    def facets(self):
        return self.queries.facets(self.conn, list(SOURCES.values()), 1)

    def test_only_populated_sources_appear(self):
        names = {x["value"] for x in self.facets()["source"]}
        self.assertEqual(names, {"hkma_press", "ecb_press", "sec_edgar"})
        self.assertNotIn("boe_news", names)

    def test_only_populated_jurisdictions_appear(self):
        codes = {x["value"] for x in self.facets()["jurisdiction"]}
        self.assertEqual(codes, {"HK", "EU", "US"})
        self.assertNotIn("UK", codes)

    def test_single_type_sources_are_not_offered_as_a_type_filter(self):
        """Their type IS their source; a chip would duplicate the row above."""
        groups = {g["source"] for g in self.facets()["type"]}
        self.assertNotIn("hkma_press", groups)
        self.assertIn("ecb_press", groups)

    def test_eight_k_items_surface_as_tags_with_official_labels(self):
        sec = [g for g in self.facets()["type"] if g["source"] == "sec_edgar"][0]
        tags = {t["value"]: t["label"] for t in sec["tags"]}
        self.assertIn("8-K:2.02", tags)
        self.assertEqual(tags["8-K:2.02"], "results of operations")

    def test_watchlist_axis_lists_only_held_tickers_with_items(self):
        self.assertEqual(self.facets()["ticker"], [])


class TestFxClassification(unittest.TestCase):
    """Three states, and the third must never read as a negative."""

    def setUp(self):
        from macrowire import fx as fxmod
        self.fx = fxmod

    def cls(self, name):
        return self.fx.Classifier(SOURCES[name])

    def test_reference_series_are_fx_by_construction(self):
        for name in ("rba_exchange_rates", "cfets_ccpr", "ecb_fx", "cftc_cot"):
            self.assertTrue(SOURCES[name].fx.get("always"), name)
            self.assertEqual(self.cls(name).classify("anything at all"), self.fx.FX)

    def test_a_source_with_no_vocabulary_is_unclassified_not_not_fx(self):
        """Absence of a rule must never read as a negative."""
        from dataclasses import replace
        bare = replace(SOURCES["boe_news"], fx={})
        self.assertEqual(self.fx.Classifier(bare).classify("Bank Rate maintained"),
                         self.fx.UNCLASSIFIED)

    def test_an_unmatched_title_is_unclassified_not_not_fx(self):
        state = self.cls("boe_news").classify("Some entirely novel committee")
        self.assertEqual(state, self.fx.UNCLASSIFIED)

    def test_the_fxjsc_case_that_defeated_the_first_ruleset(self):
        """'FXJSC' does not match \\bFX\\b - the miss that settled the design."""
        self.assertEqual(
            self.cls("boe_news").classify(
                "Minutes of the London FXJSC Main Committee Meeting"),
            self.fx.FX)

    def test_boe_prudential_material_is_not_fx(self):
        for title in ("PRA fines HDI Global SE £4,165,000",
                      "PRA and FCA propose new captive insurance regime",
                      "Banknote imagery advisory group minutes"):
            self.assertEqual(self.cls("boe_news").classify(title), self.fx.NOT_FX, title)

    def test_boe_policy_material_is_fx(self):
        for title in ("Bank Rate maintained at 3.75% - July 2026 Monetary Policy Summary",
                      "Asset Purchase Facility: Gilt Sales - Market Notice"):
            self.assertEqual(self.cls("boe_news").classify(title), self.fx.FX, title)

    def test_hkma_scam_alerts_are_not_fx_but_exchange_fund_is(self):
        self.assertEqual(self.cls("hkma_press").classify("Scam alert related to banks"),
                         self.fx.NOT_FX)
        self.assertEqual(
            self.cls("hkma_press").classify(
                "Exchange Fund Abridged Balance Sheet and Currency Board Account"),
            self.fx.FX)

    def test_chinese_macro_prints_classify(self):
        self.assertEqual(
            self.cls("nbs_releases").classify("2026年7月份居民消费价格同比上涨0.5%"),
            self.fx.FX)
        self.assertEqual(
            self.cls("nbs_releases").classify("国家统计局关于2026年早稻产量数据的公告"),
            self.fx.NOT_FX)

    def test_exclude_wins_over_include(self):
        """The ambiguous case keeps out of an FX-only view rather than in."""
        from dataclasses import replace
        src = replace(SOURCES["boe_news"],
                      fx={"include": ["interest rate"], "exclude": ["consult"]})
        self.assertEqual(
            self.fx.Classifier(src).classify("PRA consults on interest rate risk"),
            self.fx.NOT_FX)

    def test_config_rejects_vocabulary_plus_always(self):
        import yaml
        from macrowire.config import load_sources
        from macrowire.errors import ConfigError
        doc = yaml.safe_load(Path("sources.yaml").read_text())
        doc["sources"] = [dict(doc["sources"][0])]
        doc["sources"][0]["config"] = dict(doc["sources"][0]["config"])
        doc["sources"][0]["config"]["fx"] = {"always": True, "include": ["x"]}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.yaml"
            p.write_text(yaml.safe_dump(doc, allow_unicode=True))
            with self.assertRaises(ConfigError):
                load_sources(p)

    def test_every_source_declares_an_fx_policy(self):
        for src in SOURCES.values():
            self.assertTrue(src.fx, f"{src.name} has no fx block, so all its "
                                    f"items are permanently unclassified")


class TestFxDrift(TempDB):
    """A vocabulary rots silently. Drift must be observable."""

    def setUp(self):
        super().setUp()
        self.src = SOURCES["boe_news"]
        self.sid = db.upsert_source(self.conn, self.src.name, self.src.kind,
                                    self.src.config)
        self.conn.commit()

    def add(self, title, state, days_ago):
        from datetime import datetime, timedelta, timezone
        when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        self.conn.execute(
            """INSERT INTO items (id, source_id, title, fetched_at, published_at, fx_state)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"{title}-{days_ago}", self.sid, title, when, when, state))
        self.conn.commit()

    def test_counts_are_reported_per_source(self):
        self.add("a", "fx", 5); self.add("b", "not_fx", 5); self.add("c", "unclassified", 5)
        st = wire.source_status(self.conn, self.src)
        self.assertEqual(st["fx_counts"], {"fx": 1, "not_fx": 1, "unclassified": 1})
        self.assertAlmostEqual(st["fx_unclassified_pct"], 100 / 3, places=1)

    def test_drift_is_flagged_when_unclassified_rises(self):
        """A renamed committee shows as a rising unclassified rate."""
        for n in range(10):
            self.add(f"old{n}", "fx", 60)
        for n in range(10):
            self.add(f"new{n}", "unclassified", 5)
        st = wire.source_status(self.conn, self.src)
        self.assertTrue(st["fx_drift"])
        self.assertGreater(st["fx_recent_unclassified_pct"],
                           st["fx_older_unclassified_pct"])

    def test_no_drift_when_the_rate_is_steady(self):
        for n in range(10):
            self.add(f"old{n}", "fx", 60)
        for n in range(10):
            self.add(f"new{n}", "fx", 5)
        self.assertFalse(wire.source_status(self.conn, self.src)["fx_drift"])

    def test_null_fx_state_counts_as_unclassified_not_not_fx(self):
        self.conn.execute(
            """INSERT INTO items (id, source_id, title, fetched_at, published_at)
               VALUES ('legacy', ?, 't', '2026-01-01T00:00:00+00:00',
                       '2026-01-01T00:00:00+00:00')""", (self.sid,))
        self.conn.commit()
        st = wire.source_status(self.conn, self.src)
        self.assertEqual(st["fx_counts"]["unclassified"], 1)
        self.assertEqual(st["fx_counts"]["not_fx"], 0)


class TestCftcCot(unittest.TestCase):
    """Positioning. Two traps: a net field that isn't, and contract codes."""

    def setUp(self):
        from macrowire.parsers import cftc_cot
        self.mod = cftc_cot
        self.src = SOURCES["cftc_cot"]

    def row(self, **over):
        base = {
            "id": "260811232741F",
            "report_date_as_yyyy_mm_dd": "2026-08-11T00:00:00.000",
            "cftc_contract_market_code": "232741",
            "contract_market_name": "AUSTRALIAN DOLLAR",
            "open_interest_all": "300000",
            "noncomm_positions_long_all": "80538",
            "noncomm_positions_short_all": "119761",
            "change_in_noncomm_long_all": "6153",
            "change_in_noncomm_short_all": "12186",
        }
        base.update(over)
        return base

    def parse(self, rows):
        return self.mod.parse(self.src, json.dumps(rows))

    def test_net_is_long_minus_short_with_components_kept(self):
        obs = {o["series"]: o["value"] for o in self.parse([self.row()]).observations}
        self.assertEqual(obs["COT/AUD/long"], 80538)
        self.assertEqual(obs["COT/AUD/short"], 119761)
        self.assertEqual(obs["COT/AUD/net"], 80538 - 119761)
        self.assertEqual(obs["COT/AUD/change_net"], 6153 - 12186)

    def test_derived_metrics_say_so(self):
        """So a number of unknown provenance cannot appear later."""
        obs = {o["series"]: o for o in self.parse([self.row()]).observations}
        self.assertIn("derived", obs["COT/AUD/net"]["rate_type"])
        self.assertNotIn("derived", obs["COT/AUD/long"]["rate_type"])

    def test_the_concentration_fields_are_not_used(self):
        """Twelve fields carry 'net' and none is the non-commercial net.
        A maintainer reaching for one by name gets a plausible wrong number."""
        source = (Path(__file__).resolve().parent.parent
                  / "macrowire/parsers/cftc_cot.py").read_text()
        self.assertNotIn("conc_net_le", source.split('"""', 2)[2],
                         "a concentration ratio is being read as a net position")
        self.assertIn("conc_net_le", source.split('"""', 2)[1],
                      "the trap is not documented where someone would hit it")

    def test_reassigned_contract_code_raises(self):
        """A code silently changing instrument would make one continuous-
        looking series out of two different things."""
        with self.assertRaises(ParseError):
            self.parse([self.row(contract_market_name="AUSTRALIAN DOLLAR - SMALL")])

    def test_unpinned_contract_raises(self):
        with self.assertRaises(ParseError):
            self.parse([self.row(cftc_contract_market_code="232661")])

    def test_missing_week_on_week_change_omits_rather_than_zeroes(self):
        """'No prior week' and 'no change from last week' are different
        facts and must not look alike. 29 historical rows have no change."""
        rows = self.parse([self.row(change_in_noncomm_long_all=None,
                                    change_in_noncomm_short_all=None)]).observations
        series = {o["series"] for o in rows}
        self.assertIn("COT/AUD/net", series)
        self.assertNotIn("COT/AUD/change_net", series)
        self.assertNotIn("COT/AUD/change_long", series)

    def test_missing_position_still_raises(self):
        with self.assertRaises(MalformedEntryError):
            self.parse([self.row(noncomm_positions_long_all=None)])

    def test_non_numeric_position_raises(self):
        with self.assertRaises(MalformedEntryError):
            self.parse([self.row(noncomm_positions_short_all="n/a")])

    def test_all_eight_currencies_are_pinned_by_code(self):
        pinned = self.mod.contracts(self.src)
        self.assertEqual(len(pinned), 8)
        self.assertEqual({c["currency"] for c in pinned.values()},
                         {"AUD", "JPY", "EUR", "GBP", "CHF", "CAD", "NZD", "DXY"})
        for code in pinned:
            self.assertTrue(code.isdigit() or code.isalnum())

    def test_period_is_the_report_date(self):
        obs = self.parse([self.row()]).observations[0]
        self.assertEqual(obs["period"], "2026-08-11")
        self.assertEqual(obs["frequency"], "weekly")
        self.assertEqual(obs["unit"], "contracts")

    def test_not_a_json_array_raises(self):
        with self.assertRaises(ParseError):
            self.mod.parse(self.src, '{"error": "nope"}')


class TestEcbFx(unittest.TestCase):
    """Three nested elements all named Cube, told apart only by attributes."""

    DAILY = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"'
             ' xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">'
             '<gesmes:subject>Reference rates</gesmes:subject>'
             '<Cube><Cube time="2026-08-20">'
             '<Cube currency="USD" rate="1.1681"/>'
             '<Cube currency="JPY" rate="185.45"/>'
             '<Cube currency="ZAR" rate="20.1"/>'
             '</Cube></Cube></gesmes:Envelope>')

    def setUp(self):
        self.src = SOURCES["ecb_fx"]

    def parse(self, body=None):
        return get_parser("ecb_fx")(self.src, body or self.DAILY)

    def test_base_is_eur_and_series_follow_the_house_convention(self):
        obs = {o["series"]: o for o in self.parse().observations}
        self.assertIn("EUR/USD", obs)
        self.assertEqual(obs["EUR/USD"]["base_currency"], "EUR")
        self.assertEqual(obs["EUR/USD"]["target_currency"], "USD")
        self.assertEqual(obs["EUR/USD"]["value"], 1.1681)

    def test_currencies_filter_is_honoured(self):
        """ZAR is published but not configured, so it must not be stored."""
        series = {o["series"] for o in self.parse().observations}
        self.assertNotIn("EUR/ZAR", series)
        self.assertIn("EUR/JPY", series)

    def test_publication_time_resolves_per_instant(self):
        """16:00 Europe/Berlin is 14:00 UTC in summer, 15:00 in winter. An
        offset must never be stored - the ribbon lesson, applied here."""
        summer = self.parse().observations[0]["observed_at"]
        winter_xml = self.DAILY.replace("2026-08-20", "2026-01-20")
        winter = self.parse(winter_xml).observations[0]["observed_at"]
        self.assertTrue(summer.endswith("14:00:00+00:00"), summer)
        self.assertTrue(winter.endswith("15:00:00+00:00"), winter)

    def test_multi_day_file_parses_with_the_same_parser(self):
        two = self.DAILY.replace(
            '</Cube></Cube></gesmes:Envelope>',
            '</Cube><Cube time="2026-08-19">'
            '<Cube currency="USD" rate="1.1605"/></Cube></Cube></gesmes:Envelope>')
        periods = {o["period"] for o in self.parse(two).observations}
        self.assertEqual(periods, {"2026-08-20", "2026-08-19"})

    def test_out_of_bounds_value_raises(self):
        """A misparse shows as an implausible number, not a plausible one."""
        with self.assertRaises(MalformedEntryError):
            self.parse(self.DAILY.replace('rate="1.1681"', 'rate="11.681"'))

    def test_non_numeric_rate_raises(self):
        with self.assertRaises(MalformedEntryError):
            self.parse(self.DAILY.replace('rate="1.1681"', 'rate="n/a"'))

    def test_missing_rate_attribute_raises(self):
        with self.assertRaises(MalformedEntryError):
            self.parse(self.DAILY.replace(' rate="1.1681"', ''))

    def test_a_feed_with_no_dated_cube_raises(self):
        empty = ('<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"'
                 ' xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">'
                 '<Cube/></gesmes:Envelope>')
        with self.assertRaises(ParseError):
            self.parse(empty)

    def test_not_an_envelope_raises(self):
        with self.assertRaises(ParseError):
            self.parse("<html><body>503</body></html>")

    def test_jurisdiction_and_archive_are_declared(self):
        self.assertEqual(self.src.jurisdiction, "EU")
        self.assertEqual(self.src.archive, "rolling")


class TestStatusFalseAlarms(TempDB):
    """status must never report failure on healthy data. A false alarm in a
    fail-loudly system is worse than no alarm: it teaches you to ignore the
    real ones. Each test here corresponds to one that actually fired."""

    def setUp(self):
        super().setUp()
        self.cf = SOURCES["cfets_ccpr"]
        self.rba = SOURCES["rba_exchange_rates"]
        self.cf_id = db.upsert_source(self.conn, self.cf.name, self.cf.kind, self.cf.config)
        self.rba_id = db.upsert_source(self.conn, self.rba.name, self.rba.kind, self.rba.config)
        self.conn.commit()

    def observation(self, source_id, period, observed_at, fetched_at=None):
        self.conn.execute(
            """INSERT INTO observations (source_id, series, period, value,
                                         fetched_at, observed_at)
               VALUES (?, 'AUD/USD', ?, 0.71, ?, ?)""",
            (source_id, period, fetched_at or db.utc_now(), observed_at))
        self.conn.commit()

    def test_a_gated_source_that_only_skips_still_reports_contact(self):
        """CFETS's publication gate is its NORMAL outcome. Reporting 'never'
        made a healthy source look dead permanently."""
        self.observation(self.cf_id, "2026-08-21", "2026-08-21T09:15:00+08:00")
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_NO_CHANGE,
                     detail="no new fix")
        st = wire.source_status(self.conn, self.cf)
        self.assertIsNotNone(st["last_contact"])
        self.assertIsNotNone(st["seconds_since_contact"])

    def test_a_backfill_page_counts_as_contact(self):
        """A freshly seeded source reported 'log incomplete' about data it
        had just fetched, because backfill rows were not counted."""
        self.observation(self.cf_id, "2026-08-21", "2026-08-21T09:15:00+08:00")
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_BACKFILL,
                     new_item_count=500, detail="offset 0")
        st = wire.source_status(self.conn, self.cf)
        self.assertIsNotNone(st["last_contact"])
        self.assertFalse(st["log_incomplete"])

    def test_throttled_is_not_evidence_of_contact(self):
        """The rate limiter blocked the attempt; nothing was contacted."""
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_THROTTLED,
                     detail="below the minimum interval")
        st = wire.source_status(self.conn, self.cf)
        self.assertIsNone(st["last_contact"])
        self.assertIsNotNone(st["last_throttled"])

    def test_observation_sources_report_when_they_last_stored(self):
        """The old query looked only in `items`, so every observation source
        reported 'last new item stored: never' while holding thousands."""
        self.observation(self.rba_id, "2026-08-21", "2026-08-21T06:30:00+00:00")
        st = wire.source_status(self.conn, self.rba)
        self.assertEqual(st["observation_rows"], 1)
        self.assertIsNotNone(st["last_stored_at"])
        self.assertIsNotNone(st["latest_content_at"])
        self.assertIsNotNone(st["days_since_content"])

    def test_data_newer_than_the_log_is_reported_as_such(self):
        """The restore failure mode: rows survive, log rows do not."""
        self.observation(self.cf_id, "2026-08-21", "2026-08-21T09:15:00+08:00")
        st = wire.source_status(self.conn, self.cf)
        self.assertTrue(st["log_incomplete"])
        self.assertIsNone(st["last_contact"])
        self.assertIsNotNone(st["last_stored_at"])

    def test_log_complete_when_contact_follows_the_store(self):
        self.observation(self.cf_id, "2026-08-21", "2026-08-21T09:15:00+08:00")
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_OK, new_item_count=5)
        self.assertFalse(wire.source_status(self.conn, self.cf)["log_incomplete"])

    def test_stale_is_measured_on_published_content_not_the_log(self):
        """A source storing rows every day reported STALE because every poll
        after the first deduped and logged new_item_count = 0."""
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone.utc).isoformat(timespec="seconds")
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        self.observation(self.rba_id, "2026-08-21", today)
        db.log_fetch(self.conn, self.rba.name, status=db.STATUS_OK, new_item_count=21)
        self.conn.execute("UPDATE fetch_log SET timestamp = ?", (old,))
        db.log_fetch(self.conn, self.rba.name, status=db.STATUS_OK, new_item_count=0)
        st = wire.source_status(self.conn, self.rba)
        self.assertEqual(st["staleness_days"], 4)
        self.assertFalse(st["stale"], "STALE fired on content published today")

    def test_stale_still_fires_on_genuinely_old_content(self):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(timespec="seconds")
        self.observation(self.rba_id, "2026-08-01", old)
        db.log_fetch(self.conn, self.rba.name, status=db.STATUS_OK, new_item_count=21)
        self.assertTrue(wire.source_status(self.conn, self.rba)["stale"])

    def test_consecutive_failures_reset_after_any_good_cycle(self):
        """A source with no 'ok' row ever had a cutoff that never advanced,
        so one ancient blip read as a permanent failure streak."""
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_ERROR,
                     error="FetchError: blip", error_kind="network")
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_NO_CHANGE, detail="no new fix")
        st = wire.source_status(self.conn, self.cf)
        self.assertEqual(st["consecutive_failures"], 0)
        self.assertEqual(st["failure_kinds"], [])

    def test_consecutive_failures_still_count_a_real_streak(self):
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_NO_CHANGE, detail="ok")
        for _ in range(3):
            db.log_fetch(self.conn, self.cf.name, status=db.STATUS_ERROR,
                         error="FetchError: down", error_kind="network")
        st = wire.source_status(self.conn, self.cf)
        self.assertEqual(st["consecutive_failures"], 3)
        self.assertEqual(st["failure_kinds"], ["network×3"])

    def test_a_resolved_error_is_marked_resolved(self):
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_ERROR,
                     error="FetchError: blip", error_kind="network")
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_OK, new_item_count=1)
        st = wire.source_status(self.conn, self.cf)
        self.assertTrue(st["last_error"]["resolved"])

    def test_a_live_error_is_not_marked_resolved(self):
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_OK, new_item_count=1)
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_ERROR,
                     error="FetchError: down", error_kind="network")
        st = wire.source_status(self.conn, self.cf)
        self.assertFalse(st["last_error"]["resolved"])

    def test_a_gated_poll_counts_against_the_rate_limiter(self):
        """no_change made a real request, so it must count - otherwise a
        gated source is re-probed every cycle regardless of its interval."""
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_NO_CHANGE, detail="no new fix")
        self.assertIsNotNone(db.last_attempt_at(self.conn, self.cf.name))

    def test_a_throttled_row_is_not_an_attempt(self):
        db.log_fetch(self.conn, self.cf.name, status=db.STATUS_THROTTLED, detail="too soon")
        self.assertIsNone(db.last_attempt_at(self.conn, self.cf.name))


class TestUsableByAnyone(unittest.TestCase):
    """Nothing here may assume one particular person is running it."""

    ROOT = Path(__file__).resolve().parent.parent

    def test_commit_hook_does_not_hardcode_an_identity(self):
        """It would reject every other contributor to an open project."""
        hook = (self.ROOT / "git-hooks/commit-msg").read_text()
        self.assertNotIn("spyril@gmail.com", hook)
        self.assertNotIn('EXPECTED_NAME="Spyril"', hook)
        # The pin remains available, but opt-in and by git config.
        self.assertIn("macrowire.authorName", hook)

    def test_user_agent_project_url_is_overridable(self):
        yaml_text = (self.ROOT / "sources.yaml").read_text()
        self.assertIn("${MACROWIRE_PROJECT_URL:-", yaml_text,
                      "a fork cannot identify itself")

    def test_env_expansion_supports_defaults(self):
        import os
        from macrowire.config import _expand_env
        os.environ.pop("MW_TEST_UNSET", None)
        self.assertEqual(_expand_env("${MW_TEST_UNSET:-fallback}"), "fallback")
        os.environ["MW_TEST_SET"] = "real"
        self.assertEqual(_expand_env("${MW_TEST_SET:-fallback}"), "real")

    def test_missing_required_contact_explains_itself(self):
        """A stack trace mid-cycle is the wrong first contact with a tool."""
        import os
        from macrowire.config import ENV_HELP, _expand_env
        from macrowire.errors import ConfigError
        for var in ("MACROWIRE_CONTACT", "SEC_CONTACT"):
            self.assertIn(var, ENV_HELP)
            saved = os.environ.pop(var, None)
            try:
                with self.assertRaises(ConfigError) as caught:
                    _expand_env("${%s}" % var)
                message = str(caught.exception)
                self.assertIn(var, message)
                self.assertIn(".env", message)
                self.assertIn("=", message, "no example of the expected form")
            finally:
                if saved is not None:
                    os.environ[var] = saved

    def test_watchlist_hint_suggests_a_supported_market(self):
        """It suggested an ASX ticker for a market that is not a source."""
        cli = (self.ROOT / "macrowire/__main__.py").read_text()
        block = cli[cli.index('print("watchlist is empty")'):]
        block = block[:block.index("conn.close()")]
        self.assertNotIn("BHP", block)
        self.assertIn("AAPL", block)
        self.assertIn("Only US tickers", block)

    def test_backup_path_is_configurable_like_export(self):
        from macrowire.config import load_backup_settings, load_export_settings
        for settings in (load_backup_settings(), load_export_settings()):
            self.assertIn("path", settings)
            self.assertIn("external", settings)

    def test_backup_path_validated_at_config_load(self):
        import yaml
        from macrowire.config import load_backup_settings
        from macrowire.errors import ConfigError
        doc = yaml.safe_load((self.ROOT / "sources.yaml").read_text())
        doc["defaults"]["backup"]["path"] = "/nonexistent/macrowire-backups"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.yaml"
            p.write_text(yaml.safe_dump(doc, allow_unicode=True))
            with self.assertRaises(ConfigError):
                load_backup_settings(p)

    def test_opinionated_defaults_are_labelled_as_such(self):
        yaml_text = (self.ROOT / "sources.yaml").read_text()
        self.assertIn("OPINIONS, NOT NEUTRAL SETTINGS", yaml_text)
        for setting in ("importance", "staleness_days", "skip_forms", "currencies"):
            self.assertIn(setting, yaml_text)


class TestNeverWarnUnconditionally(unittest.TestCase):
    """The rule: MEASURE the actual state, never warn regardless.

    Four alarms in this project fired without checking whether the thing
    they warned about was handled. A panel that nags at a solved problem is
    one you stop reading - and then it cannot warn you about a real one."""

    JS = Path(__file__).resolve().parent.parent / "macrowire/web/static/app.js"
    CLI = Path(__file__).resolve().parent.parent / "macrowire/__main__.py"

    def test_export_panel_branches_on_measured_state(self):
        js = self.JS.read_text()
        block = js[js.index('const x = d.export'):js.index("/* ---------------- boot")]
        for probe in ("x.error", "!x.exists", "x.external && x.current", "x.external"):
            self.assertIn(probe, block, f"panel does not check {probe}")
        self.assertIn("protected", block, "no confirmation branch for a solved state")

    def test_no_git_instruction_in_the_interface(self):
        """My git workflow is not a feature and this is going to be published."""
        js = self.JS.read_text()
        for word in ("git ", "Commit ", "git commit", "git push"):
            self.assertNotIn(word, js, f"interface instructs {word!r}")

    def test_cli_commit_hint_only_when_a_repo_exists(self):
        cli = self.CLI.read_text()
        block = cli[cli.index("def cmd_export"):cli.index("def _repo_present")]
        self.assertIn("_repo_present()", block,
                      "commit hint is not gated on a repo actually existing")
        # and it is a parenthetical, never the primary instruction
        self.assertIn("one way to get it off the disk", block)

    def test_every_health_state_has_meaning_and_severity(self):
        from macrowire.web.queries import HEALTH_STATES
        for key, spec in HEALTH_STATES.items():
            self.assertTrue(spec["label"], f"{key} has no label")
            self.assertTrue(spec["meaning"], f"{key} has no plain-language meaning")
            self.assertIn(spec["severity"], {"ok", "info", "warn", "bad"})

    def test_never_polled_is_not_a_failure(self):
        """It read like an error. It is a new source nobody has fetched."""
        from macrowire.web.queries import HEALTH_STATES
        state = HEALTH_STATES["never_polled"]
        self.assertEqual(state["severity"], "info")
        self.assertIn("fetch", state["action"])
        self.assertIn("Nothing is wrong", state["meaning"])

    def test_log_incomplete_is_not_a_failure(self):
        from macrowire.web.queries import HEALTH_STATES
        self.assertEqual(HEALTH_STATES["log_incomplete"]["severity"], "info")

    def test_health_state_selection(self):
        from macrowire.web.queries import health_state
        base = {"consecutive_failures": 0, "stale": False, "log_incomplete": False,
                "last_contact": None, "last_success": None}
        self.assertEqual(health_state(base), "never_polled")
        self.assertEqual(health_state({**base, "last_contact": "x"}), "no_change")
        self.assertEqual(health_state({**base, "last_contact": "x",
                                       "last_success": "x"}), "healthy")
        self.assertEqual(health_state({**base, "log_incomplete": True}), "log_incomplete")
        self.assertEqual(health_state({**base, "stale": True}), "stale")
        self.assertEqual(health_state({**base, "consecutive_failures": 2}), "failing")


class TestExportPath(TempDB):
    def setUp(self):
        super().setUp()
        from macrowire import export as export_mod
        from macrowire import watchlist  # noqa: F401
        self.export = export_mod
        news = SOURCES["rba_media_releases"]
        sid = db.upsert_source(self.conn, news.name, news.kind, news.config)
        parsed = get_parser("cb_news")(news, fixture("rba_media_releases.xml").decode("utf-8"))
        wire.classify(news, parsed)
        wire._store_items(self.conn, sid, parsed)
        self.conn.commit()

    def test_absolute_path_required(self):
        import yaml
        from macrowire.config import load_export_settings
        from macrowire.errors import ConfigError
        doc = yaml.safe_load(Path("sources.yaml").read_text())
        doc["defaults"]["export"] = {"path": "relative/dir"}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.yaml"
            p.write_text(yaml.safe_dump(doc, allow_unicode=True))
            with self.assertRaises(ConfigError):
                load_export_settings(p)

    def test_missing_directory_fails_at_config_load_not_export_time(self):
        """Three weeks later, at the moment it mattered, is too late."""
        import yaml
        from macrowire.config import load_export_settings
        from macrowire.errors import ConfigError
        doc = yaml.safe_load(Path("sources.yaml").read_text())
        doc["defaults"]["export"] = {"path": "/nonexistent/macrowire-export"}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.yaml"
            p.write_text(yaml.safe_dump(doc, allow_unicode=True))
            with self.assertRaises(ConfigError):
                load_export_settings(p)

    def test_external_path_is_detected(self):
        import yaml
        from macrowire.config import load_export_settings
        with tempfile.TemporaryDirectory() as d:
            doc = yaml.safe_load(Path("sources.yaml").read_text())
            doc["defaults"]["export"] = {"path": d}
            p = Path(d) / "s.yaml"
            p.write_text(yaml.safe_dump(doc, allow_unicode=True))
            settings = load_export_settings(p)
            self.assertTrue(settings["external"])
            self.assertEqual(settings["path"], Path(d))

    def test_refuses_to_shrink_an_existing_export(self):
        """A scratch database must not overwrite the only off-disk copy.
        This is not hypothetical: it happened during development."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            self.export.write(self.conn, list(SOURCES.values()), out)
            before = (out / self.export.EXPORT_NAME).read_text()
            self.conn.execute("DELETE FROM items")
            self.conn.commit()
            with self.assertRaises(MacroWireError):
                self.export.write(self.conn, list(SOURCES.values()), out)
            self.assertEqual((out / self.export.EXPORT_NAME).read_text(), before)

    def test_force_overrides_the_shrink_guard(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            self.export.write(self.conn, list(SOURCES.values()), out)
            self.conn.execute("DELETE FROM items")
            self.conn.commit()
            self.export.write(self.conn, list(SOURCES.values()), out, force=True)

    def test_state_reports_solved_when_external_and_current(self):
        from macrowire.config import REPO_ROOT
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            settings = {"path": out, "external": True, "auto": True,
                        "default_path": REPO_ROOT / "export"}
            self.export.write(self.conn, list(SOURCES.values()), out)
            st = self.export.state(self.conn, list(SOURCES.values()), settings)
            self.assertTrue(st["exists"])
            self.assertTrue(st["current"])
            self.assertTrue(st["external"])
            self.assertGreater(st["rows"], 0)


class TestWriteSurface(unittest.TestCase):
    """The UI is read-only except item_state and watchlists. Two write paths,
    both POST, and no GET may mutate."""

    APP = Path(__file__).resolve().parent.parent / "macrowire/web/app.py"

    def setUp(self):
        self.src = self.APP.read_text()

    def test_bootstrap_does_not_mutate(self):
        """It used to run the mark-all-read sweep, which made a GET mutate -
        a prefetch or refresh would have consumed the one chance to do it."""
        block = self.src[self.src.index('@app.get("/api/bootstrap")'):]
        block = block[:block.index("@app.post")]
        for writer in ("mark_all_read", "mark_read", "set_flag", "wl.add", "wl.remove"):
            self.assertNotIn(writer, block, f"bootstrap calls {writer}")

    def test_no_get_endpoint_calls_a_writer(self):
        import re
        WRITERS = ("mark_all_read", "mark_read(", "set_flag", "wl.add", "wl.remove")
        blocks = re.split(r"@app\.(get|post)\(", self.src)
        # blocks alternate: [prefix, verb, body, verb, body, ...]
        for verb, body in zip(blocks[1::2], blocks[2::2]):
            if verb != "get":
                continue
            for writer in WRITERS:
                self.assertNotIn(writer, body,
                                 f"a GET endpoint calls {writer}")

    def test_watchlist_mutations_are_post_only(self):
        self.assertIn('@app.post("/api/watchlist/add")', self.src)
        self.assertIn('@app.post("/api/watchlist/remove")', self.src)
        self.assertNotIn('@app.get("/api/watchlist/add")', self.src)
        self.assertNotIn('@app.get("/api/watchlist/remove")', self.src)

    def test_ui_and_cli_share_one_validation_path(self):
        """Both call macrowire.watchlist.add; neither reimplements it."""
        self.assertIn("wl.add(conn, USER_ID", self.src)
        cli = (Path(__file__).resolve().parent.parent
               / "macrowire/__main__.py").read_text()
        self.assertIn("wl.add(conn, user", cli)

    def test_add_returns_the_cli_message_as_a_400(self):
        self.assertIn("except ConfigError as exc", self.src)
        self.assertIn("status_code=400", self.src)


class TestFilterUI(unittest.TestCase):
    """Shape assertions on the markup, so a later edit cannot drop the
    guarantees quietly."""

    ROOT = Path(__file__).resolve().parent.parent / "macrowire/web/static"

    def setUp(self):
        self.js = (self.ROOT / "app.js").read_text()
        self.css = (self.ROOT / "style.css").read_text()
        self.html = (self.ROOT / "index.html").read_text()

    def test_tokens_are_the_only_active_filter_representation(self):
        """One representation, so there is no second state to drift."""
        self.assertIn("function drawTokens", self.js)
        self.assertIn("state.f[b.dataset.axis].delete", self.js)

    def test_filter_bar_sits_between_ribbon_and_tape(self):
        ribbon = self.html.index('class="ribbon"')
        bar = self.html.index('class="filterbar"')
        tape = self.html.index('id="tape"')
        self.assertLess(ribbon, bar)
        self.assertLess(bar, tape)

    def test_keyboard_bindings_exist(self):
        for key in ('"Escape"', '"f"', '"c"', '"Tab"'):
            self.assertIn(key, self.js, f"no handler for {key}")

    def test_zero_result_state_is_distinct_from_empty(self):
        self.assertIn("No items match these filters", self.js)
        self.assertIn("Nothing in this window", self.js)

    def test_clear_all_exists_and_hides_when_inactive(self):
        self.assertIn("function clearFilters", self.js)
        self.assertIn('$("fclear").hidden', self.js)

    def test_filter_ui_encodes_no_category_in_colour(self):
        """The rule is that colour never encodes a CATEGORY - not that the
        signal tokens are unmentionable. A focus ring and an error message
        are legitimate: one is an accessibility requirement, the other is
        exactly what --fault exists for. What must never happen is a chip,
        token or axis carrying meaning through hue."""
        import re
        block = self.css[self.css.index(".filterbar"):self.css.index(".chips {")]
        for rule in re.finditer(r"([^{}]+)\{([^}]*)\}", block):
            selector, body = rule.group(1).strip(), rule.group(2)
            if "--accent" not in body and "--fault" not in body:
                continue
            allowed = ("focus-visible" in selector      # accessibility
                       or ".wl-msg.err" in selector)    # a failure, not a category
            self.assertTrue(
                allowed,
                f"{selector!r} uses a signal colour to carry meaning")

    def test_signal_colours_are_still_reserved(self):
        """--accent means unread, --fault means something is wrong. Neither
        may be reused for a filter, a chip or a category anywhere."""
        import re
        for rule in re.finditer(r"([^{}]+)\{([^}]*)\}", self.css):
            selector, body = rule.group(1).strip().split("\n")[-1].strip(), rule.group(2)
            if "var(--accent)" in body:
                self.assertTrue(
                    "unread" in selector or ".n" in selector or "focus-visible" in selector,
                    f"{selector!r} claims the unread accent")


class TestLegibility(unittest.TestCase):
    """Contrast and size floors, asserted so a future edit cannot quietly
    reintroduce 2.66:1 chrome or 9px text."""

    CSS = Path(__file__).resolve().parent.parent / "macrowire/web/static/style.css"

    @staticmethod
    def _lin(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    @classmethod
    def _lum(cls, h):
        h = h.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * cls._lin(r) + 0.7152 * cls._lin(g) + 0.0722 * cls._lin(b)

    @classmethod
    def _ratio(cls, fg, bg):
        a, b = cls._lum(fg), cls._lum(bg)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    def setUp(self):
        import re
        self.css = self.CSS.read_text()
        self.tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-f]{6})", self.css))

    def test_every_text_token_clears_7to1_on_both_surfaces(self):
        ground, raised = self.tokens["--ground"], self.tokens["--raised"]
        surfaces = {"--ground", "--raised", "--sunken"}
        venues = {"--syd", "--tyo", "--hkg", "--lon", "--nyc"}
        for name, value in self.tokens.items():
            if name in surfaces or name in venues or name.startswith("--edge"):
                continue
            for surface, label in ((ground, "ground"), (raised, "raised")):
                r = self._ratio(value, surface)
                self.assertGreaterEqual(
                    r, 7.0, f"{name} {value} is {r:.2f}:1 on {label}, below 7:1")

    def test_surface_step_is_visible_without_a_border(self):
        r = self._ratio(self.tokens["--raised"], self.tokens["--ground"])
        self.assertGreaterEqual(r, 1.2, f"surface step only {r:.3f}:1")

    def test_nothing_renders_below_12px(self):
        import re
        sizes = [float(x) for x in re.findall(r"font-size:\s*([\d.]+)px", self.css)]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), 12.0, f"smallest is {min(sizes)}px")

    def test_mark_stems_cannot_reach_mark_labels(self):
        """The CFETS colon bug: a lower lane's stem punching up into an
        upper lane's text. Stems live in the axis zone, labels strictly
        below it, and these four numbers are what keeps them apart."""
        import re
        js = (Path(__file__).resolve().parent.parent
              / "macrowire/web/static/app.js").read_text()
        stem = float(re.search(r"\.mark \.stem \{[^}]*height:\s*([\d.]+)px", self.css).group(1))
        line = float(re.search(r"\.mark \.tag \{[^}]*line-height:\s*([\d.]+)px",
                               self.css, re.S).group(1))
        axis = float(re.search(r"AXIS_H = (\d+)", js).group(1))
        lane = float(re.search(r"LANE_H = (\d+)", js).group(1))
        self.assertGreaterEqual(axis, stem, "stem overflows the axis zone into lane 0")
        self.assertGreaterEqual(lane, line, "a label overflows its lane into the next")

    def test_only_the_label_moves_between_lanes(self):
        """Moving the whole mark is what let stems collide with text."""
        js = (Path(__file__).resolve().parent.parent
              / "macrowire/web/static/app.js").read_text()
        body = js[js.index("function layoutMarkLanes"):]
        body = body[:body.index("\n}")]
        self.assertIn("tag.style.top", body)
        self.assertNotIn("el.style.top", body)

    def test_amber_is_reserved_for_unread(self):
        """A second amber thing would make unread stop meaning unread."""
        import re
        users = set()
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", self.css):
            if "var(--accent)" in m.group(2):
                users.add(m.group(1).strip().split("\n")[-1].strip())
        for sel in users:
            self.assertTrue(
                "unread" in sel or ".n" in sel or "focus-visible" in sel,
                f"{sel} uses --accent but is not unread or focus")


class TestTapeCollapsing(TempDB):
    def setUp(self):
        super().setUp()
        from macrowire.web import queries
        self.queries = queries
        src = SOURCES["hkma_press"]
        sid = db.upsert_source(self.conn, src.name, src.kind, src.config)
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        for n in range(12):
            self.conn.execute(
                """INSERT INTO items (id, source_id, title, url, fetched_at, published_at)
                   VALUES (?, ?, 'Scam alert related to banks', 'http://x', ?, ?)""",
                (f"scam{n}", sid, now.isoformat(),
                 (now - timedelta(days=n)).isoformat()))
        self.conn.execute(
            """INSERT INTO items (id, source_id, title, url, fetched_at, published_at)
               VALUES ('real', ?, 'HKMA raises countercyclical capital buffer', 'http://y', ?, ?)""",
            (sid, now.isoformat(), now.isoformat()))
        self.conn.commit()

    def test_identical_titles_collapse_to_one_row(self):
        rows = self.queries.tape(self.conn, list(SOURCES.values()), 1, days=30)
        scam = [r for r in rows if r["title"].startswith("Scam alert")]
        self.assertEqual(len(scam), 1)
        self.assertEqual(scam[0]["count"], 12)
        self.assertEqual(len(scam[0]["ids"]), 12)

    def test_collapse_can_be_disabled(self):
        rows = self.queries.tape(self.conn, list(SOURCES.values()), 1, days=30, collapse=False)
        self.assertEqual(len([r for r in rows if r["title"].startswith("Scam alert")]), 12)

    def test_unread_count_is_per_group_not_per_row(self):
        counts = self.queries.unread_counts(self.conn, list(SOURCES.values()), 1)
        # 12 identical unread notices are ONE unread thing, plus the real item.
        self.assertEqual(counts["per_source"]["hkma_press"], 2)

    def test_first_run_marks_everything_read(self):
        self.assertTrue(self.queries.first_run(self.conn, 1))
        swept = self.queries.mark_all_read(self.conn, 1)
        self.assertEqual(swept, 13)
        self.assertFalse(self.queries.first_run(self.conn, 1))
        self.assertEqual(self.queries.unread_counts(self.conn, list(SOURCES.values()), 1)["total"], 0)

    def test_marking_a_group_read_marks_every_member(self):
        rows = self.queries.tape(self.conn, list(SOURCES.values()), 1, days=30)
        scam = [r for r in rows if r["title"].startswith("Scam alert")][0]
        self.queries.mark_read(self.conn, 1, scam["ids"])
        rows = self.queries.tape(self.conn, list(SOURCES.values()), 1, days=30)
        scam = [r for r in rows if r["title"].startswith("Scam alert")][0]
        self.assertFalse(scam["unread"])

    def test_rba_uri_category_displays_as_fragment(self):
        self.assertEqual(
            self.queries._display_category(
                "http://www.cbwiki.net/wiki/index.php/RSS-CB_1.2_RDF_Schema#Media-Releases"),
            "Media Releases")
        self.assertEqual(self.queries._display_category("Monetary Policy"), "Monetary Policy")
        self.assertIsNone(self.queries._display_category(None))
