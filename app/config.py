"""Eras and environment-based settings.

Every tunable value is read from an environment variable with a default.
See `.env.example` for the full list.
"""

import os
from dataclasses import dataclass

# Era name -> (first year, last year), both inclusive.
# `None` means unbounded on that side.
ERAS: dict[str, tuple[int | None, int | None]] = {
    "all": (None, None),
    "1950-1999": (1950, 1999),
    "2000-2026": (2000, 2026),
}

DEFAULT_DB_PATH = "data/app.db"
DEFAULT_CACHE_TTL_SECONDS = 86400
DEFAULT_RATE_LIMIT_PER_MINUTE = 30
CACHE_MAX_ENTRIES = 500


@dataclass(frozen=True)
class Settings:
    db_path: str
    cache_ttl_seconds: float
    rate_limit_per_minute: float


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def load_settings() -> Settings:
    """Read settings from the environment at call time."""
    return Settings(
        db_path=os.environ.get("APP_DB_PATH", DEFAULT_DB_PATH),
        cache_ttl_seconds=_env_number("CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS),
        rate_limit_per_minute=_env_number(
            "RATE_LIMIT_PER_MINUTE", DEFAULT_RATE_LIMIT_PER_MINUTE
        ),
    )
