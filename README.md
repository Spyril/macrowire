# MacroWire

A personal news wire for macro and market announcements. It polls primary
sources — central banks, regulators, exchange company announcements — and
keeps every item in a local SQLite database.

**Single user. Personal use. Runs on localhost. Not published, not
deployed, not shared.** That matters legally as much as technically: the
licensing picture for redistributing this material is different from the
picture for reading it yourself, and nothing here has been cleared for
publication. If this ever goes public, the source terms need reviewing
first.

## What this is not

This is **news and announcements**, not price data. No OHLCV, no quotes,
no market data vendor feeds. The distinction is deliberate: announcements
are free to read for personal use; exchange price data costs four figures
a month.

The one apparent exception is the RBA exchange rate feed, which is a
central bank's own published daily reference series — not a market data
product, not tradeable, and stored in a separate table precisely so it
never gets confused with news. See *Exchange rates* below.

## Requirements

Kubuntu 26.04, Python 3.12, conda env `market`.

```bash
conda activate market
pip install -r requirements.txt
cp .env.example .env      # then edit: set MACROWIRE_CONTACT
```

`MACROWIRE_CONTACT` goes into the outbound `User-Agent`. These are public
government servers; identify yourself to them.

Secrets are read from the environment with no fallback default. If
`sources.yaml` interpolates `${NAME}` and `NAME` is unset, the config
loader raises rather than quietly substituting an empty string — a blank
API key or a half-formed User-Agent should never reach a live server.

## Usage

```bash
python -m macrowire fetch     # one poll cycle, prints what was stored
python -m macrowire status    # per-source health
python -m macrowire fetch --source rba_media_releases   # one source only

# one-off historical seed for an API source that has retrievable history
python -m macrowire backfill --source cfets_ccpr --dry-run
python -m macrowire backfill --source cfets_ccpr

python -m macrowire migrate                # apply pending schema migrations
python -m macrowire backup                 # verified, timestamped
python -m macrowire restore                # newest backup, with confirmation
python -m macrowire export                 # irreplaceable rows -> committable file
python -m macrowire import                 # load an export back

python -m macrowire serve                  # local web interface, 127.0.0.1
```

`backfill` is deliberately separate from `fetch`. A poll is small and
frequent; a backfill is a bulk pull against a server that owes us
nothing. It is strictly sequential with a configurable delay between
pages, and **resumable**: every completed page is recorded in
`fetch_log` as a `backfill` row, so an interruption costs one page
rather than the whole run. `--dry-run` prints the plan and makes no
requests.

`fetch` is a single cycle. Scheduling is not built yet — run it by hand,
or from cron/systemd, no more often than the 60-second floor.

## Adding a source

Edit `sources.yaml`. Nothing else.

```yaml
  - name: my_new_feed
    kind: cb_news
    parser: cb_news
    url: https://example.org/feed.xml
    config:
      institution: XYZ
      staleness_days: 14
```

`parser` names a handler in `macrowire/parsers/`. Two ship today:

| parser | reads | writes to |
|---|---|---|
| `cb_news` | RSS-CB `cb:news` entries (RDF/RSS 1.0) | `items` |
| `cb_statistics` | RSS-CB `cb:statistics` entries (RDF/RSS 1.0) | `observations` |
| `rss_news` | plain RSS 2.0 `<item>` entries | `items` |
| `cfets_ccpr` | CFETS CNY central parity JSON API | `observations` |

Only the RBA uses RSS-CB. Every other central bank verified publishes
plain RSS 2.0 with no `cb:` namespace at all.

`rss_news` prefers the feed's own `<category>` for `announcement_type`
and falls back to `config.announcement_type` from YAML, so classifying a
feed that carries no category stays configuration rather than a table of
feed names compiled into the parser.

The honest boundary: **a new feed of an existing shape is pure YAML.** A
genuinely new payload shape (Atom with different extensions, JSON, a
scraped page) needs a new module in `macrowire/parsers/` plus one line in
the registry in `parsers/__init__.py`. No source is ever named in the
fetch loop, the config loader, or the CLI.

Per-source keys under `config:` override anything in `defaults:`.

## The interface

```bash
python -m macrowire serve            # host and port from sources.yaml
python -m macrowire serve --port 8930   # one-off override
python -m macrowire stop             # stop it again
```

Host and port come from `defaults.web` in `sources.yaml` (127.0.0.1:8917),
so there is one place that decides them. `--host` / `--port` override for a
one-off and default to `None` so config always wins.

**`serve` binds the configured port or exits 1.** It never falls back to a
free one — a server that quietly moves is a server you lose track of, and
then you kill the wrong thing. When the port is taken it names who has it:

```
port 8917 is already in use.
  held by pid 58240: python -m macrowire serve
  stop it with:  python -m macrowire stop --port 8917
```

(uvicorn already refuses to relocate — it exits 3 with "address already in
use" — but it does not say *who* holds the port, which is the part you
need.)

### Stopping it: use `stop`, not `pkill`

**`pkill -f uvicorn` is a trap.** Matching on a command line also matches
the shell that ran the command and anything else mentioning the string, so
it will kill your own terminal while the server survives. That is not
hypothetical — it happened repeatedly during development and is how three
orphaned servers ended up holding three consecutive ports.

`python -m macrowire stop` resolves the PID **from the port** by reading
`/proc/net/tcp` for the listening socket's inode and matching it against
`/proc/<pid>/fd`. Stdlib only, no `lsof`, no `psutil`. It then refuses to
kill anything that is not a MacroWire process, and refuses to kill itself.
SIGTERM first, SIGKILL only if the process declines to go. Stopping a port
nothing is listening on is exit 0, not an error — that is the state you
wanted.

If you ever do need to do it by hand, resolve the PID rather than the name:

```bash
ss -ltnp | grep 8917        # then kill that specific pid
```

127.0.0.1 only, no auth, single user. FastAPI + uvicorn serving one static
page: vanilla JS, no build step, no npm, no webfonts, no network on load.

**Read-only except `item_state`.** The interface never fetches a source,
never parses a payload and never writes collected data.

### The ribbon computes time per instant

Every local time is resolved from an IANA zone at the moment in question.
No offset is ever stored, because an offset is a property of a moment, not
of a place — which is the entire thing the ribbon exists to show:

```
Fed, 14:00 New York, measured IQR 0 minutes:
  04:00 Sydney for 6 months   05:00 for 2   06:00 for 4
CFETS, 09:15 Beijing, no DST at origin at all:
  11:15 Sydney for 6 months   12:15 for 6
```

Neither source moves. Sydney moves under both, and the Fed's two DST
transitions land three weeks apart from Australia's, producing a third
regime for part of March.

### Marks are honest to measured timing

Each source declares a `timing.class` in `sources.yaml`, derived from the
collected data rather than assumed:

| class | measured | ribbon |
|---|---|---|
| `fixed` | single stamp, IQR 0m — CFETS 09:15 | a mark |
| `tight` | IQR ≤ 30m — Fed 14:00 ET, NBS 09:30 CST | a mark plus a window |
| `scattered` | IQR 2–7h — 5 of 11 sources | **no mark**, named below the band |
| `date_only` | feed carries no time — HKMA, all 683 items stamped 00:00 HKT | **no time position at all** |

A fixed mark for a source whose interquartile range is seven hours would
be decoration, not information. The config loader refuses `fixed`/`tight`
without both `at` and `timezone`.

Sessions crossing local midnight render as two segments, each flagged with
which edge it runs off.

### Jurisdiction

Every source declares a `jurisdiction` in `sources.yaml` — `AU US CN HK EU
UK JP`. **Required: the config loader rejects a source without one**, and
rejects a value outside the set.

This is deliberately narrower than the topic axis considered and rejected
during the vocabulary discussion. Jurisdiction is a *fact about the
publisher*, known at config time, one value per source. There are no rules
to maintain and nothing to rot — unlike a topic taxonomy, which would have
meant inventing editorial judgement for seven sources that declare none.

| | sources |
|---|---|
| AU | `rba_media_releases`, `rba_exchange_rates` |
| CN | `cfets_ccpr`, `nbs_releases`, `nbs_interpretation` |
| US | `fed_press_monetary`, `fed_speeches` |
| HK | `hkma_press` |
| JP | `boj_whatsnew` |
| EU | `ecb_press` |
| UK | `boe_news` |

It appears in three places: a code in each tape item's meta line, a chip
row above the source chips, and as grouping headers in the rail's source
health.

**No colour.** Seven hues would compete with the one accent that means
unread, so the code is a chrome-weight label in a hairline box and the
group headers are letter-spaced caps. A test asserts the jurisdiction
style block references neither `--accent` nor `--fault`.

The two chip rows are independent facets: **OR within an axis, AND across
them.** Selecting CN shows CFETS and both NBS feeds without naming any of
them; selecting CN *and* a US source is legitimately empty.

One asymmetry worth knowing: `cfets_ccpr` is CN but stores observations,
not items, so it appears in the ribbon and rail but never in the tape.
Filtering the tape to CN shows the two NBS feeds only.

### The tape

Reverse-chronological, day headers in Sydney, time gutter left. Chinese
renders as stored — no translation column exists.

**Repeated titles collapse.** Identical (source, title) pairs become one
row with a count. HKMA ships "Scam alert related to banks" 207 times — 30%
of its entire feed is that one string. Collapsing is the fix; filtering is
not, because at 5.7 items/day across all sources the tape is not
overloaded, it is repetitive.

`importance` (0–5) in `sources.yaml` drives type scale and weight, never
colour — colour is spoken for. Fed monetary policy is loud, HKMA routine
is quiet and drops its summary line.

RBA's `announcement_type` is a full cbwiki URI; the tape displays its
fragment. The stored value is never altered.

### Unread

`item_state` is wired for real. **First launch marks everything read** —
1,825 unread markers on a fresh install is a wall, not a wire. Only what
arrives after that shows unread.

Items mark read after 1.8 seconds on screen. **Unread respects
collapsing**: 207 identical notices are one unread, and marking the group
read marks every member.

### Palette and type

Ink-on-slate. `#0b0d10` rather than true black — pure black behind small
text at 4am causes halation on OLED and the type shimmers. Three neutral
greys carry all structure.

**Exactly one accent, amber `#d9963f`, and it means unread.** Two other
signals exist and are deliberately not amber: `--fault` (muted red) for a
failing or stale source, and `--cursor` (near-white) for the now-marker. A
first pass had all three sharing amber, which made a stale feed and an
unread FOMC statement look alike.

IBM Plex Mono for every timestamp, rate and count — installed locally,
true tabular figures so numeric columns align without hacks. Prose uses
the system UI stack, which hands CJK to Noto without a second declaration,
so `国家统计局关于2026年早稻产量数据的公告` sits at the same optical weight as the
English beside it.

## Verified sources

Every URL below was found on that institution's own RSS directory page
and confirmed to return parseable entries. None were guessed.

| source | feed | ver | entries | archive |
|---|---|---|---|---|
| `rba_media_releases` | rba.gov.au/rss/rss-cb-media-releases.xml | rss10 | 1 | none |
| `rba_exchange_rates` | rba.gov.au/rss/rss-cb-exchange-rates.xml | rss10 | 21 | today only |
| `fed_press_monetary` | federalreserve.gov/feeds/press_monetary.xml | rss20 | 15 | shallow |
| `fed_press_all` | federalreserve.gov/feeds/press_all.xml | rss20 | 20 | shallow |
| `fed_speeches` | federalreserve.gov/feeds/speeches.xml | rss20 | 15 | shallow |
| `ecb_press` | ecb.europa.eu/rss/press.html | rss20 | 15 | shallow |
| `boe_news` | bankofengland.co.uk/rss/news | rss20 | 50 | shallow |
| `boj_whatsnew` | boj.or.jp/en/rss/whatsnew.xml | rss20 | 46 | shallow |
| `hkma_press` | hkma.gov.hk/eng/other-information/rss/rss_press-release.xml | rss20 | 683 | deep |

All report `bozo=False`. Directory pages used: Fed `/feeds/feeds.htm`,
ECB `/home/html/rss.en.html`, BoE `/rss`, HKMA
`/eng/other-information/rss/`, RBA `/updates/rss-feeds.html`. The BoJ
publishes no RSS directory page; its single English feed is linked from
the English homepage.

### Feeds that are not what their name suggests

**`ecb_press` is a combined feed.** Press releases, speeches, interviews
and Governing Council decisions all arrive on it. The ECB publishes no
press-release-only feed. The URL path distinguishes them (`/press/pr/`,
`/press/key/`, `/press/govcdec/`) but nothing in the XML does, so every
entry is stored with the same `announcement_type`.

**`boj_whatsnew` is the BoJ's only English feed.** It is a mixed
"What's New" stream — policy releases, statistics and research papers
together. There is no releases-only English equivalent.

**`fed_press_all` and `fed_press_monetary` overlap.** See below.

### Overlapping feeds

Dedupe is per `source_id`, so a Fed monetary release that appears in both
`press_all` and `press_monetary` is stored once under each. That is
deliberate: the two feeds are separate editorial products and collapsing
them would lose the fact that the Fed filed a release under a particular
category.

The duplicate is easy to collapse on read — the two rows share a `url`.
If you would rather not carry it at all, drop `fed_press_all` from
`sources.yaml`; `press_monetary` plus `speeches` covers the decision flow
with no overlap between them.

## CFETS positional alignment

The CNY central parity fix comes from a JSON API, not a feed. Its
`records[].values` is a **bare array with no labels**, and getting the
alignment wrong stores one currency's rate under another's name while
looking entirely plausible. This is the most dangerous thing in the
project, so the measured behaviour is written down here.

**Values are not ordered by the `currency` parameter you send.** The
server returns them in its own canonical order — the order of
`data.head` — filtered to the pairs requested. Asking for
`USD/CNY,AUD/CNY,HKD/CNY` returns `[USD, HKD, AUD]`.

Verified against single-pair requests, which are unambiguous:

| pair | truth | by request order | by `data.head` |
|---|---|---|---|
| USD/CNY | 6.7817 | 6.7817 ✓ | 6.7817 ✓ |
| AUD/CNY | 4.8058 | 7.8906 ✗ | 4.8058 ✓ |
| HKD/CNY | 0.86466 | 4.2516 ✗ | 0.86466 ✓ |
| EUR/CNY | 7.8906 | 0.86466 ✗ | 7.8906 ✓ |
| 100JPY/CNY | 4.2516 | 4.8058 ✗ | 4.2516 ✓ |

Request-order alignment mis-files **four of five pairs**, and every
wrong number is a believable exchange rate.

### What the API supports as a safeguard

- **`data.head`** — the canonical 25-pair order. This is the real
  alignment key, and it is stable: identical across 2020 and 2026
  requests. The parser aligns against it and refuses to fall back to
  request order if it is missing.
- **Array length** — asserted against the resolved pair count. CFETS
  drops unknown, misspelled, duplicated and empty pairs *silently*, and
  a short array shifts every later pair by one.
- **Per-pair bounds** in `sources.yaml` — wide enough for a decade of
  real moves, tight enough that a positional swap trips them. AUD at
  4.81 landing under HKD (max 1.5) raises immediately.
- **`data.currency`** is a verbatim echo of the request string. It comes
  back unchanged even when the server drops a pair, so it detects
  nothing and is deliberately **not** used for validation.

### Server limits, both found by probing

- a date window of **365 days or more** returns HTTP 200 with
  `flagMessage: 只提供一年历史数据查询及下载` and no records. 364 works.
- a **`pageSize` above 50** returns HTTP 403.

### The fix–market gap is signal, not a data quality problem

Spot-checking the fix against ECB reference rates shows a consistent
deviation, and it is **not an error — do not "correct" it**:

| date | CFETS fix | ECB ref | diff |
|---|---|---|---|
| 2020-06-15 | 7.0902 | 7.0950 | −0.07% |
| 2022-09-15 | 6.9101 | 6.9908 | −1.15% |
| 2024-03-15 | 7.0975 | 7.1961 | −1.37% |

The sign is consistent: the fix is set **stronger for the yuan** than the
market rate, and the gap widens during periods of CNY weakness. That is
the PBoC's counter-cyclical factor — the fix is a policy instrument, not
a market observation, and the spread between it and the market rate reads
as how hard the PBoC is leaning against depreciation.

Near zero in 2020, ~1.2% in 2022 and 2024. Anyone treating this as drift
to be calibrated away would be discarding the most informative thing in
the series.

(A genuine mis-alignment would show as a 30%+ error, not 1%. These
deviations are far too small and too directional to be a parsing fault.)

### 100JPY/CNY

CFETS quotes the yen per **100** JPY and labels it `100JPY/CNY`. That
label is stored verbatim as the `series`. Rescaling it to a "JPY/CNY" of
0.042516 would mean silently altering a published official rate, so the
published form is kept and the caller divides if they want to.

## Storage

SQLite in WAL mode at `data/macrowire.db` (gitignored). Override with
`MACROWIRE_DB`.

**Global tables** carry no user column. An announcement is the same
announcement for everybody, so it is stored once — ten users watching BHP
do not produce ten copies.

- `sources` — id, name, kind, config
- `items` — announcements, content-hashed PK
- `observations` — numeric series (see below)
- `raw_responses` — the unparsed payload of every fetch
- `fetch_log` — one row per attempt: source, timestamp, status,
  new_item_count, error, detail

**Per-user tables** hold everything personal, keyed by `user_id`. One
local user is seeded at `id=1`. There is no login, no OAuth and no tenant
isolation, and none is planned right now — but multi-user would need no
migration, only code.

- `users` — id, email, created_at
- `watchlists` — user_id, ticker, market
- `item_state` — user_id, item_id, read_at, flagged

### raw_responses

Every response is stored as **original bytes, before any decoding is
attempted** — not as decoded text. That distinction is the whole point:
if the encoding was resolved wrongly, decoded text in the recovery table
would carry exactly the same corruption as the parsed rows, and there
would be nothing to recover from. `encoding` records what the decoder
settled on, and stays NULL when a payload never decoded at all.

The bytes are written before decode and before parse, so a `DecodeError`
or a parser failure still leaves the payload on disk. The RSS feeds
cannot be re-fetched retroactively, so this is the only recovery path
there is.

### raw_responses retention

`raw_retention_days` is per-source and **defaults to null — keep
forever**. That default is deliberate: for most feeds here the stored
bytes are the only copy that will ever exist. `rba_media_releases`
carries one item and no archive; discarding its payload loses history
permanently.

Retention is set only where the payload is genuinely re-fetchable. The
NBS feeds are the case that justifies it: each poll returns a rolling
500-entry window covering 2–3 years, so an old payload is recoverable by
simply asking again, and each one costs 4.3 MB.

Pruning runs **after a clean parse only**. If today's payload could not
be read, older ones are the fallback and must survive.

### Decoding

httpx does not raise on undecodable bytes — it substitutes U+FFFD and
returns a string that looks fine until you read it. Nothing here relies
on that. `macrowire/encoding.py` resolves the encoding from what the
payload declares about itself (XML declaration, then meta charset), then
the HTTP header, then UTF-8; decodes with `errors="strict"`; and rejects
any result containing U+FFFD.

Most government servers send a bare `Content-Type: text/html` with no
charset at all, so a client default is a guess rather than a detection.
Resolving from the document's own declaration is what makes it a
detection.

### Content hashing

An item's PK is a SHA-256 over source, external id, url, title and
published time. Re-polling an unchanged feed stores nothing. A corrected
headline upstream produces a *new* row rather than silently overwriting
one you may already have read — and `raw_responses` holds both payloads
either way.

Both versions share an `external_id`, which is indexed
(`idx_items_revision_chain` on `source_id, external_id, published_at`) so
a UI can collapse the chain instead of showing near-duplicates:

```sql
SELECT * FROM items WHERE source_id = ? AND external_id = ?
ORDER BY published_at DESC, fetched_at DESC, rowid DESC LIMIT 1;
```

`rowid` is the final tiebreak: a correction that leaves `published_at`
untouched and lands within the same second as the original would
otherwise be ambiguous. The 60-second poll floor makes that impossible in
normal operation, but insertion order settles it regardless.

`status` reports how many items have superseded versions, per source.

### Timestamps

Stored as UTC, ISO 8601, to the second. RBA stamps `+10:00`/`+11:00`;
normalising keeps ordering correct across the DST boundary. The original
strings survive in `raw_responses`. A timestamp with no offset is an
error — the parser refuses to guess.

## Backfill: how much history each feed carries

Archive depth varies enormously and is a property of the feed, not of
this tool. Nothing in the schema assumes history exists before first run.

**`rba_media_releases` carries exactly one item** — the most recent
release. No paging, no `since` parameter. Anything published before your
first poll is unreachable through RSS and this tool will never see it.
That feed is strictly forward-only.

Most others carry a shallow window: 15–50 entries, a few weeks to a few
months. `hkma_press` is the outlier at ~680 entries, several years deep.

The practical consequence is unchanged: **run `fetch` regularly from
today.** A sparse feed that publishes while you are not polling loses
that item permanently. Backfill would mean scraping, which is not built
and not planned.

## Exchange rates live in `observations`, not `items`

Every entry in the RBA exchange rate feed has a **permanent** id —
`.../exchange-rates.html#USD` — that is byte-identical every single day.
Only `cb:value` and `cb:period` change underneath it. The feed is a
mutable snapshot of 21 slots, not an append-only stream of announcements.

That rules out `items` on two counts. Content-hashing the entry would
make every day's USD rate collide with yesterday's; content-hashing the
*value* instead would push 21 rows a day into a tape meant for sporadic
media releases, drowning roughly 40 RBA announcements a year under 7,700
FX quotes, and making read/flagged state meaningless for them.

The feed's own namespace agrees. Media releases are `cb:news`; rates are
`cb:statistics`. Two record types at the source, two tables here.

`observations` is keyed `UNIQUE(source_id, series, period)`.

### Revisions are surfaced, not swallowed

The RBA occasionally revises a published rate. `INSERT OR IGNORE` would
discard the correction in silence, which is the worst available outcome.
Instead, on a key collision the incoming value is compared to the stored
one:

- identical → no-op
- different → the row is updated, `revised_at` is stamped, and a
  `revision` row goes into `fetch_log` recording series, period, old
  value and new value

`fetch` prints revisions as `REVISED`; `status` counts them.

### Series naming

`AUD/USD`, `AUD/JPY`, and so on. Two entries are not currency pairs: the
trade-weighted index arrives with `cb:targetCurrency` set to `XXX` (ISO
4217 "no currency") and is stored as `AUD/TWI_4pm`; the SDR arrives as
`XDR` and is stored as `AUD/XDR`.

## Failure policy

**Raises, loudly:**

- HTTP error, timeout, connection failure
- a **truncated response** — `Content-Length` disagreeing with the bytes
  received. Only checked on identity-encoded responses: with gzip in
  play `Content-Length` describes the compressed stream while the body
  is decompressed, and comparing them is nonsense. httpx enforces the
  compressed stream's own length and surfaces a short read as a
  `FetchError` anyway
- a payload that does not decode cleanly, or that decodes to text
  containing U+FFFD
- a payload that is not a feed — including an HTML error page, which is
  well-formed XML and would otherwise pass as a clean empty feed
- **any `bozo` flag from feedparser**, not merely one that yields zero
  entries. See below
- feedparser sniffing an encoding other than the UTF-8 the bytes
  actually are — the signature of a truncated or malformed document
- malformed XML, or a disagreement between feedparser's entry count and
  the parser's
- an entry missing a required field, or a timestamp without an offset
- **zero entries from a source that has parsed successfully before.** On
  a source's first ever fetch, an empty feed is unproven rather than
  broken, and does not raise

#### Why any bozo flag is fatal

The gate used to be `if parsed.bozo and not parsed.entries`, which
accepts a document that is malformed *but still yielded entries*. That
is the dangerous case, not the harmless one.

A truncated 4.5MB feed produced **313 entries with every title
mojibake**: the cut landed mid-element, feedparser fell back to lenient
parsing, its encoding sniffer guessed `iso-8859-2` for a document
declaring UTF-8, and the old gate waved all 313 through because
`parsed.entries` was non-empty. Partial garbage is still garbage.

All eight feeds currently polled report `bozo=False`, so strictness costs
nothing today. If a feed later turns out to be persistently bozo for a
benign reason, that is a decision to make deliberately, not something to
absorb silently.

Every failure is written to `fetch_log` with the error text, and then
re-raised. All sources are attempted before the cycle raises, so one dead
feed never hides another's result.

**Never raises:**

- **Item age.** RBA media releases are sporadic; weeks between releases
  is entirely normal. A staleness alarm that cries wolf gets ignored,
  which defeats the point of having one.
- Being polled sooner than `min_interval_seconds` — that is logged as
  `skipped`, not an error.

**Reported as information only, in `status`:**

- time since last successful fetch
- days since the last new item was stored
- latest content date, row counts, raw payload count, revision count
- last recorded error, if any

#### Polling on publication, not on the clock

`cfets_ccpr` supplies its own fetcher rather than using the pipeline's
single GET. Before asking for any data it reads the fix timestamp CFETS
advertises at `ccpr.json` and compares it with the newest period already
stored. If nothing new has published it skips, with the reason recorded —
a skip, not an error, because a quiet CFETS means a weekend or a PRC
public holiday.

Polling on clock time alone would fire on holidays and on any day the
fix runs late, and a no-op would look identical to a success. The gate
also self-heals: if the machine was off for a few days, the stamp is
ahead of what is stored and the next poll catches up.

#### Staleness reporting is not freshness assertion

Two different mechanisms, deliberately named apart so they do not get
conflated:

- **Staleness reporting** (`status`, `staleness_days`) is *information*.
  It answers "how long since this source published anything new", never
  raises, and exists because a sporadic feed going quiet is normal.
- **Freshness assertion** (`StaleContentError`) is a *fault*. It answers
  "is this response actually current", and exists for sources that can
  serve a years-old snapshot with an intact structure and a 200 status,
  where every shape check passes. Not wired to any source yet — every
  feed currently polled carries dated entries we can already see.

`staleness_days` is per-source in `sources.yaml` and **defaults to off**.
When set and exceeded, `status` prints `[STALE]` next to the source name
and still exits 0.

The rule for setting one: a **daily** feed going quiet is a fault; a
**sporadic** feed going quiet is not. Only `rba_exchange_rates` has a
threshold (4 days, which absorbs a long weekend plus a holiday). Every
news feed is left off — the RBA can go weeks between media releases and
that is normal. `hkma_press` is near-daily and would be a defensible
candidate if you want one more.

## Politeness

- Descriptive `User-Agent` with a contact address on every request.
- 60-second minimum poll interval per source, enforced against
  `fetch_log` so it survives restarts. `sources.yaml` cannot configure a
  value below the floor — the loader rejects it.
- `min_interval_seconds` is a floor, not a schedule: run `fetch` as often
  as you like and each source is polled no more often than its own
  interval. The NBS feeds use 21600 (6 hours) — they are daily-ish
  statistical releases, and at 4.3 MB uncompressed each, polling them
  like a news wire would move gigabytes a month for nothing.
- `timeout_seconds` (default 120, per-source overridable). 30s was too
  tight: a timeout mid-download is precisely what produces a truncated,
  mojibaked feed, and a slow poll is a far better outcome than a corrupt
  one.
- `stagger_seconds` (default 2) spaces sources within a cycle. Fetching
  is sequential, so servers are never hit simultaneously in any case;
  this stops nine requests going out inside a second across five
  different governments. A full cycle takes ~20s.

## Git hook

`git-hooks/commit-msg` enforces that commits are authored by
`Spyril <spyril@gmail.com>` and rejects machine-generated attribution
trailers.

**Hooks are not cloned.** Install it by hand:

```bash
cp git-hooks/commit-msg .git/hooks/commit-msg
chmod +x .git/hooks/commit-msg
```

## Layout

```
sources.yaml              every source definition
export/irreplaceable.jsonl  COMMIT THIS - off-machine copy of unrecoverable rows
macrowire/
  __main__.py             CLI: fetch, status
  config.py               YAML loader, ${ENV} expansion, validation
  backfill.py             resumable one-off historical seeding
  web/app.py              FastAPI: read-only except item_state
  web/ribbon.py           session + mark geometry, per-instant timezones
  web/queries.py          tape, collapsing, unread, rail
  web/static/             index.html, style.css, app.js - no build step
  backup.py               online-API backups, verified; restore
  export.py               JSONL dump of irreplaceable rows; import
  migrations.py           versioned schema, ordered and retrofittable
  encoding.py             strict decoding; U+FFFD is corruption
  db.py                   schema, connection, hashing, fetch_log
  wire.py                 poll cycle and status; names no source
  errors.py               failure taxonomy
  parsers/
    __init__.py           name -> handler registry
    base.py               dialect-independent: validation, entry
                          discovery, date normalisation
    rdfcb.py              RSS-CB / RDF specifics
    cb_news.py            RSS-CB cb:news       -> items
    cb_statistics.py      RSS-CB cb:statistics -> observations
    rss_news.py           plain RSS 2.0        -> items
git-hooks/commit-msg
data/macrowire.db         gitignored
```

## Testing

```bash
python -m unittest discover -s tests -v
```

Twenty-four regression tests over stdlib `unittest` — no new dependency.

**Tests never touch `data/macrowire.db`.** The suite sets
`MACROWIRE_REFUSE_DEFAULT_DB=1` before importing anything, which makes
`db.connect()` with no explicit path raise. A test that forgets to pass a
temp path fails loudly rather than quietly writing fabricated rows into
years of collected history — which is exactly what happened once during
development, and is why the guard exists.

Fixtures in `tests/fixtures/` are real payloads lifted from
`raw_responses`, trimmed where large.

## Durability

Some of what this database holds **cannot be re-fetched at any price**.
`rba_media_releases` carries a one-item window with no paging and no
`since` parameter: every release it collects from now on exists only
here. That changes what the tooling has to guarantee.

### Schema migrations

```bash
python -m macrowire migrate
```

Versioned migrations in `macrowire/migrations.py` — a `schema_version`
table and an ordered list. Stdlib sqlite3, no Alembic, no downgrades.
`fetch` and `status` apply pending migrations automatically.

**A schema change must never mean deleting the database again.** It did
once during development, which cost 34 re-pulled requests against CFETS
and would have been unrecoverable had it been RBA data.

A database predating this mechanism is *retrofitted*, not wiped: if the
baseline tables exist, it is stamped as version 1 and only later
migrations run. Verified against the live database holding 8,071
observations — schema advanced, zero rows lost.

Adding one: append to `MIGRATIONS` with the next integer. Never edit or
renumber a migration that has shipped; `applied_at` in an existing
database is the record it already ran.

### Backups

```bash
python -m macrowire backup            # verified, timestamped
python -m macrowire backup --list
python -m macrowire backup --keep 14
```

Uses **SQLite's online backup API, not a file copy.** In WAL mode the
`.db` file alone is not a consistent snapshot — recent commits live in
the `-wal` sidecar until a checkpoint, so copying it while anything
writes can produce a file that opens cleanly and is silently short of
data. That is the worst possible property in a backup.

Every backup is **verified before it counts as one**: reopened,
`integrity_check`ed, and row counts compared against the source across
all eight tables. A mismatch deletes the file and raises rather than
leaving something untrustworthy on disk. Backups are rolled back to a
non-WAL journal so each is a single self-contained file.

Automatic backups run **inside the fetch cycle**, not from a separate
cron entry. A second cron line is a second thing to forget or
misconfigure, and what is being protected is partly unrecoverable. A
backup is taken only when the cycle actually stored something new *and*
the newest existing backup is older than `interval_seconds` — so an
unchanged database is not copied 26 MB at a time for nothing. Configure
under `defaults.backup` in `sources.yaml`.

### A restore can leave data intact but its log missing

Worth knowing before it confuses you: **restoring from a backup taken
before a successful fetch keeps the items but loses that fetch's
`fetch_log` rows.** The source then reports `no success logged` in
`status` and the rail, while sitting on hundreds of perfectly good items.

That happened to `nbs_releases`: 500 items present, its only log row an
SSL failure, so it displayed as never having succeeded. The data was
fine; the log was not. The next successful poll clears it.

If a source shows `no success logged` but has rows, check
`SELECT * FROM fetch_log WHERE source = ?` before assuming the fetch is
broken — and check whether a restore happened in between.

### Restore

```bash
python -m macrowire restore                     # newest backup, prompts
python -m macrowire restore --backup <path>
python -m macrowire restore --yes               # skip the prompt
```

The current database is **moved aside, not overwritten** — if the
restore was a mistake, the thing being replaced may itself be the only
copy. Displaced files are left as `macrowire-replaced-<stamp>.db` and
are not pruned automatically; delete them yourself once satisfied.

An unreadable or corrupt backup is rejected *before* the live database
is touched.

Tested by round-trip, not by assertion: the suite backs up, deletes
every row, restores, and diffs counts and contents. Drilled against the
live database too — 3,640 observations destroyed and fully recovered.

### What you would actually lose

`status` classifies every source:

| | meaning | sources |
|---|---|---|
| `NO` (none) | our copy is the only one | `rba_media_releases`, `rba_exchange_rates` |
| `PARTIAL` (rolling) | only the live window is re-fetchable | the six news feeds, both NBS |
| `YES` (queryable) | fully re-fetchable on demand | `cfets_ccpr` |

Sources marked `archive: none` are **forbidden from setting
`raw_retention_days`** — the config loader rejects it, because pruning
their payloads destroys history permanently. There is a test asserting
every source is classified and that no `none` source prunes.

### Off-machine durability: the export

Backups protect against a mistake. They do not protect against a dead
disk — they sit on the same one.

```bash
python -m macrowire export     # -> export/irreplaceable.jsonl
python -m macrowire import     # load it back
```

**`export/` is deliberately NOT gitignored.** The file is meant to be
committed.

> **Committing `export/irreplaceable.jsonl` is what makes the
> irreplaceable rows survive a disk failure. Nothing in this project does
> that for you — it is on you to commit it.** An uncommitted export is
> just another file on the same disk as the database it came from.

`fetch` does not commit and never will; the project makes no commits at
all by design. `export` prints a reminder when the file has changed and
says nothing when it hasn't.

#### What goes in it

Only rows from sources classified `archive: none` — currently
`rba_media_releases` and `rba_exchange_rates`. Everything else is
re-fetchable and excluded on purpose: an export carrying 8,050 CFETS
observations and 1,800 news items would be large, would churn on every
poll, and would be protecting things that need no protection.

Today that is 25 lines and 10 KB. It grows by one line per RBA media
release and 21 per trading day of fixings.

#### Why JSONL rather than a SQL dump

Because the whole point is that this file lives in git.

Line-oriented data diffs per row: a new media release appends one line
and touches nothing else, so history stays readable and a conflict is
resolvable by eye. A SQL dump rewrites multi-row `INSERT` batches and
re-emits schema DDL, producing large uninformative diffs for a one-row
change. JSONL is also schema-loose — it survives the schema evolving
underneath it, which matters now that migrations exist — and readable
without a database.

#### Determinism

Fixed sort order (natural keys, never rowid), `sort_keys=True`, and **no
timestamp anywhere in the file** — including the header. Re-exporting
unchanged data produces a byte-identical file and the file is not
rewritten, so `git status` stays quiet unless something genuinely new was
collected. Verified by md5 across runs and by a test.

Chinese text is written as-is rather than `\uXXXX` escapes, so a diff
stays legible.

#### Import semantics

`INSERT OR IGNORE` throughout: importing restores what is missing and
**never overwrites what is present**. A local row is at least as
trustworthy as a committed copy of itself. Item ids are content hashes
and are carried in the export, so a round trip reproduces the database
exactly — verified live by wiping both `archive: none` sources, importing,
and confirming the re-export was byte-identical.

#### What it does not cover

Raw payloads are excluded. For these two sources the parsed rows carry
the full information — 21 numbers a day, and a short media release — and
base64-encoded XML would add megabytes of churn a year for little
recovery value. `raw_responses` still exists locally for parser recovery;
it simply is not part of the off-machine copy. If that trade ever looks
wrong, the export format is versioned and can carry it.

### Telling a blip from a withdrawal

`fetch_log.error_kind` distinguishes failure modes that otherwise look
identical:

| kind | meaning |
|---|---|
| `network` | DNS failure, refused connection, TLS abort, timeout — says nothing about whether the source still exists |
| `http_404`, `http_410` | the shape of a feed that has been withdrawn |
| `http_5xx`, `http_429` | the shape of a server having a bad day |
| `decode` / `parse` | the payload arrived and was wrong |
| `empty` | zero entries from a source that has parsed before |

`status` shows consecutive failures since the last success with a
breakdown by kind. One `network` failure is a blip; twelve `http_404`s
in a row is a source that has gone.
