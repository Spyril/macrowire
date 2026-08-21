"""Parser registry.

sources.yaml selects a handler by name. A new feed of an existing shape
is pure YAML; only a genuinely new payload shape needs a module here plus
one line in this table.
"""

from ..errors import ConfigError
from . import cb_news, cb_statistics, cfets_ccpr, rss_news
from .base import ParsedFeed

PARSERS = {
    "cb_news": cb_news.parse,
    "cb_statistics": cb_statistics.parse,
    "rss_news": rss_news.parse,
    "cfets_ccpr": cfets_ccpr.parse,
}

# Sources needing more than one request supply their own fetcher.
# Everything else uses the pipeline's default single GET.
FETCHERS = {
    "cfets_ccpr": cfets_ccpr.fetch,
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
