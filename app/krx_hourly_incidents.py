"""KRX hourly incident policies used by append and checked by repair tests."""

from datetime import datetime


# 2026-08-14 07:00 KST rollover was delayed because the expiry calendar did
# not walk back over the substitute holiday. These buckets contain A75608
# ticks although A75609 was the active contract.
KRX_HOURLY_INCIDENT_20260814_BUCKETS_KST: tuple[datetime, ...] = tuple(
    datetime(2026, 8, 14, hour, 0) for hour in (8, 9, 10, 11)
)
