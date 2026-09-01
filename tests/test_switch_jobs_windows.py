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

    def test_woori_in_fires_every_30_seconds_at_14_and_44(self):
        """배포 2 본단계의 우리은행 IN 30초 레인을 잠근다."""
        jobs = self._jobs("IN")
        job = jobs["task_woori"]
        self.assertEqual(job.max_instances, 1)
        self.assertEqual(job.misfire_grace_time, 30)
        self.assertEqual(job.func.__name__, "request_wrapper_woori")
        fires = _fires(
            job,
            KST.localize(datetime(2026, 9, 1, 10, 0, 0)),
            KST.localize(datetime(2026, 9, 1, 10, 1, 59)),
        )
        self.assertEqual(
            [f"{fire:%H:%M:%S}" for fire in fires],
            ["10:00:14", "10:00:44", "10:01:14", "10:01:44"],
        )

    def test_kb_hana_in_fire_every_10_seconds_and_other_modes_are_unchanged(self):
        """KB·하나 10초 상향은 IN에만 적용한다."""
        cases = {
            "IN": {
                "start": KST.localize(datetime(2026, 9, 1, 10, 0, 0)),
                "kb": [9, 19, 29, 39, 49, 59],
                "hana": [2, 12, 22, 32, 42, 52],
            },
            "BREAK1": {
                "start": KST.localize(datetime(2026, 9, 1, 20, 0, 0)),
                "kb": [15, 35, 55],
                "hana": [5, 25, 45],
            },
            "BREAK2": {
                "start": KST.localize(datetime(2026, 9, 2, 7, 0, 0)),
                "kb": [15, 35, 55],
                "hana": [5, 25, 45],
            },
            "OUT": {
                "start": KST.localize(datetime(2026, 9, 5, 10, 0, 0)),
                "kb": [28],
                "hana": [38],
            },
        }
        for mode, expected in cases.items():
            jobs = self._jobs(mode)
            end = expected["start"] + timedelta(seconds=59)
            for bank in ("kb", "hana"):
                job = jobs[f"task_{bank}"]
                fires = _fires(job, expected["start"], end)
                self.assertEqual(
                    [fire.second for fire in fires],
                    expected[bank],
                    f"{mode}/{bank}: 모드별 주기가 달라졌다",
                )
                self.assertEqual(job.max_instances, 1)
                self.assertEqual(job.misfire_grace_time, 10 if mode != "OUT" else 30)

    def test_in_canary_lanes_do_not_share_crawler_start_seconds(self):
        """세 대상과 나머지 IN 크롤러는 같은 시작초를 쓰지 않는다."""
        jobs = self._jobs("IN")
        start = KST.localize(datetime(2026, 9, 1, 10, 0, 0))
        end = start + timedelta(seconds=59)
        seconds_by_job = {
            job_id: {fire.second for fire in _fires(job, start, end)}
            for job_id, job in jobs.items()
        }
        target_ids = {"task_kb", "task_hana", "task_woori"}
        target_seconds = set()
        for job_id in target_ids:
            self.assertEqual(
                target_seconds & seconds_by_job[job_id],
                set(),
                f"{job_id}: 대상 은행끼리 같은 시작초를 쓰면 안 된다",
            )
            target_seconds |= seconds_by_job[job_id]

        other_seconds = set().union(
            *(seconds for job_id, seconds in seconds_by_job.items() if job_id not in target_ids)
        )
        self.assertEqual(
            target_seconds & other_seconds,
            set(),
            "IN의 다른 크롤러와 같은 시작초를 쓰면 안 된다",
        )

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


class TestOutModeSchedule(_SwitchJobsCase):
    """OUT(주말) 6 소스 1분 주기 + 초 분산 (배포 2A, 2026-08-28).

    ⚠️ 이번 주말이 '1분 주기가 실제로 수확을 늘리는가'의 실험이다 —
    최근 30일 OUT 구간 변경행은 hana 18 / shinhan 6 / investing 1 / kb·bs·nh 0이었고,
    bs·nh·shinhan은 60분 폴링이라 표본이 성겼다. 다음 주에 재측정해 유지/축소를 정한다.
    """

    EXPECTED_SECONDS = {
        "task_investing": 8,
        "task_nh": 10,         # Selenium enqueue
        "task_dxy": 21,        # 구 :15 — KB 뉴스(*/5분 :15) 충돌 제거 (주기는 1분 유지)
        "task_kb": 28,
        "task_shinhan": 30,    # Selenium enqueue, nh와 20초 간격
        "task_hana": 38,
        "task_bs": 51,
    }
    EXPECTED_GRACE_SECONDS = {
        "task_investing": 30,
        "task_nh": 30,
        "task_dxy": 120,  # 주기 변경 없음: 기존 최신 catch-up 정책 유지
        "task_kb": 30,
        "task_shinhan": 30,
        "task_hana": 30,
        "task_bs": 30,
    }
    # 회피해야 하는 초 (모드 무관 job들이 점유)
    RESERVED_SECONDS = {
        0,   # 분 경계 job (dxy rollup hourly 매시 :05:00 / daily 00:05:00)
        1,   # control_job · 일일 cleanup 5종(03:20~03:32)
        3, 23, 43,  # chrome cleanup + worker health (+ :03 graph_cache_refresh)
        12,  # graph v2 intraday precompute (*/10분)
        15,  # KB 뉴스 (*/5분)
        19,  # free snapshot (매시 :30)
        45,  # RSS 뉴스 (*/5분)
    }
    # ⚠️ 평시 미등록이지만 USDT_LEGACY_REST_POLLING_ENABLED=true 롤백 시 5-거래소 fan-out이
    #    이 초에 몰린다. 롤백 상황에서도 안전하도록 미리 피한다.
    USDT_LEGACY_SECONDS = {6, 16, 26, 36, 46, 56}

    def test_out_registers_exactly_seven_sources(self):
        jobs = self._jobs("OUT")
        self.assertEqual(set(jobs), set(self.EXPECTED_SECONDS))

    def test_every_out_job_fires_once_per_minute(self):
        jobs = self._jobs("OUT")
        start = KST.localize(datetime(2026, 8, 29, 10, 0, 0))   # 토요일 = OUT
        for jid in self.EXPECTED_SECONDS:
            fires = _fires(jobs[jid], start, start + timedelta(minutes=10))
            self.assertEqual(len(fires), 10, f"{jid}: 10분간 10회여야 한다")

    def test_out_start_seconds_do_not_collide(self):
        """broadcast를 제외한 열거된 고정 cron과 같은 시작초를 쓰지 않는다.

        broadcast는 의도대로 매초 발화한다. 실행시간은 겹칠 수 있고, IntervalTrigger
        job(queue_status·monitoring_stats 등)은 프로세스 기동 시각에 묶여 격자 밖이다.
        """
        jobs = self._jobs("OUT")
        start = KST.localize(datetime(2026, 8, 29, 10, 0, 0))
        actual = {}
        for jid in self.EXPECTED_SECONDS:
            f = _fires(jobs[jid], start, start + timedelta(minutes=2))
            actual[jid] = f[0].second
        self.assertEqual(actual, self.EXPECTED_SECONDS)
        secs = list(actual.values())
        self.assertEqual(len(secs), len(set(secs)), "OUT 초가 서로 겹치면 안 된다")
        self.assertEqual(set(secs) & self.RESERVED_SECONDS, set(),
                         "모드 무관 job이 점유한 초와 겹치면 안 된다")
        self.assertEqual(set(secs) & self.USDT_LEGACY_SECONDS, set(),
                         "USDT legacy polling 롤백 시 fan-out과 겹치면 안 된다")

    def test_selenium_enqueues_are_spaced(self):
        """nh·shinhan subprocess 버스트(~3-5초)가 이어붙지 않도록 간격 확보."""
        raw_gap = abs(self.EXPECTED_SECONDS["task_shinhan"] - self.EXPECTED_SECONDS["task_nh"])
        gap = min(raw_gap, 60 - raw_gap)
        self.assertGreaterEqual(gap, 15, "Selenium enqueue 두 개는 15초 이상 벌린다")

    def test_out_jobs_have_period_appropriate_misfire_and_single_instance(self):
        """주기 변경 6개는 load-shedding, DXY는 기존 catch-up 정책을 유지한다."""
        jobs = self._jobs("OUT")
        for jid in self.EXPECTED_SECONDS:
            job = jobs[jid]
            self.assertEqual(job.max_instances, 1, f"{jid}: max_instances")
            self.assertEqual(job.misfire_grace_time, self.EXPECTED_GRACE_SECONDS[jid],
                             f"{jid}: misfire grace 정책 변경")

    def test_out_excludes_woori_ibk_sc_citi(self):
        jobs = self._jobs("OUT")
        for jid in ("task_woori", "task_ibk", "task_ibk_terminal", "task_sc", "task_citi"):
            self.assertNotIn(jid, jobs)


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
