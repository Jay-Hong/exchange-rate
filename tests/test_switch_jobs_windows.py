"""switch_jobs() 실제 등록 검증 — 은행별 수집 창 (ADR-042, 2026-08-28).

market_mode 테스트는 "모드 문자열"만 잠근다. 사용자가 실제로 받는 것은 **등록된 job과 그
발화 시각**이므로 여기서 그걸 직접 잠근다. 이 리포에는 그동안 switch_jobs를 실행하는
테스트가 없었다 (scheduler 스케줄 회귀를 잡는 장치가 사실상 0이었다).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from apscheduler.triggers.combining import OrTrigger
from pytz import timezone

import app.scheduler as sched

KST = timezone("Asia/Seoul")


def _sentinel_job():
    """모드와 무관한 job 보존 테스트용 no-op."""


def _fires(job, start, end):
    """[start, end] 구간의 실제 발화 시각 목록."""
    out, prev, cur = [], None, start
    for _ in range(5000):
        nxt = job.trigger.get_next_fire_time(prev, cur)
        if nxt is None or nxt > end:
            break
        out.append(nxt)
        prev, cur = nxt, nxt + timedelta(microseconds=1)
    return out


class _SwitchJobsCase(unittest.TestCase):
    """switch_jobs를 실제로 돌려 등록 job을 얻는다 (scheduler는 미기동 상태로 사용)."""

    enabled = True

    def _jobs(self, mode):
        with patch.object(sched.crawler_manager, "is_enabled", return_value=self.enabled):
            sched.switch_jobs(mode)
        return {j.id: j for j in sched.scheduler.get_jobs() if j.id.startswith("task_")}

    def tearDown(self):
        for job in list(sched.scheduler.get_jobs()):
            if job.id.startswith("task_"):
                sched.scheduler.remove_job(job.id)


class TestBankCollectionWindows(_SwitchJobsCase):
    def test_sc_registered_only_in_in_mode_and_ends_1859(self):
        """sc의 종료는 트리거가 아니라 **모드 등록 창**이 결정한다.

        트리거 자체는 `minute='*', second='58'`이라 24시간 발화 가능하다. 실제 마지막 실행이
        18:59:58인 것은 IN 모드가 19:00에 끝나면서 switch_jobs가 job을 제거하기 때문이다.
        따라서 IN 모드 구간으로 창을 잘라서 검증한다.
        """
        jobs = self._jobs("IN")
        self.assertIn("task_sc", jobs)
        # IN 모드 구간(화요일 08:00 ~ 18:59:59.999) 안에서의 발화
        fires = _fires(jobs["task_sc"],
                       KST.localize(datetime(2026, 8, 25, 8, 0, 0)),
                       KST.localize(datetime(2026, 8, 25, 18, 59, 59, 999999)))
        self.assertEqual(f"{fires[-1]:%H:%M:%S}", "18:59:58")
        self.assertEqual(len(fires), 11 * 60, "IN 모드 11시간 × 매분")

        for mode in ("BREAK1", "BREAK2", "OUT"):
            self.assertNotIn("task_sc", self._jobs(mode), f"{mode}에 sc가 등록되면 안 된다")

    def test_in_mode_boundary_is_what_stops_sc(self):
        """모드 경계가 19:00임을 market_mode 쪽에서 재확인 (위 창의 근거)."""
        from app.market_mode import get_market_mode
        self.assertEqual(get_market_mode(KST.localize(datetime(2026, 8, 25, 18, 59))), "IN")
        self.assertEqual(get_market_mode(KST.localize(datetime(2026, 8, 25, 19, 0))), "BREAK1")

    def test_shinhan_break1_last_fire_is_025918(self):
        jobs = self._jobs("BREAK1")
        fires = _fires(jobs["task_shinhan"],
                       KST.localize(datetime(2026, 8, 25, 19, 0, 1)),
                       KST.localize(datetime(2026, 8, 26, 6, 0, 1)))
        self.assertEqual(f"{fires[0]:%H:%M:%S}", "19:00:18")
        self.assertEqual(f"{fires[-1]:%H:%M:%S}", "02:59:18")

    def test_woori_is_one_job_and_runs_through_050453(self):
        jobs = self._jobs("BREAK1")
        # ⛔ tail을 별도 job으로 나누면 max_instances=1이 배타가 아니게 된다 (Chrome 2개 위험).
        self.assertNotIn("task_woori_tail", jobs,
                         "woori는 OrTrigger 단일 job이어야 한다 (max_instances=1 전 구간 적용)")
        self.assertIsInstance(jobs["task_woori"].trigger, OrTrigger)
        self.assertEqual(jobs["task_woori"].max_instances, 1)
        self.assertEqual(jobs["task_woori"].func.__name__, "request_wrapper_woori")
        fires = _fires(jobs["task_woori"],
                       KST.localize(datetime(2026, 8, 25, 19, 0, 1)),
                       KST.localize(datetime(2026, 8, 26, 6, 0, 1)))
        self.assertEqual(f"{fires[0]:%H:%M:%S}", "19:00:53")
        self.assertEqual(f"{fires[-1]:%H:%M:%S}", "05:04:53")
        # 04:59:53 → 05:00:53 경계가 끊기지 않는다
        stamps = {f"{f:%H:%M:%S}" for f in fires}
        for s in ("04:59:53", "05:00:53", "05:01:53", "05:04:53"):
            self.assertIn(s, stamps)
        self.assertNotIn("05:05:53", stamps)

    def test_ibk_break1_last_fire_is_055934(self):
        jobs = self._jobs("BREAK1")
        fires = _fires(jobs["task_ibk"],
                       KST.localize(datetime(2026, 8, 25, 19, 0, 1)),
                       KST.localize(datetime(2026, 8, 26, 6, 0, 1)))
        self.assertEqual(f"{fires[-1]:%H:%M:%S}", "05:59:34")

    def test_ibk_terminal_fires_twice_tue_to_sat_only(self):
        jobs = self._jobs("BREAK2")
        self.assertIn("task_ibk_terminal", jobs)
        self.assertNotIn("task_ibk", jobs, "BREAK2에는 정규 ibk job이 없어야 한다")
        self.assertEqual(jobs["task_ibk_terminal"].max_instances, 1)
        self.assertEqual(jobs["task_ibk_terminal"].func.__name__, "selenium_wrapper_ibk")
        fires = _fires(jobs["task_ibk_terminal"],
                       KST.localize(datetime(2026, 8, 24, 0, 0, 0)),    # 월
                       KST.localize(datetime(2026, 8, 31, 12, 0, 0)))   # 다음 월
        stamps = [f"{f:%a %H:%M:%S}" for f in fires]
        self.assertEqual(stamps, [
            "Tue 06:00:34", "Tue 06:01:34", "Wed 06:00:34", "Wed 06:01:34",
            "Thu 06:00:34", "Thu 06:01:34", "Fri 06:00:34", "Fri 06:01:34",
            "Sat 06:00:34", "Sat 06:01:34",
        ])
        self.assertEqual([f for f in fires if f.weekday() in (0, 6)], [],
                         "월·일에는 발화하면 안 된다 (그 앞에 IBK 세션이 없다)")

    def test_shinhan_and_woori_absent_in_break2(self):
        jobs = self._jobs("BREAK2")
        for jid in ("task_shinhan", "task_woori"):
            self.assertNotIn(jid, jobs)


class TestModeTransitionRemovesWindowJobs(_SwitchJobsCase):
    def test_ibk_terminal_removed_when_leaving_break2(self):
        self.assertIn("task_ibk_terminal", self._jobs("BREAK2"))
        self.assertNotIn("task_ibk_terminal", self._jobs("IN"))
        self.assertNotIn("task_ibk_terminal", self._jobs("BREAK1"))
        self.assertNotIn("task_ibk_terminal", self._jobs("OUT"))

    def test_woori_removed_when_leaving_break1(self):
        self.assertIn("task_woori", self._jobs("BREAK1"))
        self.assertNotIn("task_woori", self._jobs("BREAK2"))
        self.assertNotIn("task_woori", self._jobs("OUT"))

    def test_mode_agnostic_job_survives_task_cleanup(self):
        sentinel_id = "mode_agnostic_test_sentinel"
        sched.scheduler.add_job(
            _sentinel_job,
            sched.CronTrigger(minute="*", timezone=KST),
            id=sentinel_id,
            replace_existing=True,
        )
        try:
            self._jobs("BREAK2")
            self.assertIsNotNone(sched.scheduler.get_job(sentinel_id))
        finally:
            if sched.scheduler.get_job(sentinel_id) is not None:
                sched.scheduler.remove_job(sentinel_id)


class TestDisabledCrawlerIsNotRegistered(_SwitchJobsCase):
    enabled = False

    def test_no_task_jobs_when_all_disabled(self):
        for mode in ("IN", "BREAK1", "BREAK2", "OUT"):
            self.assertEqual(self._jobs(mode), {}, f"{mode}: 비활성 시 등록 0이어야 한다")

    def test_ibk_terminal_respects_disabled_gate(self):
        self.assertNotIn("task_ibk_terminal", self._jobs("BREAK2"))


class TestSelectiveCrawlerDisable(_SwitchJobsCase):
    def test_disabling_only_ibk_removes_terminal_but_keeps_other_break2_jobs(self):
        with patch.object(
            sched.crawler_manager,
            "is_enabled",
            side_effect=lambda crawler: crawler != "ibk",
        ):
            sched.switch_jobs("BREAK2")
        jobs = {j.id: j for j in sched.scheduler.get_jobs() if j.id.startswith("task_")}
        self.assertNotIn("task_ibk_terminal", jobs)
        self.assertEqual(
            set(jobs),
            {"task_investing", "task_dxy", "task_kb", "task_hana",
             "task_bs", "task_citi", "task_nh"},
        )


class TestControlJobWiring(_SwitchJobsCase):
    def _run_control_at(self, now, *, current_mode):
        previous_mode = sched.current_mode
        try:
            sched.current_mode = current_mode
            with patch.object(sched, "datetime") as mock_datetime, \
                 patch.object(sched.crawler_manager, "is_enabled", return_value=True):
                mock_datetime.now.return_value = now
                sched.control_job()
            return {
                job.id: job for job in sched.scheduler.get_jobs()
                if job.id.startswith("task_")
            }
        finally:
            self.addCleanup(setattr, sched, "current_mode", previous_mode)

    def test_control_job_performs_the_1900_transition(self):
        jobs = self._run_control_at(
            KST.localize(datetime(2026, 8, 25, 19, 0, 1)),
            current_mode="IN",
        )
        self.assertEqual(sched.current_mode, "BREAK1")
        self.assertNotIn("task_sc", jobs)
        self.assertIn("task_woori", jobs)

    def test_restart_at_060020_registers_terminal_for_060034(self):
        now = KST.localize(datetime(2026, 8, 27, 6, 0, 20))  # 목요일
        jobs = self._run_control_at(now, current_mode=None)
        self.assertEqual(sched.current_mode, "BREAK2")
        terminal = jobs["task_ibk_terminal"]
        fires = _fires(terminal, now, KST.localize(datetime(2026, 8, 27, 6, 1, 40)))
        self.assertEqual([f"{fire:%H:%M:%S}" for fire in fires], ["06:00:34", "06:01:34"])


if __name__ == "__main__":
    unittest.main()
