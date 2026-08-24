# MacroWire

<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

`SPDX-License-Identifier: AGPL-3.0-or-later` — see [Licence](#licence).
**The licence covers the CODE. It does not cover the data the tool
collects**, which belongs to each publisher under their own terms.

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
| `ecb_fx` | ECB euro reference rates (gesmes XML) | `observations` |

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

### Filters: one bar, four axes

```
┌──────────────────────────────────────────────────────────────────────┐
│ Filter f │ JUR CN ×  TKR NVDA ×  TYPE sec edgar 8-K:2.02 ×│ clear all│
└──────────────────────────────────────────────────────────────────────┘
```

Collapsed to **one ~34px row**, where three chip rows previously took
~136px. The tape is the product; filters cost almost nothing unused.

**The tokens are the only representation of active state.** There is no
second copy to drift out of sync, and the bar sits between the ribbon and
the tape — you cannot look at the tape without crossing it. Each token
removes itself; `clear all` appears only when something is active.

`f` opens the panel, `Esc` closes, `c` clears, `Tab` is trapped inside the
panel while open, focus is visible throughout.

Axes combine **OR within a row, AND across rows**.

#### Type is source-scoped, and SEC decomposes

Measuring the actual vocabulary settled the shape:

```
sec_edgar   44 distinct types      ecb_press  3
boe_news, boj_whatsnew, hkma_press, nbs_*, fed_*, rba_*   1 each
```

**Nine of ten sources have exactly one type — their type *is* their
source.** A global type axis would ship nine chips selecting rows already
selectable by the source row. So type is grouped under its owning source,
and a source with a single type is not offered as a type filter at all.

And the stored string is a composite: `8-K [2.02, 9.01]`. *"Show me
results announcements"* is `form 8-K carrying item 2.02` **or** `form
10-Q/10-K` — a query that composite can never express, because form and
items are welded together.

Migration 003 splits them into `type_primary` and `type_tags`, leaving
`announcement_type` untouched for display. Named generically rather than
`form`/`sec_items`: ECB already has a primary with no tags, and HKEX's
*"Announcements and Notices - [Interim Results]"* would have fitted the
same shape had it been usable.

One correction that mattered: EDGAR's `items` field carries 8-K item
numbers **only on 8-K and 8-K/A**. On `EFFECT` it holds a related form and
a timestamp (`S-4,2024-05-06 16:00:00`), on Form D a rule reference
(`06b`). Reading those as 8-K items would have offered a filter vocabulary
invented out of a misread field, so items are parsed on 8-K forms only.

#### Populated-only, on every axis

A chip renders only if it would return rows in the current window. The
absence carries information: **no `UK` chip means the Bank of England has
published nothing this month.** It also shrinks the type axis from 85
distinct values to 16.

#### Zero results never look like an empty tape

```
No items match these filters
  JUR CN ×   TKR NVDA ×
Filters combine as OR within a row and AND across rows, so narrowing
two axes at once can leave nothing.
                                              [ clear all filters ]
```

An unfiltered empty window says something different — *"No filters are
active — the last 30 days are genuinely empty."* The token bar prevents
the confusion one layer up; this catches it where the confusion would
actually bite.

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

## Company filings: SEC EDGAR only, and why

Of ASX, HKEX and SEC EDGAR, **only the SEC permits what this does.**

| | terms |
|---|---|
| **SEC EDGAR** | *"We allow scripted access to sec.gov content"* — 10 req/sec, declared User-Agent. US federal works are public domain. |
| **ASX** | prohibits *"any spider, screen scraper, robot … to use or access the Site in any way whatsoever, including monitoring, downloading or copying"* without prior written consent. Personal non-commercial use covers **manual** reading; automated access is prohibited separately and independently of purpose. |
| **HKEX** | prohibits *"any programmatic, scripted or other mechanical means to access this Website"*, *"systematic retrieval to create collections, compilations, databases"*, and text/data mining. Its personal-use grant allows storing pages on disk *"but not on any server or other storage device connected to a network."* |

ASX and HKEX are **out**. A JSON endpoint on a different hostname does not
change the plain intent of "no spider, screen scraper, robot or similar
process". Same call as the PBoC `robots.txt`.

### The licensing position, stated plainly

**Nothing this project ships touches a prohibited endpoint.** SEC EDGAR is
public domain and explicitly invites scripted access; the central bank RSS
feeds are published for reading; CFETS is a public JSON API with no terms
restricting retrieval. The one source with an explicit prohibition — PBoC —
is not polled, and the two exchanges with explicit prohibitions are not
polled either.

That is a deliberate position, not an accident of what was easy. It also
means the code could be made public without a licensing problem — and it
now is, under AGPL-3.0-or-later. The *collected data* is a separate
question with a different answer; see [Licence](#licence) below, which
states the split explicitly.

### Editing the watchlist: CLI or UI, one code path

Both call `macrowire.watchlist.add`. Neither reimplements validation, so a
mistyped ticker fails identically in either — the UI surfaces the CLI's
exact message as a 400, in front of you, never a silent accept.

The interface writes **two** tables and no others: `item_state` and
`watchlists`. Both write paths are POST.

An earlier version ran the first-run mark-everything-read sweep inside
`GET /api/bootstrap`, which made a read mutate — a prefetch, a refresh or
a crawler would have silently consumed the one chance to do it. Bootstrap
now only *reports* `first_run: true`; the client POSTs `/api/first-run`.
Two tests enforce this: one that bootstrap calls no writer, and one that
walks every `@app.get` block asserting the same.

### Watchlist-driven, and it ships empty

Company filings are **watchlist-filtered by default**. The alternative is
pulling an exchange's entire daily output to keep a handful of rows: ASX
publishes ~611 announcements a day, HKEX ~658. Against a 5.7 items/day
tape that is not a bigger system, it is a different one.

```bash
python -m macrowire watchlist add AAPL              # validated against the SEC map
python -m macrowire watchlist add BHP --market AU
python -m macrowire watchlist list
python -m macrowire watchlist remove AAPL
python -m macrowire watchlist refresh               # re-download the ticker map
```

**Ships empty.** A default watchlist would be a guess about what someone
holds, and `sec_edgar` polls nothing at all until you add something —
recorded as a skip, not an error.

**A US ticker is validated on add and an unmatched one fails immediately.**
`APPL` is rejected against the 10,387-entry SEC map. A typo that is
accepted returns nothing forever and looks exactly like a quiet company,
which is the same silent-failure class this project has spent every step
eliminating. The ticker map is cached in `data/` and refreshed weekly.

### The SEC's User-Agent is enforced, not advisory

They require `Name email`. A normal descriptive User-Agent — the kind every
other source here accepts — was answered with **HTTP 403 "Request Rate
Threshold Exceeded"**. So `SEC_CONTACT` is required in `.env` and its
absence is a hard failure: polling in a way that gets the address blocked
is worse than not polling. Requests are spaced 0.5s, well under their
10/sec ceiling.

### Their vocabulary, not ours

`announcement_type` carries the form type plus 8-K item numbers where
present — `8-K [2.02, 9.01]`, `10-Q`, `424B2`. Nothing invented. Headlines
use the official Form 8-K captions.

`is_price_sensitive` — nullable and unpopulated since step 1 — is set
**True only where the SEC's own item number says so**:

| item | caption |
|---|---|
| 2.02 | Results of Operations and Financial Condition |
| 5.02 | Departure or Election of Directors or Certain Officers |
| 7.01 | Regulation FD Disclosure |

Everything else stays **NULL, not False**. 8.01 "Other Events" is the
obvious temptation and is deliberately excluded — it is a catch-all whose
contents range from a buyback to a name change. 9.01 is an exhibit marker,
not an event. A coin flip in that column would be worse than a null.

Form 4 (insider transactions) is skipped by default — 58% of Apple's 1,001
most recent filings. The match is exact, not a prefix: `424B2` is an
unrelated prospectus form and is kept.

## FX relevance: three states, per-source vocabularies

Measured on 90 days of collected items before designing anything. The
result that shaped it: **eight of ten sources are genuinely mixed**, and
the split is not a handful of shared patterns.

Not because central banks publish non-FX material, but because several of
these feeds carry the **whole institution** — and several of these
institutions are also prudential regulators. `boe_news` is 25% FX because
the same feed carries the PRA: insurance consultations, enforcement fines,
banknote imagery advisory group minutes.

And the vocabularies do not transfer. A first rule set missed
`Minutes of the London FXJSC Main Committee` — the **Foreign Exchange Joint
Standing Committee** — because `FXJSC` does not match `\bFX\b`. It missed
every Chinese macro print and all of BoJ's monetary operations. So each
source carries its own `fx: { include, exclude }` block.

### Unclassified is never not-FX

Three states, and the third is load-bearing:

| state | means |
|---|---|
| `fx` | matched an include pattern |
| `not_fx` | matched an exclude pattern |
| `unclassified` | matched neither, or the source declares no vocabulary |

A `NULL` `fx_state` — a row stored before classification existed — counts
as unclassified too. **Absence of a rule must never read as a negative**,
or adding a source silently hides it and renaming a committee silently
drops items out. `unclassified` is offered as a filter chip rather than
hidden, so anything missing from an FX view can still be found.

Where include and exclude both match, **exclude wins**: the ambiguous case
stays out of an FX-only view rather than in it.

Reference series (`rba_exchange_rates`, `cfets_ccpr`, `ecb_fx`, `cftc_cot`)
declare `fx: true` — FX by construction, no vocabulary needed for numbers
that have no titles to match against. Declaring both raises at config load.

### Drift is visible, because a vocabulary rots silently

When a source renames a committee its items stop matching and drop out of
the filter with no error. `status` reports coverage per source and flags a
rise against the preceding period:

```
boe_news
  fx classification     : 13 fx / 25 not / 13 unclassified (25%)
                          DRIFT: 61% unclassified in the last 30d vs 12%
                          before - the source may have renamed something
                          the vocabulary matches on.
```

Current coverage, 2,609 stored items: **775 fx / 1,390 not / 444
unclassified (17%)**. The worst is `nbs_interpretation` at 38%, which is
mostly retrospective achievement reports the vocabulary deliberately does
not chase.

## Positioning: CFTC Commitments of Traders

Weekly non-commercial (speculative) longs and shorts in currency futures —
how the money is placed and which way it moved. A different *kind* of
information from everything else here: the tape says what was announced,
this says what was positioned.

`publicreporting.cftc.gov/resource/6dca-aqww.json`, Socrata, no key. US
federal work, public domain. `robots.txt` sets `Crawl-delay: 1` and
disallows only some browse paths; `/resource/` is allowed. Honoured.

**81,309 observations over 13,767 reports back to 1986-01-15**, seeded in
**three requests**.

### There is no published net field — and twelve that look like one

The payload has 133 fields. Twelve contain `net`:

```
conc_net_le_4_tdr_long_all, conc_net_le_8_tdr_short_all, ...
```

Every one is a **trader-concentration ratio** — the net position of the
four or eight largest traders. None is the non-commercial net. Reaching
for `conc_net_le_4_tdr_long_all` because it matches on name returns a
plausible number measuring something else, and nothing downstream would
catch it.

Net is `noncomm_positions_long_all − noncomm_positions_short_all`: two
published fields, same row, same report. Both components are stored
alongside so the arithmetic stays checkable. `rate_type` marks derived
metrics as derived. This is arithmetic on one row, unlike the derived
`USD/JPY` this project declined — that crossed two different rates at a
fixing time belonging to neither.

### Contracts are pinned by code, never by name

The dataset also carries `JAPANESE YEN-dormant`, `SWISS FRANC-dormant`,
`POUND STERLING-OLD`, `MARK/YEN XRATE-OLD`, `AUSTRALIAN DOLLAR - SMALL`
and a shelf of cross-rate contracts. Name matching would sweep those in
and produce a series that looks continuous while silently mixing
instruments.

| code | currency | | code | currency |
|---|---|---|---|---|
| 232741 | AUD | | 092741 | CHF |
| 097741 | JPY | | 090741 | CAD |
| 099741 | EUR | | 112741 | NZD |
| 096742 | GBP | | 098662 | USD Index |

The expected `name` is stored beside each code and **asserted** against
what the API returns. If the CFTC reassigns a code, that raises rather
than quietly changing what is tracked.

### Missing change is omitted, not zeroed

29 of 13,767 rows have no week-on-week change: a contract's first report
has no prior week, and there are gaps mid-history too (NZ Dollar has 9,
the USD Index 5 — so not simply one per contract). Those rows store
`long`, `short` and `net` and **omit** the change metrics. *"No prior
week"* and *"no change from last week"* are different facts and must not
look alike.

Raising on them, which the parser did on the first attempt, made the
entire history unfetchable — the failure was loud and in the right place.

## Three reference series, three bases

The rail carries three daily reference series, and each is quoted against
whatever its publisher quotes against — none is converted:

| source | base | series | fixed at |
|---|---|---|---|
| `cfets_ccpr` | CNY target | `USD/CNY`, `AUD/CNY`, `EUR/CNY`, `HKD/CNY`, `100JPY/CNY` | 09:15 CST |
| `ecb_fx` | **EUR** | `EUR/USD`, `EUR/JPY`, `EUR/GBP`, `EUR/CHF`, `EUR/AUD`, `EUR/CNY`, `EUR/HKD`, `EUR/CAD` | ~16:00 CET |
| `rba_exchange_rates` | **AUD** | 21 crosses | 16:00 AEST |

The RBA panel is AUD-only and CFETS is CNY-only **because that is what
those publishers publish** — not a display choice. There is no `USD/JPY`
in either feed. Adding major crosses therefore means adding a source, which
is what `ecb_fx` is.

A derived `USD/JPY` (`AUD/JPY ÷ AUD/USD`) is deliberately **not** offered:
it would be a number nobody published, carrying the RBA's Sydney fixing
time, and storing it would break the store-what-was-published principle
that has held since step 1.

### ECB euro reference rates

`ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml` — published directly by
the ECB as gesmes XML, 1.5 KB, 29 currencies. Frankfurter and similar
services are convenience layers over this exact file, so there is no reason
to use one.

The format nests **three elements all named `Cube`**, distinguished only by
which attributes they carry: a container, a date, a rate. The parser matches
on attributes rather than nesting depth, because the daily and 90-day files
differ in ways the schema does not promise to keep.

The file carries a **date and no time**. The ECB publishes around 16:00 CET,
applied explicitly from config and resolved per-instant — so a summer date
stamps `14:00 UTC` and a winter one `15:00 UTC`. A stored offset would be
wrong for half the year, the same lesson the ribbon taught.

Seeded once from the 90-day file: **512 observations over 64 trading days**.
That file is erratically slow — three consecutive fetches of the same 69 KB
took 40.6s, 25.7s and a timeout — while the daily file is consistently fast,
hence the 300s timeout on this source. It is the server, not the payload.

Cross-checked against the two series already collected:

```
EUR/CNY   ECB 7.8538  vs CFETS 7.8906           0.47% apart
EUR/AUD   ECB 1.6438  vs RBA 1/0.6108 = 1.6372  0.40% apart
EUR/USD   ECB 1.1681  vs CFETS-implied 1.1635   0.39% apart
```

Three independent central banks, different fixing times and dates, agreeing
to well under 1%. A misparse would be off by orders of magnitude.

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
| `cninfo_announcements` | cninfo.com.cn/new/hisAnnouncement/query | json | per code | queryable |
| `sse_southbound` | query.sse.com.cn/ggt/getQuatationInfo.do | json | 1/day | from 2024-08-19 |

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

## Chinese exchanges: what the terms actually say

Surveyed SSE, SZSE, CNINFO and CFFEX before building anything. The result
is not the ASX/HKEX pattern.

**SSE and SZSE publish the same 法律声明 clause 三**, word for word apart
from the exchange's name:

> 在遵守中国有关法律与本声明的前提下，任何机构或者个人可基于**非商业目的
> 浏览、下载**本网站的内容。未经…书面许可，任何机构或者个人**不得以向他人
> 出售牟利为目的**，使用本网站的任何内容，此种使用包括但不限于拷贝、下载、
> 存贮、通过硬拷贝或电子抓取系统获取、发送…

Any organisation or individual may browse and download the content for
**non-commercial purposes**. The prohibition that follows attaches to a
**purpose** — use *for the purpose of selling to others for profit* — and
`电子抓取系统`, "electronic scraping systems", appears inside the list of
manners of that prohibited use.

The operative question is not what `此种使用包括但不限于` modifies. It is
what `不得` governs.

**Sentence 2 has exactly one modal.** `不得` is followed by
`以向他人出售牟利为目的，使用本网站的任何内容`. `以…为目的` is adverbial —
it cannot stand as a predicate and requires a following verb phrase, and
the only one available is `使用`. The purpose clause is therefore
incorporated into the VP that `不得` governs. There is no reading on which
`不得` reaches a bare, unrestricted `使用`, because that would leave
`以向他人出售牟利为目的` with nothing to attach to.

**The list clause carries no modal.** `此种使用包括但不限于…` is a copular
gloss — X *includes* Y — with no `不得` and no `禁止`. A subordinate
explanatory clause cannot enlarge the scope of a prohibition stated in the
matrix clause. So it makes no difference whether `此种使用` is read
restrictively ("use of that kind") or resumptively ("the use just
mentioned"): either way the list enumerates modalities of *using*, and the
prohibition on using still carries its purpose restriction. That is why
this is clear rather than merely dense — the ambiguity in `此种` does not
change the outcome under either reading.

For the other result the drafter would have had to coordinate the verbs
directly under `不得`. CFFEX shows exactly that construction
(`均不得复制、转载或传播`), so it was available and was not used here.

**Sentence 1 confirms it.** `下载` appears on both sides — granted in
sentence 1, listed in sentence 2. Under a general-prohibition reading
sentence 2 revokes sentence 1 entirely and the grant is dead letter. Under
the purpose-scoped reading both do work.

One gap, stated: commercial use that is not resale-for-profit is addressed
by neither sentence. It does not reach this tool, which is non-commercial
personal use and sits inside sentence 1's grant. This is a reading of
grammar, not legal advice.

(SZSE dropped the verb `获取` when it copied SSE's template, leaving
`电子抓取系统` dangling in a list of verbs. SSE's is the better-drafted
text.)

**CNINFO publishes no use restriction at all.** Its 免责声明 is three
paragraphs of warranty and liability disclaimer — nothing about copying,
automation or redistribution. Its own footer links to
`webapi.cninfo.com.cn`, an official data service platform.

**robots.txt**: absent at SSE, CNINFO and CFFEX (404). SZSE serves an
empty one — HTTP 200, zero bytes, `Last-Modified` recent, so maintained
and deliberately empty. None restricts anything, and all four are
readable, unlike NDRC's 403.

### CFFEX is out

Its two notices conflict. 版权声明 says *"任何**非私人**使用、转载和传播和
商业用途必须获得…书面授权"* — any **non-private** use requires written
authorisation, implying private use does not. But 法律声明 clause 一 says
flatly *"若未获得…书面同意，…上的所有信息、内容等，**均不得复制**、转载或
传播"* — no copying without written consent, with no carve-out. Storing to
a database is `复制`.

Where terms conflict, the prohibition controls. Same call as PBoC, ASX and
HKEX.

**And its positioning data is not a COT equivalent.** 成交持仓排名 ranks
the top 20 **member firms** by volume and by long/short open interest —
`国泰君安(代客)`, "on behalf of clients". It says which brokerage holds
positions, not whose money it is. COT's entire value is the
commercial/non-commercial split, and this has no such axis. Not built, and
not to be called one.

## Where the sources are, and switching them off

Every source ships with `enabled: true`. Set it to `false` in
`sources.yaml` to stop polling one. Its stored rows stay, its
configuration stays, and `status` marks it `[DISABLED]` instead of
reporting it stale — a disabled source cannot be stale, because nothing
is polling it.

Two of the fourteen are domestic to mainland China and served from
inside it:

| domestic to China | international |
|---|---|
| `cfets_ccpr` (chinamoney.com.cn) | RBA ×2 (AU) |
| `nbs_releases`, `nbs_interpretation` (stats.gov.cn) | Fed ×2, SEC, CFTC (US) |
| `cninfo_announcements` (cninfo.com.cn) | ECB ×2 (EU), BoE (UK) |
| `sse_southbound` (query.sse.com.cn) | BoJ (JP), HKMA (HK) |

That is five source blocks on four Chinese hosts, and eleven abroad.
Enabling only those four gives a working tool: the CNY fix, the rail
panel that draws from it, NBS statistical releases, company
announcements for any mainland code on the watchlist, and the tape. What
you lose is everything priced off the other eleven — the ECB and RBA
reference-rate panels, the CFTC positioning panel, and SEC company
filings.

The tool does not detect where you are and does not change its behaviour
based on it. It reports what happened: a source whose entire failure
streak is timeouts or connections that never landed is shown as
**network unreachable — may be connectivity rather than the source**,
never as failing, because nothing came back to judge the feed by. If two
or more sources are in that state at once the rail says so above the
per-source rows, since most of them unreachable points at the connection
rather than at fourteen publishers.

If a link is slow rather than blocked, raise `timeout_seconds` —
globally under `defaults:`, or under one source's `config:`. It is not a
workaround, it is the setting for the situation.

There is no mirror, no proxy setting and no fallback host in this tool,
and none is planned.

## Southbound Stock Connect: why history starts in 2024

港股通成交概况 — mainland money trading Hong Kong-listed shares — comes
from SSE's own site under SSE's terms. HKEX publishes the same class of
figure and prohibits scripted access; SSE does not.

The endpoint was traced from the page's own module, not guessed. The daily
tab of `/services/hkexsc/ggtscsj/ggtcjgk/` loads
`search_southboundStock_2021.js`, which calls **`ggt/getQuatationInfo.do`
with `tradeDate` and no `sqlId` at all**. The monthly and yearly tabs use
`commonQuery.do` with `COMMON_SSE_JYFW_HGT_GGTCJXX_*`; northbound uses
`commonSoaQuery.do` with `FW_HGTZL_*`. Three endpoints, none
interchangeable. `jsonCallBack` is a JSONP wrapper the page needs and we
do not — omitting it returns bare JSON.

### The boundary, and why it is enforced in code

The page carries one line, and it is the only place this is recorded
anywhere — it is **not in the payload**:

> 注：2024年8月19日起，本页面港股通成交金额单位为港元。

Verified either side of it:

| | BUY_AMOUNT | TOTAL_AMOUNT | TOTAL_VOLUME | ETF_TOTAL_AMOUNT |
|---|---|---|---|---|
| 2024-08-16 | 84.32 | **null** | **null** | **null** |
| 2024-08-19 | 73.59 | 182.82 | 38.41 | 48.05 |

It is not only the currency: three fields do not exist before the change.
`BUY_AMOUNT` is CNY on the 16th and HKD on the 19th — same field, same
series, nothing marking the switch.

So `sse_southbound.parse()` **raises on any row dated before
2024-08-19**. A prose note somebody has to remember is not a boundary; a
series that silently changes denomination is worse than a short one.
Pulling earlier data is therefore a deliberate decision to handle a
currency change, not something to discover from a chart that looks wrong.

The monthly aggregates are **not** a substitute for the missing years.
Their `DAY_*` fields are averages, and averages spliced onto a daily
series would look continuous and mean something different.

### Northbound is deliberately absent

`commonSoaQuery.do` returns `totalVolume`, `totalAmount`, `etfTotalAmount`
and `tradeDate` — **no buy/sell split**, so no net can be computed from
it. Net is the whole signal. Turnover alone did not justify a second
endpoint, a second sqlId family, a second date format and a second failure
mode.

### Four replies that look like success

**`result: [None]`** — a list of length one holding null — for a
non-trading day *and* for `tradeDate=99999999`. Truthy, so `if not result`
passes. The guard is `result[0] is not None`.

**`quatationInfo: "success"` is not a signal.** It reads `"success"` for
`tradeDate=99999999`. Nothing reads it, and a test asserts nothing ever
does — it is named in the parser only so a later reader does not wire it
in thinking it means something.

**Numbers are display-formatted strings** with thousands separators
(`"1,258.47"`). Separators are stripped by name and anything still
unparseable raises. A bare `try/except` would swallow a corrupted digit as
readily as a comma.

**Three null conventions on one host**: `null` (a field absent for that
date), `"-"` (the monthly endpoint's marker), `[None]` (no data). Each is
handled by name.

Worth knowing even though only the first is needed here: a bad `sqlId` on
`commonQuery.do` returns a silent empty envelope (`result: null`,
`total: 0`, no error field), while `commonSoaQuery.do` returns HTTP 200
with `Content-Type: application/json` and a body that is not JSON —
`\n({"success":"false",…})`, a parenthesised JSONP wrapper with no
callback name. Two failure modes on one host.

### Units are not in the payload

The scale appears only in rendered column headers, so it is stored on
every observation rather than implied:

| field | header | stored unit |
|---|---|---|
| `BUY_AMOUNT` | 当日买入成交金额（亿元） | `100 million HKD` |
| `BUY_VOLUME` | 当日买入成交笔数（万笔） | `10,000 trades` |

**`*_VOLUME` is a count of TRADES, not shares.** The field name says
volume; the column header says 笔数. Storing it as a share count would be
a silent factual error that the field name actively invites.

Values are stored as published — `647.70`, not `64_770_000_000`.

### Net

Derived as buy − sell and stored beside the two published figures it comes
from, so the arithmetic stays checkable — the same treatment `cftc_cot`
gets. It is only emitted when both sides were published: a net against a
missing half is a number about our own gaps. Positive means mainland money
into Hong Kong-listed shares.

### Backfill

One request per date — the endpoint answers a single `tradeDate` and
offers no range. 525 weekdays from the boundary to August 2026, paced at
3s, resumable: every date is logged to `fetch_log` and a resumed run skips
what it already asked.

Public holidays are **not** skipped. This tool has no CN/HK holiday
calendar, and the endpoint answers a holiday exactly as it answers a
weekend. Asking and being told nothing was published is honest; a guessed
calendar would eventually skip a day that traded.

Of the 525 weekdays asked, 472 carried a published figure and 53 did not —
the union of the mainland and Hong Kong holiday calendars, since Connect
closes when either market is shut.

### Retrying, and what is not retried

A twenty-minute paced run against a public server over a link nobody
controls will meet a dropped connection eventually. That is an expected
event, so `backfill.download()` retries **three attempts with a growing
backoff** — and only on `db.PATH_KINDS`, the same `network`/`timeout` pair
the health panel uses to say "unreachable" rather than "failing". One
definition, so the two cannot drift.

An `http_404`, a `decode` failure or a `parse` error is a statement about
the source or the payload and will be exactly as wrong on the third attempt
as on the first. Retrying those would turn a clear failure into a slow one,
so they re-raise immediately.

The ordinary `fetch` cycle deliberately does **not** retry. A poll that
fails is retried by the next cycle an hour later, and a retry loop there
would only turn one polite request into three.

When the attempts are spent the run stops with everything before that point
already committed, and prints the remedy rather than a stack trace:

    sse_southbound: network unreachable at 2025-09-02. 250 date(s) remaining.
      Everything fetched before that point is stored; resuming re-asks only
      what is left.
      Resume with:  python -m macrowire backfill --source sse_southbound
      Full traceback: re-run with --debug, or see fetch_log.

The traceback is not gone. `--debug` re-raises, and the failure is written
to `fetch_log` with its `error_kind` and the date it reached either way.

## CNINFO: three replies that look like success

Every one of these was measured against the live API, and each has a guard
in `macrowire/parsers/cninfo.py`.

**`column` cannot tell the exchanges apart.** Querying 2026-08-20 with
`column=sse` and with `column=szse` returned **byte-identical** responses —
23,554 bytes, every `secCode` beginning with `3`, which is Shenzhen
ChiNext. It is not inert: per ticker, `sse` and `szse` both give 1,284
while omitting it gives 1,684. So it filters something, just not the venue.
Nothing here derives a market from it — the exchange comes from the listing
code's own prefix, and a prefix the table does not know raises rather than
being filed under a guess.

The guard that matters needs no knowledge of the request: **a per-ticker
page must describe exactly one company.** When `stock` is not honoured the
reply is the firehose, and one page carrying several codes is that,
whatever the status said. Stating it that way means a re-parse of stored
bytes months later checks the same thing. The fetcher additionally compares
the actual request against the actual response, because there and only
there are both halves in hand.

**`pageSize` is capped at 30, silently.** Asking for 50 or 200 returns 30,
with no error and no field saying so. A loop advancing `pageNum` by one and
assuming it received `pageSize` rows skips everything past the thirtieth.
The fetcher asks for exactly the cap — leaving nothing to clamp — reads
`len(rows)`, follows `hasMore`, and raises if a page ever exceeds the cap,
because at that point the paging arithmetic is unproven.

**A miss is a well-formed empty envelope.** An unknown code gives HTTP 200
and `[]`. A query matching nothing gives HTTP 200 with `announcements:
null`, `totalRecordNum: 0`, and no error field anywhere in the object.
There is nothing in the status to check, so every check is on structure: a
missing key raises, a `null` list is an empty window, a bare list is the
no-such-code reply.

### The orgId is looked up, never constructed

CNINFO addresses a company by both its code and an opaque `orgId`, and the
announcement query needs the pair. Most follow a pattern — `600519` is
`gssh0600519`, `000001` is `gssz0000001` — and CATL, `300750`, is
`GD165627`. Building it would work for most tickers and silently return
nothing for the rest, which reads exactly like a quiet company. So it is
resolved once at add time against CNINFO's own listing index, which is the
same call that validates the code, and cached in
`data/cninfo_orgids.json`.

### Timestamps are date-only in practice

`announcementTime` is epoch milliseconds, and for most rows it is midnight
Beijing: 30 of 30 sampled for `600519` and `000001`, 17 of 30 for
`300750`. Not a recency effect — everything CATL filed on 2026-07-25 is
midnight-stamped while its 2026-08-12 filings carry 19:22. Batch
submissions appear to lose the time of day.

A lost time is indistinguishable from a real 00:00, so the source is
`timing.class: date_only` and gets no position on the ribbon. The value is
stored exactly as published either way. HKMA's feed already stores midnight
HKT the same way, and this needed no new machinery.

### Encoding: a host is not consistent with itself

The query API declares UTF-8 and honours it. CNINFO's **own 404 page is
GB2312**, and CFFEX serves its error page with **HTTP 200**. So a decode
failure here is treated as a likely error page arriving as a success, and
said so in the message rather than being retried as if the feed were fine.

### Volume, and why the watchlist is not optional

2,492 announcements on 2026-08-20 across both exchanges — SZSE's own API
reports 1,235 for its half. That is roughly three and a half times the
whole rest of the tape per day. `cninfo_announcements` polls per
watchlisted code and nothing else; with no CN codes on the watchlist it
returns nothing, which is a skip and not an error.

Add codes as six digits:

    python -m macrowire watchlist add 600519 --market CN

Letters are refused before a request is spent, and an unmatched or delisted
code fails at add time.

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

## The defaults are opinions

Several settings in `sources.yaml` are one reader's judgement, not neutral
configuration, and are marked as such in the file itself:

| setting | what it encodes |
|---|---|
| `importance` | reading priority — drives type scale in the tape |
| `staleness_days` | which feeds are expected to be daily |
| `skip_forms` | SEC forms not worth reading (Form 4 + 144 were ~88% of volume) |
| `currencies` | which ECB pairs are stored — 8 of the 29 published |
| **the source list** | **six of thirteen sources are AU, CN or HK** |

The last is the largest and least visible: it is not a setting you can
change, it is a point of view about which economies matter. Adding a source
is a YAML block; removing one is a deletion.

## Contacts are required, and fail at config load

Two environment variables are required and validated when config loads, not
when a request is about to go out:

```
SEC_CONTACT is not set, and sources.yaml needs it.
  The SEC requires a User-Agent of the form 'Name email' and ENFORCES it -
  anything else is answered with HTTP 403. Note the space: a bare email
  is not enough.
    SEC_CONTACT=Jane Doe jane@example.com
  Set it in /path/to/.env (copy .env.example to start).
```

`MACROWIRE_PROJECT_URL` is optional and defaults to the canonical repo —
set it if you fork, so your traffic identifies your project. `sources.yaml`
supports `${VAR:-default}` for exactly this.

## Git hook

`git-hooks/commit-msg` catches two things that do not show up in a diff:
a commit landing under whatever identity the shell happened to have, and a
tool appending `Co-authored-by` or `Generated with` to the message.

By default it checks only that author and committer are **set, non-empty
and identical** — enough to catch a misconfigured shell without naming
anybody, so it works for every contributor rather than one.

To pin a specific identity on a single-author repo:

```bash
git config macrowire.authorName  "Your Name"
git config macrowire.authorEmail "you@example.com"
```

Both must be set for the pin to apply.

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

## Adding a language

Discovery is a directory listing, not a list in code. Drop a JSON file
into `macrowire/locales/` and it exists:

```bash
cp macrowire/locales/en.json macrowire/locales/de.json
# translate the VALUES
$EDITOR macrowire/locales/de.json
# then in sources.yaml:  locale: de
python -m macrowire locales
```

`macrowire locales` lists what is installed and how complete each one is:

```
interface languages in macrowire/locales
  de     Deutsch         192/321 (60%)
  en     English         321/321 (100%)  <- source of truth  <- active
  zh-CN  简体中文          321/321 (100%)

  de is missing 129 key(s):
    app.aria_ribbon
    ...
```

**A partial locale is usable.** Missing keys fall back to `en`
individually, so 60% complete renders 60% translated and 40% English —
never blank, never a raw key, and every miss logged once. There is no
threshold to clear before a file is worth shipping.

`en.json` is the source of truth. A key present in another locale but
absent from `en` is reported as *orphaned*: it cannot fall back, so it
renders in one language only. Usually a typo or a string removed from
`en`.

### Translate values. Never keys, and never a publication time.

Some strings describe the **viewer** and some describe the **source**,
and only the first kind is in the catalogue at all:

| | |
|---|---|
| **Viewer-facing** — translate | "Source health", "clear all", "not polled yet" |
| **Source fact** — NOT in the catalogue | `4pm AEST`, `09:15 CST`, `~16:00 CET`, `Fri 15:30 ET` |

The RBA fixes at 4pm Sydney time whether it is read in Sydney or
Stuttgart. Those six facts live in a `FACT` constant in `app.js`, outside
the catalogue, and are interpolated into translated templates — so
`rail.rba_asof` is `"{time} · {period}"` and never `"4pm AEST · {period}"`.
Translating the label is correct; changing the fixing time would be a lie.
**Do not add them to a locale file**, and a test fails if they appear in
one.

Item titles and summaries are never translated either. A translation is
an interpretation, and storing one as the record loses the original.

Nothing here is machine-translated. A wrong word in a financial interface
reads as sloppiness, and two good locales beat six approximate ones.

### The standard is immediate comprehension, not dictionary accuracy

**A locale file is judged on whether a native reader understands it at a
glance — not on whether each word is a correct dictionary match.** Those
are different tests, and a translation can pass the second while failing
the first.

Four ways it happens, all found in this project's own zh-CN:

- **Technical register the English did not have.** `polled` became 轮询,
  the correct term for polling and engineering jargon a trader would never
  use. It is 已检查 now. The English is plain, so the Chinese must be.
- **A word that argues with its own explanation.** `stale` became 陈旧,
  which carries "obsolete, decayed" — while the explanation beneath it
  says the source is *usually just quiet*. 长期无更新 states the fact
  without the verdict.
- **English structure carried across.** 此来源从未被抓取过 is an English
  passive wearing Chinese characters; 这个来源还没有抓取过 is how the
  sentence is actually said.
- **Segmentation.** Chinese has no spaces, so two characters meeting at a
  clause boundary can read as a different word entirely:
  同一**行为**「或」 parses as 行为 (*behaviour*) before it parses as
  行 + 为. 行内取或，行间取且 cannot be misread.

**And the converse: some terms must stay technical.** 中间价, 成交,
净额, 非商业净持仓, 港股通 are what the instruments are actually called.
Simplifying them would be wrong, not friendlier — a trader knows them and
a plain-language substitute would be less precise, not more readable.

The test for every string is the same: *would someone who reads this
language natively, and trades, understand it without stopping?*

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

### A test that cannot fail is worse than no test

It reads as coverage. Three have shipped here, all the same shape — the
test's **reach** was narrower than its **claim**. The rules are in the
suite's module docstring; the short version:

- **A loop over a derived collection needs a floor.** `for x in
  computed(): assertX(...)` passes when `computed()` returns nothing.
  Use `floor(self, collection, "what", least=N)`. Inline literals need
  no floor.
- **A test seeded from an empty fixture asserts nothing.** Seed it, then
  floor the loop.
- **Scan by property, not by character range.** A slice stops covering
  whatever moves past its end marker, and `assertNotIn` inside a slice is
  vacuous where `assertIn` would fail loudly.

### Mutation testing, and clearing the bytecode

The way to know a guard bites is to break what it guards and watch it
fail. Afterwards, always:

```bash
find . -name __pycache__ -type d -not -path "./.git/*" -exec rm -rf {} +
```

Python invalidates a `.pyc` on `(mtime, size)`. A mutation of the **same
byte length**, applied and reverted inside one second, changes neither —
so the interpreter keeps running bytecode compiled from the mutated
source while the file on disk reads correctly. `"*.json"` → `"*.nope"` →
restored did exactly that here, and five unrelated tests failed against
an already-correct file.

### One file, one lifetime

Every loader in `config.py` re-reads on call. **Do not bind one at import
in a long-running process while something else reads the same file per
request.** That gives one file two freshnesses, and it has bitten this
project twice.

The second time, `app.py` bound `LOCALE = load_locale()` at import while
`_sources()` read `sources.yaml` per request. Disabling a source was live;
changing `defaults.locale` was not. And because `StaticFiles` serves
`app.js` from disk every request, new JavaScript asked for strings an old
in-memory catalogue did not have — which renders as a raw key and looks
exactly like a string nobody ever wrote.

A short-lived CLI process is exempt: it reads and exits, so there is no
window for the file to change underneath it.

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

### status is derived from the data, not the log

`fetch_log` is not a reliable witness to a source's health, and trusting it
produced **three separate false alarms on healthy sources**. Each field now
answers from whichever place can actually answer it.

**A false alarm in a fail-loudly system is worse than no alarm** — it
teaches you to ignore the real ones. That is the whole reason this was
worth unpicking.

#### Two kinds of skip, and only one is evidence

| status | meaning |
|---|---|
| `ok` | contacted, parsed, stored (possibly nothing new) |
| `no_change` | **contacted**, and the source said nothing is new. A successful poll. |
| `throttled` | **not contacted** — the rate limiter blocked the attempt |
| `error` | contacted, and it failed |

Both used to be `skipped`. CFETS's publication gate reads `ccpr.json`,
finds the fix already stored and stops — which is its *normal* outcome, so
it logged no `ok` row and reported **"last successful fetch: never"**
permanently while holding 8,050 observations.

`no_change` also now counts against the rate limiter, because it made a
real request. It previously did not, so a gated source was re-probed on
every cycle regardless of its configured interval.

#### What each field is measured from

| field | source of truth | why |
|---|---|---|
| last contact | `fetch_log` (`ok` or `no_change`) | only the log knows if we reached out |
| last stored new | `MAX(fetched_at)` over **items and observations** | a deduped row is never re-inserted, so this is exactly when we last wrote something |
| newest content | `MAX(published_at)` / `MAX(observed_at)` | what the SOURCE published |
| **staleness** | newest content | "how long since this source published" is a question about the source |
| consecutive failures | errors since the last non-error row, **by id** | |

Reading "last new item" off `items` alone reported **"never"** for every
observation-storing source — a query looking in the wrong table, not a
fault. Reading staleness off `fetch_log` made a source storing rows *every
single day* report **STALE**, because every poll after the first deduped
and logged `new_item_count = 0`.

Failure streaks are counted by row **id**, not timestamp: `utc_now()` has
one-second resolution, and a recovery logged in the same second as the
failure it recovered from would otherwise be invisible. Same tiebreak the
revision chains needed, for the same reason.

#### "Data current, log incomplete"

Where a source holds data newer than its newest logged contact, `status`
says so explicitly instead of implying failure:

```
cfets_ccpr
  last contact          : never    (no contact logged)
                          DATA CURRENT, LOG INCOMPLETE — stored 5h 38m ago,
                          newer than any logged contact. A restore from a
                          backup predating that fetch is the usual cause.
```

An error that a later contact superseded is labelled `RESOLVED` rather
than printed as though it were live.

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

### Never warn unconditionally

Four separate alarms in this project fired regardless of whether the thing
they warned about had been handled. It is a rule now, not four fixes:

> **Measure the actual state. If the user has solved the problem, confirm
> it — do not keep warning.**

A panel that nags at a solved problem is one you stop reading, and then it
cannot warn you about a real one. The disk-dies panel branches on measured
state:

| measured | shown |
|---|---|
| `export.path` outside the project, file current | *"Exporting to /path — 43 rows protected 12m ago. Off this disk, so a drive failure costs nothing."* |
| outside the project, file stale | *"…but the file is out of date — run fetch or export"* |
| default local path | *"…same disk as the database, protects against a mistake but not a drive failure. Set export.path…"* |
| never exported | *"not exported yet — run macrowire export, or set export.path…"* |
| config invalid | the specific error |

### Health states say what they mean

Every state a source can be in carries plain-language meaning and, where
there is one, an action — inline in `status`, on hover in the rail:

| state | means | do |
|---|---|---|
| **not polled yet** | never fetched. Nothing is wrong. | run `macrowire fetch` |
| **polled** | last poll succeeded and stored what it found | — |
| **polled, nothing new** | contacted; source reported nothing new. Normal for a daily source. | — |
| **waiting on interval** | rate limiter blocked this cycle before any request | — |
| **data current, log incomplete** | holds data newer than any logged fetch — usually a restore. Data is fine. | harmless; next fetch clears it |
| **failing** | consecutive failures since the last good cycle | check the error kind |
| **stale** | polling works, source has published nothing for longer than its threshold | usually the source being quiet |

`"no contact logged"` was truthful and read like an error. It was a new
source nobody had fetched yet.

### Where your data lives

`fetch` prints this once, on a database that has collected nothing:

```
Your data lives in /home/you/Documents/Projects/macrowire/data
That directory is gitignored: cloning this repo gives you the code,
not the data. Every install builds its own history by polling.

Most of what accumulates is re-fetchable - SEC, NBS, CFETS, ECB and
the news feeds all serve their recent history on request, so losing
the database costs polling time rather than data.
The exception is rba_media_releases, rba_exchange_rates: those feeds
carry one item and no archive, so what you collect is the only copy
in existence.
Set export.path in sources.yaml to a synced folder and your
irreplaceable rows are backed up automatically.
```

Proportionate on purpose. *"Back everything up"* is advice people ignore,
because most of this can be re-fetched by waiting. Naming the small part
that genuinely cannot is what makes the paragraph worth reading.

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

**The tool writes a file to a path. Where that path goes is your
arrangement.** It does not commit, push, sync, or handle a credential.

Set `export.path` in `sources.yaml` to a folder that is already synced or
backed up — Dropbox, Nextcloud, an external drive — and the irreplaceable
rows are protected automatically with no further action:

```yaml
defaults:
  export:
    path: /home/you/Dropbox/macrowire
    auto: true
```

The path must be **absolute**, and is checked for existence and
writability **at config load** — not silently at export time, three weeks
later, at the moment it mattered.

`fetch` re-exports automatically after any cycle that stores new
irreplaceable rows. Unchanged data produces a byte-identical file and
nothing is rewritten.

Left unset, the export goes to `<repo>/export`, which is the same disk as
the database — that protects against a mistake but not a drive failure,
and the rail says so rather than pretending otherwise.

#### Version control is one option, not the instruction

If you keep this in git, committing the export is a perfectly good way to
get it off the disk, and `<repo>/export` is not gitignored so that it can
be. It is **one** arrangement among several — a synced folder, an external
drive, a backup job — and the tool does not assume it. Nothing in the
interface mentions git, and the CLI only mentions it parenthetically when
a repository is actually present.

The project never commits anything itself. That is deliberate and will not
change: a background process making commits in your name, potentially
signed, is not a habit worth acquiring.

#### It refuses to shrink

The export path is global config while the database path can be overridden
with `MACROWIRE_DB`. A second instance or a test run pointed at a scratch
database would otherwise overwrite the only off-disk copy with a nearly
empty file — which happened once during development, from a database in
`/tmp`. Writing an export with **fewer** irreplaceable rows than the file
already holds now raises, and needs `--force` to proceed.

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
| `timeout` | the request was not answered in `timeout_seconds` — a slow or congested path, not a statement about the feed |
| `network` | DNS failure, refused connection, TLS abort — says nothing about whether the source still exists |
| `http_404`, `http_410` | the shape of a feed that has been withdrawn |
| `http_5xx`, `http_429` | the shape of a server having a bad day |
| `decode` / `parse` | the payload arrived and was wrong |
| `empty` | zero entries from a source that has parsed before |
| `transport` | the body failed content-decoding |

`status` shows consecutive failures since the last success with a
breakdown by kind. One `network` failure is a blip; twelve `http_404`s
in a row is a source that has gone.

`timeout` and `network` are the two that describe the **path** rather
than the source: the request never got an answer, so nothing has been
learned about the feed. When a source's entire failure streak is made of
those two, both `status` and the health panel report **network
unreachable — may be connectivity rather than the source** at `warn`,
not `failing` at `bad`. Getting this wrong is the failure this project
keeps guarding against — a panel that calls a working feed broken is one
you stop reading, and then it cannot tell you when a feed really has
gone.

The split is one list, `db.PATH_KINDS`, so "which kinds mean unreachable"
has exactly one definition. Four separate false alarms came from that
kind of fact being written down in more than one place.

## Licence

**Code: `AGPL-3.0-or-later`.** Full text in [LICENSE](LICENSE).

### In plain language

- **Free to use, read, modify and self-host.** Run it on your own machine
  for whatever you like.
- **If you modify it and run it as a network service that other people
  use**, the AGPL asks you to offer those users the source of your
  modified version. That is the clause that distinguishes the AGPL from
  the ordinary GPL, and it is the reason for choosing it here: this is a
  thing you run as a server, so "distribution" would otherwise never be
  triggered and improvements could disappear into a hosted fork.
- **Running an unmodified copy privately triggers nothing.** Neither does
  modifying it for yourself and not letting anyone else use it. The
  obligation attaches to *modified* code serving *other people over a
  network*.
- **Derived works inherit the licence.** That is the trade.

*This is a plain-language summary for orientation and it is not legal
advice. Where it and [LICENSE](LICENSE) disagree, the licence text is what
holds — and if the answer matters to you commercially, ask someone
qualified rather than reading a README.*

### The licence covers the code. It does not cover the data.

This is the distinction most likely to be got wrong, so it is stated
flatly:

| | what it is | who governs it |
|---|---|---|
| **The code** | everything in this repository — parsers, schema, interface, tests | AGPL-3.0-or-later, this project |
| **The collected data** | what lands in `data/macrowire.db` and `export/` — RBA releases, NBS statistics, SEC filings, CFETS fixes, CNINFO announcements, SSE Connect turnover | **each publisher, under their own terms.** Not this project's to license. |

Cloning this repository gives you the code and **no data at all** — the
database is gitignored and every install builds its own history by
polling. So the question rarely arises by accident. It arises when someone
exports rows and passes them on.

Nothing in the AGPL grants you any right to redistribute what the tool
fetched. Some of it is straightforward: **SEC EDGAR is US federal work and
public domain.** Some of it is not, and each publisher's terms are
recorded in [The licensing position, stated plainly](#the-licensing-position-stated-plainly)
and [Chinese exchanges: what the terms actually say](#chinese-exchanges-what-the-terms-actually-say),
including the three sources that are deliberately **not polled** because
their terms prohibit it — PBoC, ASX and HKEX.

Two practical consequences:

- **Publishing a fork is a code question.** Comply with the AGPL and you
  are done.
- **Publishing collected rows is a data question, and a different one.**
  The AGPL says nothing about it. Check the publisher.

The `export` command exists to move irreplaceable rows off one disk, not
to prepare them for redistribution — and it writes a file to a path,
which is where its job ends.
