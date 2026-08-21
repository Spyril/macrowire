"""Failure taxonomy.

Everything here is fatal by design. The brief's rule: fail loudly on
transport, parse and structure problems; never fail on an item simply
being old.
"""


class MacroWireError(Exception):
    """Base for every condition that should stop a fetch cycle.

    `kind` is recorded in fetch_log.error_kind so a transient network blip
    and a source that has genuinely gone are distinguishable after the
    fact. Without it both are just a row with an error string, and a DNS
    hiccup reads identically to a feed that has been withdrawn.
    """

    kind = "unknown"

    def __init__(self, message: str, kind: str | None = None):
        super().__init__(message)
        if kind:
            self.kind = kind


class ConfigError(MacroWireError):
    """sources.yaml is missing, malformed, or references an unset ${VAR}."""

    kind = "config"


class FetchError(MacroWireError):
    """Transport failure: non-2xx, timeout, connection reset."""

    kind = "network"


class DecodeError(MacroWireError):
    """The payload did not decode cleanly under any credible encoding.

    Covers a wrong or absent charset declaration and any text that came
    back carrying U+FFFD. Storing mojibake is worse than storing nothing:
    the original bytes cannot always be re-fetched.
    """

    kind = "decode"


class ParseError(MacroWireError):
    """The payload arrived but is not a feed we can read."""

    kind = "parse"


class MalformedEntryError(ParseError):
    """A single entry is missing a field the parser requires."""

    kind = "parse"


class StaleContentError(ParseError):
    """A source returned well-formed content that is too old to be current.

    Deliberately NOT the same thing as the staleness reporting in
    `status`, and the two must not be conflated:

      * staleness REPORTING is information. It answers "how long since
        this source published anything new", never raises, and exists
        because a sporadic feed going quiet is normal.

      * freshness ASSERTION - this error - answers "is this response
        actually current". It is for sources that can serve a stale
        snapshot with an intact structure and a 200 status, where every
        shape check passes and the content is simply years out of date.

    Not wired to any source yet: every feed currently polled either
    carries dated entries we can already see, or is a structured API.
    """

    kind = "stale"


class EmptyFeedError(ParseError):
    """Zero entries from a source that has parsed successfully before.

    Not raised on a source's first ever fetch - an empty feed we have
    never seen populated is unproven, not broken.
    """

    kind = "empty"
