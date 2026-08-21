"""sources.yaml loader.

The architectural constraint: adding a source means editing YAML. Nothing
in this module or in the fetch loop names a specific feed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

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

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


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
    raw_retention_days: int | None = None
    archive: str = "unknown"
    jurisdiction: str = ""
    importance: int = 3
    timing: dict = field(default_factory=dict)
    collapse_repeats: bool = True
    categories: list = field(default_factory=list)
    config: dict = field(default_factory=dict)


def _expand_env(value):
    """Substitute ${NAME} from the environment, recursively.

    An unset name raises. Substituting an empty string would let a
    half-configured User-Agent or a blank API key reach a live server.
    """
    if isinstance(value, str):

        def replace(match):
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(
                    f"sources.yaml references ${{{name}}} but {name} is not set. "
                    f"Copy .env.example to .env and fill it in."
                )
            return os.environ[name]

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
                raw_retention_days=retention,
                archive=archive,
                jurisdiction=jurisdiction,
                importance=importance,
                timing=timing,
                collapse_repeats=bool(setting('collapse_repeats', True)),
                categories=categories,
                config=config,
            )
        )
    return sources


def load_backup_settings(path: Path | None = None) -> dict:
    """The `backup:` block from sources.yaml defaults, with safe fallbacks."""
    load_dotenv(REPO_ROOT / ".env")
    path = path or DEFAULT_CONFIG_PATH
    document = yaml.safe_load(path.read_text()) or {}
    block = ((document.get("defaults") or {}).get("backup") or {})
    return {
        "enabled": bool(block.get("enabled", True)),
        "interval_seconds": int(block.get("interval_seconds", 86400)),
        "keep": int(block.get("keep", 7)),
    }


DEFAULT_WEB_PORT = 8917


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
