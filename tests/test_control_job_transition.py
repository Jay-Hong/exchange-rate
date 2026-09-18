"""control_job 수정 A+B 1단계 — 모드 전환 misfire 정책과 전환 기록.

2026-09-18 06:00:01 control_job 이 1.17초 지연으로 misfire 되어 전환이 06:01:01 로 밀렸고,
우리은행 마무리 첫 두 슬롯(06:00:14·06:00:44)이 그 사이에 지나갔다(ADR-044).

A 는 misfire 판정을 **APScheduler 의 `run_job` 그대로** 시험한다. 판정을 흉내 낸 함수를
시험하면 라이브러리가 실제로 하는 일을 증명하지 못한다. 운영의 AsyncIOScheduler 는 동기
함수(control_job)를 기본 스레드 풀에서 `executors.base.run_job` 으로 돌리고, 판정도 그 안에서
한다(`apscheduler/executors/asyncio.py` 의 `_do_submit_job`).
B 는 전환 기록이 전환을 바꾸지 않으면서 호출별로 남는지를 시험한다.
"""
from __future__ import annotations

import ast
import inspect
import json
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.executors import base as executor_base
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone as pytz_timezone

import app.scheduler as sched

KST = pytz_timezone("Asia/Seoul")

# 운영에서 관측한 지연 (2026-09-18 06:00:02.170 WARNING "was missed by 0:00:01.170653")
OBSERVED_DELAY = timedelta(seconds=1.170653)
# 2026-09-18 은 금요일 — 우리은행 마무리 조회(화~토) 대상일
BOUNDARY = KST.localize(datetime(2026, 9, 18, 6, 0, 0))
CONTROL_RUN_TIME = KST.localize(datetime(2026, 9, 18, 6, 0, 1))
# 스레드 게이트 대기 상한 — 변이 아래서 멈춰도 시험이 끝나야 한다.
GATE_TIMEOUT_S = 5


def _fixed_now(value):
    """`datetime.now()` 만 고정한 datetime 대체. 나머지(fromisoformat 등)는 그대로 쓴다."""
    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return value if tz is None else value.astimezone(tz)
    return _Fixed


def _run_job_at(job, run_time, now):
    """APScheduler 의 실제 misfire 판정 함수를 `now` 시각에 부른다."""
    with patch.object(executor_base, "datetime", _fixed_now(now)):
        return executor_base.run_job(job, "default", [run_time], "test.control_job")


def _events(lines, name):
    out = []
    for line in lines:
        _, _, message = line.partition(":exchange_rate.scheduler:")
        try:
            payload = json.loads(message)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("event") == name:
            out.append(payload)
    return out


def _started(lines):
    return _events(lines, "mode_switch_started")


def _finished(lines):
    return _events(lines, "mode_switch_finished")


def _noop():
    return None


class TestControlJobMisfirePolicy(unittest.TestCase):
    """A — grace 30초는 관측된 지연을 통과시키지만, 초과 지연과 지나간 슬롯은 못 막는다."""

    def setUp(self):
        # 운영과 같은 스케줄러 종류. 기동하지 않고 Job 만 만든다.
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self.calls = []

    def _job(self, **opts):
        return self.scheduler.add_job(lambda: self.calls.append(1),
                                      CronTrigger(second="1", timezone=KST), **opts)

    def test_options_are_explicit(self):
        self.assertEqual(sched.CONTROL_JOB_OPTIONS,
                         {"misfire_grace_time": 30, "coalesce": True, "max_instances": 1})

    def test_scheduler_default_grace_is_one_second(self):
        """기준선 — grace 를 비우면 받는 값. 운영 컨테이너 실측과 같아야 한다."""
        self.assertEqual(sched.scheduler._job_defaults["misfire_grace_time"], 1)

    def test_default_grace_misses_the_observed_delay(self):
        """양성 대조 — 수정 전 조건(grace 1초)은 운영과 똑같이 건너뛴다."""
        events = _run_job_at(self._job(misfire_grace_time=1),
                             CONTROL_RUN_TIME, CONTROL_RUN_TIME + OBSERVED_DELAY)
        self.assertEqual([e.code for e in events], [EVENT_JOB_MISSED])
        self.assertEqual(self.calls, [], "건너뛰었으면 실행되지 않아야 한다")

    def test_explicit_grace_runs_the_observed_delay(self):
        events = _run_job_at(self._job(**sched.CONTROL_JOB_OPTIONS),
                             CONTROL_RUN_TIME, CONTROL_RUN_TIME + OBSERVED_DELAY)
        self.assertNotIn(EVENT_JOB_MISSED, [e.code for e in events])
        self.assertEqual(self.calls, [1])

    def test_explicit_grace_runs_a_delay_past_the_first_slot(self):
        """15초 — 실행은 된다. 첫 슬롯을 잃는지는 전환 시험(TestLateTransitionConsequences)이 본다."""
        events = _run_job_at(self._job(**sched.CONTROL_JOB_OPTIONS),
                             CONTROL_RUN_TIME, CONTROL_RUN_TIME + timedelta(seconds=15))
        self.assertNotIn(EVENT_JOB_MISSED, [e.code for e in events])
        self.assertEqual(self.calls, [1])

    def test_delay_beyond_grace_is_still_missed(self):
        """30초는 상한이다 — 넘으면 여전히 건너뛴다(다음 발화가 복구해야 한다)."""
        events = _run_job_at(self._job(**sched.CONTROL_JOB_OPTIONS),
                             CONTROL_RUN_TIME, CONTROL_RUN_TIME + timedelta(seconds=31))
        self.assertEqual([e.code for e in events], [EVENT_JOB_MISSED])
        self.assertEqual(self.calls, [])


class _TransitionCase(unittest.TestCase):
    """실제 switch_jobs 를 돌린다(스케줄러 미기동). 기존 test_switch_jobs_windows 와 같은 방식."""

    def setUp(self):
        self._saved_mode = sched.current_mode
        enabled = patch.object(sched.crawler_manager, "is_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def tearDown(self):
        for job in list(sched.scheduler.get_jobs()):
            if job.id.startswith("task_"):
                sched.scheduler.remove_job(job.id)
        sched.current_mode = self._saved_mode
        sched._switch_local.record = None

    def _control_at(self, when, current_mode, cause="scheduled_control"):
        """`when` 시각에 control_job 을 돌리고 종료 기록을 돌려준다(시작 기록은 self.lines)."""
        sched.current_mode = current_mode
        if current_mode:
            sched._switch_jobs_body(current_mode)   # 전환 전 상태를 기록 없이 만든다
        with patch.object(sched, "datetime", _fixed_now(when)):
            with self.assertLogs("exchange_rate.scheduler", level="INFO") as logs:
                sched.control_job(cause=cause)
        self.lines = logs.output
        return _finished(logs.output)

    def _woori_next_fire(self, after):
        job = sched.scheduler.get_job("task_woori")
        return job.trigger.get_next_fire_time(None, after)


class TestLateTransitionConsequences(_TransitionCase):
    """A 가 막는 것과 못 막는 것 — 전환 시각에 따라 우리은행 첫 슬롯이 살거나 죽는다."""

    def test_on_time_switch_keeps_first_slot(self):
        """관측된 지연(06:00:02.17)으로 전환되면 06:00:14 는 아직 앞에 있다."""
        when = CONTROL_RUN_TIME + OBSERVED_DELAY
        [record] = self._control_at(when, "BREAK1")
        self.assertEqual(sched.current_mode, "BREAK2")
        self.assertEqual(f"{self._woori_next_fire(when):%H:%M:%S}", "06:00:14")
        self.assertAlmostEqual(record["delay_from_boundary_s"], 2.170653, places=3)

    def test_switch_past_first_slot_loses_it(self):
        """⛔ 06:00:01 실행이 15초 늦으면(06:00:16) grace 로 실행돼도 첫 슬롯은 복원되지 않는다."""
        when = CONTROL_RUN_TIME + timedelta(seconds=15)
        [record] = self._control_at(when, "BREAK1")
        self.assertEqual(f"{self._woori_next_fire(when):%H:%M:%S}", "06:00:44")
        self.assertAlmostEqual(record["delay_from_boundary_s"], 16.0, places=3)

    def test_next_fire_recovers_after_a_miss(self):
        """06:00:01 을 통째로 놓쳐도 06:01:01 이 멱등하게 전환한다(오늘 실제로 일어난 일)."""
        when = BOUNDARY + timedelta(minutes=1, seconds=1)
        [record] = self._control_at(when, "BREAK1")
        self.assertEqual(sched.current_mode, "BREAK2")
        self.assertAlmostEqual(record["delay_from_boundary_s"], 61.0, places=3)
        self.assertEqual(f"{self._woori_next_fire(when):%H:%M:%S}", "06:01:14")

    def test_same_mode_does_not_switch(self):
        """모드가 같으면 전환도 기록도 없다."""
        sched.current_mode = "BREAK2"
        with patch.object(sched, "datetime", _fixed_now(BOUNDARY + timedelta(minutes=5))):
            with patch.object(sched, "switch_jobs") as switch:
                sched.control_job()
        switch.assert_not_called()


class TestTransitionRecord(_TransitionCase):
    """B — 전환 기록은 호출별로 남고, 전환 자체를 바꾸지 않는다."""

    def test_start_and_finish_share_the_switch_id(self):
        [finished] = self._control_at(CONTROL_RUN_TIME + OBSERVED_DELAY, "BREAK1")
        [started] = _started(self.lines)
        self.assertEqual(started["switch_id"], finished["switch_id"])
        self.assertLess(self.lines.index(next(line for line in self.lines
                                              if "mode_switch_started" in line)),
                        self.lines.index(next(line for line in self.lines
                                              if "mode_switch_finished" in line)))
        for record in (started, finished):
            self.assertEqual(record["cause"], "scheduled_control")
            self.assertEqual(record["requested_mode"], "BREAK2")
            self.assertEqual(record["observed_mode_at_start"], "BREAK1")
            self.assertEqual(record["policy_boundary"], BOUNDARY.isoformat())
            self.assertIn("task_woori", record["observed_jobs_before"])
        self.assertNotIn("operations", started, "작업은 종료 기록에 싣는다")
        self.assertEqual(finished["outcome"], "completed")
        self.assertIsNone(finished["exception_type"])
        self.assertTrue(finished["operations_complete"])
        self.assertIn("task_woori", finished["observed_jobs_after"])

    def test_operations_are_the_actual_removes_and_adds(self):
        """같은 ID 재등록도 작업으로 남는다 — 트리거가 같아도(kb), 달라도(woori)."""
        [record] = self._control_at(CONTROL_RUN_TIME + OBSERVED_DELAY, "BREAK1")
        ops = record["operations"]
        removes = [op for op in ops if op["op"] == "remove"]
        adds = [op for op in ops if op["op"] == "add"]
        self.assertEqual(len(removes) + len(adds), len(ops))
        # 본문 순서: 모두 지운 뒤 다시 넣는다.
        self.assertEqual([op["op"] for op in ops], ["remove"] * len(removes) + ["add"] * len(adds))
        self.assertEqual(sorted(op["job_id"] for op in removes),
                         sorted(j for j in record["observed_jobs_before"] if j.startswith("task_")))
        self.assertEqual(sorted(op["job_id"] for op in adds),
                         sorted(j for j in record["observed_jobs_after"] if j.startswith("task_")))
        by_id = {(op["op"], op["job_id"]): op for op in ops}
        self.assertIn("19-23,0-5", by_id[("remove", "task_woori")]["trigger"])     # BREAK1
        self.assertIn("minute='0-3'", by_id[("add", "task_woori")]["trigger"])    # BREAK2 마무리
        self.assertEqual(by_id[("remove", "task_kb")]["trigger"],
                         by_id[("add", "task_kb")]["trigger"],
                         "kb 는 두 모드 트리거가 같다 — 전역 차이로는 안 보이던 재등록")
        for op in ops:
            self.assertEqual(op["at"], (CONTROL_RUN_TIME + OBSERVED_DELAY).isoformat())
            self.assertNotIn("exception_type", op)

    def test_exception_mid_switch_is_not_recorded_as_completed(self):
        sched.current_mode = "BREAK1"
        with patch.object(sched, "_switch_jobs_body", side_effect=RuntimeError("boom")):
            with self.assertLogs("exchange_rate.scheduler", level="INFO") as logs:
                with self.assertRaises(RuntimeError):
                    sched.switch_jobs("BREAK2", cause="scheduled_control")
        [started] = _started(logs.output)
        [record] = _finished(logs.output)
        self.assertEqual(started["switch_id"], record["switch_id"])
        self.assertEqual(record["outcome"], "raised")
        self.assertEqual(record["exception_type"], "RuntimeError")
        self.assertIsNone(sched._switch_local.record, "예외 뒤에도 진행 중 기록을 비운다")

    def test_failed_registration_is_recorded_with_its_exception(self):
        """실제 본문에서 첫 등록이 실패 — 앞선 제거는 남고, 실패한 등록도 작업으로 남는다."""
        sched.current_mode = "BREAK1"
        sched._switch_jobs_body("BREAK1")
        with patch.object(sched.scheduler, "add_job", side_effect=RuntimeError("add")):
            with self.assertLogs("exchange_rate.scheduler", level="INFO") as logs:
                with self.assertRaises(RuntimeError):
                    sched.switch_jobs("BREAK2", cause="scheduled_control")
        [record] = _finished(logs.output)
        self.assertEqual(record["outcome"], "raised")
        *removes, failed = record["operations"]
        self.assertTrue(removes and all(op["op"] == "remove" for op in removes))
        self.assertEqual(failed["op"], "add")
        self.assertEqual(failed["exception_type"], "RuntimeError")
        self.assertIsNotNone(failed["trigger"])

    def test_startup_is_recorded_as_pending_without_a_boundary(self):
        """시작 경로는 scheduler.start() 전이다 — 등록 완료를 실행 가능으로 읽지 않는다."""
        self.assertFalse(sched.scheduler.running)
        [record] = self._control_at(CONTROL_RUN_TIME, None, cause="startup")
        [started] = _started(self.lines)
        for r in (started, record):
            self.assertEqual(r["cause"], "startup")
            self.assertFalse(r["scheduler_running"])
            self.assertIsNone(r["policy_boundary"], "시작 시 과거 모드 지속을 추정하지 않는다")
        self.assertNotIn("delay_from_boundary_s", record)
        self.assertTrue(any(op["op"] == "add" for op in record["operations"]))

    def test_duration_includes_the_start_snapshot(self):
        """시작 스냅샷이 5초 걸리면 호출 구간에 들어가야 한다(가짜 단조 시계)."""
        clock = [100.0]
        real_snapshot = sched._snapshot_job_ids
        calls = []

        def slow_first_snapshot():
            calls.append(1)
            if len(calls) == 1:
                clock[0] += 5.0
            return real_snapshot()

        with patch.object(sched, "_snapshot_job_ids", side_effect=slow_first_snapshot), \
                patch.object(sched.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertLogs("exchange_rate.scheduler", level="INFO") as logs:
                sched.switch_jobs("BREAK2", cause="scheduled_control")
        [record] = _finished(logs.output)
        self.assertEqual(len(calls), 2)
        self.assertEqual(record["duration_ms"], 5000.0)

    def test_toggle_and_transition_get_distinct_switch_ids(self):
        with self.assertLogs("exchange_rate.scheduler", level="INFO") as logs:
            sched.switch_jobs("BREAK2", cause="scheduled_control")
            sched.switch_jobs("BREAK2", cause="admin_toggle")
        first, second = _finished(logs.output)
        self.assertNotEqual(first["switch_id"], second["switch_id"])
        self.assertEqual([first["cause"], second["cause"]], ["scheduled_control", "admin_toggle"])

    def test_overlapping_calls_keep_their_own_operations(self):
        """A 가 본문 안에서 멈춘 사이 B 가 끝난다 — 작업은 각자, 전역 스냅샷은 섞인다."""
        a_inside, release = threading.Event(), threading.Event()
        errors = []

        def fake_body(mode):
            if mode == "A":
                sched._record_add_job(_noop, CronTrigger(second="5", timezone=KST), id="task_A1")
                a_inside.set()
                if not release.wait(GATE_TIMEOUT_S):
                    raise TimeoutError("release 가 오지 않았다")
                # B 가 끝나 자기 기록을 비운 뒤다 — 진행 중 기록이 스레드별이어야 여기가 A 에 남는다.
                sched._record_add_job(_noop, CronTrigger(second="8", timezone=KST), id="task_A2")
            else:
                sched._record_add_job(_noop, CronTrigger(second="6", timezone=KST), id="task_B")

        def run_a():
            try:
                sched.switch_jobs("A", cause="scheduled_control")
            except BaseException as exc:   # 시험 스레드의 예외를 본 스레드로 넘긴다
                errors.append(exc)

        with patch.object(sched, "_switch_jobs_body", side_effect=fake_body):
            with self.assertLogs("exchange_rate.scheduler", level="INFO") as logs:
                thread = threading.Thread(target=run_a)
                thread.start()
                self.assertTrue(a_inside.wait(GATE_TIMEOUT_S), "A 가 본문에 들어가야 한다")
                mid = list(logs.output)
                sched.switch_jobs("B", cause="admin_toggle")
                release.set()
                thread.join(GATE_TIMEOUT_S)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        # 본문 진행 중에도 A 의 존재가 이미 남아 있다.
        self.assertEqual([r["requested_mode"] for r in _started(mid)], ["A"])
        self.assertEqual(_finished(mid), [])
        records = {r["requested_mode"]: r for r in _finished(logs.output)}
        self.assertEqual([op["job_id"] for op in records["A"]["operations"]], ["task_A1", "task_A2"])
        self.assertEqual([op["job_id"] for op in records["B"]["operations"]], ["task_B"])
        # 보조 스냅샷은 다른 호출의 변경을 담는다 — 그래서 작업 기록으로 쓰지 않는다.
        self.assertIn("task_B", records["A"]["observed_jobs_after"])

    def test_start_recording_failure_does_not_block_the_transition(self):
        """시작 기록 실패 — 이 호출의 기록은 없고, 전환은 일어난다."""
        sched.current_mode = "BREAK1"
        sched._switch_jobs_body("BREAK1")
        with patch.object(sched, "_snapshot_job_ids", side_effect=RuntimeError("probe")):
            with patch.object(sched, "datetime", _fixed_now(CONTROL_RUN_TIME)):
                with self.assertLogs("exchange_rate.scheduler", level="INFO") as logs:
                    sched.control_job()
        self.assertEqual(sched.current_mode, "BREAK2")
        job = sched.scheduler.get_job("task_woori")
        self.assertIn("minute='0-3'", str(job.trigger), "BREAK2 마무리 트리거가 등록돼야 한다")
        self.assertEqual(_started(logs.output) + _finished(logs.output), [])
        self.assertTrue(any("mode_switch 기록 시작 실패" in line for line in logs.output))

    def test_end_recording_failure_does_not_raise_after_the_switch(self):
        """시작 기록은 성공하고 종료 기록만 실패 — 이미 끝난 전환을 예외로 되돌리지 않는다."""
        sched.current_mode = "BREAK1"
        sched._switch_jobs_body("BREAK1")
        real_snapshot = sched._snapshot_job_ids
        calls = []

        def snapshot_fails_second_time():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("probe")
            return real_snapshot()

        with patch.object(sched, "_snapshot_job_ids", side_effect=snapshot_fails_second_time):
            with patch.object(sched, "datetime", _fixed_now(CONTROL_RUN_TIME)):
                with self.assertLogs("exchange_rate.scheduler", level="INFO") as logs:
                    sched.control_job()
        self.assertEqual(len(calls), 2, "시작·종료 두 번 불려야 종료 실패 경로를 지난 것이다")
        self.assertEqual(sched.current_mode, "BREAK2")
        self.assertTrue(any("mode_switch 기록 종료 실패" in line for line in logs.output))
        self.assertEqual(len(_started(logs.output)), 1, "시작 기록은 이미 나갔다")
        self.assertEqual(_finished(logs.output), [])

    def test_operation_recording_failure_marks_the_record_incomplete(self):
        """작업 한 건의 기록이 실패해도 등록은 되고, 기록이 불완전하다는 사실이 남는다."""

        class UnprintableTrigger(BaseTrigger):
            def get_next_fire_time(self, previous_fire_time, now):
                return None

            def __str__(self):
                raise RuntimeError("str")

        def fake_body(mode):
            sched._record_add_job(_noop, UnprintableTrigger(), id="task_unprintable")
            sched._record_add_job(_noop, CronTrigger(second="7", timezone=KST), id="task_fine")

        with patch.object(sched, "_switch_jobs_body", side_effect=fake_body):
            with self.assertLogs("exchange_rate.scheduler", level="INFO") as logs:
                sched.switch_jobs("X", cause="scheduled_control")
        [record] = _finished(logs.output)
        self.assertFalse(record["operations_complete"])
        self.assertEqual([op["job_id"] for op in record["operations"]], ["task_fine"])
        self.assertIsNotNone(sched.scheduler.get_job("task_unprintable"), "등록은 막지 않는다")

    def test_admin_toggle_path_records_its_cause(self):
        """실제 토글 함수(`toggle_crawler`)가 admin_toggle 로 기록한다 — DB 접근만 막는다."""
        sched.current_mode = "BREAK2"
        saved_cache = dict(sched.crawler_manager.config_cache)
        self.addCleanup(lambda: (sched.crawler_manager.config_cache.clear(),
                                 sched.crawler_manager.config_cache.update(saved_cache)))
        with patch.object(sched, "SessionLocal"), \
                patch.object(sched.crud, "update_crawler_config"):
            with self.assertLogs("exchange_rate.scheduler", level="INFO") as logs:
                sched.crawler_manager.toggle_crawler("woori", True)
        [record] = _finished(logs.output)
        self.assertEqual(record["cause"], "admin_toggle")
        self.assertIsNone(record["policy_boundary"])


class TestRecordingHelpers(unittest.TestCase):
    """본문 헬퍼는 스케줄러 호출을 그대로 통과시킨다 — 기록 중이 아닐 때도."""

    def tearDown(self):
        for job_id in ("task_passthrough",):
            if sched.scheduler.get_job(job_id):
                sched.scheduler.remove_job(job_id)
        sched._switch_local.record = None

    def test_add_passes_arguments_and_returns_the_job(self):
        sentinel = object()
        with patch.object(sched.scheduler, "add_job", return_value=sentinel) as add:
            result = sched._record_add_job(_noop, "trig", id="task_x", max_instances=1)
        self.assertIs(result, sentinel)
        add.assert_called_once_with(_noop, "trig", id="task_x", max_instances=1)

    def test_outside_a_switch_nothing_is_recorded(self):
        self.assertIsNone(getattr(sched._switch_local, "record", None))
        job = sched._record_add_job(_noop, CronTrigger(second="9", timezone=KST),
                                    id="task_passthrough")
        self.assertEqual(job.id, "task_passthrough")
        sched._record_remove_job(job)
        self.assertIsNone(sched.scheduler.get_job("task_passthrough"))


class TestWiring(unittest.TestCase):
    """정책 상수가 실제 등록부에 들어가는지, 모든 호출처가 원인을 밝히는지 — 구조로 잠근다."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(inspect.getsource(sched))

    def _calls(self, name, tree=None):
        for node in ast.walk(tree or self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if called == name:
                    yield node

    def _function(self, name):
        return next(n for n in ast.walk(self.tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    def test_control_job_registration_uses_the_policy(self):
        [call] = [c for c in self._calls("add_job")
                  if c.args and isinstance(c.args[0], ast.Name) and c.args[0].id == "control_job"]
        spread = [k for k in call.keywords if k.arg is None]
        self.assertEqual(len(spread), 1)
        self.assertEqual(spread[0].value.id, "CONTROL_JOB_OPTIONS")
        overridden = {k.arg for k in call.keywords} & set(sched.CONTROL_JOB_OPTIONS)
        self.assertEqual(overridden, set(), "정책 키를 등록부에서 따로 덮지 않는다")

    def test_every_switch_call_names_its_cause(self):
        calls = list(self._calls("switch_jobs"))
        self.assertGreaterEqual(len(calls), 2, "양성 대조 — 호출처(control_job·토글)를 찾아야 한다")
        for call in calls:
            named = [k for k in call.keywords if k.arg == "cause"]
            self.assertTrue(named, f"{call.lineno}행 switch_jobs 가 cause 를 밝히지 않는다")
            for k in named:
                if isinstance(k.value, ast.Constant):
                    self.assertIn(k.value.value, {"scheduled_control", "startup", "admin_toggle"})

    def test_body_is_called_only_through_the_wrapper(self):
        callers = [fn.name for fn in ast.walk(self.tree) if isinstance(fn, ast.FunctionDef)
                   for node in ast.walk(fn)
                   if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_switch_jobs_body"]
        self.assertEqual(callers, ["switch_jobs"])

    def test_body_changes_jobs_only_through_the_recording_helpers(self):
        body = self._function("_switch_jobs_body")
        self.assertEqual(list(self._calls("add_job", body)), [], "직접 add_job 은 기록을 건너뛴다")
        self.assertEqual(list(self._calls("remove_job", body)), [])
        self.assertTrue(list(self._calls("_record_add_job", body)), "양성 대조")
        self.assertTrue(list(self._calls("_record_remove_job", body)), "양성 대조")


class TestPolicyBoundary(unittest.TestCase):
    def test_break2_starts_at_six(self):
        self.assertEqual(sched._policy_boundary(CONTROL_RUN_TIME, "BREAK2"), BOUNDARY)

    def test_mid_mode_still_finds_the_start(self):
        self.assertEqual(sched._policy_boundary(BOUNDARY + timedelta(hours=1, minutes=30),
                                                "BREAK2"), BOUNDARY)

    def test_monday_morning_after_the_weekend(self):
        monday = KST.localize(datetime(2026, 9, 21, 6, 0, 5))
        self.assertEqual(sched._policy_boundary(monday, "BREAK2"),
                         KST.localize(datetime(2026, 9, 21, 6, 0, 0)))

    def test_long_weekend_mode_is_within_the_lookback(self):
        """OUT 은 47시간이다 — 일요일 오후에서도 토요일 07:00 을 찾는다."""
        sunday = KST.localize(datetime(2026, 9, 20, 15, 0, 0))
        self.assertEqual(sched._policy_boundary(sunday, "OUT"),
                         KST.localize(datetime(2026, 9, 19, 7, 0, 0)))

    def test_mode_mismatch_returns_none(self):
        self.assertIsNone(sched._policy_boundary(CONTROL_RUN_TIME, "IN"))


if __name__ == "__main__":
    unittest.main()
