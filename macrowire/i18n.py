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

# locale -> (mtime_when_read, catalogue). Keyed on mtime so this is a CACHE
# and not a FREEZE. It was a freeze, and the difference cost a bug report:
# the page's JavaScript is served from disk by StaticFiles on every request
# while the strings it asks for were read once at import, so editing a
# catalogue with the server running produced new JS asking for keys an old
# in-memory copy did not have. That renders as a raw key and is
# indistinguishable from a string nobody ever wrote.
#
# Two halves of one feature must not have two different freshnesses.
_cache: dict[str, tuple[float, dict]] = {}
_warned: set[tuple[str, str]] = set()


def available() -> list[str]:
    return sorted(p.stem for p in LOCALES_DIR.glob("*.json"))


def load(locale: str) -> dict:
    """The catalogue, re-read whenever the file on disk has changed.

    A stat per call, and a parse only when the mtime moves. Cheap enough
    that correctness is not worth trading for it: this is a single-user
    local server and the file is a few kilobytes.
    """
    path = LOCALES_DIR / f"{locale}.json"
    stamp = path.stat().st_mtime if path.exists() else 0.0
    cached = _cache.get(locale)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    if not path.exists():
        if locale == DEFAULT_LOCALE:
            raise FileNotFoundError(f"default locale file missing: {path}")
        log.warning("locale %r not found, falling back to %s", locale, DEFAULT_LOCALE)
        catalogue: dict = {}
    else:
        catalogue = json.loads(path.read_text(encoding="utf-8"))

    # A key that was missing and has now been added should be able to warn
    # again if it goes missing a second time, so the once-only log guard is
    # dropped along with the stale copy.
    _warned.difference_update({w for w in _warned if w[0] == locale})
    _cache[locale] = (stamp, catalogue)
    return catalogue


def coverage(locale: str) -> dict:
    """How complete a locale is against `en`, which is the source of truth.

    A partial catalogue is USABLE, not broken: every missing key falls back
    to English per key, so a 60%-complete file renders 60% translated and
    40% English and never a raw key. This exists so a contributor can see
    what is left rather than diffing two JSON files by hand.
    """
    english = {k: v for k, v in flatten(load(DEFAULT_LOCALE)).items()
               if not k.startswith("_meta.")}
    theirs = {k: v for k, v in flatten(load(locale)).items()
              if not k.startswith("_meta.")}
    present = [k for k in english if k in theirs]
    missing = [k for k in english if k not in theirs]
    # A key with no English original cannot fall back, so it can only ever
    # render in one language. Worth naming separately from "missing".
    orphaned = [k for k in theirs if k not in english]
    meta = load(locale).get("_meta") or {}
    return {
        "locale": locale,
        "name": meta.get("name") or locale,
        "total": len(english),
        "present": len(present),
        "missing": sorted(missing),
        "orphaned": sorted(orphaned),
        "percent": (100.0 * len(present) / len(english)) if english else 0.0,
    }


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

    def __call__(self, key: str, /, **fields) -> str:
        # `key` is POSITIONAL-ONLY. Without the slash, a catalogue string
        # with a {key} placeholder cannot be rendered - t("x", key="y")
        # collides with this parameter and raises. Same for {self}. One
        # character, and it removes the whole class of collision.
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
        flat = renderable(self.fallback)
        flat.update(renderable(self.catalogue))
        skip = tuple(f"{name}." for name in exclude)
        flat = {k: v for k, v in flat.items() if not skip or not k.startswith(skip)}
        out: dict = {}
        for path, text in flat.items():
            node = out
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = text
        return out


def renderable(catalogue: dict) -> dict[str, str]:
    """Only the strings that reach a screen.

    A key whose path contains a segment starting with `_` is DOCUMENTATION
    for whoever is translating - `_meta.name`, `rail._note_source_facts` -
    and is never rendered. The distinction has to exist because the notes
    quote the very things they warn against: the note beside the as-of keys
    spells out "4pm AEST" so a translator knows not to write it, and the
    test that forbids that string in a catalogue would otherwise fire on
    the warning itself.
    """
    return {k: v for k, v in flatten(catalogue).items()
            if not any(part.startswith("_") for part in k.split("."))}


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
