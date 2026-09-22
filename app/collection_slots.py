"""Pure scheduled collection candidates and due-time configuration classification.

No execution evidence, holiday calendar or DXY fallback clock participates here.
S4 owns scheduled_off records, aggregation, and policy revision effective ranges.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.collection_policy import (
    KST,
    POLICY,
    POLICY_REVISION,
    CollectionPolicy,
    CronSpec,
    JobPolicy,
)
from app.market_mode import get_market_mode

SLOT_SCHEMA_VERSION = 1
_KST = ZoneInfo(KST)
_UTC = timezone.utc
_TICK = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class SlotCandidate:
    slot_id: str
    crawler: str
    job_id: str
    due_at_utc: datetime
    mode: str
    policy_revision: str
    policy_grace_s: int
    dispatch_deadline_utc: datetime


def generate_candidates(
    start_utc: datetime,
    end_utc: datetime,
    *,
    policy: CollectionPolicy = POLICY,
    revision: str = POLICY_REVISION,
) -> list[SlotCandidate]:
    """Enumerate the half-open interval, sorted by (due_at_utc, job_id).

    Identical job/cron declarations across modes are enumerated once. Each fire
    is retained only if its origin includes the due-time mode, whose policy
    supplies grace. Overlapping OR children yield just one candidate; coalesce
    and overlapping grace never merge distinct scheduled times.
    """
    if start_utc.utcoffset() is None or end_utc.utcoffset() is None:
        raise ValueError("start_utc and end_utc must be timezone-aware")
    start_utc = start_utc.astimezone(_UTC)
    end_utc = end_utc.astimezone(_UTC)
    if start_utc > end_utc:
        raise ValueError("start_utc must not be after end_utc")
    if start_utc == end_utc:
        return []

    origins: dict[tuple[str, CronSpec], dict[str, JobPolicy]] = {}
    for mode, jobs in policy.items():
        for job_id, job in jobs.items():
            for spec in job.triggers:
                origins.setdefault((job_id, spec), {})[mode] = job

    candidates: dict[tuple[datetime, str], SlotCandidate] = {}
    for (job_id, spec), modes in origins.items():
        trigger = CronTrigger(**dict(spec.fields), timezone=ZoneInfo(spec.timezone))
        due = trigger.get_next_fire_time(None, start_utc)
        while due is not None:
            due_utc = due.astimezone(_UTC)
            if due_utc >= end_utc:
                break
            mode = get_market_mode(due_utc.astimezone(_KST))
            job = modes.get(mode)
            key = (due_utc, job_id)
            if job is not None and key not in candidates:
                slot_id = hashlib.sha256(
                    f"{SLOT_SCHEMA_VERSION}|{revision}|{job_id}|{due_utc.isoformat()}"
                    .encode("utf-8")
                ).hexdigest()[:32]
                candidates[key] = SlotCandidate(
                    slot_id=slot_id,
                    crawler=job.crawler,
                    job_id=job_id,
                    due_at_utc=due_utc,
                    mode=mode,
                    policy_revision=revision,
                    policy_grace_s=job.misfire_grace_s,
                    dispatch_deadline_utc=(
                        due_utc + timedelta(seconds=job.misfire_grace_s)
                    ),
                )
            due = trigger.get_next_fire_time(due, due_utc + _TICK)

    return [candidates[key] for key in sorted(candidates)]


@dataclass(frozen=True)
class ConfigState:
    enabled: bool | None
    config_revision: str | None


@dataclass(frozen=True)
class ClassifiedSlot:
    candidate: SlotCandidate
    expectation_state: str
    config_revision: str | None


def classify(
    candidates: Iterable[SlotCandidate],
    config_at: Callable[[str, datetime], ConfigState],
) -> list[ClassifiedSlot]:
    """Preserve every candidate and classify using exactly one due-time lookup.

    Unknown history remains explicit; excluding it from confirmed expectation
    denominators is the aggregator's responsibility.
    """
    classified = []
    for candidate in candidates:
        config = config_at(candidate.crawler, candidate.due_at_utc)
        if config.enabled is True:
            state = "expected"
        elif config.enabled is False:
            state = "admin_disabled"
        elif config.enabled is None:
            state = "unknown"
        else:
            raise ValueError("ConfigState.enabled must be True, False, or None")
        classified.append(ClassifiedSlot(candidate, state, config.config_revision))
    return classified
