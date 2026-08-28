"""Failure taxonomy.

Everything here is fatal by design. The brief's rule: fail loudly on
transport, parse and structure problems; never fail on an item simply
being old.
"""


def _looks_like_a_key(text) -> bool:
    """A catalogue key, or a literal message?

    SYNTACTIC on purpose. The alternative - ask the catalogue whether it
    resolves - puts a file read and a JSON parse on the error path, and
    makes the meaning of a raise site depend on whether a key happens to
    exist yet. This cannot fail and cannot change under you: a key is
    `errors.` followed by dotted words, and no message prose looks like
    that. `test_every_error_key_resolves` closes the remaining gap by
    failing on a key that is well-formed but absent.
    """
    return (isinstance(text, str) and text.startswith("errors.")
            and not any(c.isspace() for c in text))


def render(key: str, fields: dict, locale: str | None = None) -> str:
    """An error message from its key and fields, in `locale`.

    ONE definition, used by the exception when it is raised and by the CLI
    when it reads a logged one back out of fetch_log. Those were two copies
    for about ten minutes and the second one had already lost the fields.

    Never LESS informative than the raw key. A missing catalogue entry
    makes Translator return the key itself - right for a label, where an
    ugly string beats an invisible one, wrong here, because it drops the
    fields and the fields are the whole diagnostic payload. Which source,
    which value, which date. `{source} carries no TRADE_DATE` reduced to
    `errors.sse.no_trade_date` says nothing anyone can act on.

    That matters while 186 raise sites move over one at a time, and it
    keeps mattering afterwards for a mistyped key.
    """
    from . import i18n
    text = i18n.Translator(locale or i18n.DEFAULT_LOCALE)(key, **fields)
    if text == key and fields:
        return f"{key} {fields}"
    return text


class MacroWireError(Exception):
    """Base for every condition that should stop a fetch cycle.

    `kind` is recorded in fetch_log.error_kind so a transient network blip
    and a source that has genuinely gone are distinguishable after the
    fact. Without it both are just a row with an error string, and a DNS
    hiccup reads identically to a feed that has been withdrawn.

    A KEY AND ITS FIELDS, NOT A FORMATTED STRING. These messages are
    persisted: fetch_log.error holds one and `status` reads it back out and
    prints it. Formatting at the raise site would have written whichever
    locale happened to be active when the fetch ran, so a row collected
    under zh-CN would render Chinese forever after switching back to en -
    a stored translation, which is the one thing this project does not do.
    So the raise site names a key, the row stores the key and its fields,
    and rendering happens where it is read.

    A plain string still works and renders verbatim. That is deliberate:
    186 raise sites move over a site at a time, and an unmigrated one has
    to keep behaving exactly as it did.
    """

    kind = "unknown"

    def __init__(self, message: str, kind: str | None = None, **fields):
        super().__init__(message)          # args[0] stays locale-independent
        self.key = message if _looks_like_a_key(message) else None
        self.literal = None if self.key else message
        self.fields = fields
        if kind:
            self.kind = kind

    def _render(self, locale: str | None) -> str:
        return render(self.key, self.fields, locale)

    def __str__(self) -> str:
        """The message in the reader's locale.

        Reads the locale from CONFIG, never from the preference store: an
        error path must not open a database.

        Two fallbacks, and both matter. config.py raises ConfigError while
        LOADING config, so resolving the locale can be the very thing that
        is broken - an error about a malformed sources.yaml must not
        recurse through the file it is complaining about. And if the
        catalogue itself will not load, a key with its fields beats an
        exception raised while reporting an exception.
        """
        if self.key is None:
            return self.literal
        try:
            from .config import load_locale
            try:
                locale = load_locale()
            except Exception:
                locale = None
            return self._render(locale)
        except Exception:
            return f"{self.key} {self.fields}" if self.fields else self.key

    def english(self) -> str:
        """What gets STORED. Locale-independent by construction, so the
        column stays a stable forensic record you can grep months later."""
        if self.key is None:
            return self.literal
        try:
            return self._render(None)
        except Exception:
            return f"{self.key} {self.fields}" if self.fields else self.key


class ConfigError(MacroWireError):
    """sources.yaml is missing, malformed, or references an unset ${VAR}."""

    kind = "config"


class BackfillInterrupted(MacroWireError):
    """A bulk seed stopped on something retrying could not fix.

    Carries where it got to and how to resume, because that is what the
    operator needs and a traceback is not. A twenty-minute paced run
    meeting a network blip is an EXPECTED condition, not an exceptional
    one, and printing a stack trace for it buries the one line that
    matters. The traceback is still raised under --debug and still
    recorded in fetch_log either way.
    """

    kind = "interrupted"

    def __init__(self, source: str, reached, remaining: int, cause: Exception):
        self.source = source
        self.reached = reached
        self.remaining = remaining
        self.cause = cause
        # Adopt the cause's key rather than its rendered text, so an
        # interrupted backfill stores a key like everything else. A cause
        # that is not one of ours has only prose to offer.
        if isinstance(cause, MacroWireError) and cause.key:
            super().__init__(cause.key, **cause.fields)
        else:
            super().__init__(str(cause))


class FetchError(MacroWireError):
    """Transport failure: non-2xx, timeout, connection reset.

    The kind is narrowed at the raise site. `network` and `timeout` both
    describe the path rather than the source, and are the two that must
    never be reported as "this feed is broken" - on a slow or filtered
    international link they are what a perfectly healthy feed looks like.
    """

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
