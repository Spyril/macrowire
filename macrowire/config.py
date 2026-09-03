"""sources.yaml loader.

The architectural constraint: adding a source means editing YAML. Nothing
in this module or in the fetch loop names a specific feed.

ONE FILE, ONE LIFETIME.
--------------------------------------------------------------------------
Every loader here re-reads from disk on call. Do not bind the result at
import in a long-running process while something else reads the same file
per request - that gives one file two freshnesses, and it has bitten this
project twice.

The second time: `app.py` bound `LOCALE = load_locale()` at import while
`_sources()` called `load_sources()` per request. Both read sources.yaml.
Disabling a source took effect immediately; changing `defaults.locale` did
not. Worse, the page's JavaScript is served from disk by StaticFiles on
every request, so new JS asked for strings an old in-memory catalogue did
not have - which renders as a raw key and is indistinguishable from a
string nobody ever wrote.

The rule: if a file is read per-request anywhere, it must not be read at
import anywhere. A short-lived CLI process is exempt - it reads and exits,
and there is no window for the file to change underneath it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

from .errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "sources.yaml"

# Politeness floor for public government servers. Not overridable downward.
ABSOLUTE_MIN_INTERVAL = 60

# Generous by default: a slow large feed truncating mid-download is a
# worse failure than a slow poll. Override per source in sources.yaml.
DEFAULT_TIMEOUT = 120

# How much of a source's history can be recovered if this database is lost.
#   none      - the feed carries a live window only. Our copy is the sole copy.
#   rolling   - the feed carries its last N entries. Newest recoverable,
#               anything that has scrolled off is not.
#   queryable - arbitrary history retrievable on demand (paginated API).
ARCHIVE_KINDS = {"none", "rolling", "queryable", "unknown"}

# How honestly the ribbon may place a source in time. Measured, not assumed:
#   fixed     - single stamp, IQR 0m        -> a mark
#   tight     - schedulable, IQR <= 30m     -> a mark with a window
#   scattered - IQR 2-7h                    -> no mark; a band or nothing
#   date_only - feed carries no time at all -> no time position whatsoever
TIMING_CLASSES = {"fixed", "tight", "scattered", "date_only"}

# Which authority publishes the source. A fact about the publisher, fixed at
# config time - not a topic judgement, so there is nothing here to rot.
JURISDICTIONS = {"AU", "US", "CN", "HK", "EU", "UK", "JP"}

# FX relevance. THREE states, and the third is load-bearing:
#
#   fx            matches an include pattern
#   not_fx        matches an exclude pattern
#   unclassified  matches neither, OR the source declares no fx block
#
# Absence of a rule must NEVER read as a negative. A source with no
# vocabulary is unclassified, not not-FX - otherwise adding a source
# silently hides it from the filter, and renaming a committee silently
# drops items out of it. Both are the failure this project keeps catching.
FX_STATES = ("fx", "not_fx", "unclassified")

# ${NAME} is required; ${NAME:-fallback} has a default. The fallback form
# is still supported and is currently UNUSED by sources.yaml: it used to
# give MACROWIRE_PROJECT_URL the author's repository, which meant a
# downstream user's traffic identified the author rather than themselves.
# Anything that names the operator or the install has no business having a
# default, and both such values now fail loudly instead.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# What each required variable is FOR, so a missing one explains itself
# instead of naming a symbol and leaving you to search for it.
ENV_HELP = {
    "MACROWIRE_CONTACT": (
        "A contact address for the outbound User-Agent. Every source here is a\n"
        "  public government or exchange server and they expect to know who is\n"
        "  calling; some block requests that do not say.\n"
        "    MACROWIRE_CONTACT=you@example.com"
    ),
    "MACROWIRE_PROJECT_URL": (
        "The URL of YOUR fork or install, for the outbound User-Agent. A source\n"
        "  seeing unfamiliar traffic needs to know what software is calling and\n"
        "  who is operating it; the contact address answers the second and this\n"
        "  answers the first.\n"
        "  It has no default ON PURPOSE. It used to fall back to the author's\n"
        "  repository, so every request anyone made pointed a source at the\n"
        "  wrong person.\n"
        "    MACROWIRE_PROJECT_URL=https://github.com/you/macrowire"
    ),
    "SEC_CONTACT": (
        "The SEC requires a User-Agent of the form 'Name email' and ENFORCES it -\n"
        "  anything else is answered with HTTP 403. Note the space: a bare email\n"
        "  is not enough.\n"
        "    SEC_CONTACT=Jane Doe jane@example.com"
    ),
}


@dataclass(frozen=True)
class Source:
    name: str
    kind: str
    parser: str
    url: str
    user_agent: str
    min_interval_seconds: int
    timeout_seconds: int
    stagger_seconds: int
    staleness_days: int | None
    # How often this series is PUBLISHED, in days. Distinct from
    # staleness_days, which is about polling: this is the publisher's own
    # cadence, and it is what makes six-day-old CoT on time and four-day-old
    # RBA late. Absent means the age is shown and lateness is NEVER
    # asserted - a series that has not declared a cadence is not one this
    # code gets to guess at.
    cadence_days: int | None = None
    raw_retention_days: int | None = None
    archive: str = "unknown"
    jurisdiction: str = ""
    importance: int = 3
    fx: dict = field(default_factory=dict)
    timing: dict = field(default_factory=dict)
    collapse_repeats: bool = True
    # Whether this source is polled. A disabled source keeps its config, its
    # stored rows and its place in the file; it is simply not contacted.
    # Commenting a block out instead loses the configuration and makes
    # turning it back on an editing job rather than a one-word change.
    enabled: bool = True
    categories: list = field(default_factory=list)
    config: dict = field(default_factory=dict)


def _expand_env(value):
    """Substitute ${NAME} from the environment, recursively.

    An unset name raises. Substituting an empty string would let a
    half-configured User-Agent or a blank API key reach a live server.
    """
    if isinstance(value, str):

        def replace(match):
            name, fallback = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if fallback is not None:
                return fallback
            hint = ENV_HELP.get(name)
            raise ConfigError(
                f"{name} is not set, and sources.yaml needs it.\n"
                + (f"  {hint}\n" if hint else "")
                + f"  Set it in {REPO_ROOT / '.env'} (copy .env.example to start)."
            )

        return _ENV_REF.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_sources(path: Path | None = None) -> list[Source]:
    load_dotenv(REPO_ROOT / ".env")
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"config not found: {path}")

    try:
        document = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(document, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(document).__name__}")

    document = _expand_env(document)
    defaults = document.get("defaults") or {}
    entries = document.get("sources") or []
    if not entries:
        raise ConfigError(f"{path} defines no sources")

    sources: list[Source] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: sources[{index}] is not a mapping")

        for required in ("name", "kind", "url"):
            if not entry.get(required):
                raise ConfigError(f"{path}: sources[{index}] is missing '{required}'")

        name = entry["name"]
        if name in seen:
            raise ConfigError(f"{path}: duplicate source name '{name}'")
        seen.add(name)

        config = entry.get("config") or {}
        if not isinstance(config, dict):
            raise ConfigError(f"{path}: '{name}' config must be a mapping")

        def setting(key, fallback=None):
            # Per-source config wins over defaults.
            if key in config:
                return config[key]
            return defaults.get(key, fallback)

        timeout = int(setting("timeout_seconds", DEFAULT_TIMEOUT))
        if timeout < 1:
            raise ConfigError(f"{path}: '{name}' timeout_seconds must be >= 1")

        interval = int(setting("min_interval_seconds", ABSOLUTE_MIN_INTERVAL))
        if interval < ABSOLUTE_MIN_INTERVAL:
            raise ConfigError(
                f"{path}: '{name}' min_interval_seconds={interval} is below the "
                f"{ABSOLUTE_MIN_INTERVAL}s floor for public servers"
            )

        user_agent = setting("user_agent")
        if not user_agent:
            raise ConfigError(f"{path}: '{name}' has no user_agent and no default")

        # Contacts are validated HERE rather than at fetch time. Both are
        # required by the servers we poll, and discovering that through a
        # stack trace mid-cycle is the wrong first contact with the tool.
        if "@" not in user_agent:
            raise ConfigError(
                f"{path}: '{name}' user_agent has no contact address.\n"
                f"  These are public government servers and they expect one.\n"
                f"  Set MACROWIRE_CONTACT in .env, e.g.\n"
                f"    MACROWIRE_CONTACT=you@example.com"
            )
        if entry.get("parser") == "sec_edgar" or config.get("sec_contact") is not None:
            contact = (config.get("sec_contact") or "").strip()
            if not contact or "@" not in contact or " " not in contact:
                raise ConfigError(
                    f"{path}: '{name}' needs a valid sec_contact.\n"
                    f"  The SEC requires a User-Agent of the form 'Name email' and\n"
                    f"  ENFORCES it - anything else is answered with HTTP 403.\n"
                    f"  Set SEC_CONTACT in .env, e.g.\n"
                    f"    SEC_CONTACT=Jane Doe jane@example.com\n"
                    f"  Currently: {contact!r}"
                )

        # Top level, not under `config:`: whether to poll a source at all is
        # not one of its parser's settings, and a per-source default that
        # could be inherited from `defaults:` would let one line silently
        # switch off everything.
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(
                f"{path}: '{name}' enabled={enabled!r} must be true or false"
            )

        categories = setting("categories", []) or []
        if not isinstance(categories, list):
            raise ConfigError(f"{path}: '{name}' categories must be a list")
        for rule in categories:
            if not isinstance(rule, dict) or not rule.get("match") or not rule.get("name"):
                raise ConfigError(
                    f"{path}: '{name}' each categories entry needs 'match' and 'name'"
                )

        jurisdiction = setting("jurisdiction")
        if not jurisdiction:
            raise ConfigError(
                f"{path}: '{name}' has no jurisdiction. Every source must "
                f"declare one of {sorted(JURISDICTIONS)} - it is a fact about "
                f"the publisher, not a judgement, so there is no default."
            )
        if jurisdiction not in JURISDICTIONS:
            raise ConfigError(
                f"{path}: '{name}' jurisdiction={jurisdiction!r} must be one "
                f"of {sorted(JURISDICTIONS)}"
            )

        fx_block = setting("fx", {}) or {}
        if isinstance(fx_block, bool):
            # Reference-rate sources are FX by construction; `fx: true` says
            # so at source level without inventing a vocabulary for numbers
            # that have no titles to match against.
            fx_block = {"always": fx_block}
        if not isinstance(fx_block, dict):
            raise ConfigError(
                f"{path}: '{name}' fx must be `true` or a mapping with "
                f"include/exclude lists")
        for key in ("include", "exclude"):
            if key in fx_block and not isinstance(fx_block[key], list):
                raise ConfigError(f"{path}: '{name}' fx.{key} must be a list")
        if fx_block.get("always") and (fx_block.get("include") or fx_block.get("exclude")):
            raise ConfigError(
                f"{path}: '{name}' sets fx.always AND a vocabulary. Pick one - "
                f"a source is either FX by construction or classified by title.")
        # `unmeasured:` is a declared absence. It behaves exactly as no
        # vocabulary does - every item unclassified - but it is a decision on
        # the record with a reason attached, rather than a block someone
        # forgot to write. It must carry that reason, or it is the oversight
        # it exists to rule out.
        if "unmeasured" in fx_block:
            if fx_block.get("include") or fx_block.get("exclude") or fx_block.get("always"):
                raise ConfigError(
                    f"{path}: '{name}' sets fx.unmeasured AND a policy. "
                    f"Pick one.")
            if not isinstance(fx_block["unmeasured"], str) or not fx_block["unmeasured"].strip():
                raise ConfigError(
                    f"{path}: '{name}' fx.unmeasured must be a sentence saying "
                    f"why the corpus has not been measured yet.")

        importance = int(setting("importance", 3))
        if not 0 <= importance <= 5:
            raise ConfigError(f"{path}: '{name}' importance must be 0-5")

        timing = setting("timing", {}) or {}
        if not isinstance(timing, dict):
            raise ConfigError(f"{path}: '{name}' timing must be a mapping")
        tclass = timing.get("class", "scattered")
        if tclass not in TIMING_CLASSES:
            raise ConfigError(
                f"{path}: '{name}' timing.class={tclass!r} must be one of "
                f"{sorted(TIMING_CLASSES)}"
            )
        if tclass in ("fixed", "tight") and not (timing.get("at") and timing.get("timezone")):
            raise ConfigError(
                f"{path}: '{name}' timing.class={tclass} needs both 'at' and "
                f"'timezone' - the ribbon cannot place a mark without them"
            )

        archive = setting("archive", "unknown")
        if archive not in ARCHIVE_KINDS:
            raise ConfigError(
                f"{path}: '{name}' archive={archive!r} must be one of "
                f"{sorted(ARCHIVE_KINDS)}"
            )
        if archive == "none" and setting("raw_retention_days", None):
            raise ConfigError(
                f"{path}: '{name}' has archive: none but sets raw_retention_days. "
                f"Its payloads are the only copy that will ever exist - pruning "
                f"them destroys history permanently."
            )

        retention = setting("raw_retention_days", None)
        if retention is not None:
            retention = int(retention)
            if retention < 1:
                raise ConfigError(
                    f"{path}: '{name}' raw_retention_days must be >= 1, or null to keep forever"
                )

        staleness = setting("staleness_days", None)
        if staleness is not None:
            staleness = int(staleness)
            if staleness < 1:
                raise ConfigError(
                    f"{path}: '{name}' staleness_days must be >= 1, or null for off"
                )

        cadence = setting("cadence_days", None)
        if cadence is not None:
            cadence = int(cadence)
            if cadence < 1:
                raise ConfigError(
                    f"{path}: '{name}' cadence_days must be >= 1, or null to "
                    f"show age without ever calling it late"
                )

        sources.append(
            Source(
                name=name,
                kind=entry["kind"],
                parser=entry.get("parser") or entry["kind"],
                url=entry["url"],
                user_agent=user_agent,
                min_interval_seconds=interval,
                timeout_seconds=int(setting("timeout_seconds", DEFAULT_TIMEOUT)),
                stagger_seconds=int(setting("stagger_seconds", 0)),
                staleness_days=staleness,
                cadence_days=cadence,
                raw_retention_days=retention,
                archive=archive,
                jurisdiction=jurisdiction,
                importance=importance,
                fx=fx_block,
                timing=timing,
                collapse_repeats=bool(setting('collapse_repeats', True)),
                enabled=enabled,
                categories=categories,
                config=config,
            )
        )
    return sources


def _validated_dir(declared, label: str, config_path: Path, default: Path) -> tuple[Path, bool]:
    """Resolve and check a user-supplied output directory.

    Checked at config load, not at write time. A directory that does not
    exist or cannot be written should fail while you are looking at the
    config, not silently at the moment it mattered.
    """
    if not declared:
        target = default
    else:
        declared = os.path.expanduser(_expand_env(str(declared)))
        target = Path(declared)
        if not target.is_absolute():
            raise ConfigError(
                f"{config_path}: {label} {declared!r} must be an absolute path - a "
                f"relative one would resolve differently depending on where you "
                f"ran the command from")
        if not target.exists():
            raise ConfigError(
                f"{config_path}: {label} {target} does not exist. Create it, or "
                f"remove {label} to use the default {default}.")
        if not target.is_dir():
            raise ConfigError(f"{config_path}: {label} {target} is not a directory")
        if not os.access(target, os.W_OK):
            raise ConfigError(f"{config_path}: {label} {target} is not writable")
    try:
        target.relative_to(REPO_ROOT)
        return target, False
    except ValueError:
        return target, True


def load_backup_settings(path: Path | None = None) -> dict:
    """The `backup:` block from sources.yaml defaults.

    `path` may point anywhere, same as export. A backup on the same disk as
    the database protects against a mistake but not a drive failure, and the
    config should let you say so.
    """
    load_dotenv(REPO_ROOT / ".env")
    path = path or DEFAULT_CONFIG_PATH
    document = yaml.safe_load(path.read_text()) or {}
    block = ((document.get("defaults") or {}).get("backup") or {})
    default_dir = REPO_ROOT / "data" / "backups"
    target, external = _validated_dir(block.get("path"), "backup.path", path, default_dir)
    return {
        "enabled": bool(block.get("enabled", True)),
        "interval_seconds": int(block.get("interval_seconds", 86400)),
        "keep": int(block.get("keep", 7)),
        "path": target,
        "external": external,
        "default_path": default_dir,
    }


DEFAULT_WEB_PORT = 8917


def _viewer_block(path: Path | None = None) -> dict:
    """The `viewer:` block, falling back to `defaults:` for older files.

    The two blocks exist so the line between "belongs to the reader" and
    "belongs to the installation" is visible in the file. A config written
    before the split keeps working: these keys used to live under
    `defaults:` and are still read from there if `viewer:` is absent.
    """
    load_dotenv(REPO_ROOT / ".env")
    path = path or DEFAULT_CONFIG_PATH
    document = yaml.safe_load(path.read_text()) or {}
    viewer = document.get("viewer")
    if isinstance(viewer, dict):
        return viewer
    return document.get("defaults") or {}


def load_window_days(path: Path | None = None) -> int:
    """How far back the tape looks.

    A viewer preference that was hardcoded as `days=30` in five places -
    three in app.js and two as endpoint defaults. One definition.
    """
    raw = _viewer_block(path).get("window_days", 30)
    try:
        days = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path or DEFAULT_CONFIG_PATH}: window_days "
                          f"{raw!r} is not a number") from exc
    if not 1 <= days <= 3650:
        raise ConfigError(f"{path or DEFAULT_CONFIG_PATH}: window_days {days} "
                          f"must be between 1 and 3650")
    return days


def system_timezone() -> str:
    """The machine's own zone, or UTC if it cannot be determined.

    UTC is the honest fallback, not a guess at a popular zone: a wrong
    timezone silently shifts every day header and every ribbon position by
    hours, and it looks like data rather than a misconfiguration.
    """
    import datetime

    # /etc/localtime is a symlink into the zoneinfo tree on every system
    # that has one; the resolved tail is the IANA name.
    link = Path("/etc/localtime")
    if link.is_symlink():
        parts = link.resolve().parts
        if "zoneinfo" in parts:
            name = "/".join(parts[parts.index("zoneinfo") + 1:])
            try:
                ZoneInfo(name)
                return name
            except Exception:
                pass
    # TZ, when it names a zone rather than a POSIX offset rule.
    declared = os.environ.get("TZ", "").strip()
    if declared:
        try:
            ZoneInfo(declared)
            return declared
        except Exception:
            pass
    _ = datetime  # imported for the reader: everything here resolves per instant
    return "UTC"


def load_timezone(path: Path | None = None, raw: bool = False) -> str:
    """Which zone the VIEWER reads in. Never a source's own zone.

    Day headers, the clock, session bars and mark positions are all the
    reader's local time. A source's publication time is NOT - the RBA fixes
    at 4pm Sydney whether this is read in Sydney or Stuttgart - and those
    facts live outside this setting entirely. See the FACT constant in
    app.js and `ribbon.py`.

    Stored as an IANA NAME and resolved per instant, never as a fixed
    offset. The Fed publishes at a rock-solid 14:00 ET and lands at 04:00,
    05:00 or 06:00 in Sydney across a year, including a three-week window
    where both hemispheres have switched out of step. A stored offset gets
    that wrong twice a year in each direction.
    """
    declared = _viewer_block(path).get("timezone")
    if not declared or str(declared).strip().lower() == "system":
        # `raw` asks what the FILE says rather than what it resolves to, so
        # the settings panel can show "system (Australia/Sydney)" instead of
        # silently presenting a detected zone as if it had been chosen.
        return "system" if raw else system_timezone()
    declared = str(declared).strip()
    try:
        ZoneInfo(declared)
    except Exception as exc:
        raise ConfigError(
            f"{path}: timezone {declared!r} is not an IANA zone name "
            f"(e.g. Australia/Sydney, America/New_York, Europe/London, UTC). "
            f"A fixed offset is not accepted: it would be wrong across DST."
        ) from exc
    return declared


def load_ordering(path: Path | None = None) -> dict:
    """How the session band and the jurisdiction chips are ordered.

    Both default to `viewer`, which is not the same thing in each case:
    the band keeps its chronological sequence and rotates its start point,
    while the chips put the reader's own market first and alphabetise the
    rest. See macrowire/ordering.py for why the two differ.
    """
    defaults = _viewer_block(path)
    path = path or DEFAULT_CONFIG_PATH

    sessions = defaults.get("session_order", "viewer")
    if not isinstance(sessions, (str, list)):
        raise ConfigError(f"{path}: session_order must be 'viewer', 'fixed', "
                          f"or a list of session keys")
    if isinstance(sessions, str) and sessions not in ("viewer", "fixed"):
        raise ConfigError(f"{path}: session_order={sessions!r} must be "
                          f"'viewer', 'fixed', or a list of session keys")

    chips = defaults.get("jurisdiction_order", "viewer")
    if not isinstance(chips, (str, list)):
        raise ConfigError(f"{path}: jurisdiction_order must be 'viewer', "
                          f"'alphabetical', or a list of jurisdiction codes")
    if isinstance(chips, str) and chips not in ("viewer", "alphabetical"):
        raise ConfigError(f"{path}: jurisdiction_order={chips!r} must be "
                          f"'viewer', 'alphabetical', or a list of codes")
    if isinstance(chips, list):
        unknown = [c for c in chips if c not in JURISDICTIONS]
        if unknown:
            raise ConfigError(
                f"{path}: jurisdiction_order names {unknown}, which are not "
                f"jurisdictions. Known: {sorted(JURISDICTIONS)}")

    # An explicit jurisdiction beats deriving one from the timezone - a
    # reader in Zurich watching US markets should be able to say so.
    declared = defaults.get("jurisdiction")
    if declared is not None and declared not in JURISDICTIONS:
        raise ConfigError(
            f"{path}: jurisdiction={declared!r} must be one of "
            f"{sorted(JURISDICTIONS)}, or unset to derive it from timezone")

    return {"sessions": sessions, "jurisdictions": chips,
            "viewer_jurisdiction": declared}


def load_locale(path: Path | None = None) -> str:
    """Which language the interface speaks. Content is never translated.

    An unknown locale is not fatal: i18n falls back to English and logs it,
    which is the right trade for a typo in a config file. A missing English
    catalogue IS fatal, and raises in i18n.load().
    """
    return str(_viewer_block(path).get("locale", "en"))


def load_web_settings(path: Path | None = None) -> dict:
    """The `web:` block from sources.yaml defaults.

    The port lives in config rather than as a literal in the CLI so there is
    one place that decides it. --port overrides for a one-off.
    """
    load_dotenv(REPO_ROOT / ".env")
    path = path or DEFAULT_CONFIG_PATH
    document = yaml.safe_load(path.read_text()) or {}
    block = ((document.get("defaults") or {}).get("web") or {})
    port = int(block.get("port", DEFAULT_WEB_PORT))
    if not 1 <= port <= 65535:
        raise ConfigError(f"{path}: web.port {port} is not a valid port")
    return {"host": str(block.get("host", "127.0.0.1")), "port": port}


def load_export_settings(path: Path | None = None) -> dict:
    """The `export:` block from sources.yaml defaults.

    `path` may point anywhere - a synced folder, an external drive. It is
    validated HERE, at config load, rather than at export time: a directory
    that does not exist or cannot be written should fail while you are
    looking at the config, not silently three weeks later when the one copy
    of your irreplaceable rows fails to be written.
    """
    load_dotenv(REPO_ROOT / ".env")
    path = path or DEFAULT_CONFIG_PATH
    document = yaml.safe_load(path.read_text()) or {}
    block = ((document.get("defaults") or {}).get("export") or {})

    default_dir = REPO_ROOT / "export"
    target, external = _validated_dir(block.get("path"), "export.path", path, default_dir)
    return {
        "path": target,
        "external": external,
        "auto": bool(block.get("auto", True)),
        "default_path": default_dir,
    }
