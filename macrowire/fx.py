"""FX relevance: three states, per-source vocabularies.

Measured on 90 days of collected items rather than assumed. The result that
shaped this design: eight of ten sources are genuinely MIXED, and the split
is not a handful of shared patterns.

  fed_press_monetary  75% FX      hkma_press   31%
  nbs_releases        80%         nbs_interp   24%
  boj_whatsnew        58%         boe_news     25%
  ecb_press           40%         sec_edgar     0%

Not because central banks publish non-FX material, but because several of
these feeds carry the WHOLE INSTITUTION - and several of these institutions
are also prudential regulators. `boe_news` is 25% FX because the same feed
carries the PRA: insurance consultations, enforcement fines, banknote
imagery advisory group minutes.

And the vocabularies do not transfer. A first rule set missed
`Minutes of the London FXJSC Main Committee` - the Foreign Exchange Joint
Standing Committee - because "FXJSC" does not match \\bFX\\b. It missed every
Chinese macro print and all of BoJ's monetary operations. Each source has
its own lexicon, so each source carries its own.

UNCLASSIFIED IS NOT NOT-FX. A source with no vocabulary, or an item
matching neither list, is unclassified. Treating that as a negative would
mean a renamed committee silently drops out of the filter - which is
exactly the class of failure this project keeps finding.
"""

from __future__ import annotations

import re

FX = "fx"
NOT_FX = "not_fx"
UNCLASSIFIED = "unclassified"


def _compile(patterns) -> re.Pattern | None:
    if not patterns:
        return None
    return re.compile("|".join(re.escape(p) if p.isalnum() else p
                               for p in patterns), re.I)


class Classifier:
    """One source's vocabulary, compiled once."""

    def __init__(self, source):
        block = source.fx or {}
        self.always = bool(block.get("always"))
        self.include = _compile(block.get("include"))
        self.exclude = _compile(block.get("exclude"))
        self.has_vocabulary = bool(self.include or self.exclude)

    def classify(self, title: str, summary: str | None = None) -> str:
        if self.always:
            return FX
        if not self.has_vocabulary:
            return UNCLASSIFIED
        text = title or ""
        # exclude wins: a source's exclusions are the narrower, more
        # deliberate list, and an item matching both is the ambiguous case
        # where the safer answer is to keep it out of an FX-only view.
        if self.exclude and self.exclude.search(text):
            return NOT_FX
        if self.include and self.include.search(text):
            return FX
        return UNCLASSIFIED


def classify_items(source, items) -> None:
    """Tag parsed items in place."""
    classifier = Classifier(source)
    for item in items:
        item["fx_state"] = classifier.classify(item.get("title") or "",
                                               item.get("summary"))
