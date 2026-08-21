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


def parse(source, body: str) -> ParsedFeed:
    _root, entries = parse_document(source.name, body)
    result = ParsedFeed()

    institution = source.config.get("institution")
    # Only set where a source declares it. Absent, a naive timestamp raises.
    assume_tz = source.config.get("timezone")

    for position, entry in enumerate(entries):
        title = text(entry, "title")
        link = text(entry, "link")

        # <guid> is the preferred identity, but it is optional in RSS 2.0
        # and HKMA emits it empty on every entry. The link is the only
        # field present across all six feeds.
        external_id = text(entry, "guid") or link

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
