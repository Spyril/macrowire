# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe what changed for someone *using* the tool. The commit log
is the record of how it was built; this is the record of what it does.

## [0.1.0] — 2026-09-05

First public release. The project had been running privately for some weeks
before this; everything below is what it was on the day the code was
published.

### Added

- **Nineteen sources across seven jurisdictions**, polled into a local
  SQLite database — central bank media releases and statistics, US Treasury
  auctions and buybacks, CFTC Commitments of Traders with history to 1986,
  SEC EDGAR company filings driven by a watchlist, CFETS CNY central parity,
  ECB and RBA reference rates, NBS releases and interpretations, CNINFO
  announcements, and SSE Southbound Stock Connect turnover.

- **Sources are configuration, not code.** Adding a feed of an
  already-supported shape means editing `sources.yaml` and nothing else.
  Ten parsers cover the shapes that exist today.

- **A single-page interface on localhost** — a session ribbon showing which
  markets are open, a tape of announcements with read state and filters
  across five axes, and a right rail carrying the reference series. Derived
  values are marked as derived; every panel states what it is showing and
  as of when.

- **Three locales** — English, Simplified Chinese, and Hong Kong
  Traditional Chinese, which is a separate translation rather than a
  character conversion. Publication times and other facts about a *source*
  are deliberately not translated.

- **A light theme alongside the default dark one**, both measured: every
  text colour clears 7:1 against every surface, and the suite fails if a
  palette edit breaks that.

- **Honest nulls throughout.** "No data yet", "nothing ever", "could not be
  computed" and "zero" are four different statements and are rendered as
  four different things. The database stores what a source published,
  never a derived number nobody published.

- **A documented licensing position.** Each publisher's terms are recorded,
  including eight sources investigated and deliberately not polled. Consent
  to poll ASX was requested and refused in writing; that exchange is
  summarised in `docs/licensing/` with ASX's own reference number.

- **598 tests**, including browser-driven ones that assert on rendered
  pixels and layout rather than only on markup.

[0.1.0]: https://github.com/Spyril/macrowire/releases/tag/v0.1.0
