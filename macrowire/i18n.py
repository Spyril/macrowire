"""User-visible strings, by locale.

WHAT IS AND IS NOT IN HERE
--------------------------------------------------------------------------
Two kinds of string look alike on screen and must be handled differently.

  VIEWER-FACING   Addresses the person reading. "Source health", "clear
                  all", "not polled yet". Translate freely.

  SOURCE-FACT     States something true about a publisher, regardless of
                  who is reading. "4pm AEST", "09:15 CST", "~16:00 CET",
                  "released Fri 15:30 ET". The RBA fixes at 4pm Sydney
                  time whether the reader is in Sydney or Stuttgart.

The label AROUND a source fact is viewer-facing and lives here. The fact
itself does NOT, and is interpolated at render time. So the catalogue has
`rail.rba.asof` = "{time} · {period}" and never "4pm AEST" - translating
the label is correct, changing the fixing time would be a lie.

Item titles and summaries are never translated. A translation is an
interpretation, and storing one as the record loses the original. That
principle has held since step 1.

FALLBACK
--------------------------------------------------------------------------
A missing key falls back to `en` and is logged once. It never renders a
raw key or an empty string, because a UI that shows `rail.fx.heading` to
a user is worse than one showing English.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
DEFAULT_LOCALE = "en"

log = logging.getLogger("macrowire.i18n")

_cache: dict[str, dict] = {}
_warned: set[tuple[str, str]] = set()


def available() -> list[str]:
    return sorted(p.stem for p in LOCALES_DIR.glob("*.json"))


def load(locale: str) -> dict:
    if locale not in _cache:
        path = LOCALES_DIR / f"{locale}.json"
        if not path.exists():
            if locale == DEFAULT_LOCALE:
                raise FileNotFoundError(f"default locale file missing: {path}")
            log.warning("locale %r not found, falling back to %s", locale, DEFAULT_LOCALE)
            _cache[locale] = {}
        else:
            _cache[locale] = json.loads(path.read_text(encoding="utf-8"))
    return _cache[locale]


def _lookup(catalogue: dict, key: str):
    node = catalogue
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


class Translator:
    def __init__(self, locale: str = DEFAULT_LOCALE):
        self.locale = locale
        self.catalogue = load(locale)
        self.fallback = load(DEFAULT_LOCALE) if locale != DEFAULT_LOCALE else self.catalogue

    def __call__(self, key: str, **fields) -> str:
        text = _lookup(self.catalogue, key)
        if text is None:
            text = _lookup(self.fallback, key)
            if text is None:
                # Neither locale has it. Log loudly and show the key rather
                # than an empty string - an invisible label is a bug you
                # cannot see, which is worse than an ugly one you can.
                if (self.locale, key) not in _warned:
                    _warned.add((self.locale, key))
                    log.error("missing string %r in %s and in %s",
                              key, self.locale, DEFAULT_LOCALE)
                return key
            if (self.locale, key) not in _warned:
                _warned.add((self.locale, key))
                log.warning("missing string %r in %s, using %s",
                            key, self.locale, DEFAULT_LOCALE)
        try:
            return text.format(**fields) if fields else text
        except (KeyError, IndexError) as exc:
            log.error("string %r could not be formatted: %s", key, exc)
            return text


    def merged(self, exclude: tuple[str, ...] = ()) -> dict:
        """The catalogue this locale actually renders, English filling gaps.

        Handing the client a half-populated catalogue would push the
        fallback decision into JavaScript, where a missing key becomes an
        `undefined` on screen. Resolve it here, where it can be logged.
        """
        flat = flatten(self.fallback)
        flat.update(flatten(self.catalogue))
        skip = ("_meta.",) + tuple(f"{name}." for name in exclude)
        flat = {k: v for k, v in flat.items() if not k.startswith(skip)}
        out: dict = {}
        for path, text in flat.items():
            node = out
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = text
        return out


def flatten(catalogue: dict, prefix: str = "") -> dict[str, str]:
    """Every key path in a catalogue, for completeness checks."""
    out: dict[str, str] = {}
    for key, value in catalogue.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, path))
        elif isinstance(value, str):
            out[path] = value
    return out
