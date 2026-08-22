"""Regression suite. Uses stdlib unittest - no new dependency.

    python -m unittest discover -s tests -v

Every test writes to a temporary database. MACROWIRE_REFUSE_DEFAULT_DB is
set before macrowire is imported, so a test that forgets to pass a path
fails loudly rather than writing fabricated rows into collected history.
"""

import dataclasses
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

os.environ["MACROWIRE_REFUSE_DEFAULT_DB"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrowire import backup, db, export, wire                    # noqa: E402
from macrowire.config import load_sources                         # noqa: E402
from macrowire.encoding import decode                             # noqa: E402
from macrowire.errors import (                                    # noqa: E402
    ConfigError, DecodeError, EmptyFeedError, FetchError, MacroWireError,
    MalformedEntryError, ParseError,
)
from macrowire.parsers import get_parser                          # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
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
        # The reason is now a key resolved on the client, so assert on both
        # the key and the sentence it resolves to.
        from macrowire import i18n
        reason = marks["hkma_press"]["reason"]
        self.assertEqual(reason, "ribbon.reason.date_only")
        self.assertIn("no time of day", i18n.Translator("en")(reason))

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
        self.assertEqual(cn, {"cfets_ccpr", "nbs_releases", "nbs_interpretation",
                              "cninfo_announcements", "sse_southbound"})

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
        # A source may honestly have no vocabulary yet - but it has to SAY
        # so, with a reason. The failure this rules out is a block nobody
        # wrote, which looks identical from the data side to a deliberate
        # absence and leaves every item permanently unclassified.
        for src in SOURCES.values():
            self.assertTrue(src.fx, f"{src.name} has no fx block, so all its "
                                    f"items are permanently unclassified")
            declared = (src.fx.get("always") or src.fx.get("include")
                        or src.fx.get("exclude") or src.fx.get("unmeasured"))
            self.assertTrue(declared,
                            f"{src.name} has an fx block that decides nothing")

    def test_an_unmeasured_declaration_must_carry_its_reason(self):
        import yaml
        doc = yaml.safe_load((ROOT / "sources.yaml").read_text())
        doc["sources"][0]["config"]["fx"] = {"unmeasured": "   "}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.yaml"
            p.write_text(yaml.safe_dump(doc, allow_unicode=True))
            with self.assertRaises(ConfigError):
                load_sources(p)

    def test_unmeasured_leaves_every_item_unclassified_never_not_fx(self):
        from macrowire import fx as fxmod
        src = SOURCES["cninfo_announcements"]
        self.assertTrue(src.fx.get("unmeasured"))
        classifier = fxmod.Classifier(src)
        self.assertFalse(classifier.has_vocabulary)
        self.assertEqual(classifier.classify("关于外汇套期保值业务的公告"),
                         fxmod.UNCLASSIFIED)


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
        from macrowire import i18n
        t = i18n.Translator("en")
        block = "\n".join(t(f"cli.watchlist.{k}")
                          for k in ("empty", "empty_hint", "empty_note",
                                    "empty_markets_cn"))
        self.assertNotIn("BHP", block)
        self.assertIn("AAPL", block)
        # Both markets that actually poll are named, and the two that cannot
        # are named as prohibited rather than merely absent.
        self.assertIn("SEC EDGAR", block)
        self.assertIn("CNINFO", block)
        self.assertIn("ASX", block)
        self.assertIn("HKEX", block)

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
        from macrowire import i18n
        self.assertIn("cli.export.repo_hint", block)
        self.assertIn("one way to get it off the disk",
                      i18n.Translator("en")("cli.export.repo_hint"))

    def test_every_health_state_has_meaning_and_severity(self):
        # Severity is a rendering decision and stays in Python; the prose
        # lives in the locale catalogues. Both halves must exist for every
        # state, or a source renders with a colour and no explanation.
        from macrowire.web.queries import HEALTH_SEVERITY
        from macrowire import i18n
        t = i18n.Translator("en")
        for key, severity in HEALTH_SEVERITY.items():
            self.assertIn(severity, {"ok", "info", "warn", "bad"})
            self.assertTrue(t(f"health.{key}.label"), f"{key} has no label")
            self.assertTrue(t(f"health.{key}.meaning"),
                            f"{key} has no plain-language meaning")

    def test_never_polled_is_not_a_failure(self):
        """It read like an error. It is a new source nobody has fetched."""
        from macrowire.web.queries import HEALTH_SEVERITY
        from macrowire import i18n
        t = i18n.Translator("en")
        self.assertEqual(HEALTH_SEVERITY["never_polled"], "info")
        self.assertIn("fetch", t("health.never_polled.action"))
        self.assertIn("Nothing is wrong", t("health.never_polled.meaning"))

    def test_log_incomplete_is_not_a_failure(self):
        from macrowire.web.queries import HEALTH_SEVERITY
        self.assertEqual(HEALTH_SEVERITY["log_incomplete"], "info")

    def test_health_state_selection(self):
        from macrowire.web.queries import health_state
        base = {"consecutive_failures": 0, "stale": False, "log_incomplete": False,
                "last_contact": None, "last_success": None, "enabled": True}
        self.assertEqual(health_state(base), "never_polled")
        # Switched off outranks everything: nothing else measured about it
        # is a statement about the source.
        self.assertEqual(health_state({**base, "enabled": False}), "disabled")
        self.assertEqual(health_state({**base, "enabled": False,
                                       "consecutive_failures": 2}), "disabled")
        # A streak made only of timeouts is the path, not the feed.
        self.assertEqual(health_state({**base, "consecutive_failures": 3,
                                       "all_failures_are_path": True}), "unreachable")
        self.assertEqual(health_state({**base, "consecutive_failures": 3,
                                       "all_failures_are_path": False}), "failing")
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
        # The sentences moved to the catalogue; the two states must still be
        # separately worded, which is the thing this test was protecting.
        from macrowire import i18n
        t = i18n.Translator("en")
        self.assertIn("tape.no_match_title", self.js)
        self.assertIn("tape.empty_title", self.js)
        self.assertNotEqual(t("tape.no_match_title"), t("tape.empty_title"))
        self.assertNotEqual(t("tape.no_match_body"), t("tape.empty_body"))

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


class LocaleTests(unittest.TestCase):
    """Every locale is checked against `en`, which is the source of truth.

    The point of the completeness test is that adding a string cannot
    silently break another language: a new key in en.json fails here until
    every shipped locale has it.
    """

    def setUp(self):
        from macrowire import i18n
        self.i18n = i18n
        self.en = {k: v for k, v in i18n.flatten(i18n.load("en")).items()
                   if not k.startswith("_meta")}

    def test_english_catalogue_is_not_empty(self):
        self.assertGreater(len(self.en), 100)

    def test_every_key_in_the_default_exists_in_every_other_locale(self):
        others = [loc for loc in self.i18n.available() if loc != "en"]
        self.assertTrue(others, "no second locale is shipped")
        for locale in others:
            with self.subTest(locale=locale):
                keys = {k for k in self.i18n.flatten(self.i18n.load(locale))
                        if not k.startswith("_meta")}
                self.assertFalse(set(self.en) - keys,
                                 f"{locale} is missing keys present in en")

    def test_no_locale_carries_a_key_english_does_not(self):
        # A key with no English original cannot fall back, so it can only
        # ever render in one language or as the raw key.
        for locale in self.i18n.available():
            if locale == "en":
                continue
            with self.subTest(locale=locale):
                keys = {k for k in self.i18n.flatten(self.i18n.load(locale))
                        if not k.startswith("_meta")}
                self.assertFalse(keys - set(self.en),
                                 f"{locale} has keys absent from en")

    def test_placeholders_match_the_english_original(self):
        # A translation that drops {path} silently loses the one piece of
        # information the sentence existed to carry.
        import re
        fields = lambda s: set(re.findall(r"\{(\w+)\}", s))
        for locale in self.i18n.available():
            if locale == "en":
                continue
            other = self.i18n.flatten(self.i18n.load(locale))
            for key, text in self.en.items():
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(fields(text), fields(other[key]))

    def test_missing_key_falls_back_to_english_and_logs_it(self):
        translator = self.i18n.Translator("en")
        translator.catalogue = {"app": {}}       # simulate a gap in a locale
        translator.locale = "test"
        self.i18n._warned.clear()
        with self.assertLogs("macrowire.i18n", level="WARNING") as captured:
            self.assertEqual(translator("app.title"), self.en["app.title"])
        self.assertIn("app.title", captured.output[0])

    def test_a_key_missing_everywhere_renders_the_key_not_an_empty_string(self):
        translator = self.i18n.Translator("en")
        self.i18n._warned.clear()
        with self.assertLogs("macrowire.i18n", level="ERROR"):
            shown = translator("no.such.key")
        self.assertEqual(shown, "no.such.key")
        self.assertTrue(shown)

    def test_an_unknown_locale_falls_back_rather_than_failing(self):
        self.i18n._cache.pop("nonexistent", None)
        with self.assertLogs("macrowire.i18n", level="WARNING"):
            translator = self.i18n.Translator("nonexistent")
        self.assertEqual(translator("app.title"), self.en["app.title"])

    def test_merged_catalogue_is_complete_for_every_locale(self):
        # The client gets one object and never does fallback itself.
        for locale in self.i18n.available():
            with self.subTest(locale=locale):
                merged = self.i18n.flatten(self.i18n.Translator(locale).merged())
                self.assertEqual(set(merged), set(self.en))

    def test_the_browser_is_not_sent_terminal_strings(self):
        merged = self.i18n.flatten(
            self.i18n.Translator("en").merged(exclude=("cli",)))
        self.assertFalse([k for k in merged if k.startswith("cli.")])
        # and everything the page DOES use is still there
        self.assertIn("rail.health_heading", merged)

    def test_source_facts_are_not_in_the_catalogue(self):
        # Publication times belong to the publisher, not to the reader. If
        # one of these turns up in a locale file, someone has translated a
        # fact instead of the label around it.
        facts = ("4pm AEST", "09:15 CST", "16:00 CET", "15:30 ET")
        for locale in self.i18n.available():
            blob = "\n".join(self.i18n.flatten(self.i18n.load(locale)).values())
            for fact in facts:
                with self.subTest(locale=locale, fact=fact):
                    self.assertNotIn(fact, blob)


class ConnectivityHonestyTests(TempDB):
    """A source that cannot be reached has not been shown to be broken.

    On a slow or filtered international link, timeouts are what a perfectly
    healthy feed looks like. Reporting that as a fault is the same class of
    false alarm this project has already fixed four times.
    """

    def setUp(self):
        super().setUp()
        from macrowire.web import queries
        self.queries = queries
        self.base = {"consecutive_failures": 0, "stale": False,
                     "log_incomplete": False, "last_contact": "x",
                     "last_success": "x", "enabled": True}

    def test_timeout_is_its_own_error_kind(self):
        # Lumped in with DNS failure it cannot be counted separately, and
        # "raise timeout_seconds" stops being derivable from the log.
        from macrowire import db
        self.assertIn("timeout", db.PATH_KINDS)
        self.assertIn("network", db.PATH_KINDS)
        wire_src = (ROOT
                    / "macrowire/wire.py").read_text()
        self.assertIn('kind="timeout"', wire_src)
        self.assertIn("httpx.TimeoutException", wire_src)

    def test_a_streak_of_timeouts_is_unreachable_not_failing(self):
        st = {**self.base, "consecutive_failures": 5, "all_failures_are_path": True}
        self.assertEqual(self.queries.health_state(st), "unreachable")
        self.assertEqual(self.queries.HEALTH_SEVERITY["unreachable"], "warn")

    def test_one_real_error_in_the_streak_makes_it_failing_again(self):
        # A 404 among the timeouts IS a statement about the source.
        st = {**self.base, "consecutive_failures": 5, "all_failures_are_path": False}
        self.assertEqual(self.queries.health_state(st), "failing")
        self.assertEqual(self.queries.HEALTH_SEVERITY["failing"], "bad")

    def test_unreachable_wording_names_connectivity_as_the_alternative(self):
        from macrowire import i18n
        t = i18n.Translator("en")
        self.assertIn("may be connectivity rather than the source",
                      t("health.unreachable.short"))
        self.assertIn("connectivity", t("health.unreachable.meaning"))

    def test_all_failures_are_path_is_measured_not_assumed(self):
        from macrowire import db, wire
        conn = db.connect(Path(self._dir.name) / "test.db")
        source = load_sources()[0]
        for kind in ("timeout", "timeout", "network"):
            db.log_fetch(self.conn, source.name, status=db.STATUS_ERROR,
                         error="x", error_kind=kind)
        st = wire.source_status(self.conn, source)
        self.assertEqual(st["consecutive_failures"], 3)
        self.assertTrue(st["all_failures_are_path"])

        db.log_fetch(self.conn, source.name, status=db.STATUS_ERROR,
                     error="gone", error_kind="http_404")
        st = wire.source_status(self.conn, source)
        self.assertEqual(st["consecutive_failures"], 4)
        self.assertFalse(st["all_failures_are_path"],
                         "a 404 in the streak is evidence about the source")

    def test_a_success_resets_the_path_judgement(self):
        from macrowire import db, wire
        conn = db.connect(Path(self._dir.name) / "test.db")
        source = load_sources()[0]
        db.log_fetch(self.conn, source.name, status=db.STATUS_ERROR,
                     error="x", error_kind="timeout")
        db.log_fetch(self.conn, source.name, status=db.STATUS_OK)
        st = wire.source_status(self.conn, source)
        self.assertEqual(st["consecutive_failures"], 0)
        self.assertFalse(st["all_failures_are_path"])


class SourceEnablementTests(TempDB):
    """Switching a source off must be one word, not a deletion."""

    def test_sources_ship_enabled_and_say_so_explicitly(self):
        sources = load_sources()
        self.assertTrue(all(s.enabled for s in sources))
        # Written out per block rather than left to the default, so it is
        # visible where you would look for it.
        # Directly under `- name:`, so it is the first line of every block
        # rather than a default you have to know about.
        import re
        yaml_text = (ROOT / "sources.yaml").read_text()
        declared = re.findall(r"^  - name: \S+\n    enabled: (\S+)$",
                              yaml_text, re.M)
        self.assertEqual(declared, ["true"] * len(sources))

    def test_enabled_false_is_honoured_and_is_not_an_error(self):
        from macrowire import db, wire
        conn = db.connect(Path(self._dir.name) / "test.db")
        sources = [dataclasses.replace(s, enabled=False) for s in load_sources()[:2]]
        results, failures = wire.fetch_all(self.conn, sources)
        self.assertEqual(failures, [])
        self.assertEqual([r["kind"] for r in results], ["disabled", "disabled"])
        self.assertTrue(all(r["skipped"] for r in results))

    def test_a_disabled_source_is_never_reported_stale(self):
        # Nothing is polling it, so "has published nothing new" would be a
        # fact about this tool wearing the costume of a fact about the feed.
        from macrowire import db, wire
        conn = db.connect(Path(self._dir.name) / "test.db")
        stale_candidate = next(s for s in load_sources() if s.staleness_days)
        off = dataclasses.replace(stale_candidate, enabled=False)
        self.assertFalse(wire.source_status(self.conn, off)["stale"])

    def test_disabled_outranks_every_other_health_state(self):
        from macrowire.web import queries
        st = {"consecutive_failures": 9, "stale": True, "log_incomplete": True,
              "last_contact": None, "last_success": None, "enabled": False,
              "all_failures_are_path": False}
        self.assertEqual(queries.health_state(st), "disabled")
        self.assertEqual(queries.HEALTH_SEVERITY["disabled"], "info")

    def test_an_invalid_enabled_value_fails_at_config_load(self):
        import yaml as pyyaml
        from macrowire.config import ConfigError
        doc = pyyaml.safe_load(
            (ROOT / "sources.yaml").read_text())
        doc["sources"][0]["enabled"] = "yes please"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            pyyaml.safe_dump(doc, handle)
            temp = Path(handle.name)
        try:
            with self.assertRaises(ConfigError):
                load_sources(temp)
        finally:
            temp.unlink()

    def test_the_empty_state_names_the_cause(self):
        from macrowire import i18n
        t = i18n.Translator("en")
        js = (ROOT
              / "macrowire/web/static/app.js").read_text()
        self.assertIn("tape.no_sources_title", js)
        self.assertIn("every((s) => !s.enabled)", js)
        self.assertIn("enabled: true", t("tape.no_sources_body"))
        cli = (ROOT
               / "macrowire/__main__.py").read_text()
        self.assertIn("cli.fetch.none_enabled", cli)


class CninfoTests(unittest.TestCase):
    """CNINFO answers 200 to things that did not work.

    Every fixture here is a real payload or a real payload with one field
    changed. The three failure modes were measured against the live API, not
    imagined: a per-ticker query that comes back as the firehose, a page size
    silently clamped to 30, and a miss that arrives as a well-formed empty
    envelope with no error field anywhere in it.
    """

    def setUp(self):
        from macrowire.parsers import cninfo
        self.cninfo = cninfo
        self.source = SOURCES["cninfo_announcements"]

    def body(self, name):
        return (FIXTURES / name).read_text(encoding="utf-8")

    # --- trap 1: what came back must be what was asked for ---------------

    def test_a_firehose_page_is_refused(self):
        # `column=sse` and `column=szse` returned byte-identical firehose
        # pages for the same date. If `stock` is ever ignored the same way,
        # storing the result would file other companies' filings under a
        # watchlisted ticker - the CFETS misalignment, one table over.
        with self.assertRaises(ParseError) as caught:
            self.cninfo.parse(self.source, self.body("cninfo_firehose.json"))
        self.assertIn("not honoured", str(caught.exception))

    def test_the_guard_does_not_need_to_know_the_request(self):
        # It has to survive a re-parse of stored bytes months later, when
        # the original request is long gone.
        page = self.cninfo.read_page(self.body("cninfo_300750.json"))
        self.assertEqual(page["code"], "300750")
        with self.assertRaises(ParseError):
            self.cninfo.read_page(self.body("cninfo_firehose.json"))

    def test_a_page_for_the_wrong_company_is_refused(self):
        with self.assertRaises(ParseError) as caught:
            self.cninfo.read_page(self.body("cninfo_300750.json"), "600519")
        self.assertIn("600519", str(caught.exception))

    def test_the_same_code_under_a_different_entity_is_refused(self):
        with self.assertRaises(ParseError) as caught:
            self.cninfo.read_page(self.body("cninfo_300750.json"),
                                  "300750", "gssz0300750")
        self.assertIn("different entity", str(caught.exception))

    def test_the_exchange_comes_from_the_code_never_from_column(self):
        self.assertEqual(self.cninfo.venue("600519"), "SSE")
        self.assertEqual(self.cninfo.venue("688981"), "SSE")
        self.assertEqual(self.cninfo.venue("000001"), "SZSE")
        self.assertEqual(self.cninfo.venue("300750"), "SZSE")
        self.assertEqual(self.cninfo.venue("920819"), "BSE")
        # An unknown prefix is not filed under a guessed venue.
        with self.assertRaises(MalformedEntryError):
            self.cninfo.venue("123456")
        # and `column` is nowhere in the parser
        src = (ROOT / "macrowire/parsers/cninfo.py").read_text()
        self.assertNotIn('"column"', src)

    # --- trap 2: structure guards, not status checks ---------------------

    def test_an_empty_envelope_is_read_as_empty_not_as_an_error(self):
        page = self.cninfo.read_page(self.body("cninfo_empty.json"))
        self.assertEqual(page["rows"], [])
        self.assertIsNone(page["code"])
        self.assertFalse(page["has_more"])
        # and parse() turns it into no items rather than raising
        self.assertEqual(self.cninfo.parse(self.source,
                                           self.body("cninfo_empty.json")).entry_count, 0)

    def test_a_changed_envelope_shape_raises_rather_than_returning_nothing(self):
        # The dangerous version of a rename: the field goes, the status stays
        # 200, and a parser reading with .get() quietly stores zero rows
        # forever.
        payload = json.loads(self.body("cninfo_300750.json"))
        del payload["announcements"]
        with self.assertRaises(ParseError) as caught:
            self.cninfo.read_page(json.dumps(payload))
        self.assertIn("changed shape", str(caught.exception))

    def test_the_no_such_code_reply_is_a_bare_list_and_is_caught(self):
        # CNINFO answers an unknown code with HTTP 200 and `[]`.
        with self.assertRaises(ParseError) as caught:
            self.cninfo.read_page("[]")
        self.assertIn("no such code", str(caught.exception))

    def test_a_row_without_a_code_is_refused(self):
        payload = json.loads(self.body("cninfo_300750.json"))
        del payload["announcements"][0]["secCode"]
        with self.assertRaises(MalformedEntryError):
            self.cninfo.read_page(json.dumps(payload))

    def test_a_row_without_an_id_cannot_be_deduplicated_and_is_refused(self):
        payload = json.loads(self.body("cninfo_300750.json"))
        payload["announcements"][0]["announcementId"] = None
        with self.assertRaises(MalformedEntryError) as caught:
            self.cninfo.parse(self.source, json.dumps(payload))
        self.assertIn("deduplicated", str(caught.exception))

    def test_a_non_numeric_timestamp_is_refused(self):
        payload = json.loads(self.body("cninfo_300750.json"))
        payload["announcements"][0]["announcementTime"] = "2026-08-12"
        with self.assertRaises(MalformedEntryError) as caught:
            self.cninfo.parse(self.source, json.dumps(payload))
        self.assertIn("epoch milliseconds", str(caught.exception))

    # --- trap 3: the page size cap ---------------------------------------

    def test_the_page_size_cap_is_named_once_and_asked_for_exactly(self):
        # Measured: asking 50 or 200 returns 30, silently. Asking for exactly
        # the cap leaves nothing to clamp.
        self.assertEqual(self.cninfo.MAX_PAGE_SIZE, 30)
        src = (ROOT / "macrowire/parsers/cninfo.py").read_text()
        self.assertIn('"pageSize": MAX_PAGE_SIZE', src)
        self.assertIn("len(rows) > MAX_PAGE_SIZE", src)

    def test_paging_follows_hasMore_and_never_assumes_a_row_count(self):
        src = (ROOT / "macrowire/parsers/cninfo.py").read_text()
        self.assertIn('page["has_more"]', src)
        self.assertNotIn("pageSize * ", src)

    # --- the record ------------------------------------------------------

    def test_titles_are_stored_as_published(self):
        feed = self.cninfo.parse(self.source, self.body("cninfo_300750.json"))
        self.assertTrue(feed.items)
        for item in feed.items:
            self.assertTrue(any("一" <= ch <= "鿿" for ch in item["title"]),
                            "a Chinese announcement title came back without Chinese in it")
            self.assertEqual(item["ticker"], "300750")
            self.assertEqual(item["type_primary"], "SZSE")
            self.assertEqual(item["institution_abbrev"], "CNINFO")
            # Same restraint as SEC: nothing on the face of a title supports
            # asserting price sensitivity.
            self.assertIsNone(item["is_price_sensitive"])
            self.assertTrue(item["url"].startswith("http://static.cninfo.com.cn/"))

    def test_midnight_beijing_is_stored_as_published_not_repaired(self):
        # A batch submission loses its time of day and arrives as 00:00 CST.
        # It is stored exactly as it came; `date_only` keeps the source off
        # the ribbon, which is the same treatment HKMA already gets.
        payload = json.loads(self.body("cninfo_300750.json"))
        payload["announcements"][0]["announcementTime"] = 1785945600000
        feed = self.cninfo.parse(self.source, json.dumps(payload))
        self.assertTrue(feed.items[0]["published_at"].endswith("T16:00:00+00:00"))
        self.assertEqual(SOURCES["cninfo_announcements"].timing["class"], "date_only")

    def test_undecoded_category_codes_are_not_shown_as_filter_chips(self):
        feed = self.cninfo.parse(self.source, self.body("cninfo_300750.json"))
        for item in feed.items:
            self.assertIsNone(item["type_tags"])


class LiveConfigPathTests(unittest.TestCase):
    """No test may reach a path the user's real installation writes to.

    The watchlist is live config, not scratch data: a stray write puts a
    ticker on someone's list that they did not choose, and it looks exactly
    like a decision they made. It is already covered, because the watchlist
    lives in the database and db.connect() refuses the default path while
    MACROWIRE_REFUSE_DEFAULT_DB is set - but "already covered" is a property
    worth failing on rather than a fact worth remembering, so it is asserted
    here alongside the paths that needed a guard adding.
    """

    LIVE_PATHS = ("the database (watchlists, items, fetch_log)",
                  "the CNINFO orgId cache")

    def test_the_suite_cannot_open_the_default_database(self):
        self.assertTrue(os.environ.get("MACROWIRE_REFUSE_DEFAULT_DB"),
                        "the suite must set this before importing macrowire")
        with self.assertRaises(MacroWireError) as caught:
            db.connect()
        self.assertIn("refusing", str(caught.exception))

    def test_the_suite_cannot_reach_the_live_watchlist(self):
        # There is no separate watchlist file - it is a table - so this is
        # the database guard, asserted from the watchlist's side so that
        # removing the guard fails a test that names the watchlist.
        from macrowire import watchlist as wl
        with self.assertRaises(MacroWireError):
            wl.entries(db.connect(), 1)

    def test_the_suite_cannot_reach_the_live_orgid_cache(self):
        from macrowire import watchlist as wl
        for call in (wl.load_orgid_cache,
                     lambda: wl._save_orgid_cache({"000001": {}})):
            with self.assertRaises(MacroWireError) as caught:
                call()
            self.assertIn("refusing", str(caught.exception))

    def test_every_default_write_path_is_named_in_this_test(self):
        # A new module writing to a new default path must add itself here.
        # The failure this rules out is a guard nobody thought to add,
        # which is how the orgId cache got written to in the first place.
        import macrowire.db as dbmod
        import macrowire.watchlist as wl
        defaults = {dbmod.db_path.__name__, wl._orgid_path.__name__}
        self.assertEqual(defaults, {"db_path", "_orgid_path"},
                         "a new default-path resolver exists and is not "
                         "asserted above")


class CninfoWatchlistTests(TempDB):
    """CN codes are validated on add, and the orgId is looked up not built."""

    def setUp(self):
        super().setUp()
        from macrowire import watchlist as wl
        self.wl = wl
        self.hits = {
            "600519": [{"code": "600519", "orgId": "gssh0600519",
                        "zwjc": "贵州茅台", "category": "A股",
                        "delisted": "false"}],
            "300750": [{"code": "300750", "orgId": "GD165627",
                        "zwjc": "宁德时代", "category": "A股",
                        "delisted": "false"}],
            "999999": [],
        }

    def fetch(self, url, form):
        return json.dumps(self.hits[form["keyWord"]], ensure_ascii=False).encode("utf-8")

    def test_the_orgid_is_never_constructed_from_the_code(self):
        # 600519 -> gssh0600519 follows a pattern; 300750 -> GD165627 does
        # not. Building it would work for most tickers and silently return
        # nothing for the rest.
        cache = {}
        resolve = lambda code: self.wl.resolve_cn(
            code, fetch=self.fetch, cache=cache, persist=False)
        self.assertEqual(resolve("600519")["orgId"], "gssh0600519")
        self.assertEqual(resolve("300750")["orgId"], "GD165627")
        src = (ROOT / "macrowire/parsers/cninfo.py").read_text()
        self.assertNotIn("gssh", src, "an orgId is being constructed in the parser")

    def test_an_unknown_code_is_none_not_an_exception(self):
        self.assertIsNone(self.wl.resolve_cn("999999", fetch=self.fetch, cache={}, persist=False))

    def test_a_letter_ticker_is_rejected_before_a_request_is_spent(self):
        def explode(*_a, **_k):
            self.fail("a request went out for a code that is not six digits")
        with self.assertRaises(ConfigError) as caught:
            self.wl.add(self.conn, 1, "BABA", "CN", cn_fetch=explode, cn_cache={})
        self.assertIn("six digits", str(caught.exception))

    def test_an_unmatched_code_fails_at_add_time(self):
        with self.assertRaises(ConfigError) as caught:
            self.wl.add(self.conn, 1, "999999", "CN", cn_fetch=self.fetch, cn_cache={})
        self.assertIn("quiet company", str(caught.exception))

    def test_a_delisted_code_is_refused(self):
        self.hits["600519"][0]["delisted"] = "true"
        with self.assertRaises(ConfigError) as caught:
            self.wl.add(self.conn, 1, "600519", "CN", cn_fetch=self.fetch,
                        cn_cache={})
        self.assertIn("delisted", str(caught.exception))

    def test_the_suite_cannot_write_to_the_live_orgid_cache(self):
        # The same guard the database has. A test that reached this file
        # would overwrite real, hand-verified lookups with fixture values.
        with self.assertRaises(MacroWireError) as caught:
            self.wl.load_orgid_cache()
        self.assertIn("refusing", str(caught.exception))

    def test_a_non_list_search_reply_raises(self):
        def wrong(url, form):
            return b'{"error":"nope"}'
        with self.assertRaises(MacroWireError) as caught:
            self.wl.resolve_cn("600519", fetch=wrong, cache={}, persist=False)
        self.assertIn("changed shape", str(caught.exception))

    def test_an_entry_with_no_orgid_raises_rather_than_being_stored(self):
        self.hits["600519"][0]["orgId"] = ""
        with self.assertRaises(MacroWireError) as caught:
            self.wl.resolve_cn("600519", fetch=self.fetch, cache={}, persist=False)
        self.assertIn("no orgId", str(caught.exception))

    def test_an_empty_cn_watchlist_is_a_skip_not_a_failure(self):
        from macrowire.parsers import cninfo
        responses, note = cninfo.fetch(SOURCES["cninfo_announcements"], None,
                                       {"watchlist": {"US": ["AAPL"]}})
        self.assertEqual(responses, [])
        self.assertIn("no CN tickers", note)


class SseSouthboundTests(unittest.TestCase):
    """The endpoint answers 200 to a nonsense date and calls it success.

    Every fixture is a real reply captured from query.sse.com.cn.
    """

    def setUp(self):
        from macrowire.parsers import sse_southbound
        self.sb = sse_southbound
        self.source = SOURCES["sse_southbound"]

    def body(self, name):
        return (FIXTURES / name).read_text(encoding="utf-8")

    def code(self):
        """The module with its docstrings removed.

        These tests assert what the parser DOES. The docstring deliberately
        names every trap, so matching against the raw file would fail on the
        warnings rather than on the behaviour.
        """
        import ast
        tree = ast.parse((ROOT / "macrowire/parsers/sse_southbound.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body.pop(0)
        return ast.unparse(tree)

    # --- the boundary -----------------------------------------------------

    def test_a_row_before_the_currency_change_is_refused(self):
        # 2024-08-16 amounts are CNY; 2024-08-19 onward they are HKD. Same
        # field, same series, nothing in the payload marking the switch.
        # This is enforced in code, not left to the comment in sources.yaml.
        with self.assertRaises(ParseError) as caught:
            self.sb.parse(self.source, self.body("sse_sb_20240816.json"))
        message = str(caught.exception)
        self.assertIn("2024-08-19", message)
        self.assertIn("CNY", message)

    def test_the_boundary_date_itself_is_accepted(self):
        feed = self.sb.parse(self.source, self.body("sse_sb_20240819.json"))
        self.assertTrue(feed.observations)
        self.assertEqual({o["period"] for o in feed.observations}, {"2024-08-19"})

    def test_the_backfill_start_matches_the_enforced_boundary(self):
        # If one moves without the other, history either gains a silently
        # mixed year or loses days for no stated reason.
        self.assertEqual(self.source.config["backfill_start"],
                         self.sb.UNIT_BREAK.isoformat())

    def test_sources_yaml_records_the_notice_verbatim(self):
        # The reason history starts here is one line of Chinese on a
        # rendered page and nowhere in any payload. If it is not written
        # down, a future reader sees an arbitrary start date.
        text = (ROOT / "sources.yaml").read_text(encoding="utf-8")
        self.assertIn("2024年8月19日起", text)
        self.assertIn("单位为港元", text)

    def test_monthly_aggregates_are_not_offered_as_a_substitute(self):
        # Their DAY_* fields are averages. Spliced onto a daily series they
        # would look continuous and mean something different.
        self.assertNotIn("LSGYCJXX", self.code())
        self.assertNotIn("commonQuery", self.code())

    # --- result[0] is not None -------------------------------------------

    def test_a_non_trading_date_yields_nothing_and_is_not_an_error(self):
        feed = self.sb.parse(self.source, self.body("sse_sb_20260823.json"))
        self.assertEqual(feed.observations, [])

    def test_the_list_of_one_null_is_caught(self):
        # [None] is truthy and passes every emptiness check that does not
        # look inside it. This is the single most likely way to store a row
        # of garbage.
        self.assertIsNone(self.sb.read_row('{"result":[null]}'))
        self.assertIsNone(self.sb.read_row('{"result":null}'))
        self.assertIsNone(self.sb.read_row('{"result":[]}'))

    def test_quatationInfo_is_never_read_as_a_signal(self):
        # It says "success" for tradeDate=99999999.
        self.assertNotIn('["quatationInfo"]', self.code())
        self.assertNotIn('.get("quatationInfo")', self.code())
        self.assertIn("quatationInfo",
                      (ROOT / "macrowire/parsers/sse_southbound.py").read_text(),
                      "the trap should still be named in a comment")

    def test_a_missing_result_key_raises(self):
        with self.assertRaises(ParseError) as caught:
            self.sb.read_row('{"actionErrors":[],"pageHelp":{}}')
        self.assertIn("changed shape", str(caught.exception))

    def test_the_parenthesised_wrapper_from_the_sibling_endpoint_raises(self):
        # commonSoaQuery.do serves this as application/json with HTTP 200.
        body = '\n({"jsonCallBack":"null","success":"false","errorMsg":"SOA service is null"})'
        with self.assertRaises(ParseError) as caught:
            self.sb.read_row(body)
        self.assertIn("not JSON", str(caught.exception))

    def test_more_than_one_row_for_a_single_date_raises(self):
        with self.assertRaises(ParseError) as caught:
            self.sb.read_row('{"result":[{"TRADE_DATE":"2026-08-21"},'
                             '{"TRADE_DATE":"2026-08-20"}]}')
        self.assertIn("one date at a time", str(caught.exception))

    # --- numbers ----------------------------------------------------------

    def test_thousands_separators_are_stripped(self):
        self.assertEqual(self.sb.number("1,258.47", "F", "ctx"), 1258.47)
        self.assertEqual(self.sb.number("647.70", "F", "ctx"), 647.70)

    def test_a_malformed_number_raises_rather_than_being_swallowed(self):
        # A bare try/except would treat a corrupted digit exactly like a
        # comma and store nothing, silently.
        for bad in ("12.3.4", "1,2x8.47", "十二", "NaN%"):
            with self.subTest(bad=bad):
                with self.assertRaises(MalformedEntryError):
                    self.sb.number(bad, "BUY_AMOUNT", "2026-08-21")

    def test_all_three_null_conventions_are_handled_by_name(self):
        for empty in (None, "-", "", "--"):
            with self.subTest(empty=empty):
                self.assertIsNone(self.sb.number(empty, "F", "ctx"))

    def test_a_field_the_source_did_not_publish_is_omitted_not_zeroed(self):
        payload = json.loads(self.body("sse_sb_20260821.json"))
        payload["result"][0]["ETF_TOTAL_AMOUNT"] = None
        feed = self.sb.parse(self.source, json.dumps(payload))
        series = {o["series"] for o in feed.observations}
        self.assertNotIn("SOUTHBOUND/amount/etf", series)
        self.assertNotIn(0.0, [o["value"] for o in feed.observations
                               if o["series"].endswith("etf")])

    # --- units ------------------------------------------------------------

    def test_the_unit_is_stored_on_every_observation(self):
        # The scale appears only in a rendered column header, never in the
        # payload, so it cannot be left implicit.
        feed = self.sb.parse(self.source, self.body("sse_sb_20260821.json"))
        for o in feed.observations:
            self.assertTrue(o["unit"], f"{o['series']} has no unit")
        amounts = [o for o in feed.observations if o["series"].startswith("SOUTHBOUND/amount")]
        trades = [o for o in feed.observations if o["series"].startswith("SOUTHBOUND/trades")]
        self.assertTrue(amounts and trades)
        for o in amounts:
            self.assertEqual(o["unit"], "100 million HKD")
            self.assertEqual(o["base_currency"], "HKD")
        for o in trades:
            # 万笔 is a COUNT OF TRADES. The field is named *_VOLUME, which
            # invites storing it as shares.
            self.assertEqual(o["unit"], "10,000 trades")
            self.assertIsNone(o["base_currency"])
            self.assertIn("NOT of shares", o["rate_type"])

    def test_nothing_is_silently_normalised(self):
        # The published figure is stored as published: 647.70, not
        # 64_770_000_000.
        feed = self.sb.parse(self.source, self.body("sse_sb_20260821.json"))
        total = next(o for o in feed.observations
                     if o["series"] == "SOUTHBOUND/amount/total")
        self.assertEqual(total["value"], 647.70)

    # --- net --------------------------------------------------------------

    def test_net_is_derived_and_stored_beside_what_it_came_from(self):
        feed = self.sb.parse(self.source, self.body("sse_sb_20260821.json"))
        by = {o["series"]: o["value"] for o in feed.observations}
        self.assertEqual(by["SOUTHBOUND/amount/buy"], 298.18)
        self.assertEqual(by["SOUTHBOUND/amount/sell"], 349.52)
        self.assertEqual(by["SOUTHBOUND/amount/net"], -51.34)
        self.assertAlmostEqual(
            by["SOUTHBOUND/amount/net"],
            by["SOUTHBOUND/amount/buy"] - by["SOUTHBOUND/amount/sell"], places=2)

    def test_net_says_it_is_derived(self):
        feed = self.sb.parse(self.source, self.body("sse_sb_20260821.json"))
        net = next(o for o in feed.observations
                   if o["series"] == "SOUTHBOUND/amount/net")
        self.assertIn("derived", net["rate_type"])

    def test_no_net_when_only_one_side_was_published(self):
        # A net against a missing half is a number about our gaps.
        payload = json.loads(self.body("sse_sb_20260821.json"))
        payload["result"][0]["SELL_AMOUNT"] = None
        feed = self.sb.parse(self.source, json.dumps(payload))
        self.assertNotIn("SOUTHBOUND/amount/net",
                         {o["series"] for o in feed.observations})

    # --- northbound stays out --------------------------------------------

    def test_northbound_is_not_built(self):
        # Turnover with no buy/sell split cannot produce a net, and net is
        # the signal.
        self.assertNotIn("sse_northbound", SOURCES)
        self.assertNotIn("commonSoaQuery", self.code())
        self.assertNotIn("FW_HGTZL", self.code())


class SseSouthboundBackfillTests(TempDB):
    def test_weekends_are_skipped_and_holidays_are_not_guessed(self):
        from macrowire import backfill
        from datetime import date as D
        days = backfill.weekdays(D(2024, 8, 19), D(2024, 8, 25))
        self.assertEqual(days, [D(2024, 8, 19), D(2024, 8, 20), D(2024, 8, 21),
                                D(2024, 8, 22), D(2024, 8, 23)])
        # No holiday calendar anywhere: the endpoint is asked and answers.
        src = (ROOT / "macrowire/backfill.py").read_text()
        self.assertNotIn("holidays = ", src)

    def test_the_per_date_walk_is_selected_by_config(self):
        from macrowire import backfill
        self.assertTrue(backfill.per_date(SOURCES["sse_southbound"]))
        self.assertFalse(backfill.per_date(SOURCES["cfets_ccpr"]))

    def test_each_date_is_logged_so_a_resumed_run_skips_it(self):
        from macrowire import backfill
        db.log_fetch(self.conn, "sse_southbound", status=db.STATUS_BACKFILL,
                     detail="2024-08-19")
        self.assertIn("2024-08-19", backfill._completed(self.conn, "sse_southbound"))


class BackfillRetryTests(unittest.TestCase):
    """A network blip in a twenty-minute paced run is expected, not exceptional.

    Retrying is only safe because the taxonomy already distinguishes a
    failure of the PATH from a failure of the SOURCE. Retrying an http_404
    would turn a clear answer into a slow one.
    """

    def setUp(self):
        from macrowire import backfill
        self.backfill = backfill
        self._real = backfill.wire._download
        self._backoff = backfill.RETRY_BACKOFF_SECONDS
        backfill.RETRY_BACKOFF_SECONDS = (0, 0)
        self.calls = []

    def tearDown(self):
        self.backfill.wire._download = self._real
        self.backfill.RETRY_BACKOFF_SECONDS = self._backoff

    def failing(self, kind, succeed_on=None):
        def call(source, *a, **k):
            self.calls.append(kind)
            if succeed_on is not None and len(self.calls) >= succeed_on:
                return "OK"
            raise FetchError(f"simulated {kind}", kind=kind)
        self.backfill.wire._download = call

    def test_a_transient_network_failure_is_retried_and_recovers(self):
        self.failing("network", succeed_on=3)
        self.assertEqual(self.backfill.download(object()), "OK")
        self.assertEqual(len(self.calls), 3)

    def test_it_gives_up_after_three_attempts(self):
        self.failing("network")
        with self.assertRaises(FetchError):
            self.backfill.download(object())
        self.assertEqual(len(self.calls), self.backfill.RETRY_ATTEMPTS)
        self.assertEqual(self.backfill.RETRY_ATTEMPTS, 3)

    def test_only_path_kinds_are_retried(self):
        # The list is db.PATH_KINDS, shared with the health panel, so
        # "which kinds mean the path" has exactly one definition.
        for kind in ("network", "timeout", "http_404", "http_500", "decode",
                     "parse", "empty", "transport", "config"):
            with self.subTest(kind=kind):
                self.calls.clear()
                self.failing(kind)
                with self.assertRaises(FetchError):
                    self.backfill.download(object())
                retried = len(self.calls) > 1
                self.assertEqual(retried, kind in db.PATH_KINDS,
                                 f"{kind}: retried={retried}")

    def test_a_404_is_not_retried_even_once(self):
        # It will be exactly as absent on the third attempt.
        self.failing("http_404")
        with self.assertRaises(FetchError):
            self.backfill.download(object())
        self.assertEqual(len(self.calls), 1)

    def test_the_backoff_grows(self):
        real = self._backoff
        self.assertEqual(len(real), self.backfill.RETRY_ATTEMPTS - 1,
                         "one wait between each pair of attempts")
        self.assertEqual(list(real), sorted(real))
        self.assertTrue(all(w > 0 for w in real))


class BackfillInterruptionTests(TempDB):
    """Giving up must produce the remedy, not a stack trace."""

    def setUp(self):
        super().setUp()
        from macrowire import backfill
        self.backfill = backfill
        self._real = backfill.wire._download
        self._backoff = backfill.RETRY_BACKOFF_SECONDS
        backfill.RETRY_BACKOFF_SECONDS = (0, 0)

    def tearDown(self):
        self.backfill.wire._download = self._real
        self.backfill.RETRY_BACKOFF_SECONDS = self._backoff
        super().tearDown()

    def test_it_raises_a_typed_interruption_carrying_the_resume_facts(self):
        from datetime import date as D
        from macrowire.errors import BackfillInterrupted

        def down(source, *a, **k):
            raise FetchError("[Errno 101] Network is unreachable", kind="network")
        self.backfill.wire._download = down

        with self.assertRaises(BackfillInterrupted) as caught:
            self.backfill.run_dated(self.conn, SOURCES["sse_southbound"],
                                    D(2024, 8, 23))
        exc = caught.exception
        self.assertEqual(exc.source, "sse_southbound")
        self.assertEqual(exc.reached, D(2024, 8, 19))
        self.assertEqual(exc.remaining, 5)     # 19,20,21,22,23 all weekdays
        self.assertEqual(exc.cause.kind, "network")

    def test_the_stopping_point_is_recorded_in_fetch_log(self):
        # "in the log" is half of what was asked for; the traceback being
        # available is the other half.
        from datetime import date as D
        from macrowire.errors import BackfillInterrupted

        def down(source, *a, **k):
            raise FetchError("[Errno 101] Network is unreachable", kind="network")
        self.backfill.wire._download = down
        with self.assertRaises(BackfillInterrupted):
            self.backfill.run_dated(self.conn, SOURCES["sse_southbound"],
                                    D(2024, 8, 20))
        row = self.conn.execute(
            """SELECT status, error_kind, detail, error FROM fetch_log
               WHERE source = 'sse_southbound' AND status = 'error'""").fetchone()
        self.assertEqual(row["error_kind"], "network")
        self.assertIn("2024-08-19", row["detail"])
        self.assertIn("Errno 101", row["error"])

    def test_a_non_retryable_failure_still_stops_and_still_reports_cleanly(self):
        # A 404 mid-backfill is not retried, but the operator still gets the
        # date and the resume line rather than a traceback.
        from datetime import date as D
        from macrowire.errors import BackfillInterrupted

        def gone(source, *a, **k):
            raise FetchError("HTTP 404", kind="http_404")
        self.backfill.wire._download = gone
        with self.assertRaises(BackfillInterrupted) as caught:
            self.backfill.run_dated(self.conn, SOURCES["sse_southbound"],
                                    D(2024, 8, 20))
        self.assertEqual(caught.exception.cause.kind, "http_404")

    def test_the_cli_prints_the_remedy_and_not_a_traceback(self):
        import argparse, io, contextlib
        from datetime import date as D
        from macrowire import __main__ as cli
        from macrowire.errors import BackfillInterrupted

        exc = BackfillInterrupted("sse_southbound", D(2025, 9, 2), 250,
                                  FetchError("boom", kind="network"))

        def explode(args):
            raise exc
        parser_args = argparse.Namespace(debug=False, func=explode)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
             unittest.mock.patch("argparse.ArgumentParser.parse_args",
                                 return_value=parser_args):
            code = cli.main([])
        text = err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("network unreachable", text)
        self.assertIn("2025-09-02", text)
        self.assertIn("250", text)
        self.assertIn("python -m macrowire backfill --source sse_southbound", text)
        self.assertNotIn("Traceback", text)

    def test_debug_re_raises_so_the_traceback_is_still_reachable(self):
        import argparse
        from datetime import date as D
        from macrowire import __main__ as cli
        from macrowire.errors import BackfillInterrupted

        exc = BackfillInterrupted("sse_southbound", D(2025, 9, 2), 250,
                                  FetchError("boom", kind="network"))

        def explode(args):
            raise exc
        parser_args = argparse.Namespace(debug=True, func=explode)
        with unittest.mock.patch("argparse.ArgumentParser.parse_args",
                                 return_value=parser_args):
            with self.assertRaises(BackfillInterrupted):
                cli.main([])

    def test_the_debug_flag_is_global_not_per_subcommand(self):
        cli = (ROOT / "macrowire/__main__.py").read_text()
        self.assertIn('parser.add_argument("--debug"', cli)
