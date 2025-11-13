# app/scheduler.py

# 표준 라이브러리
import asyncio
import logging
import sys
import time
from datetime import datetime
from typing import Callable

# 서드파티 라이브러리
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone

# 로컬 애플리케이션
from app.crawlers import investing
from app.crawlers import hana
from app.crawlers import kb
from app.crawlers import woori
from app.crawlers import bs
from app.crawlers import citi
# Selenium 크롤러(shinhan, ibk, nh, sc)는 subprocess로 실행되므로 import 불필요
from app.crawlers.constants import SELENIUM_PRIORITY_MAP, SELENIUM_TIMEOUT_MAP
from app import crud
from app.database import SessionLocal
from app.admin.crawler_stats import crawler_stats

# 로거 설정
logger = logging.getLogger("exchange_rate.scheduler")

# 한국 시간대 스케줄러 인스턴스 생성
KST = timezone('Asia/Seoul')
scheduler = AsyncIOScheduler(timezone=KST)

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
# 크롤러 그룹 정의 (2025-11-10 재설계)
# ═════════════════════════════════════════════════════════════
# t3.small/medium 환경 최적화를 위한 3-Tier 스케줄링
#
# A Group: investing (순수 Request)
#   - 가장 중요한 기준 환율
#   - IN: 10초마다 (Broadcasting 5초 전)
#   - OUT: 10분마다 (매시간 05, 15, 25, 35, 45, 55분)
#
# B Group: Request 기반 (일부 하이브리드 폴백)
#   - kb, hana: 중요, 빈도 높음
#     - IN: 20초마다 (Broadcasting 5초 전, 서로 10초 엇갈림)
#     - OUT: 10분마다 (kb: 05,15,25..., hana: 00,10,20...)
#   - woori, bs, citi: 일반, 빈도 낮음
#     - IN: 60초마다 (Broadcasting 7초 전, 20초씩 엇갈림)
#     - OUT: 60분마다 (woori: 13분, bs: 33분, citi: 53분)
#   - 하이브리드: hana, woori, bs는 Request → Selenium 폴백 가능
#
# C Group: Selenium 기반 (Queue 순차 처리)
#   - shinhan, ibk, nh, sc
#   - IN: interval (33.3 ~ 150초, Broadcasting 독립)
#   - OUT: cron (60분마다, 완전 분산)
#   - ibk는 Request → Selenium 하이브리드 (시간대 체크)
# ─────────────────────────────────────────────────────────────

# 현재 모드 상태 저장
current_mode = None


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
        logger.info(f"⚡ [{bank_name}] subprocess 크롤링 시작 (타임아웃: {timeout}초)")

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


def is_in_time(now: datetime) -> bool:
    """월요일 04:00 ~ 토요일 07:59 구간인지 판별"""
    weekday = now.weekday()  # 월=0, 화=1 ... 일=6
    hour = now.hour

    return (
        (weekday == 0 and hour >= 4) or
        (weekday in [1, 2, 3, 4]) or
        (weekday == 5 and hour < 8)
    )

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
    모드에 따라 작업 재등록 (IN: cron 절대 시간, OUT: cron 시간 단위)

    [2025-11-10 재설계]
    - IN 모드: cron 절대 시간 동기화 (Broadcasting 동기화)
    - OUT 모드: cron 시간 단위 (완전 분산, 동시 실행 0개)
    - Queue: C Group만 사용 (Selenium 전용)
    """
    # 기존 작업 제거
    for job in scheduler.get_jobs():
        if job.id.startswith("task_"):
            scheduler.remove_job(job.id)

    if mode == "IN":
        # ═════════════════════════════════════════════════════════════
        # IN 모드: cron 절대 시간 동기화 (Broadcasting 00, 10, 20초)
        # ═════════════════════════════════════════════════════════════

        # A Group: investing (3초 전)
        scheduler.add_job(
            make_request_crawler_wrapper('investing', investing.crawl_and_save_investing_exchange_rates),
            CronTrigger(second='7,17,27,37,47,57', timezone=KST),
            id='task_investing',
            max_instances=1,
            misfire_grace_time=5
        )

        # B Group: kb, hana (5초 전, 10초 엇갈림)
        scheduler.add_job(
            make_request_crawler_wrapper('kb', kb.crawl_and_save_kb_bank_exchange_rates),
            CronTrigger(second='15,35,55', timezone=KST),
            id='task_kb',
            max_instances=1,
            misfire_grace_time=10
        )
        scheduler.add_job(
            make_request_crawler_wrapper('hana', hana.crawl_and_save_hana_bank_exchange_rates),  # 하이브리드 (내부 폴백)
            CronTrigger(second='5,25,45', timezone=KST),
            id='task_hana',
            max_instances=1,
            misfire_grace_time=10
        )

        # B Group: woori, bs, citi (7초 전, 20초씩 엇갈림)
        scheduler.add_job(
            make_request_crawler_wrapper('woori', woori.crawl_and_save_woori_bank_exchange_rates),  # 하이브리드 (내부 폴백)
            CronTrigger(minute='*', second='13', timezone=KST),
            id='task_woori',
            max_instances=1,
            misfire_grace_time=30
        )
        scheduler.add_job(
            make_request_crawler_wrapper('bs', bs.crawl_and_save_bs_bank_exchange_rates),  # 하이브리드 (내부 폴백)
            CronTrigger(minute='*', second='33', timezone=KST),
            id='task_bs',
            max_instances=1,
            misfire_grace_time=30
        )
        scheduler.add_job(
            make_request_crawler_wrapper('citi', citi.crawl_and_save_citi_bank_exchange_rates),
            CronTrigger(minute='*', second='53', timezone=KST),
            id='task_citi',
            max_instances=1,
            misfire_grace_time=30
        )

        # C Group: Selenium (Queue 순차 처리, Broadcasting 독립)
        scheduler.add_job(
            make_selenium_job_wrapper('shinhan'),
            IntervalTrigger(seconds=38.3, timezone=KST),
            id='task_shinhan',
            max_instances=1,
            misfire_grace_time=25
        )
        scheduler.add_job(
            make_selenium_job_wrapper('ibk'),  # 하이브리드 (내부 시간대 체크)
            IntervalTrigger(seconds=55.5, timezone=KST),
            id='task_ibk',
            max_instances=1,
            misfire_grace_time=25
        )
        scheduler.add_job(
            make_selenium_job_wrapper('nh'),
            IntervalTrigger(seconds=90, timezone=KST),
            id='task_nh',
            max_instances=1,
            misfire_grace_time=60
        )
        scheduler.add_job(
            make_selenium_job_wrapper('sc'),
            IntervalTrigger(seconds=150, timezone=KST),
            id='task_sc',
            max_instances=1,
            misfire_grace_time=90
        )

    else:  # OUT 모드
        # ═════════════════════════════════════════════════════════════
        # OUT 모드: cron 시간 단위 (완전 분산, 동시 실행 0개)
        # ═════════════════════════════════════════════════════════════

        # A Group: investing (10분마다)
        scheduler.add_job(
            make_request_crawler_wrapper('investing', investing.crawl_and_save_investing_exchange_rates),
            CronTrigger(minute='7,17,27,37,47,57', second='0', timezone=KST),
            id='task_investing',
            max_instances=1,
            misfire_grace_time=300
        )

        # B Group: kb, hana (10분마다)
        scheduler.add_job(
            make_request_crawler_wrapper('kb', kb.crawl_and_save_kb_bank_exchange_rates),
            CronTrigger(minute='5,15,25,35,45,55', second='0', timezone=KST),
            id='task_kb',
            max_instances=1,
            misfire_grace_time=300
        )
        scheduler.add_job(
            make_request_crawler_wrapper('hana', hana.crawl_and_save_hana_bank_exchange_rates),
            CronTrigger(minute='0,10,20,30,40,50', second='0', timezone=KST),
            id='task_hana',
            max_instances=1,
            misfire_grace_time=300
        )

        # B Group: woori, bs, citi (60분마다)
        scheduler.add_job(
            make_request_crawler_wrapper('woori', woori.crawl_and_save_woori_bank_exchange_rates),
            CronTrigger(minute='13', second='0', timezone=KST),
            id='task_woori',
            max_instances=1,
            misfire_grace_time=1800
        )
        scheduler.add_job(
            make_request_crawler_wrapper('bs', bs.crawl_and_save_bs_bank_exchange_rates),
            CronTrigger(minute='33', second='0', timezone=KST),
            id='task_bs',
            max_instances=1,
            misfire_grace_time=1800
        )
        scheduler.add_job(
            make_request_crawler_wrapper('citi', citi.crawl_and_save_citi_bank_exchange_rates),
            CronTrigger(minute='53', second='0', timezone=KST),
            id='task_citi',
            max_instances=1,
            misfire_grace_time=1800
        )

        # C Group: Selenium (60분마다, 완전 분산)
        scheduler.add_job(
            make_selenium_job_wrapper('shinhan'),
            CronTrigger(minute='23', second='0', timezone=KST),
            id='task_shinhan',
            max_instances=1,
            misfire_grace_time=1800
        )
        scheduler.add_job(
            make_selenium_job_wrapper('ibk'),
            CronTrigger(minute='43', second='0', timezone=KST),
            id='task_ibk',
            max_instances=1,
            misfire_grace_time=1800
        )
        scheduler.add_job(
            make_selenium_job_wrapper('nh'),
            CronTrigger(minute='3', second='0', timezone=KST),
            id='task_nh',
            max_instances=1,
            misfire_grace_time=1800
        )
        scheduler.add_job(
            make_selenium_job_wrapper('sc'),
            CronTrigger(minute='36', second='0', timezone=KST),
            id='task_sc',
            max_instances=1,
            misfire_grace_time=1800
        )

    logger.info(
        f"=== {mode} 모드로 전환됨 ===",
        extra={
            "mode": mode,
            "total_jobs": len(scheduler.get_jobs())
        }
    )

def control_job():
    """현재 시간대에 따라 모드 전환"""
    global current_mode
    now = datetime.now(KST)
    new_mode = "IN" if is_in_time(now) else "OUT"

    if new_mode != current_mode:
        switch_jobs(new_mode)
        current_mode = new_mode

def cleanup_old_bank_data():
    """10일 이상 지난 은행 환율 데이터 삭제"""
    db = SessionLocal()
    try:
        deleted_count = crud.delete_old_bank_data(db=db, days=10)
        logger.info("🧹 은행 데이터 정리 완료", extra={"deleted_count": deleted_count, "vacuum_executed": deleted_count >= 1000})

        if deleted_count >= 1000:
            logger.info("🗜️ VACUUM 실행 완료")

    except Exception as e:
        db.rollback()
        logger.error("❌ 데이터 정리 실패", exc_info=True)
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
        - Worker Task를 cancel하고 재시작하면 Queue는 유지되고 Worker만 재생성됨
        - 작업 시작 시 heartbeat 업데이트 → 60초 임계값 (cleanup과 동일)
        - Worker 재시작 시 cleanup을 무조건 호출 (794번 줄 이중 안전장치)
        - subprocess 기반이므로 proc.kill()로 Chrome 포함 전체 프로세스 강제 종료 가능
    """
    global selenium_worker_task, selenium_worker_last_heartbeat, selenium_worker_current_job, selenium_queue

    try:
        if selenium_worker_task is None or selenium_queue is None:
            return

        # 헬스체크: 마지막 heartbeat로부터 경과 시간 확인
        elapsed = time.time() - selenium_worker_last_heartbeat
        max_job_time = 60  # 60초 (cleanup과 동일한 임계값)

        if elapsed > max_job_time:
            current_job = selenium_worker_current_job or "unknown"
            logger.error(
                f"🚨 Selenium Worker 멈춤 감지: {current_job} 작업이 {int(elapsed)}초 동안 완료 안됨 (최대: {max_job_time}초)",
                extra={
                    "stuck_job": current_job,
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
            # 정상 상태
            if selenium_worker_current_job:
                logger.debug(
                    f"💓 Worker 정상: [{selenium_worker_current_job}] 처리 중 ({int(elapsed)}초)",
                    extra={"current_job": selenium_worker_current_job, "elapsed": int(elapsed)}
                )
            else:
                logger.debug(f"💓 Worker 정상: 대기 중")

    except Exception as e:
        logger.error("❌ Worker 헬스체크 실패", exc_info=True)


def start_scheduler():
    # ═════════════════════════════════════════════════════════════
    # WebSocket Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (정확한 시간)
    # ═════════════════════════════════════════════════════════════
    # [2025-11-12] ADR-009 전제조건 구현: 크롤러 동기화 기준 시간
    # - IN 모드 크롤러들은 Broadcasting 기준으로 스케줄링됨
    # - A Group: 3초 전 (07,17,27,37,47,57초)
    # - B Group: 5초 또는 7초 전
    # - C Group: Broadcasting 독립 (interval)
    # ─────────────────────────────────────────────────────────────
    # Note: AsyncIOScheduler는 async 함수를 직접 등록 가능
    from app.main import broadcast_rates_once  # 순환 import 방지 (함수 내부 import)

    scheduler.add_job(
        broadcast_rates_once,  # async 함수 직접 등록
        CronTrigger(second='0,10,20,30,40,50', timezone=KST),
        id="websocket_broadcast",
        max_instances=1,
        misfire_grace_time=5
    )

    # 제어 작업: 매시 0분 1초 모드 확인 (모드 전환은 정시에만 발생)
    scheduler.add_job(
        control_job,
        CronTrigger(minute='0', second='1', timezone=KST),
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

    # 로그 파일 정리: 매일 새벽 4시
    scheduler.add_job(cleanup_old_log_files, CronTrigger(hour=4, minute=0, timezone=KST), id="cleanup_old_log_files")

    # 은행 데이터 정리: 매일 새벽 3시
    scheduler.add_job(cleanup_old_bank_data, CronTrigger(hour=3, minute=0, timezone=KST), id="cleanup_old_bank_data")

    # 시작 시 즉시 모드 판별 및 등록
    control_job()

    # 스케줄러 시작
    scheduler.start()
