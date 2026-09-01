# app/scheduler.py

# 표준 라이브러리
import asyncio
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any, Callable, Dict, Optional

# 서드파티 라이브러리
from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone

# 로컬 애플리케이션
from app import models
from app.crawlers import investing
from app.crawlers import dxy_spot
from app.crawlers import hana
from app.crawlers import kb
from app.crawlers import woori
from app.crawlers import bs
from app.crawlers import citi
# Selenium 크롤러(shinhan, ibk, nh, sc)는 subprocess로 실행되므로 import 불필요
from app.crawlers.constants import SELENIUM_PRIORITY_MAP, SELENIUM_TIMEOUT_MAP
from app import config, crud
from app.config import LATEST_MIRROR_INTERVAL_SECONDS, REDIS_LATEST_ENABLED
from app.database import SessionLocal
from app.admin.crawler_stats import crawler_stats
from app.market_mode import get_market_mode

# 로거 설정
logger = logging.getLogger("exchange_rate.scheduler")

# 한국 시간대 스케줄러 인스턴스 생성
KST = timezone('Asia/Seoul')
scheduler = AsyncIOScheduler(timezone=KST)


def _scheduler_job_skip_listener(event) -> None:
    """APScheduler가 wrapper 실행 전에 버린 작업을 구조화 로그로 남긴다.

    crawler_stats는 실제로 wrapper가 호출된 작업만 볼 수 있으므로 max_instances와
    misfire는 별도 이벤트 표면이 필요하다. 운영 smoke는 메시지 문구가 아니라
    ``scheduler_event`` 필드의 델타를 집계한다.
    """
    if event.code == EVENT_JOB_MAX_INSTANCES:
        scheduled_run_times = [
            value.isoformat() for value in getattr(event, "scheduled_run_times", ())
        ]
        logger.warning(
            "⏭️ APScheduler job skipped: max_instances",
            extra={
                "scheduler_event": "max_instances",
                "job_id": event.job_id,
                "scheduled_run_times": scheduled_run_times,
                "occurrence_count": len(scheduled_run_times),
            },
        )
        return

    if event.code == EVENT_JOB_MISSED:
        scheduled_run_time = getattr(event, "scheduled_run_time", None)
        logger.warning(
            "⏰ APScheduler job missed: misfire",
            extra={
                "scheduler_event": "misfire",
                "job_id": event.job_id,
                "scheduled_run_time": (
                    scheduled_run_time.isoformat() if scheduled_run_time else None
                ),
                "occurrence_count": 1,
            },
        )


SCHEDULER_SKIP_EVENT_MASK = EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED
scheduler.add_listener(_scheduler_job_skip_listener, SCHEDULER_SKIP_EVENT_MASK)

BANK_RETENTION_DAYS = 30
SOURCE_RATE_RETENTION_DAYS = 30
MARKET_INDEX_RETENTION_DAYS = 30

# ═════════════════════════════════════════════════════════════
# AsyncIO PriorityQueue 기반 Selenium 크롤러 순차 실행 시스템
# ═════════════════════════════════════════════════════════════
# v1 (2025-11-06): Queue로 Semaphore 경합 제거 → 메모리 안정화
# v2 (2025-11-08): PriorityQueue + 타임아웃 → 느린 크롤러 격리
# v3 (2025-11-10): Worker 헬스체크 + 자동 재시작 → 무한 블로킹 방지
# - 우선순위: 빠른 크롤러(hana) → 느린 크롤러(shinhan)
# - 타임아웃: 각 크롤러별 타임아웃 설정 (60~120초)
# - 재시도: 실패 시 낮은 우선순위로 1회 재시도
# - 헬스체크: 3분 이상 같은 작업 처리 시 Worker 강제 재시작
# ─────────────────────────────────────────────────────────────
selenium_queue: asyncio.PriorityQueue = None
selenium_worker_task: asyncio.Task = None
selenium_worker_last_heartbeat: float = time.time()
selenium_worker_current_job: str = None

# ═════════════════════════════════════════════════════════════
# Queue 상태 캐시 (admin 페이지용)
# ═════════════════════════════════════════════════════════════
# Thread-safe: dict.update()는 GIL에 의해 atomic 보장
# 읽기: admin API (GET /admin/api/queue-status)
# 쓰기: report_queue_status() - 10초마다 1회 (line 688)
# ─────────────────────────────────────────────────────────────
queue_status_cache = {
    "size": 0,
    "max_size": 25,
    "usage_percent": 0.0,
    "current_job": None,
    "waiting_jobs": [],
    "updated_at": None
}

# ═════════════════════════════════════════════════════════════
# 크롤러 그룹 정의 (2025-11-16 재설계 - 4단계 모드)
# ═════════════════════════════════════════════════════════════
# 환율 고시 스케줄 기반 최적화 + 시스템 부하 분산
#
# 모드 분류:
#   - IN: 월~금 08:00~18:59 (영업시간, 전체 크롤러 활성)
#   - BREAK1: 월~금 19:00 ~ 익일 05:59 (야간, sc 제외 + 은행별 cutoff)
#   - BREAK2: 06:00~07:59 (고시 마무리, woori/shinhan/sc 제외 + ibk terminal만)
#   - OUT: 토 07:00 ~ 월 05:59 (주말, 7개 크롤러: 은행 5 + investing/dxy)
#
#   ⚠️ 2026-08-28 (ADR-042): BREAK1 시작 21:00→19:00, 종료 03:00→06:00.
#      DXY fallback 정책은 이 경계를 따라가지 않는다 — market_mode.get_dxy_policy_state() 참조.
#
# 각 은행 환율 고시 스케줄 (실제 운영 시간):
#   - investing: 월 06:00 ~ 토 06:00 (외환 시장 글로벌 운영)
#   - kb: 평일 08:30 ~ 익일(토 포함) 05:00
#   - hana: 평일 08:30 ~ 익일(토 포함) 06:00 (주말 중 가끔 변동)
#   - shinhan: 평일 08:19 ~ 익일 02:45 (주말 중 가끔 변동) → 수집 02:59:18까지
#   - woori: 평일 08:30 ~ 익일 05:00 (24시간 외환시장 전환으로 연장) → 수집 05:04:53까지
#   - ibk: 평일 약 08:26~08:30 ~ 익일 06:00 (연장, 05:59:55 관측) → BREAK1 05:59:34 + BREAK2 terminal 2회
#   - nh: 평일 08:40 ~ 당일 24:00 (자정 이후/주말 가끔 고시)
#   - sc: 평일 09:00 ~ 당일 포착 변경 약 17:50까지 → 수집 18:59:58까지
#   - bs: 평일 08:10 ~ 당일 24:00 (일요일 넘어갈 때 가끔 고시)
#   - citi: 평일 09:00 ~ 익일(토 포함) 06:00
#
# A Group: investing + dxy_spot (순수 Request, 둘 다 4모드 전부 등록)
#   - 가장 중요한 기준 환율
#   - IN/BREAK1/BREAK2: 10초마다 (고정 초 레인으로 부하 분산)
#   - OUT: investing·dxy_spot 모두 매분 (배포 2A — investing :08, dxy :21)
#
# B Group: Request 기반 (일부 하이브리드 폴백)
#   - kb, hana: 중요, 빈도 높음
#     - IN: 10초마다 (kb :09/:19/:29/:39/:49/:59,
#                     hana :02/:12/:22/:32/:42/:52)
#     - BREAK1/BREAK2: 기존 20초마다 유지 (kb :15/:35/:55, hana :05/:25/:45)
#     - OUT: 1분마다 (kb :28, hana :38) — 배포 2A
#   - woori, bs, citi: 일반, 빈도 낮음
#     - IN: woori 30초마다(:14/:44), bs·citi 60초마다(bs :33 / citi :13)
#       * 배포 2 본단계는 세 은행을 IN에서만 함께 상향. 다른 모드는 불변
#     - BREAK1: woori(53초), bs(33초), citi(13초) 유지
#     - BREAK2: bs(33초), citi(13초)만 유지 (woori는 05:05 수집 종료)
#     - OUT: bs도 1분마다 (:51) — 배포 2A
#   - 하이브리드: hana, woori는 Request → Selenium 폴백. bs/citi는 순수 Request
#
# C Group: subprocess Queue 순차 처리 (Request 우선 / Selenium 안전망)
#   - shinhan, ibk, nh, sc
#   - 시스템 부하 감소 전략: Request → Selenium 폴백 순서
#     * shinhan, nh, sc: 항상 Request(mibank) 먼저 시도 (실패 시 Selenium 폴백)
#     * ibk: 08:00 이후는 조회 당일 GET → 실패 시 공식 날짜 지정 POST.
#            08:00 전은 당일 GET을 생략하고 전 조회기준일 POST로 바로 시작한다
#            (00시 이후 고시가 전 조회기준일 화면에 누적 → 정상 경로는 Chrome 미기동).
#            응답 이상일 때만 Selenium 3회 → 조건부 mibank(**4차**, 평일 10:00~23:59).
#   - IN: 매분 cron (shinhan: 18초, ibk: 34초, nh: 54초, sc: 58초)
#   - BREAK1: shinhan(18초, ~02:59:18), ibk(34초, ~05:59:34), nh(54초) 유지 (sc는 19:00 진입과 함께 제외)
#   - BREAK2: nh(54초) + task_ibk_terminal(06:00:34·06:01:34, 화~토). shinhan/woori/sc 제외
#   - OUT: nh(:10), shinhan(:30) 모두 1분마다 (배포 2A, 20초 간격)
#
# BREAK1/BREAK2/OUT 모드 크롤러 축소 근거:
#   - 환율 고시 종료 시간 이후 크롤러 제거 (리소스 절약)
#   - 실제 운영 데이터 추적 관찰로 변동 없음 확인
#   - Queue 압력 완화: Request 우선 전략으로 Selenium 사용 최소화
# ─────────────────────────────────────────────────────────────

# 현재 모드 상태 저장
current_mode = None


# ═════════════════════════════════════════════════════════════
# 크롤러 설정 관리자 (Phase 1.8)
# ═════════════════════════════════════════════════════════════
class CrawlerManager:
    """
    크롤러 설정 관리자 (In-memory 캐시 + DB 동기화)

    Features:
        - In-memory 캐시로 빠른 조회 (< 0.001ms)
        - DB 영속성 보장
        - switch_jobs() 통합 제어
    """

    def __init__(self):
        self.config_cache: dict[str, bool] = {}  # {crawler_name: enabled}

    def load_config(self, db):
        """
        DB에서 설정을 읽어 In-memory 캐시 초기화

        Notes:
            - start_scheduler() 내부에서 1회 호출
            - 서버 재시작 시 DB 상태 복원
        """
        try:
            configs = crud.get_all_crawler_configs(db)

            for config in configs:
                self.config_cache[config["crawler_name"]] = config["enabled"]

            logger.info(
                "✅ CrawlerManager 캐시 로드 완료",
                extra={"count": len(configs)}
            )
        except Exception as e:
            logger.error("❌ CrawlerManager 캐시 로드 실패", exc_info=True)
            # 폴백: 모든 크롤러 활성화
            self.config_cache = {}

    def is_enabled(self, crawler_name: str) -> bool:
        """
        크롤러 활성화 여부 조회 (In-memory 캐시)

        Args:
            crawler_name: 크롤러 이름

        Returns:
            활성화 여부 (캐시 미스 시 기본값 True)

        Notes:
            - switch_jobs() 내부에서 매번 호출
            - 성능: < 0.001ms (dict lookup)
        """
        return self.config_cache.get(crawler_name, True)

    def toggle_crawler(self, crawler_name: str, enabled: bool):
        """
        크롤러 토글 (관리자 API에서 호출)

        Steps:
            1. DB 업데이트 (crud.update_crawler_config)
            2. In-memory 캐시 업데이트
            3. switch_jobs() 재실행 → APScheduler job 재등록

        Args:
            crawler_name: 크롤러 이름
            enabled: 활성화 상태

        Raises:
            ValueError: 존재하지 않는 크롤러 이름
        """
        # Validation: 유효한 크롤러 이름인지 체크
        VALID_CRAWLERS = [
            'investing', 'kb', 'hana', 'shinhan', 'woori',
            'ibk', 'nh', 'sc', 'bs', 'citi', 'dxy'
        ]

        if crawler_name not in VALID_CRAWLERS:
            raise ValueError(f"Invalid crawler name: {crawler_name}")

        # 1. DB 업데이트
        db = SessionLocal()
        try:
            crud.update_crawler_config(db, crawler_name, enabled)
        finally:
            db.close()

        # 2. In-memory 캐시 업데이트
        self.config_cache[crawler_name] = enabled

        # 3. switch_jobs() 재실행 (현재 모드 유지)
        global current_mode
        if current_mode:
            switch_jobs(current_mode)

            action = "활성화" if enabled else "비활성화"
            logger.info(
                f"✅ 크롤러 토글 완료: {crawler_name} → {action} (현재 모드: {current_mode})",
                extra={"crawler": crawler_name, "enabled": enabled, "mode": current_mode}
            )


# 전역 인스턴스 생성
crawler_manager = CrawlerManager()


# ═════════════════════════════════════════════════════════════
# 크롤러 Wrapper (통계 수집)
# ═════════════════════════════════════════════════════════════
def make_request_crawler_wrapper(bank_name: str, job_func: Callable):
    """
    Request 기반 크롤러 wrapper (통계 수집)

    Args:
        bank_name: 은행 이름
        job_func: 크롤러 함수

    Returns:
        wrapper 함수 (APScheduler가 호출)
    """
    def wrapper():
        start_time = time.time()
        try:
            job_func()
            duration = time.time() - start_time
            crawler_stats.record_success(bank_name, duration)
            logger.debug(f"✅ [{bank_name}] 완료 ({duration:.2f}초)")
        except Exception as e:
            duration = time.time() - start_time
            crawler_stats.record_failure(bank_name)
            logger.exception(f"❌ [{bank_name}] 실패 ({duration:.2f}초)", extra={"bank": bank_name, "error": str(e)})

    wrapper.__name__ = f"request_wrapper_{bank_name}"
    wrapper.__qualname__ = wrapper.__name__
    return wrapper


# ═════════════════════════════════════════════════════════════
# 타임아웃 래퍼 (느린 크롤러 격리 + 통계 수집)
# ═════════════════════════════════════════════════════════════
async def execute_with_timeout(bank_name: str) -> bool:
    """
    Subprocess 기반 타임아웃 제어 래퍼 함수

    Args:
        bank_name: 은행 이름

    Returns:
        bool: 성공 시 True, 실패/타임아웃 시 False

    Notes:
        - asyncio.create_subprocess_exec()로 크롤러를 독립 프로세스에서 실행
        - 타임아웃 시 proc.kill()로 Chrome 포함 전체 프로세스 강제 종료
        - driver.quit() 실행 여부와 무관하게 프로세스 정리 보장
        - event loop blocking 없음 (완전 격리)
    """
    timeout = SELENIUM_TIMEOUT_MAP.get(bank_name, 45)
    start_time = time.time()

    try:
        logger.info(f"⏳ [{bank_name}] subprocess 크롤링 시작 (타임아웃: {timeout}초)")

        # subprocess 생성 (app.crawlers.runner 실행)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.crawlers.runner", bank_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # 타임아웃 제어
        await asyncio.wait_for(proc.wait(), timeout=timeout)

        elapsed = time.time() - start_time

        # exit code 확인
        if proc.returncode == 0:
            crawler_stats.record_success(bank_name, elapsed)
            logger.info(f"✅ [{bank_name}] subprocess 완료 ({elapsed:.2f}초)")
            return True
        else:
            crawler_stats.record_failure(bank_name)
            # stderr 캡처 (디버깅용)
            stderr = await proc.stderr.read()
            logger.error(
                f"❌ [{bank_name}] subprocess 실패 (exit code: {proc.returncode})",
                extra={"bank": bank_name, "exit_code": proc.returncode, "stderr": stderr.decode()[:500]}
            )
            return False

    except asyncio.TimeoutError:
        elapsed = time.time() - start_time
        crawler_stats.record_failure(bank_name)

        # 프로세스 강제 종료 (Chrome 포함)
        logger.warning(
            f"⏱️ [{bank_name}] subprocess 타임아웃 ({timeout}초 초과) - 프로세스 강제 종료",
            extra={"bank": bank_name, "timeout": timeout}
        )

        proc.kill()
        await proc.wait()  # 종료 대기

        logger.info(f"🔪 [{bank_name}] subprocess killed (Chrome 포함 전체 정리)")
        return False

    except Exception as e:
        elapsed = time.time() - start_time
        crawler_stats.record_failure(bank_name)
        logger.exception(f"❌ [{bank_name}] subprocess 실행 실패", extra={"error": str(e)})

        # 프로세스가 살아있으면 정리
        try:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        except:
            pass

        return False


# ═════════════════════════════════════════════════════════════
# AsyncIO PriorityQueue Worker (Selenium 크롤러 순차 실행)
# ═════════════════════════════════════════════════════════════
async def selenium_job_executor():
    """우선순위 Queue Worker (타임아웃 + 재시도 로직 + 헬스체크)"""
    global selenium_queue, selenium_worker_last_heartbeat, selenium_worker_current_job
    logger.info("🔧 Selenium Priority Queue Worker 시작")

    while True:
        try:
            # PriorityQueue에서 작업 가져오기 (blocking)
            # 튜플: (priority, timestamp, bank_name, is_retry)
            priority, timestamp, bank_name, is_retry = await selenium_queue.get()

            # 헬스체크 업데이트
            selenium_worker_last_heartbeat = time.time()
            selenium_worker_current_job = bank_name

            logger.info(
                f"🔄 [{bank_name}] Queue 처리 시작 (우선순위: {priority}, 재시도: {is_retry})"
            )

            # subprocess 기반 실행 (타임아웃 제어)
            success = await execute_with_timeout(bank_name)

            # 작업 완료 후 헬스체크 업데이트
            selenium_worker_last_heartbeat = time.time()
            selenium_worker_current_job = None

            # 실패 시 재시도 (최대 1회)
            if not success and not is_retry:
                retry_priority = priority + 1000  # 낮은 우선순위로 재시도
                await selenium_queue.put((
                    retry_priority,
                    time.time(),
                    bank_name,
                    True  # 재시도 플래그
                ))
                logger.info(f"🔄 [{bank_name}] 재시도 Queue 추가 (우선순위: {retry_priority})")

            selenium_queue.task_done()

        except asyncio.CancelledError:
            logger.info("🛑 Selenium Priority Queue Worker 중단")
            selenium_worker_current_job = None
            break
        except Exception as e:
            logger.exception("Selenium Queue Worker 오류", extra={"error": str(e)})
            selenium_worker_last_heartbeat = time.time()
            selenium_worker_current_job = None


def is_bank_in_queue(bank_name: str) -> bool:
    """
    Queue에 특정 은행 작업이 이미 대기 중인지 확인

    Args:
        bank_name: 확인할 은행 이름

    Returns:
        True: Queue에 이미 대기 중, False: 없음

    Notes:
        - PriorityQueue.queue는 내부 list이므로 순회 가능
        - Python list iteration은 thread-safe
        - O(n) 복잡도지만 n≤25이므로 무시 가능 (~0.00001초)
    """
    global selenium_queue

    # PriorityQueue 내부 list 순회 (_queue는 private attribute이지만 접근 가능)
    for item in selenium_queue._queue:
        # item = (priority, timestamp, bank_name, is_retry)
        _, _, item_bank_name, _ = item
        if item_bank_name == bank_name:
            return True
    return False


def enqueue_selenium_job(bank_name: str):
    """
    Selenium 작업을 우선순위 Queue에 non-blocking 방식으로 추가 (subprocess 기반)

    Args:
        bank_name: 은행 이름 (runner.py에 전달됨)

    Notes:
        - subprocess 격리: job_func 대신 bank_name만 전달
        - 중복 방지: Worker 처리 중 + Queue 대기 중 체크
        - put_nowait() 사용으로 APScheduler event loop blocking 방지
        - Queue 80% 초과 시 작업 추가 거부 (압력 완화)
        - Queue 포화 시 조용히 skip (다음 스케줄에서 재시도)
    """
    global selenium_queue, selenium_worker_current_job

    # ═════════════════════════════════════════════════════════════
    # 1. 중복 작업 방지
    # ═════════════════════════════════════════════════════════════
    # 1-1. Worker가 현재 처리 중인지 확인
    if selenium_worker_current_job == bank_name:
        logger.debug(
            f"🔄 [{bank_name}] Worker 처리 중, skip (중복 방지)",
            extra={"bank": bank_name, "reason": "worker_processing"}
        )
        return

    # 1-2. Queue에 이미 대기 중인지 확인
    if is_bank_in_queue(bank_name):
        logger.debug(
            f"🔄 [{bank_name}] Queue에 이미 대기 중, skip (중복 방지)",
            extra={"bank": bank_name, "reason": "already_in_queue"}
        )
        return

    # ═════════════════════════════════════════════════════════════
    # 2. Queue 압력 완화
    # ═════════════════════════════════════════════════════════════
    current_size = selenium_queue.qsize()
    max_size = 25
    usage_percent = (current_size / max_size) * 100

    if current_size >= 20:  # 80% 초과 (20/25)
        logger.warning(
            f"🚫 [{bank_name}] Queue 압력 초과로 skip ({current_size}/{max_size}, {usage_percent:.0f}%) - 다음 스케줄에서 재시도",
            extra={"bank": bank_name, "queue_size": current_size, "usage_percent": usage_percent}
        )
        return

    priority = SELENIUM_PRIORITY_MAP.get(bank_name, 999)  # 기본값: 낮은 우선순위

    # PriorityQueue는 튜플의 첫 번째 요소로 정렬
    # (priority, timestamp, bank_name, is_retry)
    item = (
        priority,
        time.time(),  # 동일 우선순위 내에서 FIFO 보장
        bank_name,
        False  # 첫 실행
    )

    try:
        # Non-blocking put - Queue 가득 차면 즉시 예외 발생
        selenium_queue.put_nowait(item)
        logger.debug(
            f"📥 [{bank_name}] Priority Queue 추가 (우선순위: {priority}, 대기: {current_size + 1}/{max_size})"
        )
    except asyncio.QueueFull:
        # Queue 포화 시 조용히 skip
        # 다음 스케줄에서 자동으로 재시도됨
        logger.warning(
            f"⚠️ [{bank_name}] Queue 포화로 skip (size: {current_size}/{max_size}) - 다음 스케줄에서 재시도",
            extra={"bank": bank_name, "queue_size": current_size}
        )


def init_selenium_queue():
    """FastAPI 시작 시 Priority Queue 초기화"""
    global selenium_queue, selenium_worker_task

    # [2025-11-10] Queue 크기 최적화: 50 → 25
    # - 80% 압력 완화 정책(20개)을 고려하면 25면 충분
    # - 메모리 절약 + 과도한 작업 누적 방지
    selenium_queue = asyncio.PriorityQueue(maxsize=25)

    # Worker 시작 (백그라운드에서 계속 실행)
    loop = asyncio.get_event_loop()
    selenium_worker_task = loop.create_task(selenium_job_executor())
    logger.info("✅ Selenium Priority Queue 시스템 초기화 완료")


async def shutdown_selenium_queue():
    """FastAPI 종료 시 Queue Worker 정리"""
    global selenium_worker_task
    if selenium_worker_task:
        selenium_worker_task.cancel()
        try:
            await selenium_worker_task
        except asyncio.CancelledError:
            pass
        logger.info("✅ Selenium Queue Worker 종료 완료")



# get_market_mode()는 app.market_mode에서 import (순환 참조 방지)


def make_selenium_job_wrapper(bank_name: str):
    """
    각 Selenium 크롤러마다 고유한 wrapper 함수 생성 (subprocess 기반)

    Args:
        bank_name: 은행 이름 (runner.py에 전달됨)

    Returns:
        고유한 wrapper 함수 객체 (APScheduler가 구별 가능)

    Notes:
        - subprocess 격리: job_func 불필요, bank_name만 전달
        - __name__, __qualname__ 설정으로 APScheduler가 각 job을 구별
        - for loop 안에서 직접 wrapper 정의 시 모든 wrapper가 같은 이름을 가짐
        - enqueue는 동기 non-blocking이므로 즉시 반환 (event loop blocking 방지)
    """
    def wrapper():
        enqueue_selenium_job(bank_name)

    # APScheduler가 함수를 구별할 수 있도록 고유 이름 설정
    wrapper.__name__ = f"selenium_wrapper_{bank_name}"
    wrapper.__qualname__ = wrapper.__name__
    return wrapper


def switch_jobs(mode: str):
    """
    모드에 따라 작업 재등록 (4단계 세분화 + 크롤러 토글 통합)

    [2025-11-16 재설계]
    - IN 모드: cron 절대 시간 동기화 - 월~금 08:00~18:59 (2026-08-28 ADR-042: 20:59 → 18:59)
    - BREAK1 모드: 19시~익일 06시 - IN과 동일 스케줄에서 sc 제외 + shinhan/woori 은행별 cutoff
    - BREAK2 모드: 06:00~07:59 - shinhan/woori/sc 제외, ibk는 terminal capture(화~토)만
    - OUT 모드: 1분 주기 + 고정 시작초 분산 - 토 07:00 ~ 월 05:59
    - Queue: C Group만 사용 (Selenium 전용)

    [2025-11-27 Phase 1.8: 크롤러 토글]
    - 각 크롤러 등록 전에 crawler_manager.is_enabled() 체크
    - enabled=False인 크롤러는 job 등록 skip
    """
    global crawler_manager

    # 기존 작업 제거
    for job in scheduler.get_jobs():
        if job.id.startswith("task_"):
            scheduler.remove_job(job.id)

    if mode == "IN":
        # ═════════════════════════════════════════════════════════════
        # IN 모드: cron 절대 시간 기반 초 레인 분산
        # ═════════════════════════════════════════════════════════════

        # A Group: investing
        if crawler_manager.is_enabled('investing'):
            scheduler.add_job(
                make_request_crawler_wrapper('investing', investing.crawl_and_save_investing_exchange_rates),
                CronTrigger(second='7,17,27,37,47,57', timezone=KST),
                id='task_investing',
                max_instances=1,
                misfire_grace_time=5
            )
        else:
            logger.info("⏸️ [investing] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('dxy'):
            scheduler.add_job(
                make_request_crawler_wrapper('dxy', dxy_spot.crawl_and_save_dxy_spot),
                CronTrigger(second='1,11,21,31,41,51', timezone=KST),
                id='task_dxy',
                max_instances=1,
                misfire_grace_time=5
            )
        else:
            logger.info("⏸️ [dxy] 비활성화 상태 - job 등록 스킵")

        # B Group: kb, hana — 배포 2 본단계에서 IN만 10초로 상향.
        # KB는 짧은 순수 Request라 Selenium 실행 꼬리와 겹치는 residue 9를 맡고,
        # Selenium 폴백 가능성이 있는 hana는 residue 2로 분리한다.
        # 매초 websocket broadcast를 제외한 고정 작업과 같은 시작초는
        # KB/free snapshot 시간당 1회, hana/graph precompute 시간당 6회다.
        # 부동 IntervalTrigger는 smoke에서 별도 관측한다.
        if crawler_manager.is_enabled('kb'):
            scheduler.add_job(
                make_request_crawler_wrapper('kb', kb.crawl_and_save_kb_bank_exchange_rates),
                CronTrigger(second='9,19,29,39,49,59', timezone=KST),
                id='task_kb',
                max_instances=1,
                misfire_grace_time=10
            )
        else:
            logger.info("⏸️ [kb] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('hana'):
            scheduler.add_job(
                make_request_crawler_wrapper('hana', hana.crawl_and_save_hana_bank_exchange_rates),  # 하이브리드 (내부 폴백)
                CronTrigger(second='2,12,22,32,42,52', timezone=KST),
                id='task_hana',
                max_instances=1,
                misfire_grace_time=10
            )
        else:
            logger.info("⏸️ [hana] 비활성화 상태 - job 등록 스킵")

        # B Group: woori, bs, citi
        # woori는 배포 2 본단계에서 IN만 30초(:14/:44)로 상향.
        # 열거된 고정 IN job과 동일 시작초는 없고, 재기동 위상에 묶인 IntervalTrigger는
        # 고정 레인으로 회피할 수 없으므로 scheduler_event와 smoke에서 별도 관측한다.
        if crawler_manager.is_enabled('woori'):
            scheduler.add_job(
                make_request_crawler_wrapper('woori', woori.crawl_and_save_woori_bank_exchange_rates),  # 하이브리드 (내부 폴백)
                CronTrigger(minute='*', second='14,44', timezone=KST),
                id='task_woori',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [woori] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('bs'):
            scheduler.add_job(
                make_request_crawler_wrapper('bs', bs.crawl_and_save_bs_bank_exchange_rates),  # 하이브리드 (내부 폴백)
                CronTrigger(minute='*', second='33', timezone=KST),
                id='task_bs',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [bs] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('citi'):
            scheduler.add_job(
                make_request_crawler_wrapper('citi', citi.crawl_and_save_citi_bank_exchange_rates),
                CronTrigger(minute='*', second='13', timezone=KST),
                id='task_citi',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [citi] 비활성화 상태 - job 등록 스킵")

        # C Group: Selenium
        if crawler_manager.is_enabled('shinhan'):
            scheduler.add_job(
                make_selenium_job_wrapper('shinhan'),
                # IntervalTrigger(seconds=38.3, timezone=KST),
                CronTrigger(minute='*', second='18', timezone=KST),
                id='task_shinhan',
                max_instances=1,
                # misfire_grace_time=25
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [shinhan] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('ibk'):
            scheduler.add_job(
                make_selenium_job_wrapper('ibk'),  # 하이브리드 (내부 시간대 체크)
                # IntervalTrigger(seconds=55.5, timezone=KST),
                CronTrigger(minute='*', second='34', timezone=KST),
                id='task_ibk',
                max_instances=1,
                # misfire_grace_time=25
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [ibk] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('nh'):
            scheduler.add_job(
                make_selenium_job_wrapper('nh'),
                # IntervalTrigger(seconds=90, timezone=KST),
                CronTrigger(minute='*', second='54', timezone=KST),
                id='task_nh',
                max_instances=1,
                # misfire_grace_time=60
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [nh] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('sc'):
            scheduler.add_job(
                make_selenium_job_wrapper('sc'),
                # IntervalTrigger(seconds=150, timezone=KST),
                CronTrigger(minute='*', second='58', timezone=KST),
                id='task_sc',
                max_instances=1,
                # misfire_grace_time=90
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [sc] 비활성화 상태 - job 등록 스킵")

    elif mode == "BREAK1":
        # ═════════════════════════════════════════════════════════════
        # BREAK1 모드: 야간 시간대 (월~금 19:00 ~ 익일 05:59)
        # ═════════════════════════════════════════════════════════════
        # 제외 크롤러: sc (18시 이후 포착 변경 0건, 여유 두고 18:59:58까지만 수집)
        # 유지 크롤러: investing, dxy, kb, hana, woori, bs, citi, ibk, nh, shinhan (10개)
        # ibk는 자정~08:00 구간에서 당일 GET을 생략하고 전 조회기준일을 공식 날짜 지정
        # POST로 조회한다 (정상 경로는 Chrome 미기동). Selenium은 응답 이상 시 안전망.

        # A Group: investing
        if crawler_manager.is_enabled('investing'):
            scheduler.add_job(
                make_request_crawler_wrapper('investing', investing.crawl_and_save_investing_exchange_rates),
                CronTrigger(second='7,17,27,37,47,57', timezone=KST),
                id='task_investing',
                max_instances=1,
                misfire_grace_time=5
            )
        else:
            logger.info("⏸️ [investing] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('dxy'):
            scheduler.add_job(
                make_request_crawler_wrapper('dxy', dxy_spot.crawl_and_save_dxy_spot),
                CronTrigger(second='1,11,21,31,41,51', timezone=KST),
                id='task_dxy',
                max_instances=1,
                misfire_grace_time=5
            )
        else:
            logger.info("⏸️ [dxy] 비활성화 상태 - job 등록 스킵")

        # B Group: kb, hana (10초 엇갈림)
        if crawler_manager.is_enabled('kb'):
            scheduler.add_job(
                make_request_crawler_wrapper('kb', kb.crawl_and_save_kb_bank_exchange_rates),
                CronTrigger(second='15,35,55', timezone=KST),
                id='task_kb',
                max_instances=1,
                misfire_grace_time=10
            )
        else:
            logger.info("⏸️ [kb] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('hana'):
            scheduler.add_job(
                make_request_crawler_wrapper('hana', hana.crawl_and_save_hana_bank_exchange_rates),  # 하이브리드 (내부 폴백)
                CronTrigger(second='5,25,45', timezone=KST),
                id='task_hana',
                max_instances=1,
                misfire_grace_time=10
            )
        else:
            logger.info("⏸️ [hana] 비활성화 상태 - job 등록 스킵")

        # B Group: woori, bs, citi (7초 전, 20초씩 엇갈림)
        # woori: 고시가 새벽 05:00경까지 연장됨 (사용자 은행 페이지 실측).
        # <05:05 를 정확히 표현하려면 본구간(19-23,0-4시)과 보정구간(5시 0-4분)이 필요한데,
        # ⛔ **두 개의 job으로 나누면 안 된다** — max_instances=1은 job 단위라 서로 배타가
        #    아니고, 04:59:53 실행이 지연되면(최악: requests 10s → Selenium 45s → mibank 10s)
        #    05:00:53 tail과 겹쳐 Chrome 2개가 동시에 뜰 수 있다.
        #    OrTrigger로 묶어 **단일 job**으로 두면 max_instances=1이 전 구간에 적용된다.
        if crawler_manager.is_enabled('woori'):
            scheduler.add_job(
                make_request_crawler_wrapper('woori', woori.crawl_and_save_woori_bank_exchange_rates),  # 하이브리드 (내부 폴백)
                OrTrigger([
                    CronTrigger(hour='19-23,0-4', minute='*', second='53', timezone=KST),
                    CronTrigger(hour='5', minute='0-4', second='53', timezone=KST),  # 마지막 05:04:53
                ]),
                id='task_woori',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [woori] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('bs'):
            scheduler.add_job(
                make_request_crawler_wrapper('bs', bs.crawl_and_save_bs_bank_exchange_rates),  # 하이브리드 (내부 폴백)
                CronTrigger(minute='*', second='33', timezone=KST),
                id='task_bs',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [bs] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('citi'):
            scheduler.add_job(
                make_request_crawler_wrapper('citi', citi.crawl_and_save_citi_bank_exchange_rates),
                CronTrigger(minute='*', second='13', timezone=KST),
                id='task_citi',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [citi] 비활성화 상태 - job 등록 스킵")

        # C Group: Selenium
        if crawler_manager.is_enabled('shinhan'):
            scheduler.add_job(
                make_selenium_job_wrapper('shinhan'),
                # IntervalTrigger(seconds=38.3, timezone=KST),
                # 신한 고시는 02:45경 종료 → 02:59:18이 마지막 (현행 동작 고정, 변화 0)
                CronTrigger(hour='19-23,0-2', minute='*', second='18', timezone=KST),
                id='task_shinhan',
                max_instances=1,
                # misfire_grace_time=25
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [shinhan] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('ibk'):
            scheduler.add_job(
                make_selenium_job_wrapper('ibk'),  # 하이브리드 (내부 시간대 체크)
                # IntervalTrigger(seconds=55.5, timezone=KST),
                CronTrigger(minute='*', second='34', timezone=KST),
                id='task_ibk',
                max_instances=1,
                # misfire_grace_time=25
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [ibk] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('nh'):
            scheduler.add_job(
                make_selenium_job_wrapper('nh'),
                # IntervalTrigger(seconds=90, timezone=KST),
                CronTrigger(minute='*', second='54', timezone=KST),
                id='task_nh',
                max_instances=1,
                # misfire_grace_time=60
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [nh] 비활성화 상태 - job 등록 스킵")

    elif mode == "BREAK2":
        # ═════════════════════════════════════════════════════════════
        # BREAK2 모드: 고시 마무리 시간대 (06:00~07:59, 모든 요일 공통)
        # ═════════════════════════════════════════════════════════════
        # 제외 크롤러: woori (05:05 수집 종료), shinhan (03:00 수집 종료), sc (19:00 종료)
        #             ibk는 정규 job 없음 — task_ibk_terminal만 06:00:34·06:01:34 (화~토)
        # 유지 크롤러: investing, dxy, kb, hana, bs, citi, nh (7개) + task_ibk_terminal(화~토)
        # 08:00~09:00부터 은행 개장 준비하며 새 환율 고시 시작

        # A Group: investing
        if crawler_manager.is_enabled('investing'):
            scheduler.add_job(
                make_request_crawler_wrapper('investing', investing.crawl_and_save_investing_exchange_rates),
                CronTrigger(second='7,17,27,37,47,57', timezone=KST),
                id='task_investing',
                max_instances=1,
                misfire_grace_time=5
            )
        else:
            logger.info("⏸️ [investing] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('dxy'):
            scheduler.add_job(
                make_request_crawler_wrapper('dxy', dxy_spot.crawl_and_save_dxy_spot),
                CronTrigger(second='1,11,21,31,41,51', timezone=KST),
                id='task_dxy',
                max_instances=1,
                misfire_grace_time=5
            )
        else:
            logger.info("⏸️ [dxy] 비활성화 상태 - job 등록 스킵")

        # B Group: kb, hana (10초 엇갈림)
        if crawler_manager.is_enabled('kb'):
            scheduler.add_job(
                make_request_crawler_wrapper('kb', kb.crawl_and_save_kb_bank_exchange_rates),
                CronTrigger(second='15,35,55', timezone=KST),
                id='task_kb',
                max_instances=1,
                misfire_grace_time=10
            )
        else:
            logger.info("⏸️ [kb] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('hana'):
            scheduler.add_job(
                make_request_crawler_wrapper('hana', hana.crawl_and_save_hana_bank_exchange_rates),  # 하이브리드 (내부 폴백)
                CronTrigger(second='5,25,45', timezone=KST),
                id='task_hana',
                max_instances=1,
                misfire_grace_time=10
            )
        else:
            logger.info("⏸️ [hana] 비활성화 상태 - job 등록 스킵")

        # B Group: woori, bs, citi (7초 전)
        if crawler_manager.is_enabled('bs'):
            scheduler.add_job(
                make_request_crawler_wrapper('bs', bs.crawl_and_save_bs_bank_exchange_rates),  # 하이브리드 (내부 폴백)
                CronTrigger(minute='*', second='33', timezone=KST),
                id='task_bs',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [bs] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('citi'):
            scheduler.add_job(
                make_request_crawler_wrapper('citi', citi.crawl_and_save_citi_bank_exchange_rates),
                CronTrigger(minute='*', second='13', timezone=KST),
                id='task_citi',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [citi] 비활성화 상태 - job 등록 스킵")

        # C Group: Selenium
        if crawler_manager.is_enabled('nh'):
            scheduler.add_job(
                make_selenium_job_wrapper('nh'),
                # IntervalTrigger(seconds=90, timezone=KST),
                CronTrigger(minute='*', second='54', timezone=KST),
                id='task_nh',
                max_instances=1,
                # misfire_grace_time=60
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [nh] 비활성화 상태 - job 등록 스킵")

        # IBK terminal capture — 06:00 경계 이후 **추가 수집 시도 2회**
        # IBK는 05:59:55에 마지막 고시한 날이 관측됐는데(사용자 은행 페이지 실측),
        # BREAK1(~06:00)의 마지막 정규 실행이 05:59:34라 그 사이 고시를 놓칠 수 있다.
        # ⚠️ 포착을 보장하지는 않는다 — 06:00:34·06:01:34 두 차례 더 시도할 뿐이다.
        # day_of_week='tue-sat': IBK 세션은 평일 08:30 → 익일 06:00이므로 화~토 아침에만 종료된다.
        #   월 06:00은 일요일 세션이 없어 무의미, 토 06:00은 금요일 세션 종료라 필요.
        # ⚠️ task_ prefix 필수 — switch_jobs()가 모드 전환 시 task_* 를 제거한다.
        if crawler_manager.is_enabled('ibk'):
            scheduler.add_job(
                make_selenium_job_wrapper('ibk'),
                CronTrigger(hour=6, minute='0,1', second='34', day_of_week='tue-sat', timezone=KST),
                id='task_ibk_terminal',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [ibk] 비활성화 상태 - terminal capture job 등록 스킵")

    elif mode == "OUT":
        # ═════════════════════════════════════════════════════════════
        # OUT 모드: 주말 시간대 (토 07:00 ~ 월 05:59)
        # ═════════════════════════════════════════════════════════════
        # 제외 크롤러: woori, ibk, sc, citi (주말 환율 고시 없음)
        # 유지 크롤러: investing, dxy, kb, hana, bs, shinhan, nh (7개)
        # - hana, shinhan: 주말 중 가끔 변동
        # - bs, nh: 일요일 넘어갈 때 가끔 고시
        #
        # 2026-08-28 (배포 2A): 6개 소스를 **1분 주기**로 상향 + 초 분산.
        #   근거 — 주말 관측(최근 30일 OUT 구간 변경행): hana 18 / shinhan 6 /
        #   investing 1 / kb·bs·nh 0. 다만 bs·nh·shinhan은 60분 폴링이라 표본이 성기고
        #   change-only DB라 "변경 없음"이 "고시 없음"을 뜻하지 않는다 → 실측 확대.
        #   ⚠️ 이번 주말이 그 판정 실험이다. 다음 주 초에 소스별 수확을 재측정해 유지/축소 결정.
        #
        # 초 분산 — investing :08 / nh :10 / dxy :21 / kb :28 / shinhan :30 / hana :38 / bs :51
        #   - dxy :15 → :21  — KB 뉴스(*/5분 :15)와 시간당 12회 충돌 제거 (주기는 1분 유지)
        #   - kb·hana 구 :45 — RSS 뉴스(*/5분 :45)와 충돌하던 것이 1분 전환+분산으로 해소
        #   - nh :10 / shinhan :30 — Selenium subprocess 2개를 20초 벌려 버스트 중첩 회피
        #   - 회피 대상 초:
        #       :00        분 경계 job (dxy rollup hourly 매시 :05:00 / daily 00:05:00)
        #       :01        control_job · 일일 cleanup 5종(03:20~03:32)
        #       :03/:23/:43 chrome cleanup + worker health (:03은 graph_cache_refresh도)
        #       :12        graph v2 intraday precompute (*/10분)
        #       :15        KB 뉴스 (*/5분)
        #       :19        free snapshot (매시 :30)
        #       :45        RSS 뉴스 (*/5분)
        #       :06/:16/:26/:36/:46/:56  USDT legacy REST polling — **평시 미등록**이지만
        #                  `USDT_LEGACY_REST_POLLING_ENABLED=true` 롤백 시 5-거래소 fan-out이
        #                  이 초에 몰린다. 실행시간 비중첩은 보장하지 않고 동일 시작초만 피한다.
        #
        # ⚠️ 이 배치가 보장하는 것은 broadcast를 제외한 **위에 열거한 고정 cron과 동일
        #    시작초 0개**이지 "동시 실행 0개"가 아니다. broadcast는 의도대로 매초 발화한다.
        #    실행시간이 겹칠 수 있고, IntervalTrigger job(queue_status 등)은 프로세스 기동
        #    시각에 묶여 초가 부동이라 격자 밖이다.

        # A Group: investing (1분마다)
        if crawler_manager.is_enabled('investing'):
            scheduler.add_job(
                make_request_crawler_wrapper('investing', investing.crawl_and_save_investing_exchange_rates),
                CronTrigger(minute='*', second='8', timezone=KST),
                id='task_investing',
                max_instances=1,
                misfire_grace_time=30   # 1분 주기 → grace를 주기 이하로 (구 300은 10분 주기 전제)
            )
        else:
            logger.info("⏸️ [investing] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('dxy'):
            scheduler.add_job(
                make_request_crawler_wrapper('dxy', dxy_spot.crawl_and_save_dxy_spot),
                CronTrigger(minute='*', second='21', timezone=KST),
                id='task_dxy',
                max_instances=1,
                # DXY는 주기 변경이 없으므로 기존 120 유지. scheduler 기본 coalesce=True라
                # 누적 backlog가 아니라 최신 catch-up 1회만 실행된다.
                misfire_grace_time=120
            )
        else:
            logger.info("⏸️ [dxy] 비활성화 상태 - job 등록 스킵")

        # B Group: kb, hana (각 1분마다)
        if crawler_manager.is_enabled('kb'):
            scheduler.add_job(
                make_request_crawler_wrapper('kb', kb.crawl_and_save_kb_bank_exchange_rates),
                CronTrigger(minute='*', second='28', timezone=KST),
                id='task_kb',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [kb] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('hana'):
            scheduler.add_job(
                make_request_crawler_wrapper('hana', hana.crawl_and_save_hana_bank_exchange_rates),
                CronTrigger(minute='*', second='38', timezone=KST),
                id='task_hana',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [hana] 비활성화 상태 - job 등록 스킵")

        # B Group: OUT에는 bs만 등록 (1분마다)
        if crawler_manager.is_enabled('bs'):
            scheduler.add_job(
                make_request_crawler_wrapper('bs', bs.crawl_and_save_bs_bank_exchange_rates),
                CronTrigger(minute='*', second='51', timezone=KST),
                id='task_bs',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [bs] 비활성화 상태 - job 등록 스킵")

        # C Group: Selenium enqueue (각 1분마다)
        if crawler_manager.is_enabled('nh'):
            scheduler.add_job(
                make_selenium_job_wrapper('nh'),
                CronTrigger(minute='*', second='10', timezone=KST),
                id='task_nh',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [nh] 비활성화 상태 - job 등록 스킵")

        if crawler_manager.is_enabled('shinhan'):
            scheduler.add_job(
                make_selenium_job_wrapper('shinhan'),
                CronTrigger(minute='*', second='30', timezone=KST),
                id='task_shinhan',
                max_instances=1,
                misfire_grace_time=30
            )
        else:
            logger.info("⏸️ [shinhan] 비활성화 상태 - job 등록 스킵")

    logger.info(
        f"=== {mode} 모드로 전환됨 ===",
        extra={
            "mode": mode,
            "total_jobs": len(scheduler.get_jobs())
        }
    )

def control_job():
    """현재 시간대에 따라 모드 전환 (4단계: IN, BREAK1, BREAK2, OUT)"""
    global current_mode
    now = datetime.now(KST)
    new_mode = get_market_mode(now)

    if new_mode != current_mode:
        switch_jobs(new_mode)
        current_mode = new_mode

def cleanup_old_bank_data():
    """30일 이상 지난 은행 환율 데이터 삭제"""
    db = SessionLocal()
    try:
        deleted_count = crud.delete_old_bank_data(db=db, days=BANK_RETENTION_DAYS)
        logger.info(
            "🧹 은행 데이터 정리 완료",
            extra={"deleted_count": deleted_count, "retention_days": BANK_RETENTION_DAYS},
        )
    except Exception as e:
        db.rollback()
        logger.error("❌ 데이터 정리 실패", exc_info=True)
    finally:
        db.close()


def cleanup_old_source_rates():
    """30일 이상 지난 source_rates 데이터 삭제 (USDT Phase 1)."""
    db = SessionLocal()
    try:
        deleted_count = crud.delete_old_source_rates(db=db, days=SOURCE_RATE_RETENTION_DAYS)
        logger.info(
            "🧹 source_rates 정리 완료",
            extra={"deleted_count": deleted_count, "retention_days": SOURCE_RATE_RETENTION_DAYS},
        )
    except Exception:
        db.rollback()
        logger.error("❌ source_rates 정리 실패", exc_info=True)
    finally:
        db.close()


def cleanup_old_market_index_rates():
    """30일 이상 지난 DXY 현물/선물 realtime 시장지수 데이터 삭제."""
    db = SessionLocal()
    try:
        deleted_count = crud.delete_old_market_index_rates(db=db, days=MARKET_INDEX_RETENTION_DAYS)
        logger.info(
            "🧹 시장지수 데이터 정리 완료",
            extra={"deleted_count": deleted_count, "retention_days": MARKET_INDEX_RETENTION_DAYS},
        )
    except Exception:
        db.rollback()
        logger.error("❌ 시장지수 데이터 정리 실패", exc_info=True)
    finally:
        db.close()

def cleanup_old_user_devices():
    """
    오래된 user_devices 정리 (Retention cleanup)

    Notes:
        - 클라이언트 old token cleanup이 실행되지 않는 엣지 케이스(로그아웃 상태 토큰 회전 등) 대비.
        - updated_at 기준으로 일정 기간 미갱신 레코드 삭제.
        - Retention days는 환경변수로 조절 가능.
    """
    raw_days = os.getenv("USER_DEVICES_RETENTION_DAYS", "90")
    try:
        retention_days = int(raw_days)
    except ValueError:
        logger.warning("⚠️ USER_DEVICES_RETENTION_DAYS 파싱 실패, 기본값 90 사용", extra={"value": raw_days})
        retention_days = 90

    # Guardrail: 지나치게 작은 값은 운영 위험이 커서 방어적으로 보정
    if retention_days < 7:
        logger.warning("⚠️ USER_DEVICES_RETENTION_DAYS가 너무 작음, 최소 7로 보정", extra={"value": retention_days})
        retention_days = 7

    # DB는 naive UTC datetime을 사용한다. (models.get_utc_now())
    cutoff_utc_naive = datetime.now(dt_timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)

    db = SessionLocal()
    try:
        deleted_count = db.query(models.UserDevice).filter(
            models.UserDevice.updated_at < cutoff_utc_naive
        ).delete(synchronize_session=False)
        db.commit()
        logger.info(
            "🧹 user_devices retention cleanup 완료",
            extra={
                "deleted_count": deleted_count,
                "retention_days": retention_days,
                "cutoff_utc": cutoff_utc_naive.isoformat(),
            },
        )
    except Exception:
        db.rollback()
        logger.error("❌ user_devices retention cleanup 실패", exc_info=True)
    finally:
        db.close()

def cleanup_old_log_files():
    """오래된 로그 백업 파일 삭제 (10일 이상)"""
    from app.admin.log_cleaner import cleanup_old_log_files as cleanup_logs

    try:
        cleanup_logs(days=10)
    except Exception as e:
        logger.error("❌ 로그 파일 정리 실패", exc_info=True)


def cleanup_zombie_chrome_processes():
    """좀비 Chrome 프로세스 정리 (60초 이상 실행된 프로세스 강제 종료)"""
    try:
        # 함수 내부 import (psutil은 무거운 라이브러리, 필요 시에만 로드)
        import psutil
        import time
        from app.crawlers.constants import CHROME_MAX_LIFETIME_SECONDS

        killed_count = 0
        current_time = time.time()

        # 모든 프로세스 순회
        for proc in psutil.process_iter(['pid', 'name', 'create_time', 'cmdline']):
            try:
                # Chrome/ChromeDriver 프로세스만 체크
                proc_name = proc.info['name'].lower()
                if any(name in proc_name for name in ['chrome', 'chromedriver']):
                    # 프로세스 실행 시간 계산
                    process_age = current_time - proc.info['create_time']

                    # 최대 수명 초과 시 강제 종료
                    if process_age > CHROME_MAX_LIFETIME_SECONDS:
                        proc.kill()
                        killed_count += 1
                        logger.warning(
                            f"🔨 좀비 Chrome 프로세스 강제 종료: {proc.info['name']} (PID: {proc.info['pid']}, 실행시간: {int(process_age)}초)"
                        )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # 프로세스가 이미 종료되었거나 접근 권한 없음
                continue

        if killed_count > 0:
            logger.info(f"🧹 좀비 Chrome 프로세스 정리 완료: {killed_count}개 종료")
        else:
            logger.info("✅ 좀비 Chrome 프로세스 없음")

    except Exception as e:
        logger.error("❌ 좀비 프로세스 정리 실패", exc_info=True)


def report_queue_status():
    """
    Selenium Priority Queue 상태 모니터링 (10초마다) + admin 페이지용 캐시 업데이트

    Notes:
        - Queue 사용률 추적
        - 80% 이상 포화 시 경고
        - 성능 튜닝 및 문제 진단에 활용
        - admin 페이지에서 실시간 Queue 상태 표시
    """
    global selenium_queue, selenium_worker_current_job, queue_status_cache

    if selenium_queue is None:
        return

    try:
        size = selenium_queue.qsize()
        max_size = 25
        usage_percent = (size / max_size) * 100

        # 대기 중인 작업 목록 수집 (admin 페이지용)
        # _queue는 private attribute이지만 asyncio.PriorityQueue에서 접근 가능한 유일한 방법
        waiting_jobs = []
        for item in selenium_queue._queue:
            priority, timestamp, bank_name, is_retry = item
            waiting_jobs.append({
                "bank": bank_name,
                "priority": priority,
                "is_retry": is_retry,
                "waiting_seconds": int(time.time() - timestamp)
            })

        # 캐시 업데이트 (admin 페이지에서 조회)
        queue_status_cache.update({
            "size": size,
            "max_size": max_size,
            "usage_percent": round(usage_percent, 1),
            "current_job": selenium_worker_current_job,
            "waiting_jobs": waiting_jobs,
            "updated_at": datetime.now(KST).isoformat()
        })

        # 로그 출력 (기존 로직 유지)
        if size == 0:
            logger.debug("📊 [Queue] 비어있음")
        elif size < 20:  # 80% 미만 (20/25)
            banks = [job["bank"] for job in waiting_jobs]
            logger.debug(
                f"📊 [Queue] {size}/{max_size} ({usage_percent:.1f}%) | 대기: {banks}"
            )
        else:  # 경고 상태
            banks = [job["bank"] for job in waiting_jobs]
            logger.warning(
                f"⚠️ [Queue] 포화 임박: {size}/{max_size} ({usage_percent:.1f}%) | 대기: {banks}",
                extra={"queue_size": size, "usage_percent": usage_percent, "waiting_banks": banks}
            )

    except Exception as e:
        logger.error("❌ Queue 상태 모니터링 실패", exc_info=True)


def record_monitoring_stats():
    """시스템 모니터링 통계 기록 (5분마다)"""
    try:
        from app.admin.monitor import system_monitor
        system_monitor.record_stats()
    except Exception as e:
        logger.error("❌ 모니터링 통계 기록 실패", exc_info=True)


async def check_worker_health():
    """
    Selenium Worker 헬스체크 (매분 03, 23, 43초)

    Worker가 60초 이상 같은 작업을 처리하고 있으면 stuck 상태로 판단하여 재시작

    Note:
        - Worker가 Queue 대기 중(current_job=None)일 때는 정상 상태로 간주
        - 실제로 작업 처리 중일 때만 타임아웃 체크 (60초 임계값)
        - Worker Task를 cancel하고 재시작하면 Queue는 유지되고 Worker만 재생성됨
        - Worker 재시작 시 cleanup을 무조건 호출 (이중 안전장치)
        - subprocess 기반이므로 proc.kill()로 Chrome 포함 전체 프로세스 강제 종료 가능
    """
    global selenium_worker_task, selenium_worker_last_heartbeat, selenium_worker_current_job, selenium_queue

    try:
        if selenium_worker_task is None or selenium_queue is None:
            return

        # ═════════════════════════════════════════════════════════════
        # Worker가 Queue 대기 중일 때는 정상 상태 (작업 없음)
        # ═════════════════════════════════════════════════════════════
        if selenium_worker_current_job is None:
            logger.debug(f"💓 Worker 정상: Queue 대기 중 (작업 없음)")
            return

        # ═════════════════════════════════════════════════════════════
        # 작업 처리 중일 때만 타임아웃 체크
        # ═════════════════════════════════════════════════════════════
        elapsed = time.time() - selenium_worker_last_heartbeat
        max_job_time = 60  # 60초 (cleanup과 동일한 임계값)

        if elapsed > max_job_time:
            logger.error(
                f"🚨 Selenium Worker 멈춤 감지: {selenium_worker_current_job} 작업이 {int(elapsed)}초 동안 완료 안됨 (최대: {max_job_time}초)",
                extra={
                    "stuck_job": selenium_worker_current_job,
                    "elapsed_seconds": int(elapsed),
                    "queue_size": selenium_queue.qsize(),
                    "action": "worker_restart"
                }
            )

            # ✅ 이중 안전장치: Worker 재시작 전에 Chrome 프로세스 무조건 정리
            cleanup_zombie_chrome_processes()

            # Worker Task 강제 취소
            selenium_worker_task.cancel()
            try:
                await selenium_worker_task
            except asyncio.CancelledError:
                pass

            # Worker 재시작
            loop = asyncio.get_event_loop()
            selenium_worker_task = loop.create_task(selenium_job_executor())
            selenium_worker_last_heartbeat = time.time()
            selenium_worker_current_job = None

            logger.warning(
                f"✅ Selenium Worker 재시작 완료 (Queue 크기: {selenium_queue.qsize()}/25)",
                extra={"queue_size": selenium_queue.qsize()}
            )
        else:
            # 정상 상태 (작업 처리 중)
            logger.debug(
                f"💓 Worker 정상: [{selenium_worker_current_job}] 처리 중 ({int(elapsed)}초)",
                extra={"current_job": selenium_worker_current_job, "elapsed": int(elapsed)}
            )

    except Exception as e:
        logger.error("❌ Worker 헬스체크 실패", exc_info=True)


def _register_usdt_legacy_polling_job(target_scheduler) -> bool:
    """USDT legacy REST polling cron 조건부 등록.

    `config.USDT_LEGACY_REST_POLLING_ENABLED=true` 시 기존 cron(매분
    06,16,26,36,46,56초) 등록, false 시 미등록 + disabled 명시 log.

    Returns:
        True: job 등록됨. False: flag false라 미등록.

    설계:
        - WS + source-specific REST fallback이 같은 fanout(Redis/DB/Alert)을
          처리하므로 상시 polling은 default 미등록.
        - flag=true는 rollback 경로 — env로 토글 후 force-recreate fastapi.
        - test 친화 — start_scheduler 전체 호출 없이 본 helper만 검증 가능.
    """
    if not config.USDT_LEGACY_REST_POLLING_ENABLED:
        logger.info(
            "ℹ️ USDT legacy polling disabled (WS + fallback REST active). "
            "rollback: env USDT_LEGACY_REST_POLLING_ENABLED=true"
        )
        return False

    from app.crawlers.usdt_sources import collect_usdt_rates

    target_scheduler.add_job(
        collect_usdt_rates,
        CronTrigger(second='6,16,26,36,46,56', timezone=KST),
        id="usdt_sources",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=5,
    )
    logger.info("✅ USDT legacy polling 스케줄 등록 (매분 06,16,26,36,46,56초)")
    return True


def start_scheduler():
    global crawler_manager

    # ═════════════════════════════════════════════════════════════
    # CrawlerManager 초기화 (DB 로드) - Phase 1.8
    # ═════════════════════════════════════════════════════════════
    db = SessionLocal()
    try:
        crawler_manager.load_config(db)
        logger.info("✅ CrawlerManager 초기화 완료", extra={"config_count": len(crawler_manager.config_cache)})
    except Exception as e:
        logger.error("❌ CrawlerManager 초기화 실패", exc_info=True)
    finally:
        db.close()

    # ═════════════════════════════════════════════════════════════
    # WebSocket Broadcasting: scheduler는 매초 wake-up, main.py가 실행 cadence를 결정
    # ═════════════════════════════════════════════════════════════
    # - normal: 00/10/20/30/40/50초, fast 또는 fast window 안: 매초
    # - 운영 BROADCAST_MODE=window + BROADCAST_FAST_HOURS=0-24에서는 24시간 매초 실행
    # - 크롤러 초 슬롯은 broadcast 직전 정렬이 아니라 상호 부하 분산용
    # ─────────────────────────────────────────────────────────────
    # Note: AsyncIOScheduler는 async 함수를 직접 등록 가능
    from app.main import broadcast_rates_once  # 순환 import 방지 (함수 내부 import)

    # PR2: 매초 wake-up + main.py broadcast_rates_once 첫 줄에서 mode/second 분기 + early return.
    # normal 모드(default) 기본 동작은 기존 10초 cron과 정확히 동등 — DB 조회 빈도 회귀 0.
    # window/fast 모드 진입 시에만 매초 broadcast 실행. mode 전환은 BROADCAST_MODE env 변경 + 재시작.
    scheduler.add_job(
        broadcast_rates_once,  # async 함수 직접 등록
        CronTrigger(second='*', timezone=KST),
        id="websocket_broadcast",
        max_instances=1,
        misfire_grace_time=5
    )

    # PR3: Redis latest mirror job (REDIS_LATEST_ENABLED=true 시)
    # broadcast가 매초 DB SELECT를 실행하지 않도록 mirror가 LATEST_MIRROR_INTERVAL_SECONDS
    # 주기로 DB latest를 Redis로 동기화. 호출 함수는 latest_rates_cache.mirror_latest_rates_once
    # (no-arg async wrapper). env=false default라 코드 배포만으론 운영 영향 0.
    if REDIS_LATEST_ENABLED:
        from app.latest_rates_cache import mirror_latest_rates_once  # 함수 내부 import (broadcast_rates_once 패턴)
        scheduler.add_job(
            mirror_latest_rates_once,
            IntervalTrigger(seconds=LATEST_MIRROR_INTERVAL_SECONDS, timezone=KST),
            id="latest_mirror",
            max_instances=1,
            coalesce=True,  # Misfire 시 밀린 실행을 1번으로 합치기
            misfire_grace_time=LATEST_MIRROR_INTERVAL_SECONDS,
        )

    # P1b A2-2: write-mode cache — startup 1회 refresh + poll(N초). atomic_write_control row를
    # 읽어 atomic_write_runtime cache 갱신 (외부/admin/C6 control 변경 backstop). no-throw.
    # control table 없으면 read-fail→legacy (cache _INITIAL 유지) → writer legacy = behavior-change-0.
    from app.atomic_write_refresh import refresh_write_mode_cache  # 함수 내부 import (mirror 패턴)
    refresh_write_mode_cache()  # startup 1회 — writer가 stale _INITIAL 대신 현재 mode를 보게
    scheduler.add_job(
        refresh_write_mode_cache,
        IntervalTrigger(seconds=config.ATOMIC_MODE_POLL_INTERVAL_SECONDS, timezone=KST),
        id="atomic_write_mode_poll",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=config.ATOMIC_MODE_POLL_INTERVAL_SECONDS,
    )

    # PR6c-2d-1: KRX active contract reconcile (5/18 임시 안전모드).
    # 5분마다 master resolve → 현재 client contract 비교 → 다르면 rollover.
    # config.KRX_FUTURES_ENABLED=false 시 함수 내부에서 즉시 return.
    # 5/18 만기 통과 후 hybrid (06:01 + boundary)로 축소 검토 (PR6c-2d-5 후보).
    if config.KRX_FUTURES_ENABLED:
        scheduler.add_job(
            _reconcile_krx_futures_contract,
            IntervalTrigger(minutes=5, timezone=KST),
            id="krx_contract_reconcile",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

    # §12.9.8 ② — USDT WS 5-source collector task supervisor (주기 watchdog).
    # USDT_WS_SUPERVISOR_ENABLED=false default → job 미등록 (배포 ≠ 동작 변화, 0 경로).
    # 활성화 시 죽은 collector task를 감지 → shutdown_*→start_* 재시작 (KRX는 1차 제외 —
    # bootstrap+client 2-task + reconcile cron이라 별 case, task-death gap 잔존).
    if config.USDT_WS_SUPERVISOR_ENABLED:
        scheduler.add_job(
            _usdt_ws_supervisor_tick,
            IntervalTrigger(seconds=config.USDT_WS_SUPERVISOR_INTERVAL_SECONDS, timezone=KST),
            id="usdt_ws_supervisor",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=config.USDT_WS_SUPERVISOR_INTERVAL_SECONDS,
        )

    # 제어 작업: 매시 0분 1초 모드 확인 (4단계 모드: IN, BREAK1, BREAK2, OUT)
    # - 모드 전환 시점: 19:00 (BREAK1), 06:00 (BREAK2), 08:00 (IN), 토 07:00 (OUT 시작), 월 06:00 (OUT 종료 → BREAK2)
    scheduler.add_job(
        control_job,
        CronTrigger(second='1', timezone=KST),
        id="control_job"
    )

    # Queue 상태 모니터링: 10초마다 (포화 감지)
    scheduler.add_job(
        report_queue_status,
        IntervalTrigger(seconds=10, timezone=KST),
        id="queue_status",
        coalesce=True,  # Misfire 시 밀린 실행을 1번으로 합치기
        max_instances=1  # 동시 실행 방지
    )

    # ═════════════════════════════════════════════════════════════
    # Chrome 정리 + Worker 헬스체크: 매분 03, 23, 43초 (20초 균등 간격)
    # ═════════════════════════════════════════════════════════════
    # - cleanup 먼저 실행 → 60초+ Chrome 프로세스 정리
    # - health check 직후 실행 → Worker stuck 감지 (최대 20초 지연)
    # - cleanup 비용: ~0.004초 (Chrome 20개 기준), 무시 가능
    # - Worker 재시작 시 cleanup 한 번 더 호출 (이중 안전장치)
    # ─────────────────────────────────────────────────────────────
    for second in ['3', '23', '43']:
        # 1. Chrome 정리 먼저
        scheduler.add_job(
            cleanup_zombie_chrome_processes,
            CronTrigger(second=second, timezone=KST),
            id=f"cleanup_zombie_chrome_{second}",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=180
        )

        # 2. Worker 헬스체크 (cleanup 직후)
        scheduler.add_job(
            check_worker_health,
            CronTrigger(second=second, timezone=KST),
            id=f"worker_health_check_{second}",
            coalesce=True,
            max_instances=1
        )

    # 시스템 모니터링: 5분마다 (리소스 추적)
    scheduler.add_job(
        record_monitoring_stats,
        IntervalTrigger(minutes=5, timezone=KST),
        id="monitoring_stats",
        coalesce=True,  # Misfire 시 밀린 실행을 1번으로 합치기
        max_instances=1  # 동시 실행 방지
    )

    # user_devices retention cleanup: 매일 새벽 03:20:01 (KST)
    scheduler.add_job(
        cleanup_old_user_devices,
        CronTrigger(hour=3, minute=20, second=1, timezone=KST),
        id="cleanup_old_user_devices",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # 로그 파일 정리: 매일 새벽 03:29:01시
    scheduler.add_job(cleanup_old_log_files, CronTrigger(hour=3, minute=29, second=1, timezone=KST), id="cleanup_old_log_files")

    # 은행 데이터 정리: 매일 새벽 03:30:01시
    scheduler.add_job(cleanup_old_bank_data, CronTrigger(hour=3, minute=30, second=1, timezone=KST), id="cleanup_old_bank_data")

    # source_rates 정리: 매일 새벽 03:31:01시 (USDT Phase 1, 30일 보관)
    scheduler.add_job(cleanup_old_source_rates, CronTrigger(hour=3, minute=31, second=1, timezone=KST), id="cleanup_old_source_rates")

    # 시장지수 정리: 매일 새벽 03:32:01시 (DXY 현물/선물 realtime, 30일 보관)
    scheduler.add_job(cleanup_old_market_index_rates, CronTrigger(hour=3, minute=32, second=1, timezone=KST), id="cleanup_old_market_index_rates")

    # ═════════════════════════════════════════════════════════════
    # USDT 거래소 legacy REST polling (조건부)
    # ═════════════════════════════════════════════════════════════
    # - default 비활성 (config.USDT_LEGACY_REST_POLLING_ENABLED=false)
    # - WS 도입 전 과도기 잔재. WS + source-specific REST fallback probe가 동일
    #   fanout(Redis/DB/Alert)을 모두 처리하므로 상시 polling 중복.
    # - flag=true 시 기존 cron(매분 06,16,26,36,46,56초) 복원 — rollback 경로.
    # ─────────────────────────────────────────────────────────────
    _register_usdt_legacy_polling_job(scheduler)

    # ═════════════════════════════════════════════════════════════
    # 그래프 캐시 갱신: 매분 03초 (Phase 1A)
    # ═════════════════════════════════════════════════════════════
    # - Redis에 24시간 그래프 데이터 저장
    # - TTL 120초, 매분 갱신으로 신선도 유지
    # - 3개 통화 × 3개 소스 = 9개 데이터셋
    # ─────────────────────────────────────────────────────────────
    from app.admin.graph_cache import refresh_graph_cache

    scheduler.add_job(
        refresh_graph_cache,
        CronTrigger(second='3', timezone=KST),
        id="graph_cache_refresh",
        max_instances=1,
        coalesce=True
    )

    logger.info("✅ 그래프 캐시 갱신 스케줄 등록 (매분 03초)")

    # ═════════════════════════════════════════════════════════════
    # 그래프 v2 intraday 1d precompute: 10분 경계 + 12초 (*/10분 12초, 테더+usd/jpy/eur 4탭)
    # ═════════════════════════════════════════════════════════════
    # - 10분봉 close 직후 완료봉만 Redis(graph_v2:tab:{tab}:1d)에 갱신 (closed-bucket, 탭별 순회).
    # - 12초 오프셋: 수집기/DB write가 경계 직후 살짝 늦을 수 있어 흡수.
    # - 요청 경로(main.py)는 이 키를 read만, miss/stale-boundary 시에만 rebuild. 진행 중 봉은 iOS live-tail.
    # ─────────────────────────────────────────────────────────────
    from app.graph_v2_intraday import precompute_intraday_1d

    scheduler.add_job(
        precompute_intraday_1d,
        CronTrigger(minute='*/10', second=12, timezone=KST),
        id="graph_v2_intraday_1d_precompute",
        max_instances=1,
        coalesce=True
    )

    logger.info("✅ 그래프 v2 intraday 1d precompute 스케줄 등록 (*/10분 12초, 4탭)")

    # ═════════════════════════════════════════════════════════════
    # ADR-039 무료 hourly snapshot precompute: 매시 :30분 (무료 비구독 티어)
    # ═════════════════════════════════════════════════════════════
    # - 매시간 고정 갱신 스냅샷(rate + real hourly graph, KRX 제외)을 Redis(free:snapshot:{tab}:{period})에 SET.
    # - :30 basis(사용자 2026-07-18): as_of=HH:30 + rate/graph는 timestamp<=as_of cutoff(시간 계약) —
    #   09:00 개장 후 09:30 첫 무료 정보로 30분 만에 장 파악 가능. daily(:01)/hourly(:05/:07/:09/:11)
    #   append cron 이후라 최신 canonical row 반영 + 00:01 race 회피(N5)는 :20 시절과 동일하게 성립.
    # - 요청 경로(main.py)는 이 canonical만 read(DB 재생성 안 함, 무료=매시 고정) → Redis 장애 시 local last-good → 전무 시 503. enforcement 없음(무료=인증만).
    from app.free_snapshot import (
        FREE_SNAPSHOT_BASIS_MINUTE,
        FREE_SNAPSHOT_PRECOMPUTE_SECOND,
        precompute_free_snapshots,
    )

    scheduler.add_job(
        precompute_free_snapshots,
        # minute/second를 free_snapshot 공유 상수로 — refresh_not_before 계산이 이 타이밍을 기준(ETC, codex).
        # second=19: 정각 :30:00대의 유지보수/뉴스 job과 분리된 기존 초를 유지한다.
        # 배포 2 IN canary의 KB(:09/:19/.../:59)와는 매시 30분에 1회 같은 초에 시작하므로,
        # KB 응답시간과 scheduler_event를 smoke에서 관측한다. OUT DXY(:21)와는 겹치지 않는다.
        # 동시 시작 감소 목적이며 다른 job 완료를 보장하지 않는다. as_of는
        # basis_as_of(HH:30)라 발화 초와 무관하다.
        CronTrigger(minute=FREE_SNAPSHOT_BASIS_MINUTE, second=FREE_SNAPSHOT_PRECOMPUTE_SECOND, timezone=KST),
        id="free_snapshot_precompute",
        max_instances=1,
        coalesce=True
    )

    logger.info(
        "✅ ADR-039 무료 hourly snapshot precompute 스케줄 등록 (매시 :%02d:%02d)",
        FREE_SNAPSHOT_BASIS_MINUTE, FREE_SNAPSHOT_PRECOMPUTE_SECOND,
    )

    # ═════════════════════════════════════════════════════════════
    # DXY rollup: realtime → hourly/daily 집계
    # ═════════════════════════════════════════════════════════════
    # - hourly: 매시 :05분, 직전 완료 시간 집계
    # - daily: 매일 00:05 KST, 직전 완료일 집계
    # - idempotent (INSERT ON CONFLICT UPDATE)
    # ─────────────────────────────────────────────────────────────
    from app.admin.dxy_rollup import rollup_dxy_hourly, rollup_dxy_daily

    scheduler.add_job(
        rollup_dxy_hourly,
        CronTrigger(minute=5, second=0, timezone=KST),
        id="dxy_rollup_hourly",
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        rollup_dxy_daily,
        CronTrigger(hour=0, minute=5, second=0, timezone=KST),
        id="dxy_rollup_daily",
        max_instances=1,
        coalesce=True,
    )

    logger.info("✅ DXY rollup 스케줄 등록 (hourly=매시 :05, daily=00:05 KST)")

    # ═════════════════════════════════════════════════════════════
    # 뉴스 피드 수집: 5분마다, :45초 (Phase 1B)
    # ═════════════════════════════════════════════════════════════
    # - 모드 무관 (24시간 동일)
    # - ETag/Last-Modified 조건부 GET → 변경 없으면 304 (부하 최소)
    # - :45초 실행: 다른 정기 작업과 초 슬롯 분산
    # ─────────────────────────────────────────────────────────────
    from app.news.fetcher import fetch_all_news  # 순환 import 방지

    scheduler.add_job(
        fetch_all_news,
        CronTrigger(minute='*/5', second='45', timezone=KST),
        id="news_fetcher",
        max_instances=1,
        coalesce=True,
    )

    logger.info("✅ RSS 뉴스 수집 스케줄 등록 (5분마다 :45초)")

    # ═════════════════════════════════════════════════════════════
    # KB API 뉴스 수집: 5분마다, :15초 (Phase 1B-2)
    # ═════════════════════════════════════════════════════════════
    # - RSS보다 ~2시간 빠른 속보 소스
    # - RSS(:45)와 30초 간격으로 분산
    # - 인증 불필요 (공개 API)
    # ─────────────────────────────────────────────────────────────
    from app.news.kb_fetcher import fetch_kb_news  # 순환 import 방지

    scheduler.add_job(
        fetch_kb_news,
        CronTrigger(minute='*/5', second='15', timezone=KST),
        id="kb_news_fetcher",
        max_instances=1,
        coalesce=True,
    )

    logger.info("✅ KB API 뉴스 수집 스케줄 등록 (5분마다 :15초)")

    # 시작 시 즉시 모드 판별 및 등록
    control_job()

    # 스케줄러 시작
    scheduler.start()


# =============================================================================
# KRX 미국달러선물 lifecycle (KRX optional source 원칙)
# =============================================================================
# main.py lifespan은 호출만 (start_scheduler() 패턴 일관). 실제 lifecycle은
# 본 모듈에 집중. failure isolation: 모든 단계 실패가 다른 startup/shutdown
# 영향 0. background bootstrap으로 main.py blocking 방지.

# 모듈 globals — Optional, 시작 전 None
krx_bootstrap_task = None  # 비동기 bootstrap 추적 (shutdown cancel 용)
krx_futures_client = None  # KisFuturesClient 인스턴스
krx_futures_task = None    # client.start() 실행 중인 task


async def _run_krx_futures_client(client):
    """KisFuturesClient.start() wrapper — task crash 시 logger.exception.

    그냥 create_task만 두면 task 예외가 'Task exception was never retrieved'
    경고로만 남고 가시성 떨어짐. 본 wrapper로 broad exception 잡고 명시
    로깅. CancelledError는 propagate (shutdown 정상 흐름).
    """
    try:
        await client.start()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[krx] KisFuturesClient task crashed")


async def start_krx_futures_client():
    """KRX client startup — main.py lifespan blocking 방지 위해 즉시 return.

    실제 bootstrap (fetch + select + client 시작)은 background task에서.
    중복 호출 방지 (testing/재시작/수동 호출 시 중복 task 방지).
    """
    global krx_bootstrap_task

    if not config.KRX_FUTURES_ENABLED:
        logger.info("[krx] KRX_FUTURES_ENABLED=false, skip start")
        return

    # 중복 start 방지 — bootstrap 또는 client task 이미 진행 중이면 skip
    if krx_bootstrap_task is not None and not krx_bootstrap_task.done():
        logger.debug("[krx] bootstrap 진행 중, 중복 start 무시")
        return
    if krx_futures_task is not None and not krx_futures_task.done():
        logger.debug("[krx] client task 진행 중, 중복 start 무시")
        return

    # background bootstrap — main.py lifespan 즉시 진행
    krx_bootstrap_task = asyncio.create_task(_bootstrap_krx_futures_client())


async def resolve_active_krx_futures_contract():
    """현재 active한 KRX 미국달러선물 contract를 계산해서 반환 (PR6c-2c).

    Side-effect free — globals 변경 X, logging X. 호출자가 결과로 client
    생성 또는 contract 비교 결정. 실패 시 raise (caller가 try/except 처리).

    fetch + parse + select_active_usd_futures_contract 통합. fetch timeout
    5초로 짧게 — KRX 막혀도 빠르게 caller에 신호.

    추후 (PR6c-2d 또는 5/18 관찰 후) 세션 boundary 자동 재시작 시점에
    재사용 가능 — 같은 helper로 contract 변경 감지.

    Returns:
        ContractInfo 또는 None (contracts 0개 또는 모두 만료).

    Raises:
        Any exception from fetch/parse — caller가 logging/fallback 책임.
    """
    # 함수 내부 import — 순환 참조 방지 + 운영 미연결 시점 import 영향 0
    from app.sources.kis_master import (
        fetch_commodity_future_master,
        parse_commodity_future_master,
        select_active_usd_futures_contract,
    )

    raw = await asyncio.to_thread(fetch_commodity_future_master, timeout=5)
    contracts = await asyncio.to_thread(parse_commodity_future_master, raw)
    now_kst = datetime.now(KST).replace(tzinfo=None)
    return select_active_usd_futures_contract(contracts, now_kst)


async def _bootstrap_krx_futures_client(resolved_override=None):
    """background bootstrap — resolve + client 시작.

    main.py lifespan과 분리. 실패 시 logger.warning/exception + KRX만 비활성.
    finally 블록에서 자기 자신이 krx_bootstrap_task일 때만 cleanup —
    shutdown 경합 / 직접 호출 시 엉뚱한 task 참조 지우지 X.

    PR6c-2d-1 amend (2026-05-07): resolved_override 인자 추가.
    reconcile에서 이미 resolve한 contract를 직접 넘기면 두 번째 resolve를
    회피해 race + reliability 향상 (Codex 검토 Issue 2).

    Args:
        resolved_override: 이미 resolve된 ContractInfo. None이면 자체 resolve.
    """
    global krx_bootstrap_task, krx_futures_client, krx_futures_task

    # 함수 내부 import — 순환 참조 방지
    from app.crawlers.krx_kis import (
        KisAccessTokenManager,
        KisApprovalManager,
        KisFuturesClient,
        KrxAlertTickHandler,
        KrxCloseWindowWriter,
        KrxDbWriter,
        KrxRedisLatestWriter,
    )

    try:
        if resolved_override is not None:
            resolved = resolved_override
        else:
            try:
                resolved = await resolve_active_krx_futures_contract()
            except Exception:
                logger.exception("[krx] active contract resolve 실패 (격리)")
                return

        if resolved is None:
            logger.warning("[krx] active USD futures contract 없음, KRX 비활성")
            return

        app_key = os.getenv("KIS_APP_KEY")
        app_secret = os.getenv("KIS_APP_SECRET")
        if not app_key or not app_secret:
            logger.warning("[krx] KIS_APP_KEY/SECRET 미설정, KRX 비활성")
            return

        approval = KisApprovalManager(app_key=app_key, app_secret=app_secret)
        # PR6d-2b Stage B: REST fallback access_token manager (env=false default라
        # 만들어 두지만 실제 호출은 KRX_REST_FALLBACK_ENABLED=true 활성화 시점만)
        token_manager = KisAccessTokenManager(app_key=app_key, app_secret=app_secret)
        client = KisFuturesClient(
            approval, contract=resolved, access_token_manager=token_manager,
        )
        client.add_tick_handler(KrxDbWriter())
        # KRX_FANOUT_REFACTOR_PLAN §5.2 E — Stage E tick-level Redis writer
        # (KRX_REDIS_TICK_WRITE_ENABLED=true 시 활성). KrxDbWriter._sync_db_write가
        # flag true 시 DB-bound Redis write/trigger skip하고 본 handler가 tick-level
        # Redis SET + trigger 담당. close grace tick은 handler 자체에서 skip
        # (KrxCloseWindowWriter non-interference 정책).
        if config.KRX_REDIS_TICK_WRITE_ENABLED:
            client.add_tick_handler(KrxRedisLatestWriter())
        # F-1 (2026-05-26): KRX 가격 알림 evaluator 활성 시 등록.
        # KrxAlertTickHandler가 매 tick → AlertObservation 변환 + KrxAlertEvaluator
        # schedule. env false 시 handler 자체에 defensive guard 있어 등록되어도
        # 동작 안 함 — 안전망 + 등록은 flag 기준이라 메모리/coalescer instance 절감.
        # Close grace skip 없음 (alert는 종가 crossing 보존 우선).
        if config.KRX_ALERT_EVALUATOR_ENABLED:
            client.add_tick_handler(KrxAlertTickHandler())
        # KRX_CLOSE_SNAPSHOT_PLAN §5.2 Stage 5 (2026-05-17): close finalizer 활성 시 등록.
        # env false 시 KrxCloseWindowWriter.__call__이 early return이라 비활성 동작과 동일.
        if config.KRX_CLOSE_FINALIZER_ENABLED:
            client.add_tick_handler(KrxCloseWindowWriter())

        krx_futures_client = client
        krx_futures_task = asyncio.create_task(_run_krx_futures_client(client))
        logger.info(
            "[krx] KisFuturesClient 시작 — contract=%s expiry=%s",
            resolved.short_code, resolved.expiry_date.isoformat(),
        )
    except Exception:
        logger.exception("[krx] bootstrap 실패 (격리)")
        krx_futures_client = None
        krx_futures_task = None
    finally:
        # PR6c-2c — bootstrap 종료 시점 cleanup. current task가 자기 자신
        # (= krx_bootstrap_task)일 때만 globals 정리. shutdown 경합 / 직접
        # 호출 시 엉뚱한 task 참조 지우지 X.
        if asyncio.current_task() is krx_bootstrap_task:
            krx_bootstrap_task = None


async def shutdown_krx_futures_client():
    """KRX client + bootstrap 안전 종료.

    bootstrap 진행 중에 shutdown 호출되면 bootstrap도 cancel.
    """
    global krx_bootstrap_task, krx_futures_client, krx_futures_task

    # bootstrap 진행 중이면 먼저 cancel
    if krx_bootstrap_task is not None and not krx_bootstrap_task.done():
        krx_bootstrap_task.cancel()
        try:
            await krx_bootstrap_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[krx] bootstrap cancel 실패")
    krx_bootstrap_task = None

    if krx_futures_client is not None:
        try:
            await krx_futures_client.stop()
        except Exception:
            logger.exception("[krx] client.stop() 실패")

    if krx_futures_task is not None and not krx_futures_task.done():
        krx_futures_task.cancel()
        try:
            await krx_futures_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[krx] task await 실패")

    krx_futures_client = None
    krx_futures_task = None


# ─────────────────────────────────────────────────────────────
# PR6c-2d-1 — Active contract reconcile (5/18 임시 안전모드)
# ─────────────────────────────────────────────────────────────
# 5분마다 active KRX USD futures contract 재계산 + client 비교.
# 다르면 shutdown + start로 새 contract 적용 (rollover).
#
# 5/18 만기 통과 후 hybrid (06:01 daily + session boundary)로 축소
# 검토 (PR6c-2d-5 후보). 영구 정책 아님 — 임시 안전모드.
# ─────────────────────────────────────────────────────────────
def _is_next_contract_month(current_month: str, resolved_month: str) -> bool:
    """`resolved_month`가 `current_month`의 **정확한 다음 달**인가 ("YYYYMM").

    reconcile 점프 방지용. 날짜 산술(구 "만기 45일 차이") 대신 월물 인접성으로
    판정해 만기 계산 규칙과의 결합을 끊는다.

    True: 202608 → 202609, **202612 → 202701**(year wrap)
    False: 같은 달 / 역행 / 건너뜀(202608 → 202610) / 형식 오류

    형식이 깨지면 False = **fail-closed**. rollover를 보류하면 수집이 멈추지만
    (visible, `jump_suppressed` telemetry + 재시작 bootstrap으로 복구 가능),
    통과시키면 엉뚱한 월물을 구독한다 — 8/14 사고가 정확히 그 형태였다.
    """
    if len(current_month) != 6 or not current_month.isdigit():
        return False
    if len(resolved_month) != 6 or not resolved_month.isdigit():
        return False
    cy, cm = int(current_month[:4]), int(current_month[4:])
    ry, rm = int(resolved_month[:4]), int(resolved_month[4:])
    if not (1 <= cm <= 12 and 1 <= rm <= 12):
        return False
    # year 범위도 검증 — 구 버전은 "000001" → "000002"를 인접으로 통과시켜
    # "형식 오류 fail-closed" 서술과 반대였다 (codex 리뷰 발견).
    if not (date.min.year <= cy <= date.max.year):
        return False
    if not (date.min.year <= ry <= date.max.year):
        return False
    expected_y, expected_m = (cy + 1, 1) if cm == 12 else (cy, cm + 1)
    return (ry, rm) == (expected_y, expected_m)


async def _reconcile_krx_futures_contract():
    """5분 주기 contract reconcile (PR6c-2d-1).

    동작:
      1. KRX_FUTURES_ENABLED=false → 즉시 return
      2. resolve_active_krx_futures_contract() 호출 (실패 격리)
      3. resolved is None → warning + 기존 client 유지
      4. krx_futures_client is None → _bootstrap_krx_futures_client(resolved_override=resolved)
      5. resolved.short_code == current.short_code → no-op
      6. resolved가 current의 인접(다음) 월물이 아님 → 점프 의심 warning + 보류
      7. 정상 rollover → shutdown + _bootstrap_krx_futures_client(resolved_override=resolved)
         (Codex Issue 2 fix: 두 번째 resolve 회피)

    실패 격리: resolve / shutdown / start 실패 시 logger.exception + return.
    KRX outage 발생해도 다른 시스템 영향 0 (config.KRX_FUTURES_ENABLED 토글).

    5/18 만기 검증용. 통과 후 hybrid로 축소 검토.
    """
    if not config.KRX_FUTURES_ENABLED:
        return

    # KRX task-death gap + 기존 client-None bootstrap race guard: app shutdown 중에는
    # reconcile이 종료되는 client를 부활/재시작하지 않도록 즉시 return (공유 flag —
    # shutdown_krx의 stop()/task await ~ globals=None 구간을 'None skip'만으론 못 막음).
    if _collector_shutdown_initiated:
        return

    try:
        resolved = await resolve_active_krx_futures_contract()
    except Exception:
        logger.exception("[krx] reconcile resolve 실패 (격리)")
        _record_krx_reconcile(result="resolve_error")
        return

    if resolved is None:
        logger.warning(
            "[krx] reconcile: active USD futures contract 없음 — 수동 개입 필요"
        )
        _record_krx_reconcile(result="resolved_none")
        return

    # 1. client 없으면 bootstrap 시도 (격리된 재시작 후 복구 경로)
    # 이미 resolve한 결과를 직접 사용 (Codex Issue 2 — 두 번째 resolve 회피)
    if krx_futures_client is None:
        # PR6c-2d-1 amend (Codex 3회차 권고): main.py lifespan bootstrap이 진행 중이면 skip.
        # 5분 cron interval vs bootstrap 수초라 실제 race 가능성 낮지만 견고성 위해 가드.
        if krx_bootstrap_task is not None and not krx_bootstrap_task.done():
            logger.debug("[krx] reconcile: bootstrap 진행 중, skip (race 방지)")
            _record_krx_reconcile(result="bootstrap_skipped", resolved=resolved)
            return
        logger.info(
            "[krx] reconcile: client 없음 — bootstrap 시도 (resolved=%s)",
            resolved.short_code,
        )
        await _bootstrap_krx_futures_client(resolved_override=resolved)
        # PR6c-2d-2 fix (Codex BLOCKING): _bootstrap_krx_futures_client는 내부에서
        # 예외 catch + krx_futures_client=None으로 끝남 (raise X). 따라서 외부
        # try/except로는 실패 감지 불가. post-condition으로 globals 검증.
        # PR6c-2d-2 amend (Codex 2회차): rollover branch와 동일하게 contract 일치까지
        # 보수적으로 확인 — bootstrap_started metric 신뢰도 ↑.
        bootstrapped = getattr(krx_futures_client, "_contract", None)
        if bootstrapped is None or bootstrapped.short_code != resolved.short_code:
            _record_krx_reconcile(result="bootstrap_error", resolved=resolved)
        else:
            _record_krx_reconcile(result="bootstrap_started", resolved=resolved)
        return

    current = krx_futures_client._contract

    # KRX task-death gap (§12.9.8 ② KRX-side): client 객체는 잔존하나 client task만 죽은 경우.
    #   _run_krx_futures_client crash는 log-only + globals 미reset이라 client 비None + task
    #   done 상태로 고착 → 만기 동일 시 아래 no_op로 빠져 다음 rollover(최대 한 달)까지 사망.
    #   dead task면 contract mismatch 무관 shutdown_krx → bootstrap(resolved)로 부활 (rollover
    #   겸 처리). bootstrap-death(client set 전 사망)는 위 client-None 경로가 복구 — 본 분기는
    #   client 비None 단일 케이스. 5분 cron이 throttle이라 backoff 없음 (반복 시 ≤12/h, KIS 무해).
    if krx_futures_task is not None and krx_futures_task.done():
        if _collector_shutdown_initiated:
            return  # resolve await 중 shutdown 시작 — restart 보류 (belt-and-suspenders)
        logger.warning(
            "[krx] reconcile: client task dead (client 객체 잔존) — 재시작 (resolved=%s)",
            resolved.short_code,
        )
        try:
            await shutdown_krx_futures_client()
        except Exception:
            logger.exception("[krx] task-death restart shutdown 실패")
            _record_krx_reconcile(
                result="task_dead_restart_error", current=current, resolved=resolved,
            )
            return
        # rollover branch와 동일 패턴: resolved 직접 사용 (두 번째 resolve 회피).
        await _bootstrap_krx_futures_client(resolved_override=resolved)
        # post-condition (rollover branch와 동형): _bootstrap는 예외 catch라 raise 안 됨
        # → globals로 판정. client present + contract == resolved.
        if (
            krx_futures_client is None
            or krx_futures_client._contract.short_code != resolved.short_code
        ):
            _record_krx_reconcile(
                result="task_dead_restart_error", current=current, resolved=resolved,
            )
        else:
            _record_krx_reconcile(
                result="task_dead_restarted", current=current, resolved=resolved,
            )
        return

    # 2. 같은 contract → no-op (대부분 케이스)
    # PR6c-2d-2 (Codex 권고): no-op도 state 기록 — admin endpoint으로 reconcile 발화 검증 가능.
    # info log는 5분 cron × 24h = 288줄 폭증 차단 위해 추가 X (state만).
    if resolved.short_code == current.short_code:
        _record_krx_reconcile(result="no_op", current=current, resolved=resolved)
        return

    # 3. 점프 방지 — resolved가 current의 **정확한 다음 월물**인가
    #
    #    2026-08-16 변경: 구 구현은 `만기 차이 > 45일 또는 < 0`이라는 날짜 산술이었다.
    #    만기 계산에 휴장 보정(walk-back)이 들어가면서 이 임계값이 계산 규칙과
    #    결합된다 — 셋째 월요일 간 구조적 최대 간격 37일에 walk-back 한도 14일이
    #    더해지면 이론상 51일까지 벌어져 **정상 rollover가 가드에 막힐 수 있다**.
    #    (현행 holidays==0.97 실측은 최대 38일 / 전 보정 3일이라 발화하지 않지만,
    #     라이브러리 드리프트에 잠재 결합을 남기는 설계다.)
    #    월물 인접성은 같은 의도("master 데이터 오류 의심")를 날짜 산술 없이
    #    표현한다: 결번·역행·건너뜀이 전부 한 검사로 걸린다.
    if not _is_next_contract_month(current.contract_month, resolved.contract_month):
        logger.warning(
            "[krx] reconcile 점프 의심 %s/%s → %s/%s (인접 월물 아님) — 보류",
            current.short_code, current.contract_month,
            resolved.short_code, resolved.contract_month,
        )
        _record_krx_reconcile(result="jump_suppressed", current=current, resolved=resolved)
        return

    # 4. 정상 rollover
    logger.info(
        "[krx] rollover %s/%s → %s/%s reason=scheduled",
        current.short_code, current.contract_month,
        resolved.short_code, resolved.contract_month,
    )

    try:
        await shutdown_krx_futures_client()
    except Exception:
        logger.exception("[krx] reconcile shutdown 실패")
        _record_krx_reconcile(result="shutdown_error", current=current, resolved=resolved)
        return

    # PR6c-2d-1 amend (Codex Issue 2): resolved를 직접 넘겨 두 번째 resolve 회피.
    # 두 번째 resolve가 실패하면 기존 client 이미 shutdown됐고 새 client도 못 뜸 → 5분간 KRX outage.
    await _bootstrap_krx_futures_client(resolved_override=resolved)

    # PR6c-2d-2 fix (Codex BLOCKING): post-condition globals 검증.
    # _bootstrap_krx_futures_client 내부 예외 catch라 raise 안 됨 → globals로 판정.
    # rollover 성공 = krx_futures_client present + contract == resolved.
    # 잘못 rollover로 기록되면 rollover_count 부풀어 운영 metric 신뢰도 ↓.
    if (
        krx_futures_client is None
        or krx_futures_client._contract.short_code != resolved.short_code
    ):
        # PR6c-2d-2 amend (Codex 2회차): except 블록 밖이므로 logger.exception 사용 시
        # sys.exc_info()=(None,None,None)이 되어 "NoneType: None" 가짜 traceback이 찍힘.
        # 운영 로그 혼동 방지 위해 logger.error 사용.
        logger.error("[krx] reconcile rollover bootstrap 실패")
        _record_krx_reconcile(
            result="bootstrap_error", current=current, resolved=resolved,
        )
        return

    # PR6c-2d-2: 정상 rollover 완료 — count 증가
    _record_krx_reconcile(
        result="rollover", current=current, resolved=resolved, increment_rollover=True,
    )


# ─────────────────────────────────────────────────────────────
# PR6c-2d-2 — Reconcile state + admin endpoint 노출 (Codex 권고)
# ─────────────────────────────────────────────────────────────

# reconcile 마지막 실행 상태. 5/18 만기 검증 시 admin endpoint으로 reconcile이
# 정상 발화하고 결과가 무엇인지 즉시 확인. no-op은 5분마다 발생하므로 info log는
# 폭증 차단을 위해 추가하지 않고 state만 갱신.
_krx_reconcile_state: Dict[str, Any] = {
    "last_run_at_kst": None,
    "last_result": None,
    "last_current_contract": None,
    "last_resolved_contract": None,
    "rollover_count": 0,
}


def _contract_to_dict(contract) -> Optional[Dict[str, Any]]:
    """ContractInfo → admin/디버깅용 dict (None 그대로 전파)."""
    if contract is None:
        return None
    return {
        "code": contract.short_code,
        "month": contract.contract_month,
        "expires_on": contract.expiry_date.isoformat(),
    }


def _record_krx_reconcile(
    *,
    result: str,
    current=None,
    resolved=None,
    increment_rollover: bool = False,
) -> None:
    """reconcile branch state 기록 (PR6c-2d-2)."""
    _krx_reconcile_state["last_run_at_kst"] = datetime.now(KST).isoformat()
    _krx_reconcile_state["last_result"] = result
    _krx_reconcile_state["last_current_contract"] = _contract_to_dict(current)
    _krx_reconcile_state["last_resolved_contract"] = _contract_to_dict(resolved)
    if increment_rollover:
        _krx_reconcile_state["rollover_count"] += 1


def get_krx_reconcile_status() -> Dict[str, Any]:
    """admin endpoint용 reconcile 상태 (PR6c-2d-2).

    KRX_FUTURES_ENABLED=false 또는 scheduler 초기화 전이면 job_registered=false,
    last_* fields는 None. shape는 항상 동일 — admin UI / grep / jq 단순화.
    """
    job = scheduler.get_job("krx_contract_reconcile")
    next_run_at_kst = None
    if job is not None and job.next_run_time is not None:
        next_run_at_kst = job.next_run_time.astimezone(KST).isoformat()
    return {
        "job_registered": job is not None,
        "next_run_at_kst": next_run_at_kst,
        **_krx_reconcile_state,
    }


# ─────────────────────────────────────────────────────────────
# USDT WebSocket — Upbit canary lifecycle (Phase B.1 PR1)
# ─────────────────────────────────────────────────────────────
# USDT_WS_DESIGN_PLAN §12 PR1 — feature flag + lifecycle scaffolding only.
# 외부 connection / Redis / DB / alert / fallback 모두 X (PR2~PR7에서 추가).
#
# main.py lifespan에서 start/shutdown 호출. APScheduler job 등록 X
# (long-running task이므로 cron 부적합).
#
# 5거래소 확장 (Bithumb~Gopax) 시 동일 패턴 per-source 추가 예정.
# 현 단계는 Upbit 단독.
# ─────────────────────────────────────────────────────────────

# 모듈 globals — Optional, 시작 전 None
usdt_ws_upbit_client = None  # UpbitWsClient 인스턴스
usdt_ws_upbit_task = None    # client.start() 실행 중인 task


async def _run_usdt_ws_upbit_client(client):
    """UpbitWsClient.start() wrapper — task crash 시 logger.exception.

    KRX `_run_krx_futures_client` 패턴 동일. CancelledError는 propagate.
    """
    try:
        await client.start()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[usdt_ws.upbit] UpbitWsClient task crashed")


async def start_usdt_ws_upbit_client():
    """USDT WS Upbit client startup — main.py lifespan에서 호출.

    USDT_WS_UPBIT_ENABLED=false 시 즉시 return (lifecycle 비활성).
    중복 호출 방지 (test/재시작 시 중복 task 방지).
    """
    global usdt_ws_upbit_client, usdt_ws_upbit_task

    if not config.USDT_WS_UPBIT_ENABLED:
        logger.info("[usdt_ws.upbit] USDT_WS_UPBIT_ENABLED=false, skip start")
        return

    # 중복 start 방지 — client task 이미 진행 중이면 skip
    if usdt_ws_upbit_task is not None and not usdt_ws_upbit_task.done():
        logger.debug("[usdt_ws.upbit] client task 진행 중, 중복 start 무시")
        return

    # 함수 내부 import — 순환 참조 방지 + 미연결 시점 import 영향 0
    from app.crawlers.usdt_ws.upbit import UpbitWsClient

    client = UpbitWsClient()
    usdt_ws_upbit_client = client
    usdt_ws_upbit_task = asyncio.create_task(_run_usdt_ws_upbit_client(client))
    logger.info("[usdt_ws.upbit] UpbitWsClient skeleton 시작 (PR1)")


async def shutdown_usdt_ws_upbit_client():
    """USDT WS Upbit client + task 안전 종료.

    client.stop() → task cancel/await → globals 초기화.
    """
    global usdt_ws_upbit_client, usdt_ws_upbit_task

    if usdt_ws_upbit_client is not None:
        try:
            await usdt_ws_upbit_client.stop()
        except Exception:
            logger.exception("[usdt_ws.upbit] client.stop() 실패")

    if usdt_ws_upbit_task is not None and not usdt_ws_upbit_task.done():
        usdt_ws_upbit_task.cancel()
        try:
            await usdt_ws_upbit_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[usdt_ws.upbit] task await 실패")

    usdt_ws_upbit_client = None
    usdt_ws_upbit_task = None


# ─────────────────────────────────────────────────────────────
# USDT WS Bithumb lifecycle (Phase B.3 Stage U2 skeleton)
# USDT_WS_DESIGN_PLAN §12.5 (2026-05-17). Upbit 패턴 mirror.
# Canary 활성화 조건: KRX close finalizer 5/18~5/19 첫 실측 + 7일 telemetry 안정 후 별도 deploy GO.
# ─────────────────────────────────────────────────────────────

# 모듈 globals — Optional, 시작 전 None
usdt_ws_bithumb_client = None  # BithumbWsClient 인스턴스
usdt_ws_bithumb_task = None    # client.start() 실행 중인 task


async def _run_usdt_ws_bithumb_client(client):
    """BithumbWsClient.start() wrapper — task crash 시 logger.exception.

    Upbit `_run_usdt_ws_upbit_client` 패턴 동일. CancelledError는 propagate.
    """
    try:
        await client.start()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[usdt_ws.bithumb] BithumbWsClient task crashed")


async def start_usdt_ws_bithumb_client():
    """USDT WS Bithumb client startup — main.py lifespan에서 호출.

    USDT_WS_BITHUMB_ENABLED=false 시 즉시 return (lifecycle 비활성).
    중복 호출 방지 (test/재시작 시 중복 task 방지).

    Stage U2 acceptance (USDT_WS_DESIGN_PLAN §12.5.2 핵심):
        flag=false 시 함수 즉시 return + BithumbWsClient 생성 X + network connect X +
        Redis/DB writer X. 본 stage 이후 U3-U6 운영 영향 0 보장.
    """
    global usdt_ws_bithumb_client, usdt_ws_bithumb_task

    if not config.USDT_WS_BITHUMB_ENABLED:
        logger.info("[usdt_ws.bithumb] USDT_WS_BITHUMB_ENABLED=false, skip start")
        return

    # 중복 start 방지 — client task 이미 진행 중이면 skip
    if usdt_ws_bithumb_task is not None and not usdt_ws_bithumb_task.done():
        logger.debug("[usdt_ws.bithumb] client task 진행 중, 중복 start 무시")
        return

    # 함수 내부 import — 순환 참조 방지 + 미연결 시점 import 영향 0
    from app.crawlers.usdt_ws.bithumb import BithumbWsClient

    client = BithumbWsClient()
    usdt_ws_bithumb_client = client
    usdt_ws_bithumb_task = asyncio.create_task(_run_usdt_ws_bithumb_client(client))
    logger.info("[usdt_ws.bithumb] BithumbWsClient task 시작")


async def shutdown_usdt_ws_bithumb_client():
    """USDT WS Bithumb client + task 안전 종료.

    client.stop() → task cancel/await → globals 초기화.
    """
    global usdt_ws_bithumb_client, usdt_ws_bithumb_task

    if usdt_ws_bithumb_client is not None:
        try:
            await usdt_ws_bithumb_client.stop()
        except Exception:
            logger.exception("[usdt_ws.bithumb] client.stop() 실패")

    if usdt_ws_bithumb_task is not None and not usdt_ws_bithumb_task.done():
        usdt_ws_bithumb_task.cancel()
        try:
            await usdt_ws_bithumb_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[usdt_ws.bithumb] task await 실패")

    usdt_ws_bithumb_client = None
    usdt_ws_bithumb_task = None


# ─────────────────────────────────────────────────────────────
# USDT WS Coinone lifecycle (Phase B.4 Stage C2 skeleton)
# USDT_WS_DESIGN_PLAN §12.6 (2026-05-19). Bithumb 패턴 mirror.
# Canary 활성화 조건: KRX close finalizer 5/26 telemetry 안정 + C2~C7 land 안정 후 별도 deploy GO.
# ─────────────────────────────────────────────────────────────

# 모듈 globals — Optional, 시작 전 None
usdt_ws_coinone_client = None  # CoinoneWsClient 인스턴스
usdt_ws_coinone_task = None    # client.start() 실행 중인 task


async def _run_usdt_ws_coinone_client(client):
    """CoinoneWsClient.start() wrapper — task crash 시 logger.exception.

    Bithumb `_run_usdt_ws_bithumb_client` 패턴 동일. CancelledError는 propagate.
    """
    try:
        await client.start()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[usdt_ws.coinone] CoinoneWsClient task crashed")


async def start_usdt_ws_coinone_client():
    """USDT WS Coinone client startup — main.py lifespan에서 호출.

    USDT_WS_COINONE_ENABLED=false 시 즉시 return (lifecycle 비활성).
    중복 호출 방지 (test/재시작 시 중복 task 방지).

    Stage C2 acceptance (USDT_WS_DESIGN_PLAN §12.6.4 핵심):
        flag=false 시 함수 즉시 return + CoinoneWsClient 생성 X + network connect X +
        Redis/DB writer X. 본 stage 이후 C3-C7 운영 영향 0 보장.
    """
    global usdt_ws_coinone_client, usdt_ws_coinone_task

    if not config.USDT_WS_COINONE_ENABLED:
        logger.info("[usdt_ws.coinone] USDT_WS_COINONE_ENABLED=false, skip start")
        return

    # 중복 start 방지 — client task 이미 진행 중이면 skip
    if usdt_ws_coinone_task is not None and not usdt_ws_coinone_task.done():
        logger.debug("[usdt_ws.coinone] client task 진행 중, 중복 start 무시")
        return

    # 함수 내부 import — 순환 참조 방지 + 미연결 시점 import 영향 0
    from app.crawlers.usdt_ws.coinone import CoinoneWsClient

    client = CoinoneWsClient()
    usdt_ws_coinone_client = client
    usdt_ws_coinone_task = asyncio.create_task(_run_usdt_ws_coinone_client(client))
    logger.info("[usdt_ws.coinone] CoinoneWsClient task 시작")


async def shutdown_usdt_ws_coinone_client():
    """USDT WS Coinone client + task 안전 종료.

    client.stop() → task cancel/await → globals 초기화.
    """
    global usdt_ws_coinone_client, usdt_ws_coinone_task

    if usdt_ws_coinone_client is not None:
        try:
            await usdt_ws_coinone_client.stop()
        except Exception:
            logger.exception("[usdt_ws.coinone] client.stop() 실패")

    if usdt_ws_coinone_task is not None and not usdt_ws_coinone_task.done():
        usdt_ws_coinone_task.cancel()
        try:
            await usdt_ws_coinone_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[usdt_ws.coinone] task await 실패")

    usdt_ws_coinone_client = None
    usdt_ws_coinone_task = None


# ─────────────────────────────────────────────────────────────
# USDT WS Korbit lifecycle (Phase B.5 Stage K2 skeleton)
# USDT_WS_DESIGN_PLAN §12.7 (2026-05-19). Coinone 패턴 mirror.
# Canary 활성화 조건: Coinone canary 24h+ 안정 + K2~K7 land 안정 후 별도 deploy GO.
# ─────────────────────────────────────────────────────────────

# 모듈 globals — Optional, 시작 전 None
usdt_ws_korbit_client = None  # KorbitWsClient 인스턴스
usdt_ws_korbit_task = None    # client.start() 실행 중인 task


async def _run_usdt_ws_korbit_client(client):
    """KorbitWsClient.start() wrapper — task crash 시 logger.exception.

    Coinone `_run_usdt_ws_coinone_client` 패턴 동일. CancelledError는 propagate.
    """
    try:
        await client.start()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[usdt_ws.korbit] KorbitWsClient task crashed")


async def start_usdt_ws_korbit_client():
    """USDT WS Korbit client startup — main.py lifespan에서 호출.

    USDT_WS_KORBIT_ENABLED=false 시 즉시 return (lifecycle 비활성).
    중복 호출 방지 (test/재시작 시 중복 task 방지).

    Stage K2 acceptance (USDT_WS_DESIGN_PLAN §12.7.4 핵심):
        flag=false 시 함수 즉시 return + KorbitWsClient 생성 X + network connect X +
        Redis/DB writer X. 본 stage 이후 K3-K7 운영 영향 0 보장.
    """
    global usdt_ws_korbit_client, usdt_ws_korbit_task

    if not config.USDT_WS_KORBIT_ENABLED:
        logger.info("[usdt_ws.korbit] USDT_WS_KORBIT_ENABLED=false, skip start")
        return

    # 중복 start 방지 — client task 이미 진행 중이면 skip
    if usdt_ws_korbit_task is not None and not usdt_ws_korbit_task.done():
        logger.debug("[usdt_ws.korbit] client task 진행 중, 중복 start 무시")
        return

    # 함수 내부 import — 순환 참조 방지 + 미연결 시점 import 영향 0
    from app.crawlers.usdt_ws.korbit import KorbitWsClient

    client = KorbitWsClient()
    usdt_ws_korbit_client = client
    usdt_ws_korbit_task = asyncio.create_task(_run_usdt_ws_korbit_client(client))
    logger.info("[usdt_ws.korbit] KorbitWsClient task 시작")


async def shutdown_usdt_ws_korbit_client():
    """USDT WS Korbit client + task 안전 종료.

    client.stop() → task cancel/await → globals 초기화.
    """
    global usdt_ws_korbit_client, usdt_ws_korbit_task

    if usdt_ws_korbit_client is not None:
        try:
            await usdt_ws_korbit_client.stop()
        except Exception:
            logger.exception("[usdt_ws.korbit] client.stop() 실패")

    if usdt_ws_korbit_task is not None and not usdt_ws_korbit_task.done():
        usdt_ws_korbit_task.cancel()
        try:
            await usdt_ws_korbit_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[usdt_ws.korbit] task await 실패")

    usdt_ws_korbit_client = None
    usdt_ws_korbit_task = None


# ─────────────────────────────────────────────────────────────
# USDT WS Gopax lifecycle (Phase B.6 Stage G1 skeleton)
# USDT_WS_DESIGN_PLAN §12.9 (2026-05-21). Bithumb/Coinone/Korbit 패턴 mirror.
# Canary 활성화 조건: Korbit/Coinone canary 24h+ 안정 + G2~G7 land 안정 후 별도 deploy GO.
# G1 (현재): flag=true 시에도 stop_event 대기만, network 호출 없음.
# ─────────────────────────────────────────────────────────────

# 모듈 globals — Optional, 시작 전 None
usdt_ws_gopax_client = None  # GopaxWsClient 인스턴스
usdt_ws_gopax_task = None    # client.start() 실행 중인 task


async def _run_usdt_ws_gopax_client(client):
    """GopaxWsClient.start() wrapper — task crash 시 logger.exception.

    Bithumb/Coinone/Korbit `_run_usdt_ws_*_client` 패턴 동일. CancelledError는 propagate.
    """
    try:
        await client.start()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[usdt_ws.gopax] GopaxWsClient task crashed")


async def start_usdt_ws_gopax_client():
    """USDT WS Gopax client startup — main.py lifespan에서 호출.

    USDT_WS_GOPAX_ENABLED=false 시 즉시 return (lifecycle 비활성).
    중복 호출 방지.

    Stage G1 acceptance (USDT_WS_DESIGN_PLAN §12.9.1):
        flag=false 시 함수 즉시 return + GopaxWsClient 생성 X + network connect X +
        Redis/DB writer X. 본 stage 이후 G2-G7 운영 영향 0 보장.
    """
    global usdt_ws_gopax_client, usdt_ws_gopax_task

    if not config.USDT_WS_GOPAX_ENABLED:
        logger.info("[usdt_ws.gopax] USDT_WS_GOPAX_ENABLED=false, skip start")
        return

    # 중복 start 방지 — client task 이미 진행 중이면 skip
    if usdt_ws_gopax_task is not None and not usdt_ws_gopax_task.done():
        logger.debug("[usdt_ws.gopax] client task 진행 중, 중복 start 무시")
        return

    # 함수 내부 import — 순환 참조 방지 + 미연결 시점 import 영향 0
    from app.crawlers.usdt_ws.gopax import GopaxWsClient

    client = GopaxWsClient()
    usdt_ws_gopax_client = client
    usdt_ws_gopax_task = asyncio.create_task(_run_usdt_ws_gopax_client(client))
    logger.info("[usdt_ws.gopax] GopaxWsClient task 시작")


async def shutdown_usdt_ws_gopax_client():
    """USDT WS Gopax client + task 안전 종료.

    client.stop() → task cancel/await → globals 초기화.
    """
    global usdt_ws_gopax_client, usdt_ws_gopax_task

    if usdt_ws_gopax_client is not None:
        try:
            await usdt_ws_gopax_client.stop()
        except Exception:
            logger.exception("[usdt_ws.gopax] client.stop() 실패")

    if usdt_ws_gopax_task is not None and not usdt_ws_gopax_task.done():
        usdt_ws_gopax_task.cancel()
        try:
            await usdt_ws_gopax_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[usdt_ws.gopax] task await 실패")

    usdt_ws_gopax_client = None
    usdt_ws_gopax_task = None


# ═════════════════════════════════════════════════════════════
# §12.9.8 ② — 5-source USDT WS collector task supervisor
# ═════════════════════════════════════════════════════════════
# ①(silent-stale reconnect)은 살아있는 task 안의 reconnect라, task 자체가 죽으면
# (client.start() 예외 escape / cancel) 미커버. _run_usdt_ws_*_client의 except는
# logger.exception만 → 부활 X. 본 supervisor가 주기 watchdog으로 done() task를 감지 →
# shutdown_*(idempotent teardown) → start_*(fresh client+task). 재시작 메커니즘은
# start_*의 `not task.done()` dedup 가드 재사용 (새 메커니즘 불필요).
# USDT_WS_SUPERVISOR_ENABLED=false default → job 미등록 (배포 ≠ 동작 변화, 0 경로).

# app shutdown 중 collector 재시작 로직(USDT supervisor tick + KRX reconcile task-death
# 분기)이 종료되는 task/client를 되살리지 못하도록 차단하는 **공유** flag. main.py
# lifespan이 USDT/KRX shutdown 체인 전에 signal_collector_shutdown_initiated()로 set.
# 각 shutdown_* 내부 set 금지 — 부분 shutdown 시 영구 비활성 + 호출자 자신이 shutdown_*를
# 호출하므로. "app shutdown 시작"은 단일 사실 → 단일 flag (USDT/KRX 이중 flag로 두면
# 미래에 한쪽만 set하는 silent-rot 위험 — 단일 진실 소스, DRY).
_collector_shutdown_initiated = False

# per-source backoff state — crash-restart storm 방지.
#   consecutive: 연속 재시작 횟수 (backoff escalation 기준)
#   last_restart_at: 마지막 재시작 epoch (5분+ 생존 시 counter reset 판정)
#   next_allowed_at: 다음 재시작 허용 epoch (backoff throttle)
_usdt_ws_supervisor_state: dict = {}

_SUPERVISOR_RESET_AFTER_SEC = 300.0    # 마지막 restart 이후 5분 생존 시 backoff counter reset
_SUPERVISOR_BACKOFF_BASE_SEC = 60.0    # consecutive 2부터 60s × 2^(n-2)
_SUPERVISOR_BACKOFF_MAX_SEC = 300.0    # backoff 상한 (영구 포기 X — cap 후에도 재시도 유지)


def signal_collector_shutdown_initiated():
    """app shutdown 시작 시 호출 (main.py lifespan, USDT/KRX shutdown 체인 전).

    collector 재시작 로직(USDT supervisor tick + KRX reconcile task-death 분기)이
    '종료 중인 task/client'를 death로 오인 → 부활/재시작하지 못하도록 **공유** flag set.
    shutdown_*의 `await stop()`/`await task` ~ globals=None 구간에서 'None skip'만으론
    race가 뚫림 (main.py shutdown 순서 직접 확인). §12.9.8 ② + KRX task-death gap 공용.
    """
    global _collector_shutdown_initiated
    _collector_shutdown_initiated = True


def reset_usdt_ws_supervisor_state():
    """테스트/재시작용 — collector shutdown flag + USDT supervisor backoff state 초기화."""
    global _collector_shutdown_initiated
    _collector_shutdown_initiated = False
    _usdt_ws_supervisor_state.clear()


def _compute_supervisor_backoff(consecutive: int) -> float:
    """연속 재시작 횟수 → 다음 재시작까지 backoff(초). consecutive 1=즉시(다음 tick),
    2부터 지수(60·120·240…) cap 300s. 영구 포기 X (cap 도달 후에도 5분마다 재시도)."""
    if consecutive <= 1:
        return 0.0
    return min(
        _SUPERVISOR_BACKOFF_BASE_SEC * (2 ** (consecutive - 2)),
        _SUPERVISOR_BACKOFF_MAX_SEC,
    )


# registry: (name, flag_getter, shutdown_fn, start_fn, task_getter).
# task_getter는 module-global lambda — start_*가 reassign하는 최신 task를 call-time에 읽음.
_USDT_WS_SUPERVISOR_REGISTRY = [
    ("upbit", lambda: config.USDT_WS_UPBIT_ENABLED,
     shutdown_usdt_ws_upbit_client, start_usdt_ws_upbit_client, lambda: usdt_ws_upbit_task),
    ("bithumb", lambda: config.USDT_WS_BITHUMB_ENABLED,
     shutdown_usdt_ws_bithumb_client, start_usdt_ws_bithumb_client, lambda: usdt_ws_bithumb_task),
    ("coinone", lambda: config.USDT_WS_COINONE_ENABLED,
     shutdown_usdt_ws_coinone_client, start_usdt_ws_coinone_client, lambda: usdt_ws_coinone_task),
    ("korbit", lambda: config.USDT_WS_KORBIT_ENABLED,
     shutdown_usdt_ws_korbit_client, start_usdt_ws_korbit_client, lambda: usdt_ws_korbit_task),
    ("gopax", lambda: config.USDT_WS_GOPAX_ENABLED,
     shutdown_usdt_ws_gopax_client, start_usdt_ws_gopax_client, lambda: usdt_ws_gopax_task),
]


async def _usdt_ws_supervisor_tick():
    """§12.9.8 ② — watchdog 1회: 죽은 USDT WS collector task 부활.

    각 source: flag enabled + not shutting_down + task.done() → backoff 통과 시
    shutdown_*(teardown) → start_*(fresh). 정상 running은 skip(중복 방지), 단 마지막
    restart 이후 5분+ 생존 시 backoff counter reset (crash-loop은 5분 생존 못 해 escalate
    지속, 일시 crash 후 안정만 reset). task None(미시작/정상종료/disabled)은 자연 skip.
    per-source 격리 — 한 source 실패가 다른 source watchdog를 막지 않음.
    """
    if _collector_shutdown_initiated:
        return
    now = time.time()
    for name, flag_getter, shutdown_fn, start_fn, task_getter in _USDT_WS_SUPERVISOR_REGISTRY:
        try:
            if not flag_getter():
                continue  # source disabled
            task = task_getter()
            if task is None:
                continue  # 미시작 / 정상 종료 후 globals None
            state = _usdt_ws_supervisor_state.setdefault(
                name, {"consecutive": 0, "last_restart_at": None, "next_allowed_at": 0.0},
            )
            if not task.done():
                # 건강하게 running — 마지막 restart 이후 5분+ 생존 시 backoff counter reset.
                # (단일-tick-alive reset 금지: 45s-crash-loop이 30s tick에서 'alive 1회
                #  관측 → reset → 재crash'를 반복해 backoff 무력화되기 때문.)
                if (state["consecutive"] > 0 and state["last_restart_at"] is not None
                        and now - state["last_restart_at"] >= _SUPERVISOR_RESET_AFTER_SEC):
                    logger.info(
                        "[usdt_ws.supervisor] %s 5분+ 생존 — backoff counter reset (was %d)",
                        name, state["consecutive"],
                    )
                    state["consecutive"] = 0
                    state["last_restart_at"] = None
                continue
            # task.done() = crash/비정상 종료 → 재시작 후보
            if now < state["next_allowed_at"]:
                continue  # backoff throttle
            if _collector_shutdown_initiated:
                return  # restart 직전 재확인 (belt-and-suspenders)
            consecutive = state["consecutive"] + 1
            logger.warning(
                "[usdt_ws.supervisor] %s task dead — restart (consecutive=%d, next backoff=%.0fs)",
                name, consecutive, _compute_supervisor_backoff(consecutive),
            )
            try:
                await shutdown_fn()   # #8 idempotent teardown (구 client 잔여물 정리)
                await start_fn()      # fresh client+task (start_* dedup 재사용)
            except Exception:
                logger.exception("[usdt_ws.supervisor] %s restart 실패", name)
            # state 갱신은 restart try/except 밖 — 실패한 restart 시도도 backoff 소비.
            # (의도: 지속 실패 source의 retry storm 방지. '실패는 카운트 안 함'으로
            #  리팩토링하면 restart-fail-restart storm 재발 — 변경 금지.)
            state["consecutive"] = consecutive
            state["last_restart_at"] = now
            state["next_allowed_at"] = now + _compute_supervisor_backoff(consecutive)
        except Exception:
            logger.exception("[usdt_ws.supervisor] %s tick 처리 실패 (격리)", name)
