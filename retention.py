"""Shared rolling-history policy for SSD Research Radar.

Old rows remain in SQLite as deduplication/version evidence, but only the
rolling window is eligible for historical backfill, dashboard publication, and
history feeds.  Material events are handled separately so a new update to an
older document can still be delivered as a current event.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Optional


DEFAULT_HISTORY_WINDOW_YEARS = 5
MIN_HISTORY_WINDOW_YEARS = 1
MAX_HISTORY_WINDOW_YEARS = 20


def history_window_years(config: Mapping[str, Any]) -> int:
    value = int(config.get("history_window_years", DEFAULT_HISTORY_WINDOW_YEARS))
    if not MIN_HISTORY_WINDOW_YEARS <= value <= MAX_HISTORY_WINDOW_YEARS:
        raise ValueError(
            f"history_window_years must be between {MIN_HISTORY_WINDOW_YEARS} "
            f"and {MAX_HISTORY_WINDOW_YEARS}"
        )
    return value


def _subtract_years(value: dt.date, years: int) -> dt.date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        # February 29 rolls to February 28 in a non-leap target year.
        return value.replace(year=value.year - years, day=28)


def history_cutoff(
    config: Mapping[str, Any], *, today: Optional[dt.date] = None
) -> str:
    """Return the stricter of the rolling window and configured hard floor."""

    current = today or dt.datetime.now(dt.timezone.utc).date()
    rolling = _subtract_years(current, history_window_years(config))
    configured_text = str(config.get("history_start_date") or "").strip()
    if configured_text:
        try:
            configured = dt.date.fromisoformat(configured_text[:10])
        except ValueError as exc:
            raise ValueError("history_start_date must be an ISO date") from exc
        rolling = max(rolling, configured)
    return rolling.isoformat()


def item_date_sql(alias: str = "i") -> str:
    """SQLite expression for the monotonic earliest-known publication date."""

    if not alias.replace("_", "").isalnum():
        raise ValueError("unsafe SQL alias")
    return f"NULLIF(SUBSTR({alias}.original_published_at,1,10),'')"


def record_is_in_scope(record: Mapping[str, Any], cutoff: str) -> bool:
    """Filter one-time/full history while retaining undated current records."""

    published = str(record.get("published_at") or "").strip()
    if not published:
        return True
    return published[:10] >= cutoff


def event_is_in_scope(
    event_type: str,
    original_published_at: Optional[str],
    event_created_at: str,
    cutoff: str,
) -> bool:
    """Apply separate policy to current events and historical snapshots.

    A current material update remains useful even when the underlying document
    is old. A newly discovered record with a known old publication date is not
    allowed to masquerade as current; an undated current announcement remains
    eligible as an event but never enters historical backfill.
    """

    if event_created_at[:10] < cutoff:
        return False
    if event_type == "updated":
        return True
    published = str(original_published_at or "").strip()
    return not published or published[:10] >= cutoff


__all__ = [
    "DEFAULT_HISTORY_WINDOW_YEARS",
    "history_cutoff",
    "history_window_years",
    "event_is_in_scope",
    "item_date_sql",
    "record_is_in_scope",
]
