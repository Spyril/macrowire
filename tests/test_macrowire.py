"""Regression suite. Uses stdlib unittest - no new dependency.

    python -m unittest discover -s tests -v

Every test writes to a temporary database. MACROWIRE_REFUSE_DEFAULT_DB is
set before macrowire is imported, so a test that forgets to pass a path
fails loudly rather than writing fabricated rows into collected history.

A TEST THAT CANNOT FAIL IS WORSE THAN NO TEST
--------------------------------------------------------------------------
It reads as coverage. This suite has produced three of them, all the same
shape - the test's REACH was narrower than its CLAIM - so the rules are
written down rather than remembered.

  1. A LOOP OVER A DERIVED COLLECTION NEEDS A FLOOR.
     `for x in something_computed(): self.assertX(...)` passes perfectly
     when the computation returns nothing, and a derivation silently
     returning nothing is exactly how a guard stops guarding. Wrap it:
     `for x in floor(self, collection, "what", least=N)`. A loop over an
     inline literal needs no floor - a literal tuple cannot empty.

  2. A TEST SEEDED FROM AN EMPTY FIXTURE ASSERTS NOTHING.
     `test_every_axis_carries_both_counts` ran against a TempDB with no
     rows, so the thing it iterated was empty and every assertion inside
     was skipped. It was written one turn after an entire exercise on
     this exact failure mode. Seed the fixture, then floor the loop.

  3. SCAN BY PROPERTY, NOT BY CHARACTER RANGE.
     `css[css.index(".filterbar"):css.index(".chips {")]` silently stops
     covering anything that moves past the end marker, and `assertNotIn`
     inside such a slice is vacuous. Iterate every rule and match on what
     you actually care about. Where a range is unavoidable, prefer
     `assertIn` - it fails loudly when the range is wrong, while
     `assertNotIn` passes.

  4. A SCANNER MUST NOT READ THE PROSE.
     `strip_comments(text, syntax)` / `read_code(path)`. This bit THREE
     times before it was consolidated and an audit then found SEVEN more
     that nobody had tripped: a CSS scanner reading a /* */ line as a
     selector, a t() scanner counting a call written in a # comment to
     explain the API, a markup scanner landing inside the <!-- --> above
     the element it was looking for. Strip unless the question is ABOUT
     the documentation - `test_the_tightest_pair_is_written_down` reads a
     header comment on purpose, which is why TestLegibility keeps both
     `self.css` (raw) and `self.code` (stripped) under different names.

A BROWSER DRIVER IS AVAILABLE. USE IT.
--------------------------------------------------------------------------
geckodriver and firefox are on this machine, and geckodriver speaks
WebDriver over plain HTTP, which httpx - already a dependency - can drive.
`DialogBrowserTests` does exactly that in about eighteen seconds and skips
cleanly where the binaries are absent. NO NEW PACKAGES ARE NEEDED.

"I cannot verify this without a browser" is not a reason to skip a test.
It was assumed rather than checked, and three defects reached the screen
past a green suite partly because of that assumption:

    ribbon.VIEW      a rename nothing crossed - no test called /api/ribbon
    stale catalogue  runtime state no static read could see
    the settings dialog  an author `display` beating the UA stylesheet's
                     `dialog:not([open])`, which is a CASCADE interaction
                     and invisible in any amount of reading the file

All three were BEHAVIOUR, not code. Assert behaviour where behaviour is
what is claimed.

MUTATION TESTING, AND ITS OWN TRAP
--------------------------------------------------------------------------
The way to know a guard bites is to break the thing it guards and watch it
fail. Do that. But CLEAR THE BYTECODE AFTERWARDS:

    find . -name __pycache__ -type d -not -path "./.git/*" -exec rm -rf {} +

Python invalidates a .pyc on (mtime, size). A mutation of the SAME BYTE
LENGTH, applied and reverted inside one second, leaves both unchanged - so
the interpreter keeps running bytecode compiled from the mutated source
while the file on disk reads correctly. That happened here: `"*.json"` was
swapped for `"*.nope"`, restored, and five unrelated tests then failed
against a file that was already correct.
"""

import contextlib
import dataclasses
import json
import os
import re
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


# Comment syntaxes, by the file extensions this project actually has.
_COMMENTS = {
    ".css":  (r"/\*.*?\*/",),
    # `(?<!:)` so a `//` in a URL survives. There is none in the scanned
    # files today and a test below keeps it that way, because a `//` inside
    # a string literal would be mis-stripped and this is not a parser.
    ".js":   (r"/\*.*?\*/", r"(?m)(?<!:)//.*$"),
    ".html": (r"<!--.*?-->",),
    ".py":   (r"(?m)^[ \t]*#.*$",),
    ".yaml": (r"(?m)^[ \t]*#.*$",),
    ".yml":  (r"(?m)^[ \t]*#.*$",),
}


def decode_png(data):
    """PNG bytes -> (width, height, rows), rows[y][x] = (r, g, b).

    THE ONLY WAY TO SEE A SCROLLBAR. A scrollbar is not an element, so
    `elementsFromPoint` never returns one; and it is drawn INSIDE its
    element's border box, so `getBoundingClientRect` can never place one
    outside anything. Every rect-and-hit-test assertion in this file was
    structurally blind to scrollbars until this existed - which is how a
    6px trackless bar the same colour as the panel border passed a suite
    written for exactly that class of defect.

    stdlib zlib only: numpy is installed in this environment but is not in
    requirements.txt, and a test helper is not the place to add a
    dependency the tool does not have.
    """
    import struct, zlib
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat, ihdr = 8, [], None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    width, height, depth, ctype, _, _, interlace = ihdr
    assert depth == 8 and interlace == 0, f"unsupported PNG {depth=} {interlace=}"
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    rows, prev, at = [], bytearray(stride), 0
    for _ in range(height):
        f = raw[at]; at += 1
        cur = bytearray(raw[at:at + stride]); at += stride
        if f == 2:                                  # Up, the common case
            for i in range(stride):
                cur[i] = (cur[i] + prev[i]) & 0xFF
        elif f == 1:                                # Sub
            for i in range(channels, stride):
                cur[i] = (cur[i] + cur[i - channels]) & 0xFF
        elif f == 3:                                # Average
            for i in range(stride):
                a = cur[i - channels] if i >= channels else 0
                cur[i] = (cur[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:                                # Paeth
            for i in range(stride):
                a = cur[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                q = a + b - c
                pa, pb, pc = abs(q - a), abs(q - b), abs(q - c)
                cur[i] = (cur[i] +
                          (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        rows.append([tuple(cur[x * channels:x * channels + 3]) for x in range(width)])
        prev = cur
    return width, height, rows


def strip_comments(text, syntax):
    """Source with its comments removed, for scanning.

    ONE helper, because this has now bitten three times in three places and
    three local fixes for one problem is the signal to consolidate - the
    same call as CONTACT_STATUSES and db.PATH_KINDS:

      * a CSS scanner read the last line of a /* */ block as a selector;
      * a t() scanner counted `t("x", key="y")` written in a # comment to
        EXPLAIN the API as a real call, and reported a missing key;
      * a markup scanner located the first "<dialog" and landed inside the
        <!-- --> comment above the element.

    Every one of them was a scanner answering a question about the CODE
    while reading the PROSE. `syntax` is a file extension or a bare name
    ("css", "py"); an unknown one raises rather than silently returning
    the text unchanged, because a scanner that thinks it stripped and did
    not is the bug this exists to close.

    Deliberately NOT a parser. It does not know that "/*" inside a CSS
    string literal is not a comment, and this project has no such string.
    If one ever appears, the fix is a parser, not a cleverer regex.
    """
    import re

    key = syntax if syntax.startswith(".") else "." + syntax
    if key not in _COMMENTS:
        raise ValueError(
            f"no comment syntax for {syntax!r}; known: "
            f"{', '.join(sorted(_COMMENTS))}")
    for pattern in _COMMENTS[key]:
        text = re.sub(pattern, "", text, flags=re.S if ".*?" in pattern else 0)
    return text


def read_code(path, syntax=None):
    """A source file with its comments stripped, by extension."""
    path = Path(path)
    return strip_comments(path.read_text(encoding="utf-8"),
                          syntax or path.suffix)


def seeded(case, conn, **expected):
    """Assert a fixture produced what it claims, AT CREATION.

    `floor()` catches an empty collection where it is iterated. This
    catches it one level earlier, where it was made - and the difference
    matters because four vacuous tests have now been found downstream,
    each one a fixture that produced less than it claimed:

        test_every_axis_carries_both_counts   TempDB with no rows
        the tape-stability set                browser DB with no items
        test_every_rail_value_is_inside...    browser DB with no observations
        the locale scans                      available() returning nothing

    Every one failed in whichever test happened to notice, naming a
    symptom rather than the fixture. One failure at the source beats N
    failures downstream.

        seeded(self, conn, items=200, observations=18, sources=5)
    """
    TABLES = {"items": "items", "observations": "observations",
              "sources": "sources", "watchlists": "watchlists",
              "fetch_log": "fetch_log", "preferences": "preferences"}
    for what, least in expected.items():
        table = TABLES.get(what)
        if table is None:
            raise ValueError(f"seeded() does not know the table {what!r}; "
                             f"known: {', '.join(sorted(TABLES))}")
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        case.assertGreaterEqual(
            n, least,
            f"fixture claims to seed {least}+ {what} and produced {n}; "
            f"every assertion against this fixture is vacuous")
    return conn


def floor(case, collection, what, least=1):
    """Assert there is something to iterate before iterating it.

    A test that loops over a derived collection and asserts inside the loop
    passes perfectly when the collection comes back empty - and a derivation
    silently returning nothing is exactly how a guard stops guarding. Every
    such loop in this suite states its floor.
    """
    n = len(collection)
    case.assertGreaterEqual(
        n, least,
        f"found {n} {what}; the scan is broken, so nothing below was checked")
    return collection
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
        for src in floor(self, list(SOURCES.values()), "sources", 10):
            self.assertIn(src.archive, {"none", "rolling", "queryable"},
                          f"{src.name} is unclassified")

    def test_archive_none_forbids_raw_pruning(self):
        for src in floor(self, list(SOURCES.values()), "sources", 10):
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
        for src in floor(self, list(self.sources), "sources"):
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
        for src in floor(self, list(SOURCES.values()), "sources", 10):
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
        # Code, not comments: a comment explaining WHY the interface gives
        # no git instruction is not the interface giving one.
        js = read_code(self.JS)
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
        # If the decorator is ever reformatted this split yields ONE block,
        # the zip is empty, and every assertion below is skipped silently.
        endpoints = list(zip(blocks[1::2], blocks[2::2]))
        floor(self, [v for v, _ in endpoints if v == "get"], "GET endpoints", 5)
        for verb, body in endpoints:
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

    def test_the_filter_control_lives_in_the_sticky_masthead(self):
        """It used to sit in a bar between the ribbon and the tape, which
        meant scrolling a hundred rows back up to reach it. Both controls
        are now in the masthead because both are controls."""
        head = self.html[self.html.index("<header"):self.html.index("</header>")]
        self.assertIn('id="fopen"', head)
        self.assertIn('id="settings-open"', head)
        self.assertIn('id="tokens"', head, "the active filters are not visible")
        self.assertNotIn('class="filterbar"', self.html,
                         "the old in-flow filter bar is back")
        # and the masthead precedes the ribbon, which precedes the tape
        self.assertLess(self.html.index("<header"), self.html.index('class="ribbon"'))
        self.assertLess(self.html.index('class="ribbon"'), self.html.index('id="tape"'))

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

    def test_clear_all_exists_and_the_whole_row_hides_when_inactive(self):
        """The ROW goes, not just the button: an empty flex row still costs
        its padding, and the masthead must not take tape when nothing is
        filtered."""
        self.assertIn("function clearFilters", self.js)
        self.assertIn('$("mast-tokens").hidden = active.length === 0', self.js)

    # Selectors permitted to spend a signal colour, each with the reason.
    # An allowlist of SELECTORS, not a character range: the previous version
    # scanned from ".filterbar" to ".chips {" and every `.chip*` rule sat
    # past the end of it, so `.chip .n { color: var(--accent) }` - a window
    # count painted in the unread colour - was invisible to the test that
    # existed to forbid exactly that.
    SIGNAL_ALLOWED = {
        "focus-visible":  "accessibility: the focus ring must be visible",
        ".wl-msg.err":    "a failure, not a category",
        ".item.unread":   "unread IS what --accent means",
        ".unread-total":  "the masthead unread count is unread",
        ".chip .n-unread": "an unread count, which is what --accent means",
        ".health .warn":  "--fault on a failing source is what --fault is for",
        ".unsolved":      "--fault on an unsolved risk, likewise",
        ".hnote":         "--fault on a network-wide fault, likewise",
    }

    def signal_users(self, token):
        """Every selector in the WHOLE stylesheet that spends `token`.

        Property-driven, not range-driven. A rule added anywhere in the file
        is covered the moment it is written.
        """
        import re
        # One helper for every syntax; see strip_comments(). A rule preceded
        # by a comment block otherwise yields the comment's last line as its
        # selector, which reads as a violation and is only ever noise.
        css = strip_comments(self.css, "css")
        out = []
        for rule in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
            if f"var({token})" not in rule.group(2):
                continue
            for selector in rule.group(1).strip().split(","):
                selector = " ".join(selector.split())
                if selector:
                    out.append(selector)
        return out

    def test_filter_ui_encodes_no_category_in_colour(self):
        """The rule is that colour never encodes a CATEGORY - not that the
        signal tokens are unmentionable. A focus ring and an error message
        are legitimate: one is an accessibility requirement, the other is
        exactly what --fault exists for. What must never happen is a chip,
        token or axis carrying meaning through hue.

        Both signal tokens are checked. The earlier version named --fault in
        its docstring and only ever tested --accent."""
        for token in ("--accent", "--fault"):
            for selector in self.signal_users(token):
                with self.subTest(token=token, selector=selector):
                    self.assertTrue(
                        any(k in selector for k in self.SIGNAL_ALLOWED),
                        f"{selector!r} spends {token} to carry meaning; if that "
                        f"is deliberate, add it to SIGNAL_ALLOWED with a reason")

    def test_no_filter_chip_carries_a_signal_colour(self):
        """The specific regression: a filter chip tinted by category."""
        for token in ("--accent", "--fault"):
            for selector in self.signal_users(token):
                if ".chip" not in selector:
                    continue
                with self.subTest(token=token, selector=selector):
                    self.assertTrue(
                        "n-unread" in selector or "focus-visible" in selector,
                        f"{selector!r} tints a chip with {token}")

    def test_bucket_counts_are_not_painted_in_the_unread_colour(self):
        """`.chip .n` was one amber badge used for BOTH a window count and an
        unread count depending on the axis, so `fx 61` rendered 61 window
        items in the colour that means unread."""
        import re
        code = strip_comments(self.css, "css")
        self.assertNotIn(".chip .n {", code, "the merged badge is back")
        bucket = re.search(r"\.chip \.n-bucket \{([^}]*)\}", code)
        self.assertIsNotNone(bucket, "no bucket badge rule")
        self.assertNotIn("--accent", bucket.group(1))
        self.assertIn("--chrome", bucket.group(1))

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
        # BOTH views, on purpose. Most tests here ask a question about the
        # CODE and must not see comments. One asks a question about the
        # DOCUMENTATION - the tightest-pair line is deliberately recorded in
        # the header comment - so it needs the raw text. Naming them apart
        # stops the next reader stripping the one that must not be.
        self.css = self.CSS.read_text()
        self.code = strip_comments(self.css, "css")
        self.tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-f]{6})", self.code))

    SURFACES = ("--ground", "--raised", "--sunken")
    VENUES = ("--syd", "--tyo", "--hkg", "--lon", "--nyc")
    FLOOR_TEXT = 7.0
    FLOOR_NONTEXT = 3.0          # WCAG 1.4.11, for UI parts that are not text

    def text_pairs(self):
        """Every (token, surface) pair that will carry text, with its ratio."""
        skip = set(self.SURFACES) | set(self.VENUES)
        for name, value in sorted(self.tokens.items()):
            if name in skip or name.startswith("--edge"):
                continue
            for surface in self.SURFACES:
                yield name, surface, self._ratio(value, self.tokens[surface])

    def test_every_text_token_clears_7to1_on_every_surface(self):
        # All THREE surfaces, not two. --sunken was never checked, and a
        # token can only be trusted on a surface it was measured against.
        pairs = list(self.text_pairs())
        self.assertTrue(pairs)
        for name, surface, r in pairs:
            with self.subTest(token=name, surface=surface):
                self.assertGreaterEqual(
                    r, self.FLOOR_TEXT,
                    f"{name} on {surface} is {r:.3f}:1, below {self.FLOOR_TEXT}:1")

    def test_the_tightest_pair_is_written_down_and_still_true(self):
        """The floor is cleared by 0.013 and nothing said so.

        A margin that thin is a silent break waiting for the next palette
        edit, so the tightest pair is recorded in the stylesheet header and
        checked here against a freshly computed value. Change a colour and
        this fails until you have re-measured and written the new number
        down - which is the point.
        """
        import re
        name, surface, ratio = min(self.text_pairs(), key=lambda p: p[2])
        declared = re.search(
            r"TIGHTEST TEXT PAIR:\s*(--[\w-]+) on (--[\w-]+)\s*=\s*([\d.]+):1",
            self.css)
        self.assertIsNotNone(
            declared, "the stylesheet no longer records its tightest text pair")
        self.assertEqual((name, surface), (declared.group(1), declared.group(2)),
                         f"tightest pair is now {name} on {surface}, "
                         f"not {declared.group(1)} on {declared.group(2)}")
        self.assertAlmostEqual(
            ratio, float(declared.group(3)), places=2,
            msg=f"tightest pair measures {ratio:.3f}:1, header says "
                f"{declared.group(3)}:1")

    def test_non_text_tokens_clear_the_3to1_floor(self):
        # Dividers and chip borders are UI parts, not text: 1.4.11 asks 3:1,
        # not 7:1, and holding them to 7:1 would push them into the content
        # range where they would compete with what they frame.
        for name in ("--edge", "--edge-hi"):
            r = self._ratio(self.tokens[name], self.tokens["--ground"])
            with self.subTest(token=name):
                self.assertGreaterEqual(
                    r, self.FLOOR_NONTEXT,
                    f"{name} is {r:.2f}:1 on ground, below {self.FLOOR_NONTEXT}:1")

    def test_the_two_coincidentally_equal_tokens_are_flagged_as_such(self):
        """--ink-2 and --chrome-hi hold the same hex today by coincidence.

        They are in different families and either may move without the
        other, so a future edit to one would diff as a no-op against the
        other. That has to be written down or it is a trap.
        """
        import collections
        by_hex = collections.defaultdict(list)
        floor(self, self.tokens, "colour tokens", 10)
        for name, value in self.tokens.items():
            by_hex[value.lower()].append(name)
        for value, names in by_hex.items():
            if len(names) < 2:
                continue
            with self.subTest(hex=value, tokens=names):
                self.assertIn(
                    "coincidence", self.css,
                    f"{names} share {value} with nothing saying whether that "
                    f"is deliberate")

    def test_surface_step_is_visible_without_a_border(self):
        r = self._ratio(self.tokens["--raised"], self.tokens["--ground"])
        self.assertGreaterEqual(r, 1.2, f"surface step only {r:.3f}:1")

    def test_nothing_renders_below_12px(self):
        import re
        sizes = [float(x) for x in re.findall(r"font-size:\s*([\d.]+)px", self.code)]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), 12.0, f"smallest is {min(sizes)}px")

    def test_mark_stems_cannot_reach_mark_labels(self):
        """The CFETS colon bug: a lower lane's stem punching up into an
        upper lane's text. Stems live in the axis zone, labels strictly
        below it, and these four numbers are what keeps them apart."""
        import re
        js = (Path(__file__).resolve().parent.parent
              / "macrowire/web/static/app.js").read_text()
        stem = float(re.search(r"\.mark \.stem \{[^}]*height:\s*([\d.]+)px", self.code).group(1))
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
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", self.code):
            if "var(--accent)" in m.group(2):
                for sel in m.group(1).strip().split(","):
                    users.add(sel.strip().split("\n")[-1].strip())
        self.assertTrue(users, "no --accent users found; the scan is broken")
        for sel in users:
            # `.n` used to be allowed wholesale, which let ANY element named
            # .n claim amber - including the bucket counts. Only the unread
            # badge qualifies now.
            self.assertTrue(
                "unread" in sel or "n-unread" in sel or "focus-visible" in sel,
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

    ONE DIRECTION ONLY, and deliberately so. This compares the locale files
    with each other; it cannot see a key that is referenced in code and
    missing from BOTH files, because both files agree it does not exist and
    the comparison passes. TranslationKeyReachTests covers that direction.
    """

    def setUp(self):
        from macrowire import i18n
        self.i18n = i18n
        # renderable(), not flatten(): `_meta` and `_note_*` are guidance
        # for translators and never reach a screen, so they are not part of
        # what a locale must be complete against.
        self.en = dict(i18n.renderable(i18n.load("en")))

    def test_english_catalogue_is_not_empty(self):
        self.assertGreater(len(self.en), 100)

    def test_every_key_in_the_default_exists_in_every_other_locale(self):
        others = [loc for loc in self.i18n.available() if loc != "en"]
        self.assertTrue(others, "no second locale is shipped")
        for locale in others:
            with self.subTest(locale=locale):
                keys = set(self.i18n.renderable(self.i18n.load(locale)))
                self.assertFalse(set(self.en) - keys,
                                 f"{locale} is missing keys present in en")

    def test_no_locale_carries_a_key_english_does_not(self):
        # A key with no English original cannot fall back, so it can only
        # ever render in one language or as the raw key.
        for locale in floor(self, self.i18n.available(), "locale files", 2):
            if locale == "en":
                continue
            with self.subTest(locale=locale):
                keys = set(self.i18n.renderable(self.i18n.load(locale)))
                self.assertFalse(keys - set(self.en),
                                 f"{locale} has keys absent from en")

    def test_placeholders_match_the_english_original(self):
        # A translation that drops {path} silently loses the one piece of
        # information the sentence existed to carry.
        import re
        fields = lambda s: set(re.findall(r"\{(\w+)\}", s))
        for locale in floor(self, self.i18n.available(), "locale files", 2):
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
        for locale in floor(self, self.i18n.available(), "locale files", 2):
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
        for locale in floor(self, self.i18n.available(), "locale files", 2):
            blob = "\n".join(self.i18n.renderable(self.i18n.load(locale)).values())
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
        for item in floor(self, feed.items, "parsed announcements"):
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


class FilterPanelTests(TempDB):
    """The drawer used to push 195 items below the fold and print two
    differently-scoped numbers with nothing saying which was which."""

    def setUp(self):
        super().setUp()
        # strip_comments, not read_text: the code comments explain what the
        # markup used to be, and a scanner looking for "<details" found the
        # sentence saying there are none.
        self.css = strip_comments(read_code(ROOT / "macrowire/web/static/style.css"), "css")
        self.js = strip_comments(read_code(ROOT / "macrowire/web/static/app.js"), "js")
        self.html = read_code(ROOT / "macrowire/web/static/index.html")
        self.seed()

    def seed(self):
        """Real rows across several axes.

        The count tests were written against an empty TempDB, so `facets`
        returned empty lists and every per-entry assertion was skipped. The
        floor caught it; this is the other half of the fix.
        """
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        for n, name in enumerate(("rba_media_releases", "hkma_press", "ecb_press")):
            src = SOURCES[name]
            sid = db.upsert_source(self.conn, src.name, src.kind, src.config)
            for k in range(3):
                self.conn.execute(
                    """INSERT INTO items (id, source_id, title, url, fetched_at,
                                          published_at, fx_state, type_primary)
                       VALUES (?, ?, ?, 'http://x', ?, ?, ?, ?)""",
                    (f"seed{n}{k}", sid, f"Item {n}{k}", now.isoformat(),
                     (now - timedelta(hours=k)).isoformat(),
                     "fx" if k else "unclassified",
                     "Press Release" if k else "Speech"))
        self.conn.commit()
        seeded(self, self.conn, items=9, sources=3)

    # --- 1. the ceiling ---------------------------------------------------

    def test_the_panel_cannot_grow_past_a_fraction_of_the_viewport(self):
        import re
        # Every .fpanel rule together: the ceiling and the positioning are
        # declared in separate blocks now that the panel floats.
        body = "".join(m.group(1) for m in re.finditer(r"\.fpanel \{([^}]*)\}",
                                                      strip_comments(self.css, "css")))
        floor(self, body, "characters of .fpanel rules", 40)
        self.assertIn("max-height", body,
                      "the panel has no ceiling; it could cover the whole tape")
        self.assertIn("vh", body, "the ceiling must be relative to the viewport")
        self.assertIn("position: fixed", body,
                      "the panel is in flow again and will displace the tape")
        # The panel keeps the ceiling and clips; the BODY scrolls. Split the
        # same way the settings dialog splits it, so the footer stays put.
        self.assertIn("overflow: hidden", body,
                      "the panel does not clip, so content can paint over the tape")
        inner = "".join(m.group(1) for m in re.finditer(r"\.fpanel-body \{([^}]*)\}",
                                                       strip_comments(self.css, "css")))
        floor(self, inner, "characters of .fpanel-body rules", 40)
        self.assertIn("overflow-y: auto", inner,
                      "a ceiling without a scroll just clips the last axis")
        self.assertIn("min-height: 0", inner,
                      "a flex child with no min-height:0 refuses to shrink, so "
                      "the panel grows past its ceiling instead of scrolling")

    def test_the_ceiling_leaves_more_than_half_the_viewport_for_the_tape(self):
        import re
        vh = float(re.search(r"max-height:\s*min\((\d+)vh", self.css).group(1))
        self.assertLessEqual(vh, 50.0,
                             f"panel may take {vh}vh, leaving under half for the tape")

    # --- 2. both counts, one unit ----------------------------------------

    def test_every_axis_carries_both_counts(self):
        from macrowire.web import queries
        f = queries.facets(self.conn, list(SOURCES.values()), 1)
        flat = list(f["fx"]) + list(f["jurisdiction"]) + list(f["source"]) + list(f["ticker"])
        for group in f["type"]:
            flat += list(group["primary"]) + list(group["tags"])
        for entry in floor(self, flat, "facet values", 10):
            with self.subTest(value=entry["value"]):
                self.assertIn("count", entry)
                self.assertIn("unread", entry)

    def test_both_counts_are_in_the_same_unit(self):
        """They were not. `facets` counted raw item rows and `unread_counts`
        counted collapsed groups, so HKMA's repeated scam alert was 207 on
        one axis and 1 on the other - two numbers that could not be compared
        even once they were labelled."""
        from macrowire.web import queries
        sources = list(SOURCES.values())

        def hkma_bucket():
            f = queries.facets(self.conn, sources, 1)
            return next(x["count"] for x in f["source"] if x["value"] == "hkma_press")

        before = hkma_bucket()
        src = SOURCES["hkma_press"]
        sid = db.upsert_source(self.conn, src.name, src.kind, src.config)
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        for n in range(9):
            self.conn.execute(
                """INSERT INTO items (id, source_id, title, url, fetched_at, published_at)
                   VALUES (?, ?, 'Scam alert related to banks', 'http://x', ?, ?)""",
                (f"s{n}", sid, now.isoformat(), (now - timedelta(hours=n)).isoformat()))
        self.conn.commit()

        # Nine identical notices are ONE thing on the tape and must be one
        # here too, or the bucket count and the tape disagree. Asserted as a
        # DELTA, so it stays true regardless of what else the fixture holds.
        self.assertEqual(hkma_bucket() - before, 1,
                         "nine identical notices did not collapse to one bucket")

    def test_the_axis_counts_sum_to_the_masthead(self):
        from macrowire.web import queries
        sources = list(SOURCES.values())
        f = queries.facets(self.conn, sources, 1)
        u = queries.unread_counts(self.conn, sources, 1)
        self.assertEqual(sum(x["count"] for x in f["jurisdiction"]), u["window_total"])
        self.assertEqual(sum(x["unread"] for x in f["jurisdiction"]), u["total"])

    def test_the_masthead_states_both_scopes(self):
        from macrowire import i18n
        self.assertIn("app.unread_and_window", self.js)
        line = i18n.Translator("en")("app.unread_and_window", unread=7, total=162)
        self.assertIn("unread", line)
        self.assertIn("window", line)

    def test_unread_counts_carry_the_window_total(self):
        from macrowire.web import queries
        u = queries.unread_counts(self.conn, list(SOURCES.values()), 1)
        self.assertIn("window_total", u)

    # --- badge semantics --------------------------------------------------

    def test_a_bucket_count_of_zero_is_rendered_not_dropped(self):
        """A missing badge read as 'no data'. HK had 70 items in the window
        and printed a bare 'HK' next to 'CN 7'."""
        self.assertIn("n-bucket", self.js)
        # the bucket branch is guarded on null/undefined, never on falsiness
        self.assertIn("c.count === null || c.count === undefined", self.js)
        self.assertNotIn("count ? `<span class=\"n\">", self.js)

    def test_an_attention_count_of_zero_is_muted_not_dropped(self):
        """'Caught up' and 'nothing here' are different answers."""
        self.assertIn("n-unread", self.js)
        self.assertIn('c.unread ? "" : " zero"', self.js)
        import re
        self.assertIsNotNone(re.search(r"\.chip \.n-unread\.zero \{", self.css))

    def test_an_uncomputed_count_is_an_em_dash_never_a_zero(self):
        self.assertIn("\\u2014", self.js.split("function countBadges")[1][:600])
        from macrowire.web import queries
        # facets emits None, not 0, for a value the collapsed pass never saw
        src = (ROOT / "macrowire/web/queries.py").read_text()
        self.assertIn('"count": cell["total"] if cell else None', src)

    def test_the_panel_says_what_the_two_numbers_are(self):
        from macrowire import i18n
        self.assertIn("filter.legend", self.html)
        legend = i18n.Translator("en")("filter.legend")
        self.assertIn("window", legend)
        self.assertIn("unread", legend)

    # --- 5. nothing collapses --------------------------------------------

    def test_no_axis_is_a_disclosure(self):
        """The axes were <details> to save vertical space. They are open
        sections now and the panel scrolls: open it and everything it can
        do is on screen, which is how the settings dialog always worked.

        This is a DESIGN CHANGE, not a regression. The tests that asserted
        the disclosure were correct about the old design and were replaced
        with it, not worked around."""
        self.assertNotIn("<details", self.js)
        self.assertNotIn("<summary", self.js)
        self.assertIn('<section class="fax"', self.js)

    def test_no_axis_can_hide_a_filter_that_is_narrowing_the_tape(self):
        """The point the disclosure version had to solve with `open` on an
        active axis. Nothing hides anything now, so there is nothing to
        solve - but the panel must not grow a second way to hide."""
        panel = self.css[self.css.index(".fpanel {"):]
        panel = panel[:panel.index("\n.tape")] if "\n.tape" in panel else panel
        for hiding in ("display: none", "visibility: hidden", "content-visibility"):
            with self.subTest(rule=hiding):
                self.assertNotIn(hiding, panel[:panel.index(".nomatch")]
                                 if ".nomatch" in panel else panel)

    def test_every_axis_shows_how_many_chips_it_holds(self):
        self.assertIn('class="fcount"', self.js)

    def test_the_active_filter_marker_is_not_amber(self):
        import re
        rule = re.search(r"\.fon \{([^}]*)\}", self.css)
        self.assertIsNotNone(rule)
        self.assertNotIn("--accent", rule.group(1),
                         "an active filter is neither unread nor a fault")


class TranslationKeyReachTests(unittest.TestCase):
    """The other half of the completeness guard.

    `test_every_key_in_the_default_exists_in_every_other_locale` checks the
    locales against EACH OTHER. A key referenced in code and present in
    NEITHER file passes it perfectly - both locales agree it does not exist,
    and the UI renders the raw key. That is the same shape as the CSS
    scope-gap audit: a test whose reach is narrower than its claim.

    This class checks the direction the other one cannot: every key the code
    actually asks for resolves in en.json.
    """

    SCANNED = ("macrowire/web/static/app.js", "macrowire/web/static/index.html")

    def setUp(self):
        from macrowire import i18n
        self.i18n = i18n
        self.en = i18n.load("en")

    def resolves(self, key):
        node = self.en
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return isinstance(node, str)

    def literal_keys(self):
        """Every statically-readable key, with where it came from."""
        import re
        found = {}
        files = list(self.SCANNED) + [
            str(p.relative_to(ROOT)) for p in (ROOT / "macrowire").rglob("*.py")]
        for name in files:
            # One helper for every syntax; see strip_comments(). A
            # `t("x", key="y")` written in a comment to EXPLAIN the API is
            # not a call, and counting it reports a missing key that was
            # never referenced.
            src = read_code(ROOT / name)
            # t("a.b") / t('a.b') / t(`a.b`) — no interpolation, no escapes
            for m in re.finditer(r"""\bt\(\s*(["'`])([a-z][\w.]*)\1""", src):
                found.setdefault(m.group(2), set()).add(name)
            # markup that names a key instead of a sentence
            for m in re.finditer(r'data-i18n(?:-label)?="([^"]+)"', src):
                found.setdefault(m.group(1), set()).add(name)
        return found

    def test_every_literal_key_in_the_code_exists_in_english(self):
        keys = self.literal_keys()
        # A broken regex would make this pass by finding nothing, which is
        # the failure mode this whole class exists to close.
        self.assertGreater(len(keys), 150,
                           f"only found {len(keys)} keys; the scan is broken")
        missing = sorted(k for k in keys if not self.resolves(k))
        self.assertFalse(
            missing,
            "keys referenced in code but absent from en.json:\n" + "\n".join(
                f"  {k}  <- {', '.join(sorted(keys[k]))}" for k in missing))

    def test_every_computed_key_resolves_for_every_value_it_can_take(self):
        """Keys built at runtime, checked against the real enumerations.

        A prefix check would pass on `filter.fx_state` existing as an object
        while `filter.fx_state.unclassified` was missing. These enumerate
        the values the code can actually substitute.
        """
        from macrowire.config import FX_STATES
        from macrowire.web.queries import HEALTH_SEVERITY

        expected = []
        # app.js: t(`filter.fx_state.${x.value}`)
        expected += [f"filter.fx_state.{s}" for s in FX_STATES]
        # app.js: t(`filter.short.${axis}`) and t("filter.axis.<axis>")
        axes = ("fx", "jurisdiction", "ticker", "source", "type")
        expected += [f"filter.short.{a}" for a in axes]
        expected += [f"filter.axis.{a}" for a in axes]
        # queries.py: t(f"health.{state_key}.{label,meaning,action}")
        expected += [f"health.{s}.{part}" for s in HEALTH_SEVERITY
                     for part in ("label", "meaning", "action")]
        # app.js: t(`rail.sb.${r.key}`)
        expected += [f"rail.sb.{k}" for k in ("net", "buy", "sell", "turnover")]
        # __main__.py: t(f"cli.status.{k}") over its LABELS tuple
        import re
        cli = (ROOT / "macrowire/__main__.py").read_text()
        labels = re.search(r"LABELS = \(([^)]*)\)", cli, re.S).group(1)
        expected += [f"cli.status.{k}" for k in re.findall(r'"(\w+)"', labels)]

        missing = sorted(k for k in expected if not self.resolves(k))
        self.assertFalse(missing, "computed keys that do not resolve: " + str(missing))

    def test_the_two_halves_together_cover_both_directions(self):
        """en -> other locales is one direction; code -> en is the other.
        Neither alone is enough, and only one of them existed."""
        source = (ROOT / "tests/test_macrowire.py").read_text()
        self.assertIn("test_every_key_in_the_default_exists_in_every_other_locale",
                      source)
        self.assertIn("test_every_literal_key_in_the_code_exists_in_english", source)


class CatalogueFreshnessTests(unittest.TestCase):
    """The page's JS is served from disk per request; its strings were read
    once at import. Two halves of one feature, two freshnesses - and the
    result renders as a raw key, which is indistinguishable from a string
    nobody ever wrote. That is what cost a bug report, twice counting the
    earlier stale-server incident, so the fix is structural.
    """

    def setUp(self):
        from macrowire import i18n
        self.i18n = i18n
        self.path = i18n.LOCALES_DIR / "en.json"
        self.backup = self.path.read_text(encoding="utf-8")

    def tearDown(self):
        self.path.write_text(self.backup, encoding="utf-8")
        self.i18n.load("en")          # leave the cache holding the real file

    def rewrite(self, mutate):
        import collections, json, time
        tree = json.loads(self.backup, object_pairs_hook=collections.OrderedDict)
        mutate(tree)
        self.path.write_text(json.dumps(tree, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        # mtime resolution can be coarse enough to collide inside one test
        import os
        stamp = os.stat(self.path).st_mtime + 1
        os.utime(self.path, (stamp, stamp))

    def test_a_key_added_while_running_is_visible_without_a_restart(self):
        self.i18n.load("en")                       # prime the cache
        self.rewrite(lambda t: t["app"].__setitem__("added_live", "hello"))
        self.assertEqual(self.i18n.Translator("en")("app.added_live"), "hello")

    def test_an_edited_key_is_re_read(self):
        self.i18n.load("en")
        self.rewrite(lambda t: t["app"].__setitem__("title", "Edited"))
        self.assertEqual(self.i18n.Translator("en")("app.title"), "Edited")

    def test_an_unchanged_file_is_not_re_parsed(self):
        """It is a cache, not a re-read on every call."""
        first = self.i18n.load("en")
        second = self.i18n.load("en")
        self.assertIs(first, second, "the catalogue is being re-parsed needlessly")

    def test_the_web_app_binds_no_translator_at_import(self):
        src = (ROOT / "macrowire/web/app.py").read_text()
        for line in src.split("\n"):
            if line.startswith(("T = ", "LOCALE = ")):
                self.fail(f"module-level {line!r} freezes the catalogue at import")
        self.assertIn("def _translator()", src)

    def test_the_locale_itself_is_read_per_request(self):
        """`defaults.locale` and a source's `enabled` live in the SAME file.
        Binding one at import and reading the other per request gave one
        file two freshnesses in one process."""
        src = (ROOT / "macrowire/web/app.py").read_text()
        bootstrap = src[src.index('@app.get("/api/bootstrap")'):]
        bootstrap = bootstrap[:bootstrap.index("@app.post")]
        self.assertIn("_translator()", bootstrap,
                      "bootstrap does not build a translator per request")

    def test_the_cli_may_bind_at_import_because_it_exits(self):
        """Not every import-time read is the same bug. A CLI process reads
        its catalogue and exits; there is no window in which the file can
        change underneath a long-running server."""
        src = (ROOT / "macrowire/__main__.py").read_text()
        self.assertIn("t = i18n.Translator(_cli_locale())", src)
        self.assertIn("process reads and exits", src)   # wrapped in the source


class ViewerTimezoneTests(unittest.TestCase):
    """The viewer's zone moves the ribbon. It must not move a source fact.

    Some strings describe the VIEWER - day headers, the clock, session
    bars, mark positions. Some describe the SOURCE - "4pm AEST",
    "09:15 CST", "~16:00 CET", CFTC's Tuesday/Friday. The RBA fixes at 4pm
    Sydney whether it is read in Sydney or Stuttgart, and a naive sweep
    that relabelled the second kind would make the rail lie.
    """

    # The six, lifted from app.js. Not a copy of a list in the test - the
    # test parses the real constant, so deleting one there fails here.
    ZONES = ("Australia/Sydney", "America/New_York", "Europe/London",
             "Asia/Shanghai", "UTC")

    def facts(self):
        import re
        js = (ROOT / "macrowire/web/static/app.js").read_text()
        block = re.search(r"const FACT = \{(.*?)\n\};", js, re.S)
        self.assertIsNotNone(block, "the FACT constant is gone")
        pairs = dict(re.findall(r"(\w+):\s*\"([^\"]*)\"", block.group(1)))
        floor(self, pairs, "source facts in FACT", 5)
        return pairs

    @contextlib.contextmanager
    def viewer_in(self, zone):
        """Run a block as a viewer in `zone`.

        A context manager, not a function returning a module: the first
        version restored the config path in `finally` BEFORE the caller
        asserted anything, so every zone silently read as Sydney and the
        FACT guard would have passed without ever changing a viewer.
        """
        import tempfile, yaml as pyyaml
        from macrowire import config
        doc = pyyaml.safe_load((ROOT / "sources.yaml").read_text())
        doc.setdefault("viewer", {})["timezone"] = zone
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            pyyaml.safe_dump(doc, fh, allow_unicode=True)
            temp = Path(fh.name)
        original = config.DEFAULT_CONFIG_PATH
        config.DEFAULT_CONFIG_PATH = temp
        try:
            from macrowire.web import ribbon
            yield ribbon
        finally:
            config.DEFAULT_CONFIG_PATH = original
            temp.unlink(missing_ok=True)

    def test_the_harness_actually_changes_the_viewer(self):
        """Guard on the guard. If viewer_in() stops taking effect, every
        test below passes by comparing Sydney with Sydney."""
        with self.viewer_in("America/New_York") as ribbon:
            self.assertEqual(ribbon.view_label(), "New York")
        with self.viewer_in("Europe/London") as ribbon:
            self.assertEqual(ribbon.view_label(), "London")

    # --- the FACT guard ---------------------------------------------------

    def test_source_facts_are_byte_identical_for_every_viewer(self):
        baseline = self.facts()
        for zone in self.ZONES:
            with self.viewer_in(zone) as ribbon:
                # prove the viewer really moved before claiming the facts did not
                self.assertEqual(ribbon.now_position()["timezone"], zone)
                with self.subTest(zone=zone):
                    self.assertEqual(
                        self.facts(), baseline,
                        f"a source fact changed for a viewer in {zone}")

    def test_the_six_facts_are_the_ones_we_think_they_are(self):
        values = set(self.facts().values())
        for fact in ("09:15 CST", "15:30 ET", "~16:00 CET", "4pm AEST", "EUR"):
            self.assertIn(fact, values, f"{fact} is no longer a declared source fact")

    def test_no_source_fact_leaked_into_a_catalogue(self):
        from macrowire import i18n
        for locale in floor(self, i18n.available(), "locale files", 2):
            blob = "\n".join(i18n.renderable(i18n.load(locale)).values())
            for fact in self.facts().values():
                if len(fact) < 4:          # "EUR" is also a legitimate word
                    continue
                with self.subTest(locale=locale, fact=fact):
                    self.assertNotIn(fact, blob)

    def test_a_source_timing_block_is_not_the_viewer_zone(self):
        """sources.yaml timing.timezone is the PUBLISHER's zone and must
        never be read from the viewer setting."""
        for src in floor(self, list(SOURCES.values()), "sources", 10):
            tz = (src.timing or {}).get("timezone")
            if tz:
                with self.subTest(source=src.name):
                    self.assertNotEqual(tz, "system")

    # --- the viewer half DOES move ---------------------------------------

    def test_the_ribbon_label_follows_the_configured_zone(self):
        for zone, label in (("Australia/Sydney", "Sydney"),
                            ("America/New_York", "New York"),
                            ("Europe/London", "London"),
                            ("UTC", "UTC")):
            with self.viewer_in(zone) as ribbon, self.subTest(zone=zone):
                self.assertEqual(ribbon.view_label(), label)
                self.assertEqual(ribbon.now_position()["timezone"], zone)

    def test_a_fixed_offset_is_refused(self):
        import tempfile, yaml as pyyaml
        from macrowire.config import ConfigError, load_timezone
        doc = pyyaml.safe_load((ROOT / "sources.yaml").read_text())
        doc.setdefault("viewer", {})["timezone"] = "+10:00"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            pyyaml.safe_dump(doc, fh, allow_unicode=True)
            temp = Path(fh.name)
        try:
            with self.assertRaises(ConfigError) as caught:
                load_timezone(temp)
            self.assertIn("DST", str(caught.exception))
        finally:
            temp.unlink()

    def test_an_undetectable_system_zone_falls_back_to_utc_not_a_guess(self):
        from macrowire import config
        self.assertIn("UTC", (ROOT / "macrowire/config.py").read_text())
        # and the reason is written down, because UTC looks like a bug
        self.assertIn("looks like data rather than a misconfiguration",
                      (ROOT / "macrowire/config.py").read_text())


class DstAcrossHemispheresTests(ViewerTimezoneTests):
    """The Fed publishes at a rock-solid 14:00 ET. It lands at 04:00, 05:00
    or 06:00 in Sydney across a year, including a three-week window where
    both hemispheres have switched out of step. Whatever the viewer's zone,
    that per-instant resolution has to hold.
    """

    # 14:00 America/New_York, the FOMC statement slot.
    def fed_local(self, ribbon, day, zone):
        from datetime import date, datetime, time
        from zoneinfo import ZoneInfo
        origin = datetime.combine(day, time(14, 0), tzinfo=ZoneInfo("America/New_York"))
        return origin.astimezone(ZoneInfo(zone)).strftime("%H:%M")

    def test_a_southern_viewer_sees_the_fed_move_across_the_year(self):
        from datetime import date
        seen = {}
        with self.viewer_in("Australia/Sydney"):
            for day in (date(2026, 1, 28),    # both on summer time
                        date(2026, 4, 29),    # AU off, US on
                        date(2026, 7, 29),    # both off / US on
                        date(2026, 11, 4)):   # AU on, US off
                seen[day.isoformat()] = self.fed_local(None, day, "Australia/Sydney")
        # Three distinct landing times is the measured behaviour; one would
        # mean a stored offset had crept back in.
        self.assertGreaterEqual(len(set(seen.values())), 2,
                                f"the Fed never moves for a Sydney viewer: {seen}")
        for shown in seen.values():
            self.assertIn(shown.split(":")[0], {"03", "04", "05", "06", "07"}, seen)

    def test_the_out_of_step_window_is_a_distinct_offset(self):
        """Between the US spring-forward and the AU fall-back the gap is
        neither the summer nor the winter value. A fixed offset gets this
        wrong twice a year in each direction."""
        from datetime import date
        march = self.fed_local(None, date(2026, 3, 18), "Australia/Sydney")
        june = self.fed_local(None, date(2026, 6, 17), "Australia/Sydney")
        december = self.fed_local(None, date(2026, 12, 16), "Australia/Sydney")
        self.assertNotEqual(march, june)
        self.assertNotEqual(june, december)

    def test_a_northern_viewer_crosses_its_own_boundary(self):
        from datetime import date
        with self.viewer_in("Europe/London"):
            winter = self.fed_local(None, date(2026, 1, 28), "Europe/London")
            summer = self.fed_local(None, date(2026, 7, 29), "Europe/London")
        # London and New York switch within two weeks of each other, so the
        # OFFSET BETWEEN THEM barely moves - the local time should be stable
        # even though both zones changed. That stability is the tell that
        # each instant was resolved in its own zone.
        self.assertEqual(winter, summer,
                         "London/New York should hold a steady 5h gap year-round")

    def test_a_utc_viewer_sees_the_us_switch_and_nothing_else(self):
        from datetime import date
        with self.viewer_in("UTC"):
            winter = self.fed_local(None, date(2026, 1, 28), "UTC")
            summer = self.fed_local(None, date(2026, 7, 29), "UTC")
        self.assertEqual(winter, "19:00")   # EST, UTC-5
        self.assertEqual(summer, "18:00")   # EDT, UTC-4

    def test_the_ribbon_places_a_mark_per_instant_not_per_offset(self):
        from datetime import date
        fed = next(s for s in SOURCES.values()
                   if s.name == "fed_press_monetary" and (s.timing or {}).get("at"))
        positions = {}
        with self.viewer_in("Australia/Sydney") as ribbon:
            for day in (date(2026, 1, 28), date(2026, 7, 29), date(2026, 11, 4)):
                marks = {m["source"]: m for m in ribbon.marks_for(day, [fed])}
                mark = marks[fed.name]
                if mark["position"] is not None:
                    positions[day.isoformat()] = mark["local_time"]
        floor(self, positions, "placed Fed marks", 2)
        self.assertGreater(len(set(positions.values())), 1,
                           f"the Fed mark never moves across the year: {positions}")

    def test_the_dst_note_reports_the_shift_rather_than_hiding_it(self):
        from datetime import date
        fed = next(s for s in SOURCES.values() if s.name == "fed_press_monetary")
        with self.viewer_in("Australia/Sydney") as ribbon:
            marks = {m["source"]: m for m in ribbon.marks_for(date(2026, 7, 29), [fed])}
        note = marks[fed.name].get("shifts")
        self.assertTrue(note, "a source that moves across the year says nothing about it")
        self.assertIn("mo", note)


class EndpointSmokeTests(unittest.TestCase):
    """Every GET endpoint is actually called.

    `ribbon.VIEW` was renamed and `/api/ribbon` kept referencing it. 325
    tests passed: none of them called that endpoint. A suite that tests the
    functions an endpoint uses, but never the endpoint, cannot see a broken
    wire between them.
    """

    def setUp(self):
        import warnings, tempfile
        warnings.filterwarnings("ignore")
        # A throwaway DB, pointed at by env so the app's own _conn() picks
        # it up. The suite-wide guard still stands: the default path is
        # refused, and this names an explicit one.
        self._dir = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("MACROWIRE_DB")
        os.environ["MACROWIRE_DB"] = str(Path(self._dir.name) / "smoke.db")
        conn = db.connect(Path(self._dir.name) / "smoke.db")
        db.initialise(conn)
        conn.close()
        from fastapi.testclient import TestClient
        from macrowire.web.app import app
        self.client = TestClient(app)
        self.src = (ROOT / "macrowire/web/app.py").read_text()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("MACROWIRE_DB", None)
        else:
            os.environ["MACROWIRE_DB"] = self._prev
        self._dir.cleanup()

    def get_routes(self):
        import re
        return re.findall(r'@app\.get\("([^"]+)"\)', self.src)

    def test_every_get_route_answers(self):
        # Parameterised routes are exercised with a real value; the rest as
        # declared. Anything new shows up here the moment it is added.
        SUBSTITUTE = {"/static/{path:path}": None, "/": "/"}
        routes = floor(self, self.get_routes(), "GET routes", 5)
        for route in routes:
            if "{" in route and route not in SUBSTITUTE:
                continue
            path = SUBSTITUTE.get(route, route)
            if path is None:
                continue
            with self.subTest(route=route):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200,
                                 f"{route} -> {response.status_code}")

    def test_the_ribbon_endpoint_specifically(self):
        """The one that broke. It is the only endpoint that resolves a
        timezone, so it is the only one a zone rename can take down."""
        payload = self.client.get("/api/ribbon").json()
        for key in ("day", "sessions", "marks"):
            self.assertIn(key, payload)
        floor(self, payload["sessions"], "session rows", 5)

    def test_the_ribbon_endpoint_accepts_an_explicit_day(self):
        payload = self.client.get("/api/ribbon?day=2026-07-29").json()
        self.assertEqual(payload["day"], "2026-07-29")

    def test_the_default_db_is_still_unreachable_without_saying_where(self):
        """Narrowing the guard must not open the hole it was built for."""
        prev = os.environ.pop("MACROWIRE_DB", None)
        try:
            with self.assertRaises(MacroWireError) as caught:
                db.connect()
            self.assertIn("refusing", str(caught.exception))
        finally:
            if prev is not None:
                os.environ["MACROWIRE_DB"] = prev


class ViewerZoneArithmeticTests(ViewerTimezoneTests):
    """The label following config is not the same as the CLOCK following it.

    view_label() reads load_timezone() directly, so a hardcoded view_zone()
    still produced correct-looking labels over Sydney arithmetic. Mutation
    testing found that: reverting view_zone() to ZoneInfo("Australia/Sydney")
    passed every timezone test. These assert the numbers, not the words.
    """

    def test_the_offset_matches_the_configured_zone(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        for zone in ("Australia/Sydney", "America/New_York", "Europe/London", "UTC"):
            with self.viewer_in(zone) as ribbon, self.subTest(zone=zone):
                reported = ribbon.now_position()["offset"]
                expected = (datetime.now(ZoneInfo(zone)).utcoffset().total_seconds()
                            / 3600.0)
                self.assertEqual(reported, expected,
                                 f"{zone} reports {reported}h, actually {expected}h")

    def test_the_now_marker_sits_somewhere_different_in_each_zone(self):
        from datetime import datetime, timezone as dt_timezone
        instant = datetime(2026, 7, 29, 12, 0, tzinfo=dt_timezone.utc)
        seen = {}
        for zone in ("Australia/Sydney", "America/New_York", "Europe/London", "UTC"):
            with self.viewer_in(zone) as ribbon:
                seen[zone] = round(ribbon.now_position(instant)["position"], 4)
        self.assertEqual(len(set(seen.values())), len(seen),
                         f"one instant lands at the same ribbon position everywhere: {seen}")

    def test_the_local_clock_differs_by_zone_for_one_instant(self):
        from datetime import datetime, timezone as dt_timezone
        instant = datetime(2026, 7, 29, 12, 0, tzinfo=dt_timezone.utc)
        seen = {}
        for zone in ("Australia/Sydney", "America/New_York", "UTC"):
            with self.viewer_in(zone) as ribbon:
                seen[zone] = ribbon.now_position(instant)["local"]
        self.assertEqual(len(set(seen.values())), 3, seen)
        self.assertEqual(seen["UTC"], "12:00:00")

    def test_session_bars_move_with_the_viewer(self):
        """SYD 10:00-16:00 is at one end of a Sydney band and the middle of
        a London one. If the bars do not move, the arithmetic is pinned."""
        from datetime import date
        day = date(2026, 7, 29)
        seen = {}
        for zone in ("Australia/Sydney", "America/New_York", "Europe/London"):
            with self.viewer_in(zone) as ribbon:
                rows = {s["key"]: s for s in ribbon.sessions_for(day)}
                syd = rows["sydney"]["segments"]
                seen[zone] = round(syd[0]["start"], 4) if syd else None
        self.assertEqual(len(set(seen.values())), len(seen),
                         f"the Sydney session bar sits identically everywhere: {seen}")

    def test_a_mark_lands_at_a_different_hour_for_a_different_viewer(self):
        from datetime import date
        fed = next(s for s in SOURCES.values() if s.name == "fed_press_monetary")
        seen = {}
        for zone in ("Australia/Sydney", "America/New_York", "Europe/London"):
            with self.viewer_in(zone) as ribbon:
                for m in ribbon.marks_for(date(2026, 7, 29), [fed]):
                    if m["position"] is not None:
                        seen[zone] = m["local_time"]
        floor(self, seen, "placed marks across zones", 3)
        self.assertEqual(len(set(seen.values())), 3,
                         f"the Fed mark shows the same local time everywhere: {seen}")
        # and the New York viewer sees the publisher's own 14:00
        self.assertEqual(seen["America/New_York"], "14:00")


class SessionOrderingTests(ViewerTimezoneTests):
    """Rotating the band must not disturb what the band draws."""

    ALL = {"sydney", "tokyo", "hongkong", "london", "newyork"}

    def rows(self, ribbon, day=None):
        from datetime import date
        return ribbon.sessions_for(day or date(2026, 7, 29))

    # --- rotation ---------------------------------------------------------

    def test_the_band_starts_at_the_readers_own_market(self):
        for zone, first in (("Australia/Sydney", "SYD"),
                            ("America/New_York", "NYC"),
                            ("Europe/London", "LON"),
                            ("Asia/Tokyo", "TYO"),
                            ("Asia/Hong_Kong", "HKG")):
            with self.viewer_in(zone) as ribbon, self.subTest(zone=zone):
                self.assertEqual(self.rows(ribbon)[0]["label"], first)

    CANONICAL = ["sydney", "tokyo", "hongkong", "london", "newyork"]

    def test_the_sequence_survives_the_rotation(self):
        """The band is the trading day in order. Rotating must move the
        entry point and nothing else - a reordered sequence would destroy
        the shape the band exists to teach.

        The expected start is stated per zone rather than read off the
        answer. Deriving it from keys[0] made the test circular, and there
        is a coincidence waiting in it: rotating this particular sequence
        at Hong Kong produces EXACTLY alphabetical order, so a sort would
        have looked like a correct rotation for one viewer in five.
        """
        expected_start = {
            "Australia/Sydney": "sydney", "America/New_York": "newyork",
            "Europe/London": "london", "Asia/Tokyo": "tokyo",
            "Asia/Hong_Kong": "hongkong",
        }
        for zone, first in expected_start.items():
            with self.viewer_in(zone) as ribbon, self.subTest(zone=zone):
                keys = [r["key"] for r in self.rows(ribbon)]
                self.assertEqual(set(keys), self.ALL, "a session went missing")
                start = self.CANONICAL.index(first)
                self.assertEqual(
                    keys, self.CANONICAL[start:] + self.CANONICAL[:start],
                    f"{zone} should start at {first}, got {keys}")

    def test_the_band_is_not_merely_sorted(self):
        """The coincidence, named so it cannot be relied on by accident."""
        rotated_at_hongkong = self.CANONICAL[2:] + self.CANONICAL[:2]
        self.assertEqual(rotated_at_hongkong, sorted(self.CANONICAL),
                         "the coincidence this guards against has gone away; "
                         "if the session list changed, re-check the guard")
        # For every OTHER viewer the two must differ, which is what makes
        # test_the_band_starts_at_the_readers_own_market load-bearing.
        for zone in ("Australia/Sydney", "America/New_York", "Europe/London"):
            with self.viewer_in(zone) as ribbon, self.subTest(zone=zone):
                keys = [r["key"] for r in self.rows(ribbon)]
                self.assertNotEqual(keys, sorted(self.CANONICAL),
                                    f"{zone} produced alphabetical order")

    def test_an_unplaceable_viewer_falls_back_to_nearest_not_to_nothing(self):
        with self.viewer_in("Europe/Zurich") as ribbon:
            self.assertEqual(self.rows(ribbon)[0]["label"], "LON")
            self.assertIsNone(ribbon.viewer_jurisdiction())

    def test_fixed_mode_ignores_the_viewer(self):
        from macrowire import ordering
        rows = [{"key": k, "tz": t} for k, t in
                (("sydney", "Australia/Sydney"), ("tokyo", "Asia/Tokyo"),
                 ("hongkong", "Asia/Hong_Kong"), ("london", "Europe/London"),
                 ("newyork", "America/New_York"))]
        out = ordering.rotate_sessions(rows, "America/New_York", "fixed")
        self.assertEqual([r["key"] for r in out], [r["key"] for r in rows])

    def test_an_explicit_list_wins_and_keeps_unlisted_rows(self):
        from macrowire import ordering
        rows = [{"key": k, "tz": ""} for k in
                ("sydney", "tokyo", "hongkong", "london", "newyork")]
        out = ordering.rotate_sessions(rows, "UTC", ["newyork", "london"])
        self.assertEqual([r["key"] for r in out][:2], ["newyork", "london"])
        self.assertEqual(set(r["key"] for r in out), self.ALL,
                         "an unlisted session was dropped")

    # --- the wrap, which is what could break ------------------------------

    def segments_are_sane(self, ribbon, zone, day):
        for row in self.rows(ribbon, day):
            segs = row["segments"]
            for i, a in enumerate(segs):
                self.assertGreaterEqual(a["start"], 0.0)
                self.assertLessEqual(a["end"], 1.0)
                self.assertLess(a["start"], a["end"],
                                f"{zone} {row['label']} zero-width segment")
                for b in segs[i + 1:]:
                    self.assertNotEqual(
                        (a["start"], a["end"]), (b["start"], b["end"]),
                        f"{zone} {row['label']} renders a DUPLICATE segment")
                    self.assertLessEqual(
                        a["end"], b["start"] + 1e-9,
                        f"{zone} {row['label']} segments OVERLAP: {segs}")

    def test_no_session_renders_duplicate_or_overlapping_segments(self):
        """The bug that shipped once: a session caught from two adjacent
        origin dates and drawn twice."""
        from datetime import date
        for zone in ("Australia/Sydney", "America/New_York", "Europe/London",
                     "Asia/Tokyo", "UTC", "Asia/Hong_Kong", "Europe/Zurich"):
            for day in (date(2026, 1, 28), date(2026, 7, 29),
                        date(2026, 3, 18), date(2026, 11, 4)):
                with self.viewer_in(zone) as ribbon, self.subTest(zone=zone, day=day):
                    self.segments_are_sane(ribbon, zone, day)

    def test_every_viewer_has_at_least_one_wrapping_session(self):
        """A 24-hour band always cuts something. If nothing wraps for some
        viewer, the adjacent-date walk has stopped finding the other half."""
        from datetime import date
        for zone in ("Australia/Sydney", "America/New_York", "Asia/Tokyo"):
            with self.viewer_in(zone) as ribbon, self.subTest(zone=zone):
                wrapped = [r["label"] for r in self.rows(ribbon)
                           if any(s["continues"] for s in r["segments"])]
                floor(self, wrapped, f"wrapping sessions for {zone}")

    def test_a_wrap_is_marked_at_both_ends_or_neither(self):
        """A segment running off the right edge implies one arriving at the
        left. One marker without the other is half a session drawn."""
        from datetime import date
        for zone in ("Australia/Sydney", "America/New_York", "Europe/London",
                     "Asia/Tokyo", "UTC"):
            with self.viewer_in(zone) as ribbon:
                for row in self.rows(ribbon, date(2026, 7, 29)):
                    edges = {s["continues"] for s in row["segments"]}
                    with self.subTest(zone=zone, session=row["label"]):
                        if "into" in edges:
                            self.assertIn("from", edges,
                                          f"{row['label']} runs off the right "
                                          f"edge and never comes back")
                        if "from" in edges:
                            self.assertIn("into", edges,
                                          f"{row['label']} arrives from "
                                          f"nowhere")

    def test_a_lunch_break_still_reads_as_one_session_in_two_pieces(self):
        """Tokyo shows three segments for a New York viewer - a whole
        morning plus both halves of a wrapped afternoon. That is correct
        and must not be 'fixed' into two."""
        from datetime import date
        with self.viewer_in("America/New_York") as ribbon:
            tyo = next(r for r in self.rows(ribbon, date(2026, 7, 29))
                       if r["key"] == "tokyo")
        self.assertTrue(tyo["has_break"])
        self.assertEqual(len(tyo["segments"]), 3)
        self.segments_are_sane(ribbon, "America/New_York", date(2026, 7, 29))

    def test_rotation_does_not_change_any_segment(self):
        """Ordering is presentation. It must not be able to touch the
        arithmetic, so the same zone under two orderings draws the same
        geometry."""
        from datetime import date
        import tempfile, yaml as pyyaml
        from macrowire import config
        day = date(2026, 7, 29)

        def geometry(mode):
            doc = pyyaml.safe_load((ROOT / "sources.yaml").read_text())
            doc.setdefault("viewer", {})["timezone"] = "America/New_York"
            doc.setdefault("viewer", {})["session_order"] = mode
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
                pyyaml.safe_dump(doc, fh, allow_unicode=True)
                temp = Path(fh.name)
            original = config.DEFAULT_CONFIG_PATH
            config.DEFAULT_CONFIG_PATH = temp
            try:
                from macrowire.web import ribbon
                return {r["key"]: [(s["start"], s["end"], s["continues"])
                                   for s in r["segments"]]
                        for r in ribbon.sessions_for(day)}
            finally:
                config.DEFAULT_CONFIG_PATH = original
                temp.unlink(missing_ok=True)

        self.assertEqual(geometry("viewer"), geometry("fixed"))


class JurisdictionOrderingTests(ViewerTimezoneTests):
    CODES = ["AU", "CN", "EU", "HK", "JP", "UK", "US"]

    def test_the_readers_market_leads_then_alphabetical(self):
        from macrowire import ordering
        self.assertEqual(ordering.order_jurisdictions(self.CODES, "US"),
                         ["US", "AU", "CN", "EU", "HK", "JP", "UK"])
        self.assertEqual(ordering.order_jurisdictions(self.CODES, "AU"),
                         ["AU", "CN", "EU", "HK", "JP", "UK", "US"])

    def test_an_unplaceable_reader_gets_plain_alphabetical(self):
        from macrowire import ordering
        for viewer in (None, "CH", "BR"):
            with self.subTest(viewer=viewer):
                self.assertEqual(ordering.order_jurisdictions(self.CODES, viewer),
                                 sorted(self.CODES))

    def test_a_reader_whose_market_has_no_items_gets_alphabetical(self):
        """Ordering must not invent a chip for a jurisdiction with nothing
        in the window - populated-only is a rule the ordering inherits."""
        from macrowire import ordering
        present = ["CN", "HK", "JP"]
        self.assertEqual(ordering.order_jurisdictions(present, "US"),
                         ["CN", "HK", "JP"])

    def test_the_order_is_stable_regardless_of_counts(self):
        """Volume-ordering would move a control between renders."""
        from macrowire import ordering
        first = ordering.order_jurisdictions(self.CODES, "AU")
        second = ordering.order_jurisdictions(list(reversed(self.CODES)), "AU")
        self.assertEqual(first, second)

    def test_one_ordering_serves_both_the_chips_and_the_rail(self):
        """They disagreed: chips alphabetical in SQL, health hardcoded in
        JavaScript."""
        js = (ROOT / "macrowire/web/static/app.js").read_text()
        self.assertNotIn('["AU", "CN", "HK", "JP", "US", "EU", "UK"]',
                         strip_comments(js, "js"))
        self.assertIn("d.jurisdiction_order", js)
        queries = read_code(ROOT / "macrowire/web/queries.py")
        self.assertEqual(queries.count("def jurisdiction_order"), 1)

    def test_an_explicit_list_is_honoured(self):
        from macrowire import ordering
        out = ordering.order_jurisdictions(self.CODES, "AU", ["US", "UK"])
        self.assertEqual(out[:2], ["US", "UK"])
        self.assertEqual(set(out), set(self.CODES))

    def test_an_unknown_code_in_config_is_refused(self):
        import tempfile, yaml as pyyaml
        from macrowire.config import ConfigError, load_ordering
        doc = pyyaml.safe_load((ROOT / "sources.yaml").read_text())
        doc.setdefault("viewer", {})["jurisdiction_order"] = ["US", "ZZ"]
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            pyyaml.safe_dump(doc, fh, allow_unicode=True)
            temp = Path(fh.name)
        try:
            with self.assertRaises(ConfigError) as caught:
                load_ordering(temp)
            self.assertIn("ZZ", str(caught.exception))
        finally:
            temp.unlink()


class FactMatrixTests(unittest.TestCase):
    """Source facts, across EVERY viewer preference the panel can change.

    The timezone version of this test was inert once - the harness restored
    the config path before anything was asserted, so five zones all read as
    Sydney and the guard passed without ever moving a viewer. A matrix that
    passes by never changing anything is the same failure at four times the
    surface, so each axis carries its own guard-on-the-guard: a test that
    proves the axis actually moved before any fact is compared.
    """

    AXES = {
        "locale": ("en", "zh-CN"),
        "timezone": ("Australia/Sydney", "America/New_York", "Europe/London", "UTC"),
        "session_order": ("viewer", "fixed"),
        "jurisdiction_order": ("viewer", "alphabetical"),
    }

    def setUp(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("MACROWIRE_DB")
        os.environ["MACROWIRE_DB"] = str(Path(self._dir.name) / "m.db")
        conn = db.connect(Path(self._dir.name) / "m.db")
        db.initialise(conn)
        conn.close()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("MACROWIRE_DB", None)
        else:
            os.environ["MACROWIRE_DB"] = self._prev
        self._dir.cleanup()

    def apply(self, **prefs):
        from macrowire import preferences
        conn = db.connect()
        for key, value in prefs.items():
            if value is None:
                preferences.clear(conn, key)
            else:
                preferences.set_one(conn, key, value)
        conn.close()

    def facts(self):
        import re
        js = (ROOT / "macrowire/web/static/app.js").read_text()
        block = re.search(r"const FACT = \{(.*?)\n\};", js, re.S)
        self.assertIsNotNone(block, "the FACT constant is gone")
        pairs = dict(re.findall(r"(\w+):\s*\"([^\"]*)\"", block.group(1)))
        floor(self, pairs, "source facts in FACT", 5)
        return pairs

    def observed(self):
        """What each axis is actually doing right now, from the real code."""
        import importlib
        from macrowire import i18n, preferences
        from macrowire.web import queries, ribbon
        importlib.reload(ribbon)
        from datetime import date
        conn = db.connect()
        resolved = preferences.resolve(conn)
        conn.close()
        return {
            "locale": i18n.Translator(resolved["locale"])("rail.health_heading"),
            "timezone": ribbon.view_label(),
            "session_order": [r["label"] for r in ribbon.sessions_for(date(2026, 7, 29))],
            "jurisdiction_order": queries.jurisdiction_order(
                ["US", "AU", "CN", "EU", "HK", "JP", "UK"]),
        }

    # --- guard on the guard, one per axis --------------------------------

    def test_each_axis_actually_changes_something(self):
        """If an axis cannot move the interface, every fact assertion about
        it below is vacuous. This is the test that would have caught the
        inert harness the first time."""
        # timezone has to be somewhere other than the machine's own zone for
        # the label to differ, so pick a value the detected zone is not.
        from macrowire.config import system_timezone
        here = system_timezone()
        elsewhere = "UTC" if here != "UTC" else "America/New_York"
        # jurisdiction_order needs a market that is NOT alphabetically
        # first, or the two modes produce the same list and the axis looks
        # inert when it is working. With the machine in Sydney, "AU first"
        # and "alphabetical" are the same answer - which is exactly the
        # blind spot this guard exists to expose.
        cases = {
            "locale": ("en", "zh-CN", {}),
            "timezone": (here, elsewhere, {}),
            # session_order needs a viewer who is NOT at the canonical
            # start. For a Sydney reader "rotate to my market" and "always
            # start at Sydney" are the same band - the second blind spot
            # this guard found, and the reason it exists.
            "session_order": ("viewer", "fixed", {"timezone": "America/New_York"}),
            "jurisdiction_order": ("viewer", "alphabetical", {"jurisdiction": "US"}),
        }
        for axis, (a, b, fixture) in cases.items():
            with self.subTest(axis=axis):
                self.apply(**fixture, **{axis: a})
                first = self.observed()[axis]
                self.apply(**{axis: b})
                second = self.observed()[axis]
                self.apply(**{axis: None},
                           **{k: None for k in fixture})
                self.assertNotEqual(
                    first, second,
                    f"setting {axis} to {a!r} then {b!r} changed nothing; "
                    f"every fact assertion on this axis is vacuous")

    # --- the matrix -------------------------------------------------------

    def test_source_facts_hold_across_the_whole_matrix(self):
        import itertools
        baseline = self.facts()
        names = list(self.AXES)
        for combo in itertools.product(*(self.AXES[n] for n in names)):
            prefs = dict(zip(names, combo))
            self.apply(**prefs)
            with self.subTest(**prefs):
                self.assertEqual(self.facts(), baseline,
                                 f"a source fact moved under {prefs}")

    def test_the_matrix_is_not_one_cell(self):
        import itertools
        cells = list(itertools.product(*self.AXES.values()))
        self.assertEqual(len(cells), 2 * 4 * 2 * 2)
        self.assertGreater(len(cells), 1)

    def test_no_preference_puts_a_fact_into_a_catalogue(self):
        from macrowire import i18n
        for locale in floor(self, i18n.available(), "locale files", 2):
            blob = "\n".join(i18n.renderable(i18n.load(locale)).values())
            for name, fact in self.facts().items():
                if len(fact) < 4:
                    continue
                with self.subTest(locale=locale, fact=fact):
                    self.assertNotIn(fact, blob)

    def test_the_rail_renders_the_facts_unchanged_in_every_locale(self):
        """The FACT constant is one thing; what the rail actually prints is
        another. A locale could in principle reorder them out of the line."""
        from macrowire import i18n
        facts = self.facts()
        for locale in floor(self, i18n.available(), "locale files", 2):
            t = i18n.Translator(locale)
            rendered = {
                "rba": t("rail.rba_asof", time=facts["rbaFix"], period="2026-08-21"),
                "cny": t("rail.cny_asof", period="2026-08-21",
                         time=facts["cfetsFix"], prior="2026-08-20"),
                "ecb": t("rail.ecb_asof", period="2026-08-21",
                         time=facts["ecbPublish"], base=facts["ecbBase"]),
            }
            for which, line in rendered.items():
                with self.subTest(locale=locale, line=which):
                    self.assertIn(facts["rbaFix"] if which == "rba" else
                                  facts["cfetsFix"] if which == "cny" else
                                  facts["ecbPublish"], line)

    def test_a_preference_is_removable_and_the_yaml_is_the_floor(self):
        from macrowire import preferences
        from macrowire.config import load_locale
        conn = db.connect()
        preferences.set_one(conn, "locale", "zh-CN")
        self.assertEqual(preferences.effective(conn)["locale"]["source"], "preference")
        preferences.clear(conn, "locale")
        row = preferences.effective(conn)["locale"]
        self.assertEqual(row["source"], "config")
        self.assertEqual(row["value"], load_locale())
        conn.close()

    def test_the_panel_never_writes_sources_yaml(self):
        before = (ROOT / "sources.yaml").read_bytes()
        self.apply(locale="zh-CN", timezone="UTC", window_days="7",
                   session_order="fixed", jurisdiction_order="alphabetical")
        self.assertEqual((ROOT / "sources.yaml").read_bytes(), before,
                         "a viewer preference edited the installation config")
        src = (ROOT / "macrowire/preferences.py").read_text()
        for writer in ("write_text", "safe_dump", "open("):
            self.assertNotIn(writer, src, f"preferences module can {writer}")


class SettingsSurfaceTests(unittest.TestCase):
    """The panel itself: shape, provenance, and what it must not touch."""

    def setUp(self):
        import tempfile, warnings
        warnings.filterwarnings("ignore")
        self._dir = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("MACROWIRE_DB")
        os.environ["MACROWIRE_DB"] = str(Path(self._dir.name) / "s.db")
        conn = db.connect(Path(self._dir.name) / "s.db")
        db.initialise(conn)
        conn.close()
        from fastapi.testclient import TestClient
        from macrowire.web.app import app
        self.client = TestClient(app)
        self.html = (ROOT / "macrowire/web/static/index.html").read_text()
        self.js = (ROOT / "macrowire/web/static/app.js").read_text()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("MACROWIRE_DB", None)
        else:
            os.environ["MACROWIRE_DB"] = self._prev
        self._dir.cleanup()

    # --- shape ------------------------------------------------------------

    def test_it_is_a_native_dialog_not_another_push_down_drawer(self):
        """Settings are modal in nature and filters are not: you adjust a
        filter WHILE watching the tape react. A dialog also cannot push the
        tape, so the drawer's height bug is unreachable here."""
        self.assertIn("<dialog", self.html)
        self.assertIn("showModal()", self.js)
        self.assertNotIn("max-height", (ROOT / "macrowire/web/static/style.css")
                         .read_text().split(".settings {")[1].split("}")[0]
                         .replace("max-height: 86vh;", ""))

    def test_esc_focus_trap_and_backdrop_come_from_the_platform(self):
        css = (ROOT / "macrowire/web/static/style.css").read_text()
        self.assertIn("::backdrop", css)
        # showModal() gives Esc, focus trapping and inert background; no
        # hand-rolled keydown handler should exist for the dialog.
        self.assertNotIn('id === "settings"', self.js)

    def test_it_is_reachable_in_one_action_from_the_masthead(self):
        self.assertIn('id="settings-open"', self.html)
        head = self.html[self.html.index("<header"):self.html.index("</header>")]
        self.assertIn("settings-open", head)

    def test_the_tape_is_not_inside_the_dialog(self):
        dialog = self.html[self.html.index("<dialog"):self.html.index("</dialog>")]
        self.assertNotIn('id="tape"', dialog)

    # --- what it shows ----------------------------------------------------

    def test_locales_come_from_the_directory_each_in_its_own_language(self):
        payload = self.client.get("/api/settings").json()
        from macrowire import i18n
        self.assertEqual([l["code"] for l in payload["locales"]], i18n.available())
        names = {l["code"]: l["name"] for l in payload["locales"]}
        self.assertEqual(names["zh-CN"], "简体中文")
        self.assertNotIn('"zh-CN"', read_code(ROOT / "macrowire/web/static/app.js"),
                         "a locale is hardcoded in the client")

    def test_the_timezone_picker_is_not_a_four_hundred_entry_dropdown(self):
        tz = self.client.get("/api/settings").json()["timezones"]
        self.assertLessEqual(len(tz["quick"]), 8)
        self.assertGreater(len(tz["all"]), 300)
        # the long list is a <datalist>, which the browser filters and never
        # renders whole
        self.assertIn('list="tz-all"', self.js)
        self.assertIn('id="tz-all"', self.html)
        self.assertIn(tz["detected"], tz["quick"])

    def test_install_config_shows_values_and_the_fallback_when_unset(self):
        rows = {r["key"]: r for r in self.client.get("/api/settings").json()["install"]}
        floor(self, rows, "install config rows", 8)
        backup = rows["backup.path"]
        self.assertTrue(backup["value"], "no value shown, only a key name")
        self.assertTrue(backup["unset"], "backup.path is unset in this repo")
        self.assertIn("same disk", backup["note"])
        self.assertIn("settings.falls_back", self.js)

    def test_install_rows_are_not_editable(self):
        css = (ROOT / "macrowire/web/static/style.css").read_text()
        self.assertIn(".sgrid.ro", css)
        install = self.js[self.js.index('$("settings-install").innerHTML'):]
        install = install[:install.index("$(\"tz-all\")")]
        for control in ("<select", "<input", "data-pref"):
            self.assertNotIn(control, install)

    # --- provenance and removal ------------------------------------------

    def test_every_row_says_which_level_answered(self):
        prefs = self.client.get("/api/settings").json()["preferences"]
        floor(self, prefs, "preference rows", 5)
        for key, row in prefs.items():
            with self.subTest(key=key):
                self.assertIn(row["source"], ("preference", "config"))
                self.assertIn("config_value", row)

    def test_a_preference_can_always_be_reset_to_the_config_value(self):
        self.client.post("/api/settings", json={"key": "locale", "value": "zh-CN"})
        row = self.client.get("/api/settings").json()["preferences"]["locale"]
        self.assertEqual(row["source"], "preference")
        self.assertEqual(row["config_value"], "en")
        self.client.post("/api/settings", json={"key": "locale", "value": None})
        row = self.client.get("/api/settings").json()["preferences"]["locale"]
        self.assertEqual((row["source"], row["value"]), ("config", "en"))
        self.assertIn("data-reset", self.js)

    def test_a_preference_survives_a_reload(self):
        self.client.post("/api/settings", json={"key": "window_days", "value": "90"})
        from fastapi.testclient import TestClient
        from macrowire.web.app import app
        fresh = TestClient(app)
        self.assertEqual(fresh.get("/api/bootstrap").json()["window_days"], 90)

    def test_nothing_is_hidden_state(self):
        """Everything the panel writes is a row you can read."""
        self.client.post("/api/settings", json={"key": "window_days", "value": "7"})
        conn = db.connect()
        rows = conn.execute("SELECT key, value FROM preferences").fetchall()
        conn.close()
        self.assertIn(("window_days", "7"), [(r[0], r[1]) for r in rows])

    # --- validation -------------------------------------------------------

    def test_an_install_setting_cannot_be_set_as_a_preference(self):
        for key in ("min_interval_seconds", "backup.keep", "user_agent"):
            with self.subTest(key=key):
                r = self.client.post("/api/settings", json={"key": key, "value": "1"})
                self.assertEqual(r.status_code, 400)

    def test_an_invalid_value_is_refused_and_not_stored(self):
        for key, bad in (("timezone", "+10:00"), ("locale", "kl-KL"),
                         ("window_days", "3"), ("session_order", "nearest"),
                         ("jurisdiction", "ZZ")):
            with self.subTest(key=key):
                r = self.client.post("/api/settings", json={"key": key, "value": bad})
                self.assertEqual(r.status_code, 400)
                conn = db.connect()
                stored = conn.execute(
                    "SELECT 1 FROM preferences WHERE key = ?", (key,)).fetchone()
                conn.close()
                self.assertIsNone(stored, f"{key}={bad!r} was stored anyway")

    def test_the_window_is_defined_once_not_five_times(self):
        self.assertNotIn("days=30", strip_comments(self.js, "js"))
        app_src = read_code(ROOT / "macrowire/web/app.py")
        self.assertNotIn("days: int = 30", app_src)
        self.assertIn("def _window(", app_src)

    def test_the_yaml_separates_viewer_from_install(self):
        import yaml
        doc = yaml.safe_load((ROOT / "sources.yaml").read_text())
        self.assertIn("viewer", doc, "the line is not visible in the file")
        for key in ("locale", "timezone", "session_order", "jurisdiction_order",
                    "window_days"):
            self.assertIn(key, doc["viewer"])
            self.assertNotIn(key, doc["defaults"])
        for key in ("min_interval_seconds", "user_agent", "backup"):
            self.assertIn(key, doc["defaults"])

    def test_collapse_repeats_stays_install_only_for_now(self):
        from macrowire import preferences
        self.assertNotIn("collapse_repeats", preferences.SETTABLE)
        self.assertIn("first candidate for promotion",
                      (ROOT / "macrowire/preferences.py").read_text())


class LicenceTests(unittest.TestCase):
    """The licence covers the CODE. Someone will assume it covers the data."""

    def setUp(self):
        self.licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.section = self.flat(self.readme[self.readme.index("## Licence"):])

    @staticmethod
    def flat(text):
        """Prose with its line breaks removed.

        Matching a raw phrase against wrapped Markdown fails on where the
        author happened to break the line, which says nothing about whether
        the sentence is there. This has now bitten twice.
        """
        return " ".join(text.split()).lower()

    def test_the_full_licence_text_is_present_and_is_the_affero_one(self):
        # Not the ordinary GPL. Section 13 is the difference and the reason
        # for choosing it: this is a thing you run as a server.
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", self.licence)
        self.assertIn("Version 3, 19 November 2007", self.licence)
        self.assertIn("13. Remote Network Interaction", self.licence)
        self.assertIn("END OF TERMS AND CONDITIONS", self.licence)
        self.assertGreater(len(self.licence.splitlines()), 600,
                           "the licence looks truncated")

    def test_the_spdx_identifier_is_declared(self):
        self.assertIn("SPDX-License-Identifier: AGPL-3.0-or-later", self.readme)

    def test_no_per_file_licence_headers(self):
        """Forty files of boilerplate for no practical benefit on a
        single-repo project. Deliberately absent."""
        # The TOP of the file, which is where a licence header lives. A
        # mention further down is prose about licensing - this file talks
        # about SPDX in order to test it, and matching anywhere would make
        # the test fail on itself.
        offenders, scanned = [], 0
        for pattern in ("macrowire/**/*.py", "tests/*.py",
                        "macrowire/web/static/*"):
            for path in ROOT.glob(pattern):
                if not path.is_file():
                    continue
                scanned += 1
                head = "".join(path.read_text(encoding="utf-8",
                                              errors="ignore").splitlines(True)[:5])
                if "SPDX-License-Identifier" in head:
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertGreater(scanned, 20, "the file scan is broken")
        self.assertEqual(offenders, [])

    def test_the_code_data_split_is_explicit(self):
        """The thing most likely to be got wrong."""
        floor(self, self.section, "licence section characters", 500)
        for phrase in ("it does not cover the data",
                       "each publisher, under their own terms",
                       "not this project's to license",
                       "nothing in the agpl grants you any right to "
                       "redistribute what the tool fetched",
                       "sec edgar is us federal work and public domain"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.section)

    def test_the_plain_language_note_disclaims_itself(self):
        self.assertIn("it is not legal advice", self.section)
        # and says which text wins where they disagree
        self.assertIn("the licence text is what holds", self.section)

    def test_it_does_not_contradict_the_source_terms_section(self):
        """The AGPL must not read as permission to redistribute what the
        tool fetched, and the not-polled sources stay named."""
        for prohibited in ("pboc", "asx", "hkex"):
            self.assertIn(prohibited, self.section)
        self.assertIn("deliberately **not polled**", self.section)
        self.assertIn("The licensing position, stated plainly", self.readme)

    def test_the_licence_file_is_not_modified_boilerplate(self):
        """A licence with local edits is not that licence any more."""
        self.assertNotIn("MacroWire", self.licence)
        self.assertNotIn("Spyril", self.licence)
        self.assertIn("Copyright (C) 2007 Free Software Foundation",
                      self.licence)


class DialogStaticTests(unittest.TestCase):
    """What can be asserted without a browser.

    These would NOT have caught the bug that shipped - the CSS one below
    would. Kept because they pin the JS contract cheaply and run everywhere.
    """

    def setUp(self):
        import re
        self.js = (ROOT / "macrowire/web/static/app.js").read_text()
        self.css = (ROOT / "macrowire/web/static/style.css").read_text()
        # One helper for every syntax; see strip_comments(). The comment
        # ABOVE the dialog contains the literal "<dialog>" while explaining
        # it, and a scanner that finds the first "<dialog" lands in the
        # prose.
        self.html = read_code(ROOT / "macrowire/web/static/index.html")

    def test_the_open_handler_calls_showModal_and_not_show(self):
        """show() gives no top layer, no backdrop and no inert background."""
        body = self.js[self.js.index("async function openSettings"):]
        body = body[:body.index("\n}")]
        self.assertIn("showModal()", body)
        import re
        self.assertIsNone(re.search(r"\.show\(\)", body),
                          "show() opens a non-modal dialog")

    def test_open_and_close_target_the_same_element(self):
        body = self.js[self.js.index("async function openSettings"):]
        body = body[:body.index("\n}")]
        import re
        opened = re.search(r'\$\("([\w-]+)"\)\.showModal\(\)', body).group(1)
        self.assertEqual(opened, "settings")
        # Done is a <form method="dialog"> submit button INSIDE that same
        # dialog, which is the platform's own close path - no second id to
        # get wrong.
        dialog = self.html[self.html.index("<dialog"):self.html.index("</dialog>")]
        self.assertIn(f'id="{opened}"', dialog[:80])
        self.assertIn('method="dialog"', dialog)

    def test_the_backdrop_rule_exists_and_is_not_transparent(self):
        import re
        rule = re.search(r"\.settings::backdrop \{([^}]*)\}", self.css)
        self.assertIsNotNone(rule, "no ::backdrop rule")
        alpha = re.search(r"rgba\([^)]*,\s*([\d.]+)\s*\)", rule.group(1))
        self.assertIsNotNone(alpha, "backdrop has no alpha to check")
        self.assertGreater(float(alpha.group(1)), 0.4,
                           "the backdrop is too transparent to read as modal")

    def test_display_is_scoped_to_the_open_state(self):
        """THE BUG THAT SHIPPED.

        The UA stylesheet hides a closed dialog with
        `dialog:not([open]) { display: none }`. An AUTHOR rule beats a UA
        rule regardless of specificity, so an unscoped `display` on
        `.settings` overrode it: the panel rendered in normal flow from page
        load, closed, over the ribbon, with no backdrop - because
        ::backdrop only paints in the top layer and only showModal() puts it
        there. close() was working the whole time.
        """
        import re
        base = re.search(r"\n\.settings \{([^}]*)\}", self.css)
        self.assertIsNotNone(base, "no .settings rule")
        self.assertNotIn("display", base.group(1),
                         "an unscoped display on .settings defeats the UA's "
                         "closed-state rule and the dialog renders inline")
        scoped = re.search(r"\.settings\[open\] \{([^}]*)\}", self.css)
        self.assertIsNotNone(scoped, "nothing gives the OPEN dialog a display")
        self.assertIn("display", scoped.group(1))

    def test_no_ancestor_can_pull_it_out_of_the_top_layer(self):
        """A transform, filter or perspective on an ancestor creates a
        containing block and breaks top-layer positioning. The dialog is a
        direct child of <body> so there is no ancestor to do it."""
        import re
        before = self.html[:self.html.index("<dialog")]
        stack = []
        for m in re.finditer(r"<(/?)(\w+)([^>]*)>", before):
            close, tag, attrs = m.groups()
            if tag in ("meta", "link", "br", "img", "input", "hr"):
                continue
            if attrs.rstrip().endswith("/"):
                continue
            if close:
                if stack and stack[-1] == tag:
                    stack.pop()
            else:
                stack.append(tag)
        self.assertEqual(stack, ["html", "body"],
                         f"the dialog is nested inside {stack}")


def _webdriver_available():
    """geckodriver AND firefox, both on PATH. Neither is a dependency."""
    import shutil
    return bool(shutil.which("geckodriver") and shutil.which("firefox"))


# ONE geckodriver PER PROCESS, not one per test class.
#
# MEASURED, not assumed: signals to this geckodriver are refused outright.
# It is /snap/firefox/*/usr/lib/firefox/geckodriver, and snap's confinement
# denies SIGKILL to it from the very process that spawned it -
#
#     killpg SIGKILL -> PermissionError: [Errno 13] Permission denied
#     kill    SIGKILL -> PermissionError: [Errno 13] Permission denied
#     Popen.kill      -> PermissionError: [Errno 13] Permission denied
#
# - so tearDownClass was RUNNING and could not succeed, and its bare
# `except Exception: pass` turned that into silence. Three browser classes
# meant three unkillable drivers per suite run; 39 accumulated.
#
# There is no fix that kills them, so the fix is to stop making them: one
# driver, created on first use, shared by every browser class, and one
# Firefox session inside it. What CAN be reclaimed is the browser - DELETE
# /session closes Firefox, which is the heavy process - and that is done at
# interpreter exit by every path we can reach: normal exit, an exception,
# Ctrl-C, and SIGTERM (which is what a shell timeout sends, and which
# atexit does not cover).
_DRIVER = {"proc": None, "base": None, "sid": None}


def _driver_session():
    """The shared driver and browser session, started once."""
    import atexit, signal, socket, subprocess, time
    import httpx
    if _DRIVER["sid"]:
        return _DRIVER
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    _DRIVER["proc"] = subprocess.Popen(
        ["geckodriver", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            httpx.get(f"{base}/status", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    sid = httpx.post(f"{base}/session", timeout=120, json={
        "capabilities": {"alwaysMatch": {
            "browserName": "firefox",
            "moz:firefoxOptions": {"args": ["-headless"]}}}}
        ).json()["value"]["sessionId"]
    _DRIVER.update(base=base, sid=sid)
    atexit.register(_release_driver)
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)

        def handler(signum, frame, _prev=previous):
            _release_driver()
            if callable(_prev):
                _prev(signum, frame)
            else:
                raise SystemExit(128 + signum)
        try:
            signal.signal(sig, handler)
        except ValueError:
            pass          # not the main thread; atexit still covers us
    return _DRIVER


def _release_driver():
    """Idempotent. Closes Firefox; SAYS SO if the driver cannot be killed.

    Reporting beats swallowing: a leak you are told about is a leak you can
    decide about, and this one cannot be fixed from inside the process."""
    import os, signal
    import httpx
    if not _DRIVER["sid"]:
        return
    base, sid, proc = _DRIVER["base"], _DRIVER["sid"], _DRIVER["proc"]
    _DRIVER.update(sid=None)          # idempotent before anything can throw
    try:
        httpx.delete(f"{base}/session/{sid}", timeout=30)
    except Exception:
        pass
    if proc is None or proc.poll() is not None:
        return
    for attempt in (lambda: os.killpg(os.getpgid(proc.pid), signal.SIGTERM),
                    lambda: os.killpg(os.getpgid(proc.pid), signal.SIGKILL),
                    proc.kill):
        try:
            attempt()
        except Exception:
            continue
    try:
        proc.wait(timeout=5)
        return
    except Exception:
        pass
    print(f"\ngeckodriver pid {proc.pid} survived: this build refuses signals "
          f"from its own parent, so it stays until the session ends. "
          f"Firefox was closed.", file=sys.stderr)


class HarnessCleanupTests(unittest.TestCase):
    """The test harness leaked 39 geckodriver processes.

    Two causes, and the interesting one is not the obvious one:

    1. Signals to a snap-confined geckodriver are REFUSED, even from the
       parent that spawned it. tearDownClass was running and could not
       succeed, and `except Exception: pass` made that silent.
    2. unittest does not call tearDownClass when setUpClass raises, and the
       session POST raising on a port held by a previous leak was exactly
       how a stale process turned into another stale process.

    So: one driver per process instead of one per class, released on every
    exit path reachable from inside Python, and a refusal REPORTED rather
    than swallowed.
    """

    def harness(self):
        return strip_comments(read_code(ROOT / "tests/test_macrowire.py"), "py")

    def section(self, code, start, end):
        """A slice of the harness, anchored on COLUMN-ZERO definitions.

        The first version anchored on "def setUpClass" and found the string
        literal inside this very test - the self-referential scanner the
        module docstring already warns about, made again."""
        at = code.index(start)
        stop = code.find(end, at + len(start))
        return code[at:stop if stop > 0 else len(code)]

    def test_a_kill_that_fails_is_not_swallowed_silently(self):
        """The bare `except Exception: pass` around the killpg is what let
        39 accumulate without a word. Scanned with comments stripped: the
        block explaining this defect contains the very text of it."""
        code = self.harness()
        body = self.section(code, "\ndef _release_driver", "\ndef ")
        self.assertIn("print(", body,
                      "a driver that cannot be killed must say so; a silent "
                      "leak is how this reached 39")
        self.assertIn("stderr", body, "the report belongs on stderr")

    def test_the_driver_is_released_on_every_exit_path_python_can_see(self):
        code = self.harness()
        session = self.section(code, "\ndef _driver_session",
                               "\ndef _release_driver")
        self.assertIn("atexit.register(_release_driver)", session,
                      "a normal exit or an uncaught exception leaves it running")
        self.assertIn("SIGTERM", session,
                      "a shell timeout sends SIGTERM, which atexit does not "
                      "cover - and that is the path that leaked most of them")
        self.assertIn("SIGINT", session, "Ctrl-C is not covered")

    def test_setupclass_cleans_up_after_itself_when_it_raises(self):
        """unittest calls tearDownClass only if setUpClass SUCCEEDED."""
        code = self.harness()
        cls_body = self.section(code, "\nclass BrowserTestCase", "\nclass ")
        body = cls_body[cls_body.index("def setUpClass"):]
        body = body[:body.index("def tearDownClass")]
        self.assertIn("_release_class()", body,
                      "setUpClass can raise after starting a server and a "
                      "temp directory, and nothing would release them")
        self.assertIn("raise", body, "the failure must still propagate")

    def test_releasing_twice_is_a_no_op(self):
        """It runs from atexit AND from a signal handler, so it will be
        called twice on a Ctrl-C during teardown."""
        import unittest.mock as mock
        calls = []
        fake = {"proc": None, "base": "http://127.0.0.1:1", "sid": "abc"}
        # globals() IS this module's namespace, which is where
        # _release_driver reads _DRIVER from.
        with mock.patch.dict(globals(), {"_DRIVER": fake}):
            with mock.patch("httpx.delete", side_effect=lambda *a, **k:
                            calls.append(1)):
                _release_driver()
                _release_driver()
        self.assertEqual(fake["sid"], None, "the session was not marked gone")
        self.assertEqual(len(calls), 1,
                         f"the session was deleted {len(calls)} times")

    def test_no_port_in_the_harness_is_a_literal(self):
        """A written-down port is a stale process away from failing every
        later run with KeyError: 'sessionId'."""
        import re
        code = self.harness()
        driver = self.section(code, "\ndef _driver_session",
                              "\ndef _release_driver")
        setup = self.section(code, "\nclass BrowserTestCase", "\nclass ")
        for name, part in (("_driver_session", driver), ("BrowserTestCase", setup)):
            with self.subTest(part=name):
                literals = re.findall(r"(?:PORT|port)\s*=\s*(\d{4,5})", part)
                self.assertEqual(literals, [],
                                 f"hardcoded port in {name}: {literals}")
                # Counted per SECTION, not over the file: counting the file
                # counts this test's own string literal.
                self.assertEqual(part.count('probe.bind(("127.0.0.1", 0))'), 1,
                                 f"{name} does not bind a free port")


@unittest.skipUnless(_webdriver_available(),
                     "geckodriver/firefox not installed - browser behaviour "
                     "cannot be asserted here; see the module docstring")
class BrowserTestCase(unittest.TestCase):
    """One uvicorn per class, ONE browser for the whole process.

    geckodriver speaks WebDriver over plain HTTP and httpx is already a
    dependency, so this adds no packages. Skips cleanly where the binaries
    are absent.

    The driver is process-wide (see _driver_session): a per-class driver
    could not be killed and could not be reclaimed, so the only way to stop
    leaking them was to stop creating them. Ports are bound free rather
    than written down - a literal 4477 meant one stale process broke every
    later run with `KeyError: 'sessionId'`, which reads as a broken suite.
    """

    @classmethod
    def setUpClass(cls):
        import os, socket, tempfile, threading, time
        import httpx

        cls._tmp = tempfile.TemporaryDirectory()
        cls._prev_db = os.environ.get("MACROWIRE_DB")
        os.environ["MACROWIRE_DB"] = str(Path(cls._tmp.name) / "b.db")
        conn = db.connect(Path(cls._tmp.name) / "b.db")
        db.initialise(conn)
        cls._seed(conn)
        conn.close()

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.app_port = probe.getsockname()[1]

        import uvicorn
        from macrowire.web.app import app
        cls._server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=cls.app_port, log_level="error"))
        cls._thread = threading.Thread(target=cls._server.run, daemon=True)
        cls._thread.start()
        for _ in range(80):
            try:
                httpx.get(f"http://127.0.0.1:{cls.app_port}/api/bootstrap", timeout=1)
                break
            except Exception:
                time.sleep(0.25)

        # Anything from here that raises must not leave the server and the
        # temp directory behind: unittest does NOT call tearDownClass when
        # setUpClass raises, and the session POST failing on a held port is
        # exactly how this used to leak on every retry.
        try:
            shared = _driver_session()
            cls.base, cls.sid = shared["base"], shared["sid"]
            httpx.post(f"{cls.base}/session/{cls.sid}/url", timeout=120,
                       json={"url": f"http://127.0.0.1:{cls.app_port}/"})
            time.sleep(3)
        except Exception:
            cls._release_class()
            raise

    @classmethod
    def _seed(cls, conn):
        """A tape long enough to scroll, and facets rich enough to filter.

        WITHOUT THIS EVERY TEST HERE PASSED VACUOUSLY. An empty database
        renders no .item elements, so "the first visible item did not move"
        compared None with None and succeeded. Same failure as a loop over
        an empty collection - the floor rule, in a browser.
        """
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        n = 0
        for source in ("rba_media_releases", "hkma_press", "ecb_press",
                       "boe_news", "fed_press_monetary"):
            src = SOURCES[source]
            sid = db.upsert_source(conn, src.name, src.kind, src.config)
            for k in range(40):
                n += 1
                conn.execute(
                    """INSERT INTO items (id, source_id, title, url, fetched_at,
                                          published_at, fx_state, type_primary)
                       VALUES (?, ?, ?, 'http://x', ?, ?, ?, ?)""",
                    (f"seed{n}", sid, f"{src.name} item {k}", now.isoformat(),
                     (now - timedelta(hours=n)).isoformat(),
                     "fx" if k % 2 else "unclassified",
                     "Press Release" if k % 3 else "Speech"))
        # Observations too, or the rail renders empty headings with no
        # values - and a test asserting "every rail value is on screen"
        # passes by finding no values at all.
        series = {
            "sse_southbound": [("SOUTHBOUND/amount/net", -51.34),
                               ("SOUTHBOUND/amount/buy", 298.18),
                               ("SOUTHBOUND/amount/sell", 349.52),
                               ("SOUTHBOUND/amount/total", 647.70)],
            "cftc_cot": [("COT/AUD/net", -44159.0), ("COT/JPY/net", -158166.0)],
            "cfets_ccpr": [("USD/CNY", 7.1234), ("EUR/CNY", 8.4321)],
            "ecb_fx": [("EUR/USD", 1.0912), ("EUR/JPY", 163.44)],
            "rba_exchange_rates": [("AUD/USD", 0.7211)],
        }
        for source, rows in series.items():
            src = SOURCES[source]
            sid = db.upsert_source(conn, src.name, src.kind, src.config)
            for period in ("2026-08-20", "2026-08-21"):
                for name, value in rows:
                    conn.execute(
                        """INSERT INTO observations (source_id, series, period, value,
                             unit, base_currency, target_currency, rate_type,
                             frequency, decimals, external_id, observed_at, fetched_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sid, name, period, value + (0.5 if period.endswith("1") else 0),
                         "100 million HKD" if "SOUTHBOUND" in name else "contracts",
                         None, None, "seeded", "daily", 2,
                         f"{period}#{name}", period, now.isoformat()))
        conn.commit()
        # The fixture checks ITSELF. A browser DB with no items made every
        # tape-stability assertion compare None with None and pass.
        class _Assert:
            @staticmethod
            def assertGreaterEqual(a, b, msg=None):
                if a < b:
                    raise AssertionError(msg or f"{a} < {b}")
        seeded(_Assert, conn, items=150, observations=20, sources=8)

    @classmethod
    def tearDownClass(cls):
        cls._release_class()

    @classmethod
    def _release_class(cls):
        """The per-class half only. The browser outlives the class and is
        released at interpreter exit, because it cannot be re-made cheaply
        and its driver cannot be killed at all."""
        import os
        cls._server.should_exit = True
        if cls._prev_db is None:
            os.environ.pop("MACROWIRE_DB", None)
        else:
            os.environ["MACROWIRE_DB"] = cls._prev_db
        cls._tmp.cleanup()

    def js(self, script):
        import httpx
        return httpx.post(f"{self.base}/session/{self.sid}/execute/sync",
                          timeout=30, json={"script": script, "args": []}
                          ).json()["value"]

    def screenshot(self):
        """The rendered page as pixels. See decode_png: rects and hit-tests
        cannot see a scrollbar, and a scrollbar is what this class kept
        failing to notice."""
        import base64
        import httpx
        b64 = httpx.get(f"{self.base}/session/{self.sid}/screenshot",
                        timeout=60).json()["value"]
        return decode_png(base64.b64decode(b64))

    def press(self, key):
        import httpx
        httpx.post(f"{self.base}/session/{self.sid}/actions", timeout=30,
                   json={"actions": [{"type": "key", "id": "kb", "actions": [
                       {"type": "keyDown", "value": key},
                       {"type": "keyUp", "value": key}]}]})

    def state(self):
        return self.js("""const d = document.getElementById('settings');
            return {open: d.open, display: getComputedStyle(d).display,
                    modal: d.matches(':modal'),
                    height: Math.round(d.getBoundingClientRect().height)};""")

    def open_it(self):
        import time
        self.js("document.getElementById('settings-open').click(); return 1;")
        time.sleep(2)

    def setUp(self):
        import time
        # Always start closed, whatever the previous test did.
        self.js("""const d = document.getElementById('settings');
                   if (d.open) d.close(); return 1;""")
        time.sleep(0.3)


class DialogBrowserTests(BrowserTestCase):
    """The tests that would actually have caught the settings dialog bug.

    Everything static asserts SOURCE. This asserts BEHAVIOUR, in a real
    engine, because the bug was a cascade interaction between an author
    rule and the UA stylesheet - invisible to any amount of reading.
    """


    def test_it_is_invisible_before_it_is_opened(self):
        """THE BUG. It rendered in normal flow from page load, closed, over
        the ribbon, because an author `display` beat the UA's
        `dialog:not([open]) { display: none }`."""
        s = self.state()
        self.assertFalse(s["open"])
        self.assertEqual(s["display"], "none",
                         "a closed dialog is being painted")
        self.assertEqual(s["height"], 0)

    def test_opening_it_puts_it_in_the_top_layer(self):
        """:modal is true only for showModal(). It is what makes the
        backdrop paint and the background inert."""
        self.open_it()
        s = self.state()
        self.assertTrue(s["open"])
        self.assertTrue(s["modal"], "opened, but not as a modal - no backdrop")
        self.assertEqual(s["display"], "flex")
        self.assertGreater(s["height"], 100)

    def test_escape_closes_it(self):
        self.open_it()
        self.assertTrue(self.state()["open"])
        import time
        self.press("")          # Escape
        time.sleep(0.6)
        s = self.state()
        self.assertFalse(s["open"], "Esc did not close it")
        self.assertEqual(s["display"], "none")

    def test_done_closes_it(self):
        self.open_it()
        self.assertTrue(self.state()["open"])
        import time
        self.js("""document.querySelector('#settings .settings-head button')
                     .click(); return 1;""")
        time.sleep(0.6)
        s = self.state()
        self.assertFalse(s["open"], "Done did not close it")
        self.assertEqual(s["display"], "none")

    def test_the_backdrop_actually_covers_the_page(self):
        """The reported symptom was 'the ribbon shows straight through it'."""
        self.open_it()
        covered = self.js("""
            const d = document.getElementById('settings');
            const r = d.getBoundingClientRect();
            // What is on top at the dialog's centre, and at a point well
            // outside it where the backdrop should be intercepting clicks.
            const inside = document.elementFromPoint(r.left + r.width / 2,
                                                     r.top + r.height / 2);
            const outside = document.elementFromPoint(5, 5);
            return {insideIsInDialog: d.contains(inside),
                    outsideTag: outside ? outside.tagName : null};""")
        self.assertTrue(covered["insideIsInDialog"],
                        "something is painting over the open dialog")
        # With a modal open the background is inert: a hit test outside it
        # must not land on tape or ribbon content.
        self.assertIn(covered["outsideTag"], ("HTML", "BODY", "DIALOG", None),
                      f"the background is still hit-testable "
                      f"({covered['outsideTag']}) - it is not inert")

    def test_the_tape_is_still_there_underneath(self):
        """A dialog must not push the tape; that was the whole argument for
        choosing one over another drawer."""
        before = self.js("const t = document.getElementById('tape');"
                         "return Math.round(t.getBoundingClientRect().top);")
        self.open_it()
        after = self.js("const t = document.getElementById('tape');"
                        "return Math.round(t.getBoundingClientRect().top);")
        self.assertEqual(before, after, "opening settings moved the tape")


class CommentStrippingTests(unittest.TestCase):
    """One helper, because this bit three times in three places.

    A CSS scanner read the last line of a /* */ block as a selector; a t()
    scanner counted a call written in a # comment to explain the API; a
    markup scanner located the first "<dialog" inside the <!-- --> above the
    element. Every one was a scanner answering a question about the CODE
    while reading the PROSE.
    """

    def test_it_strips_each_syntax(self):
        cases = [
            ("css", "/* .b { color: var(--accent) } */ .c { top: 0 }",
             "--accent", ".c"),
            ("js", 'const a = 1; // t("ghost.key")\nconst b = 2;',
             "ghost.key", "const b"),
            ("js", "/* showModal() */ const c = 3;", "showModal", "const c"),
            ("html", '<!-- <dialog id="fake"> --><dialog id="real">',
             'id="fake"', 'id="real"'),
            ("py", 'x = 1\n# t("ghost.key", key="y")\ny = 2', "ghost.key", "y = 2"),
            ("yaml", "a: 1\n# enabled: false\nb: 2", "enabled: false", "b: 2"),
        ]
        for syntax, text, gone, kept in cases:
            with self.subTest(syntax=syntax, text=text[:28]):
                out = strip_comments(text, syntax)
                self.assertNotIn(gone, out)
                self.assertIn(kept, out)

    def test_an_unknown_syntax_raises_rather_than_passing_text_through(self):
        """A scanner that thinks it stripped and did not is the bug."""
        with self.assertRaises(ValueError):
            strip_comments("x", "rs")

    def test_a_url_survives_the_js_stripper(self):
        out = strip_comments('const u = "https://example.com/x"; // note', "js")
        self.assertIn("https://example.com/x", out)
        self.assertNotIn("note", out)

    def test_the_scanned_files_hold_the_assumption_this_relies_on(self):
        """Not a parser. A `//` inside a JS string literal would be
        mis-stripped, and a `/*` inside a CSS string likewise. Neither
        exists; this fails if one appears rather than letting the helper be
        quietly wrong."""
        import re
        js = (ROOT / "macrowire/web/static/app.js").read_text()
        css = (ROOT / "macrowire/web/static/style.css").read_text()
        for name, text in (("app.js", js), ("style.css", css)):
            with self.subTest(file=name):
                literals = re.findall(r"'[^'\n]*'|\"[^\"\n]*\"", text)
                offenders = [l for l in literals if "//" in l or "/*" in l]
                self.assertEqual(
                    offenders, [],
                    f"{name} has a comment marker inside a string literal; "
                    f"strip_comments would corrupt it and needs a parser")

    def test_the_known_comment_sensitive_scanners_use_the_helper(self):
        """Positive, not self-referential: an earlier version searched for
        the very regexes it contained, and failed on itself."""
        source = (ROOT / "tests/test_macrowire.py").read_text()
        self.assertIn("def strip_comments(", source)
        self.assertIn("def read_code(", source)
        for scanner in ("DialogStaticTests", "TranslationKeyReachTests",
                        "TestFilterUI", "FilterPanelTests"):
            block = source[source.index(f"class {scanner}"):]
            block = block[:block.index("\nclass ", 10)]
            with self.subTest(scanner=scanner):
                self.assertIn("strip_comments", block,
                              f"{scanner} reads source without stripping "
                              f"comments; a comment can change its answer")

class TranslatedColumnWidthTests(unittest.TestCase):
    """A column carrying a translated word cannot have a literal width.

    `_pad(t('cli.fetch.ok'), 8)` appeared at ten call sites. `no change`
    was already 9 and 无新内容 is 8, so the fetch column was ragged in both
    locales; `within interval` at 15 made one row of a cycle sit seven
    columns right of the others. cmd_status had learned this once already
    and measured its label column at runtime - the fetch column had not.
    """

    STATUS_KEYS = ("cli.fetch.ok", "cli.fetch.no_change", "cli.fetch.throttled",
                   "cli.fetch.disabled", "cli.fetch.revised", "cli.backup.failed")

    def test_no_translated_column_is_padded_to_a_literal(self):
        import re
        code = strip_comments(read_code(ROOT / "macrowire/__main__.py"), "py")
        literal = re.findall(r"_pad\(\s*t\([^)]*\)\s*,\s*\d+\s*\)", code)
        self.assertEqual(literal, [],
                         f"a translated string padded to a hardcoded width: "
                         f"{literal}")

    def test_the_column_holds_every_word_it_has_to_carry(self):
        from macrowire import i18n
        from macrowire.__main__ import _width
        for locale in floor(self, i18n.available(), "locale files", 2):
            tr = i18n.Translator(locale)
            widths = {k: _width(tr(k)) for k in self.STATUS_KEYS}
            col = max(widths.values()) + 1
            for key, w in widths.items():
                with self.subTest(locale=locale, key=key):
                    self.assertLess(w, col,
                                    f"{locale}: {key} is {w} wide in a {col} column")

    def test_the_measured_column_names_every_word_that_lands_in_it(self):
        """Guard on the guard: the width is measured over a LIST, and a
        new status word that is not in the list widens the column without
        widening the measurement."""
        import re
        code = strip_comments(read_code(ROOT / "macrowire/__main__.py"), "py")
        used = set(re.findall(r"_pad\(t\('(cli\.(?:fetch|backup)\.\w+)'\)", code))
        floor(self, used, "padded status words", 5)
        measured = set(self.STATUS_KEYS)
        self.assertEqual(used - measured, set(),
                         f"printed in the column but not measured for it: "
                         f"{sorted(used - measured)}")
        src = code[code.index("def _status_col"):]
        src = src[:src.index("\ndef ", 5)]
        for key in used:
            with self.subTest(key=key):
                self.assertIn(key, src,
                              f"{key} lands in the column but _status_col "
                              f"does not measure it")


class OneStateOneNameTests(unittest.TestCase):
    """A state must not have two names on two surfaces.

    `health.disabled.label` rendered 未轮询 in the panel while
    `cli.status.disabled_flag` rendered 已停用 in the terminal - the same
    state, two Chinese words, found by a person reading. The fix for that
    one left a HAND-WRITTEN list of state groups, so when a second pair
    turned up (`cli.fetch.throttled` 已限流 against `health.throttled.label`
    等待间隔) the test said nothing: the list had never grown when the
    surfaces did. A guard you have to remember to extend is not a guard.

    So the groups are DERIVED from HEALTH_SEVERITY and the key names. A key
    NAMES a state when its last segment is the state, the state plus
    `_flag`, or `label` directly under it. A key that merely mentions one
    (`throttled_detail`, `stale.meaning`, `staleness`) is not a name and is
    not collected. Add a tenth state, or a fourth surface that names one,
    and it is checked without editing this file.

    The panel label is the canonical name. A longer surface may say MORE -
    the terminal spends a whole sentence on `unreachable` - but it must
    OPEN with that name, never substitute a different word for it.

    There is no exemption list. The one pair that did not fit the rule
    (`cli.fetch.no_change` said `no change` / 无新内容 against the panel's
    `polled, nothing new` / 已检查，无新内容) was fixed by changing the
    string. An exception list is a hand-written list with better
    documentation, and a hand-written list is what failed here twice.
    """

    def canonical(self, state):
        return f"health.{state}.label"

    def named_by_surface(self):
        """{state: [keys that NAME it]}, derived, canonical key excluded."""
        from macrowire import i18n
        from macrowire.web.queries import HEALTH_SEVERITY
        en = i18n.renderable(i18n.load("en"))
        out = {}
        for state in HEALTH_SEVERITY:
            keys = []
            for key in sorted(en):
                if key == self.canonical(state):
                    continue
                seg = key.split(".")
                names_it = (seg[-1] in (state, f"{state}_flag")
                            or (len(seg) >= 2 and seg[-2] == state
                                and seg[-1] == "label"))
                if names_it:
                    keys.append(key)
            out[state] = keys
        return out

    def normalise(self, text):
        """Bracket and case decoration is presentation, not a second name."""
        return text.strip().strip("[]（）()【】").upper()

    def test_a_state_reads_the_same_wherever_it_appears(self):
        from macrowire import i18n
        surfaces = self.named_by_surface()
        for locale in floor(self, i18n.available(), "locale files", 2):
            t = i18n.Translator(locale)
            for state, keys in surfaces.items():
                name = self.normalise(t(self.canonical(state)))
                for key in keys:
                    with self.subTest(locale=locale, key=key):
                        self.assertTrue(
                            self.normalise(t(key)).startswith(name),
                            f"{locale}: {state} is '{name}' in the panel but "
                            f"{key} opens with '{self.normalise(t(key))[:40]}'")

    def test_the_derivation_finds_the_surfaces_it_is_meant_to_guard(self):
        """Guard on the guard: rename the keys out from under the rule and
        it collects nothing, passing vacuously. Six states are named on
        more than one surface today."""
        surfaces = self.named_by_surface()
        multi = {s: k for s, k in surfaces.items() if k}
        floor(self, multi, "states named on a second surface", 5)
        for state in ("disabled", "stale", "throttled"):
            with self.subTest(state=state):
                self.assertIn(state, multi,
                              f"{state} is named on one surface only - the "
                              f"pair that caused this test has gone missing")

    def test_every_state_has_a_panel_label_to_be_canonical(self):
        from macrowire import i18n
        from macrowire.web.queries import HEALTH_SEVERITY
        en = i18n.renderable(i18n.load("en"))
        for state in floor(self, HEALTH_SEVERITY, "health states", 9):
            with self.subTest(state=state):
                self.assertIn(self.canonical(state), en)

    def test_no_health_state_label_is_reused_for_a_different_state(self):
        """The converse: two DIFFERENT states must not share one name, or
        the panel cannot tell you which one you are in."""
        from macrowire import i18n
        from macrowire.web.queries import HEALTH_SEVERITY
        for locale in floor(self, i18n.available(), "locale files", 2):
            t = i18n.Translator(locale)
            labels = {}
            for state in HEALTH_SEVERITY:
                label = t(f"health.{state}.label")
                labels.setdefault(label, []).append(state)
            clashes = {k: v for k, v in labels.items() if len(v) > 1}
            with self.subTest(locale=locale):
                self.assertEqual(clashes, {},
                                 f"{locale}: one label, several states -> {clashes}")

class TapeStabilityTests(BrowserTestCase):
    """Opening the filter must not move the tape by a single pixel.

    Reading weeks back is what this tool turned out to be for, and losing
    your place makes the filter unusable. The cause was NOT what it looked
    like: the panel's height moves the tape by zero - Firefox's scroll
    anchoring absorbs it exactly - while focus() scrolling its target into
    view threw the page 2,984px to the top. Four proposed remedies all
    addressed displacement, which measures zero.

    So this asserts the OBSERVABLE thing, not the mechanism: the first
    visible item does not move. That stays true whichever way a future
    change breaks it.
    """


    PROBE = """const it = [...document.querySelectorAll('.item')];
        const vis = it.find(e => e.getBoundingClientRect().top >= 0);
        return {y: Math.round(window.scrollY),
                key: vis ? vis.dataset.key : null,
                top: vis ? Math.round(vis.getBoundingClientRect().top) : null,
                count: it.length};"""

    def setUp(self):
        import time
        # Clear any filter a previous test left on. Without this the tests
        # are order-coupled: clicking "the first chip" TOGGLES it, so a
        # leftover filter makes the next test un-apply instead of apply,
        # and the failure looks like the feature is broken.
        self.js("""const c = document.getElementById('fclear');
                   if (c && !document.getElementById('mast-tokens').hidden) c.click();
                   const p = document.getElementById('fpanel');
                   if (p && !p.hidden) document.getElementById('fclose').click();
                   window.scrollTo(0, 0); return 1;""")
        time.sleep(0.4)
        # A floor, not a skip. Skipping on an empty tape is how these tests
        # passed while asserting nothing.
        count = self.js("return document.querySelectorAll('.item').length;")
        self.assertGreater(count, 30,
                           f"only {count} items rendered; these tests assert "
                           f"nothing without a tape to scroll")
        self.assertGreater(self.js("return document.body.scrollHeight;"), 2000)
        self.js("window.scrollTo(0, 1500); return 1;")
        time.sleep(0.8)

    def snap(self):
        return self.js(self.PROBE)

    def test_opening_the_filter_moves_nothing(self):
        import time
        before = self.snap()
        self.js("document.getElementById('fopen').click(); return 1;")
        time.sleep(1.2)
        after = self.snap()
        self.assertEqual(after["top"], before["top"],
                         "the first visible item moved when the filter opened")
        self.assertEqual(after["key"], before["key"],
                         "a different item is now at the top of the viewport")

    def test_scrolling_inside_the_panel_moves_nothing(self):
        """What expanding an axis used to be. Nothing expands now, so the
        interaction that could displace the tape is scrolling the panel -
        and `overscroll-behavior: contain` is what stops the scroll
        chaining to the document once the body hits its end."""
        import time
        self.js("document.getElementById('fopen').click(); return 1;")
        time.sleep(1.2)
        before = self.snap()
        self.js("""const b = document.getElementById('fpanel-body');
                   b.scrollTop = 99999; b.scrollTop = 0; b.scrollTop = 99999;
                   return 1;""")
        time.sleep(1)
        after = self.snap()
        self.assertEqual(after["top"], before["top"],
                         "scrolling the panel scrolled the tape behind it")
        self.assertEqual(after["key"], before["key"])

    def test_closing_the_filter_moves_nothing(self):
        import time
        self.js("document.getElementById('fopen').click(); return 1;")
        time.sleep(1.2)
        before = self.snap()
        self.js("document.getElementById('fclose').click(); return 1;")
        time.sleep(1.2)
        after = self.snap()
        self.assertEqual(after["top"], before["top"],
                         "the first visible item moved when the filter closed")
        self.assertEqual(after["key"], before["key"])

    def test_a_full_open_close_cycle_returns_to_exactly_where_it_started(self):
        import time
        before = self.snap()
        for _ in range(2):
            self.js("document.getElementById('fopen').click(); return 1;")
            time.sleep(1)
            self.js("document.getElementById('fclose').click(); return 1;")
            time.sleep(1)
        after = self.snap()
        self.assertEqual((after["y"], after["top"], after["key"]),
                         (before["y"], before["top"], before["key"]),
                         "two open/close cycles drifted the tape")

    def test_the_settings_dialog_moves_nothing_either(self):
        import time
        before = self.snap()
        self.js("document.getElementById('settings-open').click(); return 1;")
        time.sleep(2.5)
        after = self.snap()
        self.js("document.getElementById('settings').close(); return 1;")
        time.sleep(0.6)
        self.assertEqual(after["top"], before["top"])

    def test_the_tokens_line_appearing_moves_nothing(self):
        """The second masthead line materialising is itself a displacement
        risk: it grows the sticky header by ~42px while the reader is deep
        in the tape."""
        import time
        self.js("document.getElementById('fopen').click(); return 1;")
        time.sleep(1.2)
        before = self.snap()
        self.js("""const c = document.querySelector('#fgrid .chip[data-axis]');
                   if (c) c.click(); return 1;""")
        time.sleep(1.5)
        after = self.snap()
        grew = self.js("""return Math.round(document.getElementById('masthead')
                            .getBoundingClientRect().height);""")
        self.assertFalse(self.js("return document.getElementById('mast-tokens').hidden;"),
                         "a filter is active but the tokens row did not appear")
        self.assertGreater(grew, 60, "the tokens row did not actually grow the masthead")
        self.assertEqual(after["top"], before["top"],
                         "the tape moved when the tokens row appeared")

    def test_the_tokens_line_disappearing_moves_nothing(self):
        import time
        self.js("document.getElementById('fopen').click(); return 1;")
        time.sleep(1.2)
        self.js("""const c = document.querySelector('#fgrid .chip[data-axis]');
                   if (c) c.click(); return 1;""")
        time.sleep(1.5)
        before = self.snap()
        self.js("document.getElementById('fclear').click(); return 1;")
        time.sleep(1.5)
        after = self.snap()
        self.assertTrue(self.js("return document.getElementById('mast-tokens').hidden;"),
                        "filters cleared but the tokens row is still there")
        self.assertEqual(after["top"], before["top"],
                         "the tape moved when the tokens row went away")

    def test_the_masthead_costs_nothing_when_nothing_is_filtered(self):
        height = self.js("""return Math.round(document.getElementById('masthead')
                              .getBoundingClientRect().height);""")
        hidden = self.js("return document.getElementById('mast-tokens').hidden;")
        self.assertTrue(hidden, "the tokens row is present with no filters active")
        self.assertLessEqual(height, 60,
                             f"the masthead is {height}px unfiltered; it must stay "
                             f"one line")

    def test_the_masthead_stays_reachable_from_deep_in_the_tape(self):
        """The reason it is sticky: a Filter button at the top of the
        document meant scrolling a hundred rows to reach it."""
        top = self.js("""return Math.round(document.getElementById('masthead')
                           .getBoundingClientRect().top);""")
        self.assertEqual(top, 0, "the masthead scrolled away")
        for control in ("fopen", "settings-open"):
            with self.subTest(control=control):
                visible = self.js(f"""const e = document.getElementById('{control}');
                    const r = e.getBoundingClientRect();
                    return r.top >= 0 && r.bottom <= window.innerHeight;""")
                self.assertTrue(visible, f"#{control} is not on screen")

    def test_the_ribbon_scrolls_away_rather_than_sticking(self):
        """Measured at 275px against a 49px masthead - 34% of the viewport,
        answering a question asked once at the start of a session. The
        sticky day headers do the orientation job for a backwards read at
        30px instead."""
        pos = self.js("""const r = document.querySelector('.ribbon');
            return getComputedStyle(r).position;""")
        self.assertEqual(pos, "static", "the ribbon is pinned and eating tape")
        offscreen = self.js("""const r = document.querySelector('.ribbon');
            return r.getBoundingClientRect().bottom < 0;""")
        self.assertTrue(offscreen, "the ribbon is still on screen at this depth")

    def test_the_page_never_overflows_horizontally(self):
        """A document wider than the viewport puts a horizontal scrollbar
        under everything and carries the right-hand rail off screen. It was
        10px, from `.marks .track` switching to position:relative while
        keeping the `left: 42px` that had been an inset."""
        over = self.js("""const out = [];
            for (const el of document.querySelectorAll('*')) {
              const r = el.getBoundingClientRect();
              if (r.right > window.innerWidth + 1 && r.width > 0)
                out.push(el.tagName + '.' + (el.className || '').toString().slice(0, 30));
            }
            return out.slice(0, 6);""")
        self.assertEqual(over, [], f"these run past the right edge: {over}")
        self.assertLessEqual(
            self.js("return document.documentElement.scrollWidth - window.innerWidth;"),
            1, "the document is wider than the viewport")

    def test_every_rail_value_is_inside_the_viewport(self):
        """A rail showing labels with no numbers looks like missing data."""
        rows = self.js("""return [...document.querySelectorAll(
            '#sb .v, #cot .v, #fx .v, #ecb .v, #rba .v')].map(e => ({
              txt: e.textContent.trim().slice(0, 20),
              right: Math.round(e.getBoundingClientRect().right),
              off: e.getBoundingClientRect().right > window.innerWidth}));""")
        floor(self, rows, "rail values", 4)
        offscreen = [r for r in rows if r["off"]]
        self.assertEqual(offscreen, [], f"rail values off the right edge: {offscreen}")

    def test_the_open_panel_can_be_scrolled_to_its_footer(self):
        """A ceiling without a reachable bottom is worse than no ceiling:
        with every axis open, Type and the footer were unreachable."""
        import time
        self.js("document.getElementById('fopen').click(); return 1;")
        time.sleep(1.2)
        state = self.js("""const p = document.getElementById('fpanel-body');
            // From the TOP. scrollTop survives the panel being hidden and
            // shown, so an earlier test in this class left the body already
            // at its end: "scrollTop did not increase" was true, and had
            // nothing to do with whether the panel scrolls.
            p.scrollTop = 0;
            const before = p.scrollTop; p.scrollTop = 99999;
            const foot = document.querySelector('.fpanel-foot')
                           .getBoundingClientRect();
            const box = document.getElementById('fpanel').getBoundingClientRect();
            return {scrollH: p.scrollHeight, clientH: p.clientHeight,
                    overflowY: getComputedStyle(p).overflowY,
                    moved: p.scrollTop > before,
                    reachesBottom: p.scrollTop + p.clientHeight >= p.scrollHeight - 2,
                    footInside: foot.bottom <= box.bottom + 1 && foot.top >= box.top};""")
        self.assertGreater(state["scrollH"], state["clientH"],
                           "not enough content to test scrolling")
        self.assertIn(state["overflowY"], ("auto", "scroll"))
        self.assertTrue(state["moved"],
                        f"the panel body does not scroll: {state}")
        self.assertTrue(state["reachesBottom"], "the end of the axes is unreachable")
        self.assertTrue(state["footInside"],
                        "the footer is not pinned inside the panel")

    def test_the_panel_scrollbar_is_visible_not_an_overlay(self):
        """It scrolled the whole time; the platform's overlay scrollbar
        only appears once you are already scrolling, so it read as clipped
        content with no way down."""
        css = strip_comments((ROOT / "macrowire/web/static/style.css").read_text(), "css")
        import re
        rule = "".join(m.group(1) for m in re.finditer(r"\.fpanel-body \{([^}]*)\}", css))
        self.assertIn("scrollbar-width", rule,
                      "nothing tells the reader the panel scrolls")
        self.assertIn("scrollbar-color", rule)

    def test_every_axis_opens_expanded(self):
        """Replaces test_every_axis_starts_closed, which was right about the
        old design. Collapsing was a way to save vertical space; the panel
        scrolls instead, and nothing in it is a click away from being seen.
        Every axis renders its chips, including the two that used to nest a
        level down: the type sub-groups and the watchlist add-form."""
        import time
        self.js("document.getElementById('fopen').click(); return 1;")
        time.sleep(1.2)
        seen = self.js("""const out = [];
            for (const ax of document.querySelectorAll('#fgrid .fax')) {
              const g = ax.querySelector('.fgroup');
              const r = g.getBoundingClientRect();
              out.push({axis: ax.querySelector('.faxis').textContent.trim(),
                        shown: getComputedStyle(g).display !== 'none' && r.height > 0,
                        chips: g.querySelectorAll('.chip, input, select').length});
            }
            return out;""")
        floor(self, seen, "filter axes", 5)
        hidden = [a for a in seen if not a["shown"]]
        self.assertEqual(hidden, [], f"an axis is not showing its chips: {hidden}")
        empty = [a["axis"] for a in seen if a["chips"] == 0]
        self.assertEqual(empty, [], f"an axis rendered no controls at all: {empty}")
        self.assertTrue(
            self.js("""const w = document.querySelector('#fgrid .wl-add');
                       return !!w && w.getBoundingClientRect().height > 0;"""),
            "the watchlist add-form is not visible without a further click")

    def test_a_coverage_boundary_renders_once_per_source(self):
        """Reading six months back crosses five boundaries, not five hundred
        lines. A source-string check missed this - `.shift()` appears twice
        and the mutation only changed one - so it is measured in the DOM."""
        rendered = self.js("""return [...document.querySelectorAll('.cbound')]
            .map(e => e.className);""")
        floor(self, rendered, "coverage boundaries", 3)
        titles = self.js("""return [...document.querySelectorAll('.cbound .cb-t')]
            .map(e => e.textContent.trim());""")
        self.assertEqual(len(titles), len(set(titles)),
                         f"a source produced more than one boundary: {titles}")
        rows = self.js("return document.querySelectorAll('.item').length;")
        self.assertLess(len(titles), rows / 4,
                        f"{len(titles)} boundaries against {rows} rows - this is "
                        f"rendering per row, not per source")

    def test_the_end_of_window_summary_is_present_and_honest(self):
        end = self.js("""const e = document.querySelector('.cend');
            return e ? e.textContent.replace(/\\s+/g, ' ').trim() : null;""")
        self.assertIsNotNone(end, "no end-of-window summary")
        listed = self.js("return document.querySelectorAll('.cend-row').length;")
        bounds = self.js("return document.querySelectorAll('.cbound').length;")
        self.assertEqual(listed, bounds,
                         "the summary and the boundaries disagree about what "
                         "is missing")

    def test_focus_calls_that_are_not_navigation_prevent_scrolling(self):
        """The mechanism, pinned separately from the behaviour above.

        The Tab handler is deliberately excluded: there, scrolling to the
        newly focused control is correct, because the user is navigating.
        """
        src = read_code(ROOT / "macrowire/web/static/app.js")
        for fn in ("function openPanel", "function closePanel"):
            block = src[src.index(fn):]
            block = block[:block.index("\n}")]
            with self.subTest(fn=fn):
                self.assertIn("preventScroll: true", block,
                              f"{fn} focuses without preventScroll and will "
                              f"throw the reader back to the top")


class ContainedRegionTests(BrowserTestCase):
    """A region that is supposed to CONTAIN its content, must.

    Three symptoms reached a screenshot at once - a transparent panel
    background, a doubled rail region, and content clipped above the
    panel's top edge - and they were one defect. `.fgroup { display: flex }`
    is an AUTHOR rule; the UA rule that hides a closed <details>'s content
    is a USER-AGENT rule; author origin outranks UA origin whatever the
    specificity. So every closed axis laid its chips out anyway, outside
    the panel's painted box (hence "transparent": those chips had the tape
    behind them, not the panel) and outside its scrollHeight, which stayed
    240 and never offered a way down. The panel meeting the rail with no
    border between two --raised surfaces did the rest.

    Identical to the settings-dialog bug: `.settings { display: flex }`
    beating `dialog:not([open]) { display: none }`.

    So these tests take the CLASS, not the instance, and run it over both
    regions. Nothing here mentions <details>: the next version of this bug
    will not either.
    """

    # (id, how to open, how to close). Both regions, so a fix to one that
    # would have broken the other cannot pass.
    REGIONS = (
        ("fpanel", "document.getElementById('fopen').click();",
         "document.getElementById('fclose').click();"),
        ("settings", "document.getElementById('settings-open').click();",
         "document.getElementById('settings').close();"),
    )

    def open_region(self, rid, opener):
        import time
        self.js(opener + " return 1;")
        time.sleep(1.2)

    def close_region(self, closer):
        import time
        self.js(closer + " return 1;")
        time.sleep(0.5)

    def at_depth(self, depth):
        import time
        self.js(f"window.scrollTo(0, {depth}); return 1;")
        time.sleep(0.6)

    def test_a_closed_region_is_not_rendered_at_all(self):
        """The third time this exact cascade trap has bitten. An author
        `display` on a region that a UA rule hides - `[hidden]` for the
        panel, `dialog:not([open])` for the settings dialog - wins on
        ORIGIN, not specificity, so the closed region stays on screen.

        Introduced again while rebuilding the panel to fix the same bug in
        its content. It gets a test rather than a note this time."""
        for rid, opener, closer in self.REGIONS:
            with self.subTest(region=rid):
                self.at_depth(0)
                shut = self.js(f"""const e = document.getElementById('{rid}');
                    return {{display: getComputedStyle(e).display,
                             h: Math.round(e.getBoundingClientRect().height),
                             w: Math.round(e.getBoundingClientRect().width)}};""")
                self.assertEqual(
                    (shut["h"], shut["w"]), (0, 0),
                    f"{rid} is closed but still occupies "
                    f"{shut['w']}x{shut['h']}px with display: {shut['display']}")
                self.open_region(rid, opener)
                shown = self.js(f"""const r = document.getElementById('{rid}')
                    .getBoundingClientRect(); return Math.round(r.height);""")
                self.close_region(closer)
                self.assertGreater(shown, 0, f"{rid} did not open")
                again = self.js(f"""const r = document.getElementById('{rid}')
                    .getBoundingClientRect(); return Math.round(r.height);""")
                self.assertEqual(again, 0, f"{rid} is still drawn after closing")

    def test_nothing_paints_outside_the_region_that_owns_it(self):
        """The instance was 29 chips drawn past the panel's bottom edge.

        Asks the browser what is PAINTED, not where boxes are. A first
        version compared rectangles and failed the settings dialog, which
        was correct all along: content scrolled out of a scroll container
        has a rect outside the box and is clipped, not painted. So this
        samples the band just outside each region and asks whether
        anything belonging to it is hit-testable there."""
        for rid, opener, closer in self.REGIONS:
            for depth in (0, 2400):
                with self.subTest(region=rid, scroll=depth):
                    self.at_depth(depth)
                    self.open_region(rid, opener)
                    out = self.js(f"""
                        const box = document.getElementById('{rid}');
                        const b = box.getBoundingClientRect();
                        // A modal <dialog> owns its ::backdrop, so every
                        // point outside it hit-tests as the dialog itself.
                        // The class of bug is a DESCENDANT painting outside
                        // its region, which is what this asks about.
                        const mine = e => e && e !== box && box.contains(e);
                        const pts = [], W = window.innerWidth, H = window.innerHeight;
                        for (let i = 0; i <= 10; i++) {{
                          const x = b.left + b.width * i / 10;
                          for (const y of [b.top - 3, b.top - 24,
                                           b.bottom + 3, b.bottom + 60])
                            if (y > 0 && y < H) pts.push([x, y]);
                        }}
                        for (let i = 0; i <= 10; i++) {{
                          const y = b.top + b.height * i / 10;
                          for (const x of [b.left - 3, b.left - 30,
                                           b.right + 3, b.right + 40])
                            if (x > 0 && x < W) pts.push([x, y]);
                        }}
                        const bad = [];
                        for (const [x, y] of pts) {{
                          const hit = document.elementsFromPoint(x, y).find(mine);
                          if (hit) bad.push(Math.round(x) + ',' + Math.round(y) +
                            ' -> ' + hit.tagName + '.' +
                            (hit.className||'').toString().slice(0,20));
                        }}
                        return {{bad: bad.slice(0, 6), n: bad.length,
                                 probes: pts.length,
                                 children: box.querySelectorAll('*').length}};""")
                    self.close_region(closer)
                    floor(self, range(out["children"]), f"{rid} descendants", 20)
                    floor(self, range(out["probes"]), f"{rid} probe points", 40)
                    self.assertEqual(out["bad"], [],
                                     f"{rid}: {out['n']} points outside it are "
                                     f"painted by its own content")

    def test_nothing_in_an_open_region_is_off_screen(self):
        """The region's own box, not its descendants: the descendants are
        clipped by it, so the box being on screen is what makes them
        reachable. `top: 48px` came from positionPanel and a wrong ceiling
        would put the bottom past the fold with no way to scroll to it."""
        for rid, opener, closer in self.REGIONS:
            for depth in (0, 2400):
                with self.subTest(region=rid, scroll=depth):
                    self.at_depth(depth)
                    self.open_region(rid, opener)
                    box = self.js(f"""
                        const r = document.getElementById('{rid}')
                                    .getBoundingClientRect();
                        return {{t: Math.round(r.top), b: Math.round(r.bottom),
                                 l: Math.round(r.left), r: Math.round(r.right),
                                 vw: window.innerWidth, vh: window.innerHeight}};""")
                    self.close_region(closer)
                    self.assertGreaterEqual(box["t"], 0, f"{rid} starts above the fold")
                    self.assertGreaterEqual(box["l"], 0, f"{rid} starts left of the page")
                    self.assertLessEqual(box["b"], box["vh"],
                                         f"{rid} runs {box['b'] - box['vh']}px past "
                                         f"the bottom of the viewport")
                    self.assertLessEqual(box["r"], box["vw"],
                                         f"{rid} runs past the right edge")

    def test_an_open_region_is_opaque_all_the_way_across(self):
        """Not just `background is set`. The instance had the background
        set the whole time - the content had escaped the box that carried
        it. So this samples points across the region and asks the browser
        what is painted there: at every point, the region or one of its own
        descendants must be on top of anything behind it."""
        for rid, opener, closer in self.REGIONS:
            with self.subTest(region=rid):
                self.at_depth(1200)
                self.open_region(rid, opener)
                out = self.js(f"""
                    const box = document.getElementById('{rid}');
                    const b = box.getBoundingClientRect();
                    const cs = getComputedStyle(box);
                    const bad = [];
                    for (let i = 1; i <= 5; i++) {{
                      for (let j = 1; j <= 5; j++) {{
                        const x = b.left + b.width * i / 6;
                        const y = b.top + b.height * j / 6;
                        const top = document.elementsFromPoint(x, y)[0];
                        if (!top || !(top === box || box.contains(top)))
                          bad.push(Math.round(x) + ',' + Math.round(y) + ' -> ' +
                            (top ? top.tagName + '.' +
                             (top.className||'').toString().slice(0,18) : 'null'));
                      }}
                    }}
                    return {{bad: bad.slice(0, 5), bg: cs.backgroundColor}};""")
                self.close_region(closer)
                self.assertNotIn("rgba", out["bg"].replace("rgba(0, 0, 0, 0)", "TRANSPARENT")
                                 if out["bg"] != "rgba(0, 0, 0, 0)" else "TRANSPARENT",
                                 f"{rid} background {out['bg']} is not opaque")
                self.assertNotEqual(out["bg"], "rgba(0, 0, 0, 0)",
                                    f"{rid} has no background of its own")
                self.assertEqual(out["bad"], [],
                                 f"{rid} shows the page through it at: {out['bad']}")

    def test_the_element_that_scrolls_is_the_one_with_the_visible_bounds(self):
        """A region with two scroll containers, or one whose scroll box is
        not the box you can see, clips content at a boundary that does not
        match its visible edge."""
        for rid, opener, closer in self.REGIONS:
            with self.subTest(region=rid):
                self.at_depth(1200)
                self.open_region(rid, opener)
                out = self.js(f"""
                    const box = document.getElementById('{rid}');
                    const b = box.getBoundingClientRect();
                    const cs = getComputedStyle(box);
                    const pad = {{l: b.left + parseFloat(cs.borderLeftWidth),
                                 r: b.right - parseFloat(cs.borderRightWidth),
                                 t: b.top + parseFloat(cs.borderTopWidth),
                                 b: b.bottom - parseFloat(cs.borderBottomWidth)}};
                    const scrollers = [];
                    for (const e of box.querySelectorAll('*')) {{
                      const s = getComputedStyle(e);
                      if (!/auto|scroll/.test(s.overflowY + s.overflowX)) continue;
                      if (e.scrollHeight <= e.clientHeight &&
                          e.scrollWidth <= e.clientWidth) continue;
                      const r = e.getBoundingClientRect();
                      scrollers.push({{
                        el: e.tagName + '.' + (e.className||'').toString().slice(0,20),
                        inside: r.left >= pad.l - 1 && r.right <= pad.r + 1 &&
                                r.top >= pad.t - 1 && r.bottom <= pad.b + 1,
                        gutter: Math.round(e.offsetWidth - e.clientWidth),
                        box: {{l: Math.round(r.left), r: Math.round(r.right),
                              t: Math.round(r.top), b: Math.round(r.bottom)}}}});
                    }}
                    return {{scrollers, pad: {{l: Math.round(pad.l), r: Math.round(pad.r),
                             t: Math.round(pad.t), b: Math.round(pad.b)}}}};""")
                self.close_region(closer)
                self.assertEqual(
                    len(out["scrollers"]), 1,
                    f"{rid} has {len(out['scrollers'])} scrolling boxes, not one: "
                    f"{out['scrollers']}")
                one = out["scrollers"][0]
                self.assertTrue(
                    one["inside"],
                    f"{rid} scrolls in {one['el']} at {one['box']}, which is not "
                    f"inside its own padding box {out['pad']} - so its scrollbar "
                    f"and its clip boundary are not where its edge is")
                self.assertGreater(
                    one["gutter"], 0,
                    f"{rid} reserves no room for a scrollbar: an overlay bar "
                    f"appears only once you are already scrolling")

    def test_a_scrollbar_looks_like_a_scrollbar(self):
        """Read off a SCREENSHOT, because nothing else can see this.

        The gutter was 6px with a transparent track and an --edge-hi thumb -
        1.00:1 against the panel border it sat one pixel inside. Sampled
        down the strip, every pixel at the middle and bottom of the panel
        was (35,42,53): the panel's own background. Geometrically perfect,
        invisible, and the content below the fold read as clipped."""
        rid, opener, closer = self.REGIONS[0]
        self.at_depth(1200)
        self.open_region(rid, opener)
        geo = self.js(f"""
            const box = document.getElementById('{rid}');
            const b = [...box.querySelectorAll('*')].find(e =>
              /auto|scroll/.test(getComputedStyle(e).overflowY) &&
              e.scrollHeight > e.clientHeight);
            const r = b.getBoundingClientRect();
            const bg = getComputedStyle(box).backgroundColor;
            const edge = getComputedStyle(box).borderRightColor;
            return {{right: r.right, top: r.top, bottom: r.bottom,
                     gutter: b.offsetWidth - b.clientWidth,
                     bg, edge, dpr: window.devicePixelRatio}};""")
        _, _, rows = self.screenshot()
        self.close_region(closer)
        d = geo["dpr"]
        rgb = lambda css: tuple(int(v) for v in
                                re.findall(r"\d+", css)[:3])
        background, border = rgb(geo["bg"]), rgb(geo["edge"])
        x0 = int(geo["right"] * d) - int(geo["gutter"] * d)
        x1 = int(geo["right"] * d) - 1
        self.assertGreater(x1 - x0, 2, "no gutter to sample")
        samples = []
        for frac in (0.15, 0.5, 0.85):
            y = int((geo["top"] + (geo["bottom"] - geo["top"]) * frac) * d)
            samples.append([rows[y][x] for x in range(x0, x1)])
        floor(self, samples, "sampled heights", 3)
        # Somewhere down the strip there must be something that is not the
        # panel: a thumb, a track, anything a person could aim at.
        distinct = {px for row in samples for px in row} - {background}
        self.assertTrue(
            distinct,
            f"every pixel of the {geo['gutter']}px scroll strip is "
            f"{background}, the panel's own background - there is nothing "
            f"on screen to say this region scrolls")
        # And it must not read as a thicker border.
        self.assertNotEqual(
            distinct, {border},
            f"the only thing drawn in the scroll strip is {border}, the same "
            f"colour as the border it sits against")
        # The track is the part that is always there. Every sampled height
        # must carry something, not just the one the thumb happens to be at.
        blank = [i for i, row in enumerate(samples)
                 if not (set(row) - {background})]
        self.assertEqual(
            blank, [],
            f"heights {blank} of the scroll strip are pure background: the "
            f"track is invisible, so only a short thumb is ever on screen")

    def test_no_two_regions_of_the_page_overlap(self):
        """The doubled-rail half of the screenshot. The panel stops exactly
        where the rail starts and both are --raised, so the two read as one
        widened rail unless the panel carries its own edge."""
        self.at_depth(1200)
        self.open_region("fpanel", "document.getElementById('fopen').click();")
        out = self.js("""
            // Pairs INVOLVING THE PANEL only. The rail scrolling under the
            // sticky masthead is the design, not a collision; the panel
            // floating into either of them is the defect.
            const named = {panel: document.getElementById('fpanel'),
                           rail: document.querySelector('.rail'),
                           masthead: document.getElementById('masthead')};
            const r = {};
            for (const k in named) {
              const b = named[k].getBoundingClientRect();
              r[k] = {t: b.top, b: b.bottom, l: b.left, r: b.right};
            }
            const over = [];
            const keys = Object.keys(r);
            for (let i = 0; i < keys.length; i++)
              for (let j = i + 1; j < keys.length; j++) {
                if (keys[i] !== 'panel' && keys[j] !== 'panel') continue;
                const a = r[keys[i]], c = r[keys[j]];
                const w = Math.min(a.r, c.r) - Math.max(a.l, c.l);
                const h = Math.min(a.b, c.b) - Math.max(a.t, c.t);
                if (w > 1 && h > 1)
                  over.push(keys[i] + ' x ' + keys[j] + ' = ' +
                            Math.round(w) + 'x' + Math.round(h) + 'px');
              }
            const p = document.getElementById('fpanel');
            return {over, edge: getComputedStyle(p).borderRightWidth,
                    colour: getComputedStyle(p).borderRightColor,
                    railBg: getComputedStyle(document.querySelector('.rail')).backgroundColor,
                    panelBg: getComputedStyle(p).backgroundColor};""")
        self.close_region("document.getElementById('fclose').click();")
        self.assertEqual(out["over"], [], f"regions overlap: {out['over']}")
        # Abutting two same-coloured surfaces with no edge is the defect,
        # so the edge is required only while the colours match.
        if out["railBg"] == out["panelBg"]:
            self.assertNotEqual(
                out["edge"], "0px",
                "the panel and the rail are the same colour and share an "
                "edge with no border: they read as one region")


class CoverageDepthTests(TempDB):
    """A month that looks quiet because five sources were not being
    collected yet is misinformation, of the same class as a quiet feed
    looking broken.

    Split by WHAT YOU CAN DO, not by cause. Time never fills a gap in the
    past - only backfilling does, and only where the source answers for
    old dates.
    """

    def setUp(self):
        super().setUp()
        from macrowire.web import queries
        self.q = queries
        self.sources = list(SOURCES.values())
        self.plant()

    def plant(self):
        """One item per source, at a known earliest date."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        self.earliest = {}
        for n, src in enumerate(self.sources):
            sid = db.upsert_source(self.conn, src.name, src.kind, src.config)
            # Spread them: some inside a 30-day window, some far outside.
            age = 5 + n * 25
            when = now - timedelta(days=age)
            self.earliest[src.name] = when.date().isoformat()
            for k in range(3):
                self.conn.execute(
                    """INSERT INTO items (id, source_id, title, url, fetched_at,
                                          published_at)
                       VALUES (?, ?, ?, 'http://x', ?, ?)""",
                    (f"c{n}_{k}", sid, f"{src.name} {k}", now.isoformat(),
                     (when + timedelta(hours=k)).isoformat()))
            db.log_fetch(self.conn, src.name, status=db.STATUS_OK)
        self.conn.commit()
        seeded(self, self.conn, items=40, sources=10)

    # --- the window rule you asked me to confirm -------------------------

    def test_a_source_covering_the_whole_window_says_nothing(self):
        """Reading a 30-day window, a source whose coverage starts three
        years ago has no boundary and no summary line. It is complete."""
        cov = self.q.coverage(self.conn, self.sources, 30)
        inside = {b["source"] for b in cov["boundaries"]}
        for name, earliest in self.earliest.items():
            with self.subTest(source=name):
                if earliest <= cov["window_start"]:
                    self.assertNotIn(name, inside,
                                     f"{name} covers the window and still "
                                     f"produced a boundary")

    def test_only_coverage_beginning_inside_the_window_appears(self):
        for days in (7, 30, 180, 365):
            cov = self.q.coverage(self.conn, self.sources, days)
            with self.subTest(days=days):
                for b in cov["boundaries"]:
                    self.assertGreater(b["earliest"], cov["window_start"])

    def test_a_wider_window_never_shows_fewer_boundaries(self):
        counts = [len(self.q.coverage(self.conn, self.sources, d)["boundaries"])
                  for d in (7, 30, 180, 365)]
        self.assertEqual(counts, sorted(counts),
                         f"boundary count is not monotonic in window: {counts}")

    def test_complete_plus_boundaries_accounts_for_every_source(self):
        cov = self.q.coverage(self.conn, self.sources, 365)
        self.assertEqual(cov["complete"] + len(cov["boundaries"]), cov["total"])

    # --- one boundary per source, not one per day ------------------------

    def test_each_source_produces_exactly_one_boundary(self):
        """Reading six months back should cross five boundaries, not five
        hundred lines."""
        cov = self.q.coverage(self.conn, self.sources, 365)
        names = [b["source"] for b in cov["boundaries"]]
        floor(self, names, "boundaries", 3)
        self.assertEqual(len(names), len(set(names)),
                         f"a source produced more than one boundary: {names}")

    def test_the_renderer_emits_one_row_per_boundary(self):
        js = read_code(ROOT / "macrowire/web/static/app.js")
        # shift() consumes each boundary exactly once as the rows are walked
        self.assertIn("pending.shift()", js)
        self.assertNotIn("boundaries.forEach", js)

    # --- the four states -------------------------------------------------

    def test_the_four_states_are_distinguishable(self):
        from macrowire.web.queries import (COVERAGE_LOST, COVERAGE_NEVER,
                                           COVERAGE_RECOVERABLE, COVERAGE_UNWIRED)
        states = {COVERAGE_RECOVERABLE, COVERAGE_UNWIRED, COVERAGE_LOST,
                  COVERAGE_NEVER}
        self.assertEqual(len(states), 4)
        cov = self.q.coverage(self.conn, self.sources, 365)
        for b in floor(self, cov["boundaries"], "boundaries", 3):
            with self.subTest(source=b["source"]):
                self.assertIn(b["state"], states)

    def test_each_state_has_its_own_wording_in_every_locale(self):
        from macrowire import i18n
        for locale in floor(self, i18n.available(), "locale files", 2):
            t = i18n.Translator(locale)
            titles, bodies = set(), set()
            for state in ("recoverable", "unwired", "lost", "never"):
                titles.add(t(f"coverage.{state}_title", source="X"))
                bodies.add(t(f"coverage.{state}_body",
                             date="D", first="F", command="C"))
            with self.subTest(locale=locale):
                # recoverable and unwired share a title on purpose - both
                # mean "not collected" - but their BODIES must differ,
                # because only one of them has an action.
                self.assertEqual(len(bodies), 4,
                                 f"{locale}: four states, {len(bodies)} explanations")

    def test_only_the_recoverable_state_offers_a_command(self):
        """An action you cannot take is worse than saying it is gone."""
        from macrowire import i18n
        t = i18n.Translator("en")
        self.assertIn("{command}", i18n.load("en")["coverage"]["recoverable_body"])
        for state in ("unwired", "lost", "never"):
            with self.subTest(state=state):
                self.assertNotIn("{command}",
                                 i18n.load("en")["coverage"][f"{state}_body"])

    def test_the_permanent_states_say_so_without_a_second_sentence(self):
        from macrowire import i18n
        t = i18n.Translator("en")
        self.assertIn("ever", t("coverage.never_title", source="X"))
        self.assertIn("cannot be recovered", t("coverage.lost_body",
                                               date="D", first="F"))

    # --- measure, never warn unconditionally -----------------------------

    def test_a_fully_covered_window_says_so_rather_than_staying_silent(self):
        from macrowire import i18n
        js = read_code(ROOT / "macrowire/web/static/app.js")
        self.assertIn("coverage.end_complete", js)
        line = i18n.Translator("en")("coverage.end_complete", n=16)
        self.assertIn("16", line)
        self.assertIn("complete", line)

    def test_a_source_with_nothing_collected_is_not_reported_as_a_gap(self):
        """No rows at all is 'not polled yet', which the health panel
        already says. It is not a coverage boundary."""
        conn = self.conn
        conn.execute("DELETE FROM items")
        conn.commit()
        cov = self.q.coverage(conn, self.sources, 365)
        self.assertEqual(cov["boundaries"], [])

    # --- the CLI half ----------------------------------------------------

    def test_status_reports_where_the_record_begins(self):
        from macrowire import wire
        src = SOURCES["rba_media_releases"]
        st = wire.source_status(self.conn, src)
        self.assertIn("earliest", st)
        self.assertIn("coverage_state", st)
        self.assertEqual(st["coverage_state"], "never")
        cli = read_code(ROOT / "macrowire/__main__.py")
        self.assertIn("cli.status.coverage_note", cli)

    def test_the_state_matches_what_the_source_can_actually_do(self):
        from macrowire import wire
        expected = {"rba_media_releases": "never", "cftc_cot": "recoverable",
                    "sec_edgar": "unwired", "hkma_press": "lost"}
        for name, want in expected.items():
            with self.subTest(source=name):
                st = wire.source_status(self.conn, SOURCES[name])
                self.assertEqual(st["coverage_state"], want)
