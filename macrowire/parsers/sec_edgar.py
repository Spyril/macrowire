"""SEC EDGAR company filings -> rows in `items`.

Per ticker, from the watchlist. `data.sec.gov/submissions/CIK{10}.json`
returns a company's recent filings as parallel arrays - one list per field,
indexed together - which is compact and entirely positional, so the parser
asserts the arrays are the same length before zipping them. Same class of
risk as the CFETS `values` array, handled the same way.

ACCESS. The SEC explicitly permits scripted access and states the terms:
10 requests/second maximum, and a declared User-Agent in the form
`Name email`. That format is ENFORCED at their edge, not merely requested -
a descriptive UA of the kind every other source here accepts was answered
with HTTP 403 "Request Rate Threshold Exceeded". So the contact is required
config and its absence is a hard failure rather than a guess.

VOCABULARY. `announcement_type` carries the SEC's own labels and nothing
invented: the form type, plus 8-K item numbers where the filing has them.
The item titles below are the official captions from Form 8-K itself.
"""

from __future__ import annotations

import json

from ..errors import MalformedEntryError, ParseError
from .base import ParsedFeed, iso_to_utc

# Official captions from Form 8-K. Reproduced, not invented.
ITEM_TITLES = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate a Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure or Election of Directors or Certain Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.05": "Amendment to Code of Ethics",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

# 8-K items where "price sensitive" is defensible on the face of the item,
# not inferred. Everything else leaves the column NULL.
#
#   2.02  results of operations - earnings, by definition
#   5.02  departure or election of directors/officers - a CEO leaving moves
#         a price and the item exists to disclose exactly that
#   7.01  Regulation FD - the rule exists BECAUSE the information is material
#
# 8.01 "Other Events" is the obvious temptation and is deliberately excluded:
# it is a catch-all and its contents range from a buyback to a name change.
# 9.01 is an exhibit marker, not an event. Both stay NULL. The column has
# been nullable and unpopulated since step 1; a coin flip is worse than NULL.
PRICE_SENSITIVE_ITEMS = {"2.02", "5.02", "7.01"}

ARRAY_FIELDS = (
    "accessionNumber", "filingDate", "reportDate", "acceptanceDateTime",
    "form", "items", "primaryDocument", "primaryDocDescription",
)


# EDGAR's `items` field means 8-K item numbers ONLY on 8-K and 8-K/A. On
# other forms it carries something else entirely: EFFECT puts a related form
# and an effectiveness timestamp there ("S-4,2024-05-06 16:00:00"), Form D a
# rule reference ("06b"). Reading those as 8-K items would present a filter
# vocabulary invented out of a misread field.
ITEM_FORMS = {"8-K", "8-K/A"}


def _split_items(raw: str | None, form: str = "8-K") -> list[str]:
    if not raw or form.upper() not in ITEM_FORMS:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def price_sensitive(form: str, items: list[str]) -> bool | None:
    """True only where the SEC's own item number says so. Otherwise NULL."""
    if form.upper() not in ITEM_FORMS:
        return None
    if any(item in PRICE_SENSITIVE_ITEMS for item in items):
        return True
    return None


def describe(form: str, items: list[str]) -> str:
    """The SEC's own vocabulary: form type, plus item numbers if present."""
    if items:
        return f"{form} [{', '.join(items)}]"
    return form


def parse(source, body: str) -> ParsedFeed:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ParseError(f"{source.name}: response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParseError(f"{source.name}: expected a JSON object")

    company = payload.get("name") or ""
    cik = payload.get("cik")
    if cik is None:
        raise ParseError(f"{source.name}: submissions payload has no cik")
    tickers = [t.upper() for t in (payload.get("tickers") or [])]

    filings = (payload.get("filings") or {}).get("recent")
    if not isinstance(filings, dict):
        raise ParseError(
            f"{source.name}: {company or cik} has no filings.recent object")

    missing = [f for f in ARRAY_FIELDS if f not in filings]
    if missing:
        raise ParseError(
            f"{source.name}: filings.recent is missing {missing}. The payload "
            f"shape has changed.")

    # These are parallel arrays zipped by index. A length mismatch would
    # silently file one company's dates against another filing's forms.
    lengths = {field: len(filings[field]) for field in ARRAY_FIELDS}
    if len(set(lengths.values())) != 1:
        raise ParseError(
            f"{source.name}: filings.recent arrays disagree in length {lengths}. "
            f"Refusing to zip them.")

    count = next(iter(lengths.values()))
    skip_forms = {f.upper() for f in (source.config.get("skip_forms") or [])}
    result = ParsedFeed()

    for index in range(count):
        form = (filings["form"][index] or "").strip()
        if not form:
            raise MalformedEntryError(
                f"{source.name}: filing {index} for {company} has no form type")
        if form.upper() in skip_forms:
            continue

        accession = filings["accessionNumber"][index]
        if not accession:
            raise MalformedEntryError(
                f"{source.name}: filing {index} for {company} has no accessionNumber")

        items = _split_items(filings["items"][index], form)
        # acceptanceDateTime is the moment EDGAR accepted it, to the second,
        # and is what a tape should order by. filingDate is date-only.
        stamp = filings["acceptanceDateTime"][index] or filings["filingDate"][index]
        published = iso_to_utc(stamp, source.name, f"filing {index} acceptanceDateTime")

        naked = accession.replace("-", "")
        document = filings["primaryDocument"][index] or ""
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{naked}/{document}"
               if document else
               f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{naked}/")

        captions = [ITEM_TITLES[i] for i in items if i in ITEM_TITLES]
        headline = f"{company} — {form}"
        if captions:
            headline += ": " + "; ".join(captions)

        result.items.append({
            "external_id": accession,
            "title": headline,
            "url": url,
            "summary": filings["primaryDocDescription"][index] or None,
            "content": None,
            "published_at": published,
            "announcement_type": describe(form, items),
            # The two filterable halves. The composite above stays for
            # display; "results announcements" needs these.
            "type_primary": form,
            "type_tags": ",".join(items) or None,
            "institution_abbrev": "SEC",
            "simple_title": None,
            "occurrence_date": iso_to_utc(
                f"{filings['reportDate'][index]}T00:00:00+00:00",
                source.name, f"filing {index} reportDate",
            ) if filings["reportDate"][index] else None,
            # The payload declares its own tickers, so there is no need to
            # thread the requested one back through the pipeline.
            "ticker": tickers[0] if tickers else None,
            "is_price_sensitive": price_sensitive(form, items),
        })
    return result


# --- fetching -------------------------------------------------------------

def sec_user_agent(source) -> str:
    """SEC's documented `Name email` form. Absence is a hard failure.

    Sending something else is not a soft degradation: their edge answered a
    normal descriptive User-Agent with HTTP 403. Better to refuse to poll
    than to poll in a way that gets the address blocked.
    """
    contact = (source.config.get("sec_contact") or "").strip()
    if not contact:
        raise ParseError(
            f"{source.name}: no sec_contact configured. The SEC requires a "
            f"User-Agent of the form 'Name email' and enforces it with a 403. "
            f"Set SEC_CONTACT in .env, e.g. 'Jane Doe jane@example.com'."
        )
    if "@" not in contact or " " not in contact:
        raise ParseError(
            f"{source.name}: sec_contact {contact!r} is not in the required "
            f"'Name email' form, e.g. 'Jane Doe jane@example.com'."
        )
    return contact


def fetch(source, get, state):
    """One request per watchlisted US ticker.

    Returns nothing at all when the watchlist is empty - which is a skip,
    not an error. Company announcements are watchlist-driven by design: the
    alternative is pulling an exchange's whole daily output to keep a
    handful of rows.
    """
    import time as _time

    from .. import watchlist as wl

    tickers = state.get("watchlist_us") or []
    if not tickers:
        return [], "watchlist is empty - nothing to poll"

    agent = sec_user_agent(source)
    headers = {"User-Agent": agent, "Accept-Encoding": "gzip, deflate"}

    cik_map = wl.load_cik_map(
        fetch=lambda url: get(url, headers=headers).content
    )

    # Well under the published 10/sec ceiling. We have no need of the
    # headroom and they are doing us a favour by allowing this at all.
    delay = float(source.config.get("request_interval_seconds", 0.5))

    responses = []
    unresolved = []
    for position, ticker in enumerate(sorted(tickers)):
        entry = cik_map.get(ticker.upper())
        if entry is None:
            unresolved.append(ticker)
            continue
        if position:
            _time.sleep(delay)
        responses.append(
            get(f"https://data.sec.gov/submissions/CIK{entry['cik']:010d}.json",
                headers=headers)
        )

    if unresolved and not responses:
        raise ParseError(
            f"{source.name}: none of the watchlisted tickers {unresolved} "
            f"resolve against the SEC ticker map")
    note = f"{len(responses)} ticker(s)"
    if unresolved:
        note += f"; unresolved: {', '.join(unresolved)}"
    return responses, note
