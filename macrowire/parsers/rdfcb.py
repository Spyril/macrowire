"""RSS-CB 1.2 specifics - the Central Bank RSS extension.

Of the seven feeds verified so far, only the RBA's two use this. Every
other central bank checked publishes plain RSS 2.0 with no cb: namespace
at all.

These feeds are RDF/RSS 1.0, not RSS 2.0: entries are top-level <item>
siblings of <channel>, dates arrive as dc:date, and there is no pubDate.
The interesting payload sits in the cb: namespace, nested several levels
deep inside rdf:parseType="Resource" wrappers.

The cb: tree is walked with ElementTree rather than read off feedparser.
feedparser does flatten cb: elements into a single namespace-less dict,
but that flattening is lossy: it discards the nesting that tells
cb:observation/cb:value apart from any other cb:value, and collapses the
four sibling rdf:type elements in a statistics entry into one.
"""

from __future__ import annotations

from .base import ATOM, RDF, RSS1, ParsedFeed, iso_to_utc, parse_document, text

NS = {
    "rss": RSS1,
    "rdf": RDF,
    "dc": "http://purl.org/dc/elements/1.1/",
    "cb": "http://www.cbwiki.net/wiki/index.php/Specification_1.2/",
    "atom": ATOM,
}

RDF_ABOUT = f"{{{RDF}}}about"
RDF_RESOURCE = f"{{{RDF}}}resource"


def cb_text(element, path: str) -> str | None:
    return text(element, path, NS)


def resource(element, path: str) -> str | None:
    """The rdf:resource URI of a child element.

    This is how RSS-CB carries its machine-readable category: not a
    cb:type element, but an <rdf:type rdf:resource="...#Media-Releases"/>
    nested inside the cb:news or cb:statistics wrapper.
    """
    found = element.find(path, NS)
    if found is None:
        return None
    return found.get(RDF_RESOURCE)


def to_utc(value, source_name, context):
    return iso_to_utc(value, source_name, context)


__all__ = [
    "NS", "RDF_ABOUT", "RDF_RESOURCE", "ParsedFeed",
    "cb_text", "parse_document", "resource", "text", "to_utc",
]
