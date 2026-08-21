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
    DecodeError, EmptyFeedError, MacroWireError, MalformedEntryError, ParseError,
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
