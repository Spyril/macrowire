"""Plain RSS 2.0 news feeds -> rows in `items`.

This exists because RSS 2.0 shares nothing mechanical with the RSS-CB
feeds cb_news reads. Different root (<rss> with <channel><item>, not
rdf:RDF with sibling <item>), no namespace on the core elements, <guid>
instead of rdf:about, <description> instead of a namespaced summary, and
<pubDate> in RFC 822 rather than dc:date in ISO 8601 - the two date
formats need different parsers outright. Teaching cb_news to branch on
all of that would have made one module that understands two dialects
badly instead of two that each understand one well.

Verified against the Fed (3 feeds), ECB, Bank of England, Bank of Japan
and HKMA. All report feedparser version 'rss20', bozo=False, and none
carries the cb: namespace.

Field availability differs and the parser tolerates it:

  guid          BoE has opaque non-permalink guids; HKMA ships <guid/>
                empty on all 683 entries, so identity falls back to <link>
  description   absent on ECB, BoJ and HKMA; stored as NULL
  category      only the Fed emits it ('Monetary Policy', 'Speech', ...)

`announcement_type` is set only from the feed's own <category>. Feeds
that classify nothing leave it None, and the pipeline fills it in from
sources.yaml - either by URL rule or by a flat default - so no feed name
is ever compiled into this module.
"""

from __future__ import annotations

from ..errors import MalformedEntryError
from .base import ParsedFeed, parse_document, rfc822_to_utc, text


def _links_identify_entries(entries) -> bool:
    """Whether <link> tells this feed's entries apart.

    Asked of the WHOLE feed, once, rather than per entry. A per-entry rule
    would give an entry the bare link on a fetch where its link happened to
    be unique and a composite on the next one, and the id would move under
    a row that had not changed.
    """
    links = [text(e, "link") for e in entries if not text(e, "guid")]
    return len(links) == len(set(links))


def parse(source, body: str) -> ParsedFeed:
    _root, entries = parse_document(source.name, body)
    result = ParsedFeed()

    institution = source.config.get("institution")
    # Only set where a source declares it. Absent, a naive timestamp raises.
    assume_tz = source.config.get("timezone")

    # SOME FEEDS HAVE NEITHER A guid NOR A PER-ENTRY link. TreasuryDirect's
    # auction feeds carry no <guid> at all and only two distinct <link>
    # values across 22 entries - every announcement points at the same
    # press page - so `guid or link` gave all of them ONE external_id.
    #
    # Storage survives that, because content_hash mixes in title and
    # published_at, so 22 entries still store as 22 rows. What does not
    # survive is the truth: source_status counts revision chains with
    # GROUP BY external_id HAVING n > 1, and would have reported one chain
    # with 21 superseded versions. Nothing was superseded. The fix belongs
    # here, where the wrong id is minted, and not in the reader.
    #
    # ONLY WHERE THE LINK IS NOT ENOUGH. Every feed polled today either
    # carries a guid or has unique links - nbs_releases has no guid across
    # 500 entries and 501 distinct links - so all of them keep the exact
    # ids they already have in the database. Changing the derivation for
    # every no-guid feed would have changed 502 stored NBS ids and
    # re-inserted every one of them as a new row on the next fetch.
    composite = not _links_identify_entries(entries)

    for position, entry in enumerate(entries):
        title = text(entry, "title")
        link = text(entry, "link")

        # <guid> is the preferred identity, but it is optional in RSS 2.0
        # and HKMA emits it empty on every entry. The link is the only
        # field present across all six feeds.
        guid = text(entry, "guid")
        external_id = guid or link
        if not guid and composite and link:
            # The fields that actually identify the entry: where it points,
            # what it is called, and when it was published. Title alone
            # repeats - "Treasury announces 13-Week Bill" comes round every
            # week - and pubDate alone collides when two auctions are
            # announced in the same second, which this feed does. Together
            # they are unique, and they are all drawn from the entry, so a
            # re-fetch of an unchanged entry mints the identical id.
            external_id = "\n".join((link, title or "", text(entry, "pubDate") or ""))

        if not title:
            raise MalformedEntryError(
                f"{source.name}: entry {position} has no title (link={link!r})"
            )
        if not external_id:
            raise MalformedEntryError(
                f"{source.name}: entry {position} ({title!r}) has neither guid "
                f"nor link; nothing stable to identify it by"
            )

        published_at = rfc822_to_utc(
            text(entry, "pubDate"), source.name, f"entry {position} pubDate",
            assume_timezone=assume_tz,
        )

        result.items.append(
            {
                "external_id": external_id,
                "title": title,
                "url": link,
                "summary": text(entry, "description"),
                # Some feeds carry the whole article alongside the abstract.
                # NBS does; the central banks do not.
                "content": text(entry, "content") or text(entry, "{http://purl.org/rss/1.0/modules/content/}encoded"),
                "published_at": published_at,
                # Only what the feed itself declares. Configured
                # classification is applied afterwards by the pipeline.
                "announcement_type": text(entry, "category"),
                "type_primary": text(entry, "category"),
                "type_tags": None,
                "institution_abbrev": institution,
                "simple_title": None,
                "occurrence_date": None,
                # Not carried by central bank feeds. Present because
                # exchange announcement sources will populate them.
                "ticker": None,
                "is_price_sensitive": None,
            }
        )
    return result
