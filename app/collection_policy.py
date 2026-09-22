"""Independent collection schedule declaration and reviewed revision fingerprints.

This policy describes scheduled opportunities with every crawler enabled. It is
handwritten from the registration contract, never derived from scheduler jobs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from app.market_mode import get_market_mode

KST = "Asia/Seoul"


@dataclass(frozen=True)
class CronSpec:
    """Only explicitly supplied CronTrigger fields, in declaration order."""

    fields: tuple[tuple[str, str], ...]
    timezone: str = KST


@dataclass(frozen=True)
class JobPolicy:
    job_id: str
    crawler: str
    triggers: tuple[CronSpec, ...]
    misfire_grace_s: int
    max_instances: int = 1
    coalesce: bool = True


CollectionPolicy = Mapping[str, Mapping[str, JobPolicy]]
POLICY_REVISION = "2026-09-23.1"

# These immutable declarations are shared only where the full policy is equal.
_INVESTING = JobPolicy(
    "task_investing", "investing",
    (CronSpec((("second", "7,17,27,37,47,57"),)),), 5,
)
_DXY = JobPolicy(
    "task_dxy", "dxy",
    (CronSpec((("second", "1,11,21,31,41,51"),)),), 5,
)
_KB = JobPolicy(
    "task_kb", "kb",
    (CronSpec((("second", "9,19,29,39,49,59"),)),), 10,
)
_HANA = JobPolicy(
    "task_hana", "hana",
    (CronSpec((("second", "2,12,22,32,42,52"),)),), 10,
)
_BS = JobPolicy(
    "task_bs", "bs",
    (CronSpec((("minute", "*"), ("second", "33"))),), 30,
)
_CITI = JobPolicy(
    "task_citi", "citi",
    (CronSpec((("minute", "*"), ("second", "13"))),), 30,
)
_IBK = JobPolicy(
    "task_ibk", "ibk",
    (CronSpec((("minute", "*"), ("second", "34"))),), 30,
)
_NH = JobPolicy(
    "task_nh", "nh",
    (CronSpec((("minute", "*"), ("second", "54"))),), 30,
)

POLICY: CollectionPolicy = MappingProxyType({
    "IN": MappingProxyType({
        "task_investing": _INVESTING,
        "task_dxy": _DXY,
        "task_kb": _KB,
        "task_hana": _HANA,
        "task_woori": JobPolicy(
            "task_woori", "woori",
            (CronSpec((("minute", "*"), ("second", "14,44"))),), 30,
        ),
        "task_bs": _BS,
        "task_citi": _CITI,
        "task_shinhan": JobPolicy(
            "task_shinhan", "shinhan",
            (CronSpec((("minute", "*"), ("second", "18"))),), 30,
        ),
        "task_ibk": _IBK,
        "task_nh": _NH,
        "task_sc": JobPolicy(
            "task_sc", "sc",
            (CronSpec((("minute", "*"), ("second", "58"))),), 30,
        ),
    }),
    "BREAK1": MappingProxyType({
        "task_investing": _INVESTING,
        "task_dxy": _DXY,
        "task_kb": _KB,
        "task_hana": _HANA,
        "task_woori": JobPolicy(
            "task_woori", "woori",
            (CronSpec((("hour", "19-23,0-5"), ("minute", "*"),
                       ("second", "14,44"))),), 30,
        ),
        "task_bs": _BS,
        "task_citi": _CITI,
        "task_shinhan": JobPolicy(
            "task_shinhan", "shinhan",
            (CronSpec((("hour", "19-23,0-2"), ("minute", "*"),
                       ("second", "18"))),), 30,
        ),
        "task_ibk": _IBK,
        "task_nh": _NH,
    }),
    "BREAK2": MappingProxyType({
        "task_investing": _INVESTING,
        "task_dxy": _DXY,
        "task_kb": _KB,
        "task_hana": _HANA,
        "task_bs": _BS,
        "task_citi": _CITI,
        "task_nh": _NH,
        "task_ibk_terminal": JobPolicy(
            "task_ibk_terminal", "ibk",
            (CronSpec((("hour", "6"), ("minute", "0,1"),
                       ("second", "34"), ("day_of_week", "tue-sat"))),), 30,
        ),
        "task_woori": JobPolicy(
            "task_woori", "woori",
            (
                CronSpec((("hour", "6"), ("minute", "0-3"),
                          ("second", "14,44"), ("day_of_week", "tue-sat"))),
                CronSpec((("hour", "6"), ("minute", "4"),
                          ("second", "53"), ("day_of_week", "tue-sat"))),
            ), 30,
        ),
    }),
    "OUT": MappingProxyType({
        "task_investing": JobPolicy(
            "task_investing", "investing",
            (CronSpec((("minute", "*"), ("second", "8"))),), 30,
        ),
        "task_dxy": JobPolicy(
            "task_dxy", "dxy",
            (CronSpec((("minute", "*"), ("second", "21"))),), 120,
        ),
        "task_kb": JobPolicy(
            "task_kb", "kb",
            (CronSpec((("minute", "*"), ("second", "28"))),), 30,
        ),
        "task_hana": JobPolicy(
            "task_hana", "hana",
            (CronSpec((("minute", "*"), ("second", "38"))),), 30,
        ),
        "task_bs": JobPolicy(
            "task_bs", "bs",
            (CronSpec((("minute", "*"), ("second", "51"))),), 30,
        ),
        "task_nh": JobPolicy(
            "task_nh", "nh",
            (CronSpec((("minute", "*"), ("second", "10"))),), 30,
        ),
        "task_shinhan": JobPolicy(
            "task_shinhan", "shinhan",
            (CronSpec((("minute", "*"), ("second", "30"))),), 30,
        ),
    }),
})

_CRAWLERS = MappingProxyType({
    job_id: job.crawler for jobs in POLICY.values() for job_id, job in jobs.items()
})


def crawler_of(job_id: str) -> str:
    """Resolve registered policy IDs, including the separate IBK terminal ID."""
    return _CRAWLERS[job_id]


FINGERPRINT_WINDOW_KST = (
    datetime(2026, 9, 28, tzinfo=ZoneInfo(KST)),
    datetime(2026, 10, 12, tzinfo=ZoneInfo(KST)),
)


def policy_fingerprint(policy: CollectionPolicy = POLICY) -> str:
    """Hash canonical policy JSON and every second's mode transitions.

    Mapping and field order are immaterial; OR child order is preserved. The
    starting mode anchors even a constant-mode window. Do not cache this result:
    it must reflect the currently supplied policy and get_market_mode function.
    """
    declarations = {
        mode: {
            job_id: {
                "job_id": job.job_id,
                "crawler": job.crawler,
                "triggers": [
                    {"fields": sorted(spec.fields), "timezone": spec.timezone}
                    for spec in job.triggers
                ],
                "misfire_grace_s": job.misfire_grace_s,
                "max_instances": job.max_instances,
                "coalesce": job.coalesce,
            }
            for job_id, job in jobs.items()
        }
        for mode, jobs in policy.items()
    }
    start, end = FINGERPRINT_WINDOW_KST
    step = timedelta(seconds=1)
    initial_mode = previous_mode = get_market_mode(start)
    transitions = []
    current = start + step
    while current < end:
        mode = get_market_mode(current)
        if mode != previous_mode:
            transitions.append((current.isoformat(), previous_mode, mode))
            previous_mode = mode
        current += step

    payload = {
        "policy": declarations,
        "window_kst": [start.isoformat(), end.isoformat()],
        "initial_mode": initial_mode,
        "mode_transitions": transitions,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Literal review anchors: never compute these from the live policy/source at
# import time. A semantic change requires a new revision and an added entry.
APPROVED_POLICY_FINGERPRINTS: Mapping[str, str] = MappingProxyType({
    "2026-09-23.1": "8b213384f637702cd7b6452bd20fee80023e7e65b16c984f6651a58a8e1e6161",
})
APPROVED_MODE_SOURCE_SHA256: Mapping[str, str] = MappingProxyType({
    "2026-09-23.1": "d8e071a117995495f4e7da5e79dfe296487cb1c321f3f50af9197b661306d67d",
})
