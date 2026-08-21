"""Explicit decoding.

httpx does not raise on undecodable bytes - it substitutes U+FFFD and
hands back a string that looks fine until you read it. Nothing here
relies on that behaviour: the encoding is resolved from what the payload
itself declares, decoding is strict, and replacement characters are
treated as corruption rather than content.
"""

from __future__ import annotations

import codecs
import re

from .errors import DecodeError

# The XML declaration wins over the HTTP header: several government
# servers send a bare `Content-Type: text/html` with no charset at all,
# and a client default is a guess, not a detection.
_XML_DECL = re.compile(rb'<\?xml[^>]*?encoding\s*=\s*["\']([A-Za-z0-9_.\-]+)["\']')
_META_CHARSET = re.compile(rb'<meta[^>]+charset\s*=\s*["\']?\s*([A-Za-z0-9_.\-]+)', re.I)

REPLACEMENT = "�"


def canonical(name: str | None) -> str | None:
    """Normalise an encoding label, or None if Python does not know it."""
    if not name:
        return None
    try:
        return codecs.lookup(name.strip()).name
    except LookupError:
        return None


def declared_encoding(body: bytes) -> str | None:
    """The encoding the document declares about itself, if any."""
    for pattern in (_XML_DECL, _META_CHARSET):
        found = pattern.search(body[:4096])
        if found:
            return canonical(found.group(1).decode("ascii", "ignore"))
    return None


def charset_from_content_type(header: str | None) -> str | None:
    """Pull the charset out of a Content-Type header, if it declares one."""
    if not header:
        return None
    found = re.search(r"charset\s*=\s*([A-Za-z0-9_.\-]+)", header, re.I)
    return canonical(found.group(1)) if found else None


def decode(source_name: str, body: bytes, content_type: str | None = None) -> tuple[str, str]:
    """Decode strictly and return (text, encoding_used).

    Precedence: what the document declares, then the HTTP header, then
    UTF-8. Decoding uses errors='strict', so the wrong encoding raises
    here instead of quietly producing mojibake further down.
    """
    declared = declared_encoding(body)
    header_charset = charset_from_content_type(content_type)
    chosen = declared or header_charset or "utf-8"

    try:
        text = body.decode(chosen)
    except UnicodeDecodeError as exc:
        raise DecodeError(
            f"{source_name}: payload does not decode as {chosen} "
            f"(declared={declared!r}, header={header_charset!r}): {exc}"
        ) from exc

    if REPLACEMENT in text:
        count = text.count(REPLACEMENT)
        index = text.find(REPLACEMENT)
        raise DecodeError(
            f"{source_name}: decoded text contains {count} U+FFFD replacement "
            f"character(s) - first at offset {index}: "
            f"{text[max(0, index - 40):index + 40]!r}. Refusing to store mojibake."
        )

    return text, chosen
