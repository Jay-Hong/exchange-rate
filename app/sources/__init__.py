"""External provider protocol adapters — no persistence, no scheduler.

This package contains parsers, session helpers, and auth utilities for
external data providers (KIS Open API, exchange WebSockets, etc.).

Operational glue (scheduler entry, DB write, Redis write, crawler_config
toggle) lives in `app/crawlers/`. This boundary keeps protocol parsing
testable in isolation from runtime state.

Naming note: `source_registry.py` (top-level) holds source/asset metadata
for `source_rates` DB model. This `app.sources` package is unrelated —
it adapts external provider protocols. Keep them mentally distinct.
"""
