"""cb:news feeds -> rows in `items`.

Shape confirmed against the live RBA media releases feed:

    <item rdf:about="https://www.rba.gov.au/media-releases/2026/mr-26-21.html">
      <title>...</title>
      <link>...</link>
      <description>...</description>
      <dc:date>2026-08-19T19:00:00+10:00</dc:date>
      <cb:news rdf:parseType="Resource">
        <rdf:type rdf:resource=".../RSS-CB_1.2_RDF_Schema#Media-Releases"/>
        <cb:simpleTitle>...</cb:simpleTitle>
        <cb:occurrenceDate>2026-08-19T19:00:00+10:00</cb:occurrenceDate>
        <cb:institutionAbbrev>RBA</cb:institutionAbbrev>
      </cb:news>
    </item>

Note there is no cb:type element. The category the brief asked for is
the rdf:type resource URI nested inside cb:news; its fragment
(#Media-Releases) is the announcement category, and it is stored whole
in items.announcement_type so a future URI change stays visible.
"""

from __future__ import annotations

from ..errors import MalformedEntryError
from .base import ParsedFeed, parse_document
from .rdfcb import NS, RDF_ABOUT, cb_text as text, resource, to_utc


def parse(source, body: str) -> ParsedFeed:
    _root, entries = parse_document(source.name, body)
    result = ParsedFeed()

    for position, entry in enumerate(entries):
        external_id = entry.get(RDF_ABOUT) or text(entry, "rss:link")
        title = text(entry, "rss:title")
        if not title:
            raise MalformedEntryError(
                f"{source.name}: entry {position} has no title (rdf:about="
                f"{entry.get(RDF_ABOUT)!r})"
            )
        if not external_id:
            raise MalformedEntryError(
                f"{source.name}: entry {position} ({title!r}) has neither "
                f"rdf:about nor link; nothing stable to identify it by"
            )

        news = entry.find("cb:news", NS)
        announcement_type = institution = simple_title = occurrence = None
        if news is not None:
            announcement_type = resource(news, "rdf:type")
            institution = text(news, "cb:institutionAbbrev")
            simple_title = text(news, "cb:simpleTitle")
            occurrence = to_utc(
                text(news, "cb:occurrenceDate"),
                source.name,
                f"entry {position} cb:occurrenceDate",
            )

        published_at = to_utc(
            text(entry, "dc:date"), source.name, f"entry {position} dc:date"
        ) or occurrence

        result.items.append(
            {
                "external_id": external_id,
                "title": title,
                "url": text(entry, "rss:link"),
                "summary": text(entry, "rss:description"),
                "content": None,
                "published_at": published_at,
                "announcement_type": announcement_type,
                "institution_abbrev": institution or source.config.get("institution"),
                "simple_title": simple_title,
                "occurrence_date": occurrence,
                # Not carried by central bank feeds. Present because
                # exchange announcement sources will populate them.
                "ticker": None,
                "is_price_sensitive": None,
            }
        )
    return result
