"""Best-effort receipt clock for valid FX values reaching a writer."""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone

from app.crawlers.constants import MIBANK_RATE_RANGES, MIBANK_REQUIRED_PAIRS

SEEN_KEY = "source_health:last_valid_seen"
logger = logging.getLogger("exchange_rate.source_seen")
_retry_after = 0.0
_last_warning = 0.0
_failed_getter = None


def valid_pairs(current_rates) -> dict[str, float]:
    """Keep only required, finite, in-range values without changing the input."""
    if not isinstance(current_rates, dict):
        return {}
    valid = {}
    for pair in MIBANK_REQUIRED_PAIRS:
        value = current_rates.get(pair)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            finite = math.isfinite(value)
        except (OverflowError, TypeError, ValueError):
            continue
        if finite:
            low, high = MIBANK_RATE_RANGES[pair]
            if low <= value <= high:
                valid[pair] = value
    return valid


def record_valid_seen(source, current_rates, *, now_ms=None) -> None:
    """Write one hash update; observation failure never changes writer behavior."""
    global _retry_after, _last_warning, _failed_getter
    try:
        pairs = valid_pairs(current_rates)
        if not pairs:
            return
        # latest_rates_cache imports crud; resolve its module attribute here to avoid
        # a crud <-> latest_rates_cache import cycle and retain its patchable client.
        from app import latest_rates_cache

        getter = latest_rates_cache._get_sync_client
        if getter is _failed_getter and time.monotonic() < _retry_after:
            return
        stamp = int(datetime.now(timezone.utc).timestamp() * 1000) if now_ms is None else now_ms
        client = getter()
        if client is None:
            raise ConnectionError("Redis client unavailable")
        client.hset(SEEN_KEY, mapping={f"{source}:{pair}": stamp for pair in pairs})
    except Exception as exc:
        now = time.monotonic()
        _retry_after = now + 60
        _failed_getter = locals().get("getter")
        if now - _last_warning >= 60:
            logger.warning("source_seen write failed: %s", type(exc).__name__)
            _last_warning = now
