"""APScheduler wrapper 이전 누락의 구조화 계측과 양성 대조."""

import logging
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.events import (
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    JobExecutionEvent,
    JobSubmissionEvent,
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import scheduler as app_scheduler


def test_listener_emits_stable_structured_fields(caplog):
    caplog.set_level(logging.WARNING, logger="exchange_rate.scheduler")
    now = datetime.now(timezone.utc)

    app_scheduler._scheduler_job_skip_listener(
        JobSubmissionEvent(EVENT_JOB_MAX_INSTANCES, "task_kb", "default", [now])
    )
    app_scheduler._scheduler_job_skip_listener(
        JobExecutionEvent(EVENT_JOB_MISSED, "task_hana", "default", now)
    )

    records = [record for record in caplog.records if hasattr(record, "scheduler_event")]
    assert [record.scheduler_event for record in records] == ["max_instances", "misfire"]
    assert records[0].job_id == "task_kb"
    assert records[0].occurrence_count == 1
    assert records[0].scheduled_run_times == [now.isoformat()]
    assert records[1].job_id == "task_hana"
    assert records[1].scheduled_run_time == now.isoformat()


def test_real_scheduler_delay_emits_max_instances_event():
    observed = threading.Event()
    release = threading.Event()
    local = BackgroundScheduler(timezone=timezone.utc)

    def listener(event):
        app_scheduler._scheduler_job_skip_listener(event)
        if event.code == EVENT_JOB_MAX_INSTANCES:
            observed.set()

    def slow_job():
        release.wait(timeout=1.0)

    local.add_listener(listener, app_scheduler.SCHEDULER_SKIP_EVENT_MASK)
    local.add_job(
        slow_job,
        IntervalTrigger(seconds=0.05, timezone=timezone.utc),
        id="task_positive_max_instances",
        max_instances=1,
        coalesce=True,
    )
    local.start()
    try:
        assert observed.wait(timeout=2.0), "실제 max_instances 이벤트가 발화하지 않았다"
    finally:
        release.set()
        local.shutdown(wait=True)


def test_real_scheduler_late_job_emits_misfire_event():
    observed = threading.Event()
    local = BackgroundScheduler(timezone=timezone.utc)

    def listener(event):
        app_scheduler._scheduler_job_skip_listener(event)
        if event.code == EVENT_JOB_MISSED:
            observed.set()

    local.add_listener(listener, app_scheduler.SCHEDULER_SKIP_EVENT_MASK)
    local.add_job(
        lambda: None,
        DateTrigger(
            run_date=datetime.now(timezone.utc) - timedelta(seconds=2),
            timezone=timezone.utc,
        ),
        id="task_positive_misfire",
        misfire_grace_time=1,
    )
    local.start()
    try:
        assert observed.wait(timeout=2.0), "실제 misfire 이벤트가 발화하지 않았다"
    finally:
        local.shutdown(wait=True)
