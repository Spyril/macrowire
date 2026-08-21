"""Shared feed plumbing, independent of any one feed dialect.

Everything here is about deciding whether a payload is a feed at all,
locating its entries, and turning its timestamps into one storable form.
Dialect-specific extraction lives in the sibling modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import feedparser

from ..encoding import REPLACEMENT, canonical
from ..errors import MalformedEntryError, ParseError

RSS1 = "http://purl.org/rss/1.0/"
ATOM = "http://www.w3.org/2005/Atom"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

# Matches the encoding attribute of an XML declaration, for rewriting.
_XML_DECL_ENCODING = re.compile(r'(?<=<\?xml)([^>]*?)encoding\s*=\s*["\'][A-Za-z0-9_.\-]+["\']', re.I)

# Root elements that can legitimately carry entries.
FEED_ROOTS = {
    f"{{{RDF}}}RDF",    # RSS 1.0 / RDF - the RBA feeds
    "rss",              # RSS 2.0 - every other central bank checked
    f"{{{ATOM}}}feed",  # Atom
}


@dataclass
class ParsedFeed:
    """What a parser hands back. A feed yields one kind or the other."""

    items: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)

    @property
    def entry_count(self) -> int:
        return len(self.items) + len(self.observations)


def find_entries(root: ElementTree.Element) -> list:
    """Locate entries across the three dialects.

    RSS 1.0 puts <item> at the top level as a sibling of <channel>;
    RSS 2.0 nests it inside <channel>; Atom uses <entry>.
    """
    for path in (f"{{{RSS1}}}item", "./channel/item", "./item", f"{{{ATOM}}}entry"):
        found = root.findall(path)
        if found:
            return found
    return []


def parse_document(source_name: str, body: str) -> tuple[ElementTree.Element, list]:
    """Validate a payload and return its root plus entry elements.

    feedparser is the gate - it decides whether this is a feed at all -
    and its entry count cross-checks ours, so a silent shape change shows
    up as an error rather than as missing items.
    """
    if not body.strip():
        raise ParseError(f"{source_name}: empty response body")

    # The caller already decoded strictly, so `body` is text and these
    # bytes are UTF-8 by construction. The declaration inside the document
    # still says whatever the server originally sent, though, so rewrite
    # it to match: leaving a stale `encoding="gb2312"` in front of UTF-8
    # bytes would make feedparser mis-sniff and mojibake the result.
    raw = _XML_DECL_ENCODING.sub(r'\1encoding="UTF-8"', body, count=1).encode("utf-8")
    parsed = feedparser.parse(raw)

    # Any bozo flag is fatal, not just one that yields zero entries.
    # A partially-read feed still returns entries - a truncated 4.5MB
    # NBS feed produced 313 of them, every title mojibake, because the
    # unclosed element at the cut point derailed feedparser's encoding
    # sniffer. Accepting bozo-with-entries is precisely how silent
    # corruption gets stored.
    if parsed.bozo:
        raise ParseError(
            f"{source_name}: feed did not parse cleanly "
            f"({len(parsed.entries)} entries recovered, but the document is "
            f"malformed): {parsed.get('bozo_exception')}"
        )
    if not parsed.get("version"):
        # An HTML maintenance page is well-formed XML with zero entries,
        # so a shape check alone would call it a clean empty feed.
        # feedparser reports no version for anything that is not actually
        # a feed; that is the tell.
        raise ParseError(
            f"{source_name}: payload is not a feed - feedparser detected no "
            f"feed version. Probably an error or maintenance page."
        )

    # feedparser sniffs an encoding of its own. When that disagrees with
    # what the document declares, one of them is wrong and the entries
    # cannot be trusted - the truncated NBS feed declared utf-8 and was
    # sniffed as iso-8859-2.
    sniffed = canonical(parsed.get("encoding"))
    if sniffed and sniffed != canonical("utf-8"):
        raise ParseError(
            f"{source_name}: encoding disagreement - these bytes are UTF-8 "
            f"but feedparser detected {sniffed}. That is what a truncated or "
            f"malformed document looks like; refusing to trust the entries."
        )

    if REPLACEMENT in body:
        raise ParseError(
            f"{source_name}: parsed text contains U+FFFD replacement "
            f"characters. Refusing to store mojibake."
        )

    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ParseError(f"{source_name}: payload is not well-formed XML: {exc}") from exc

    if root.tag not in FEED_ROOTS:
        raise ParseError(
            f"{source_name}: unexpected root element <{root.tag}>; "
            f"expected one of {sorted(FEED_ROOTS)}"
        )

    entries = find_entries(root)
    if len(entries) != len(parsed.entries):
        raise ParseError(
            f"{source_name}: entry count disagreement - feedparser saw "
            f"{len(parsed.entries)}, ElementTree saw {len(entries)}. "
            f"The feed shape has changed."
        )
    return root, entries


def text(element, path: str, namespaces: dict | None = None) -> str | None:
    found = element.find(path, namespaces) if namespaces else element.find(path)
    if found is None or found.text is None:
        return None
    stripped = found.text.strip()
    return stripped or None


def _normalise(moment: datetime, source_name: str, context: str, raw: str) -> str:
    if moment.tzinfo is None:
        raise MalformedEntryError(
            f"{source_name}: {context} has no timezone offset: {raw!r}. "
            f"Refusing to guess."
        )
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def iso_to_utc(value: str | None, source_name: str, context: str) -> str | None:
    """ISO 8601 / RFC 3339, as used by dc:date in the RSS-CB feeds."""
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise MalformedEntryError(
            f"{source_name}: {context} is not a parseable timestamp: {value!r}"
        ) from exc
    return _normalise(moment, source_name, context, value)


def rfc822_to_utc(
    value: str | None,
    source_name: str,
    context: str,
    assume_timezone: str | None = None,
) -> str | None:
    """Parse an RSS 2.0 <pubDate> and normalise it to UTC.

    Most feeds use RFC 822 ('Thu, 20 Aug 2026 20:00:00 GMT'). NBS instead
    emits a bare '2026-08-21 09:30:01' with no offset at all, so ISO is
    tried as a fallback.

    A timestamp with no offset is only accepted when the source has
    declared one via `timezone:` in sources.yaml. Without that it raises:
    a statistical release stamped nine hours wrong is worse than a
    missing one, and the feed gives us nothing to infer from.
    """
    if value is None:
        return None
    raw = value.strip()

    moment = None
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise MalformedEntryError(
                f"{source_name}: {context} is not a parseable date "
                f"(tried RFC 822 and ISO 8601): {raw!r}"
            ) from exc

    if moment.tzinfo is None and assume_timezone:
        try:
            offset = datetime.fromisoformat(f"2000-01-01T00:00:00{assume_timezone}").tzinfo
        except ValueError as exc:
            raise MalformedEntryError(
                f"{source_name}: configured timezone {assume_timezone!r} is not a "
                f"valid UTC offset such as '+08:00'"
            ) from exc
        moment = moment.replace(tzinfo=offset)

    return _normalise(moment, source_name, context, raw)
