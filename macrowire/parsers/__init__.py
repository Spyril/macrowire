"""Parser registry.

sources.yaml selects a handler by name. A new feed of an existing shape
is pure YAML; only a genuinely new payload shape needs a module here plus
one line in this table.
"""

from ..errors import ConfigError
from . import (buyback_schedule, cb_news, cb_statistics, cfets_ccpr, cftc_cot,
               cninfo, ecb_fx, rss_news, sec_edgar, sse_southbound)
from .base import ParsedFeed

PARSERS = {
    "buyback_schedule": buyback_schedule.parse,
    "cb_news": cb_news.parse,
    "cb_statistics": cb_statistics.parse,
    "rss_news": rss_news.parse,
    "cfets_ccpr": cfets_ccpr.parse,
    "cftc_cot": cftc_cot.parse,
    "cninfo": cninfo.parse,
    "ecb_fx": ecb_fx.parse,
    "sec_edgar": sec_edgar.parse,
    "sse_southbound": sse_southbound.parse,
}

# Sources needing more than one request supply their own fetcher.
# Everything else uses the pipeline's default single GET.
FETCHERS = {
    "cfets_ccpr": cfets_ccpr.fetch,
    "cftc_cot": cftc_cot.fetch,
    "cninfo": cninfo.fetch,
    "sec_edgar": sec_edgar.fetch,
    "sse_southbound": sse_southbound.fetch,
}

__all__ = ["ParsedFeed", "PARSERS", "FETCHERS", "get_parser", "get_fetcher"]


def get_parser(name: str):
    try:
        return PARSERS[name]
    except KeyError:
        raise ConfigError(
            f"unknown parser {name!r}. Available: {', '.join(sorted(PARSERS))}"
        ) from None


def get_fetcher(name: str):
    """The source's own fetcher, or None to use the default single GET."""
    return FETCHERS.get(name)
