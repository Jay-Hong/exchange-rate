# app/scheduler.py

# 표준 라이브러리
import asyncio
import logging
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
from app.crawlers import shinhan
from app.crawlers import woori
from app.crawlers import ibk
from app.crawlers import nh
from app.crawlers import sc
from app.crawlers import bs
from app.crawlers import citi
from app.crawlers.constants import SELENIUM_PRIORITY_MAP, SELENIUM_TIMEOUT_MAP
from app import crud
from app.database import SessionLocal

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
# - 우선순위: 빠른 크롤러(hana) → 느린 크롤러(shinhan)
# - 타임아웃: 각 크롤러별 타임아웃 설정 (30~90초)
# - 재시도: 실패 시 낮은 우선순위로 1회 재시도
# ─────────────────────────────────────────────────────────────
selenium_queue: asyncio.PriorityQueue = None
selenium_worker_task: asyncio.Task = None

# ═════════════════════════════════════════════════════════════
# 크롤러 그룹 정의 (Request vs. Selenium)
# ═════════════════════════════════════════════════════════════
# Group A: Request 기반 (경량, 동시 실행 가능)
# - investing, kb, woori, bs, citi
# - 기존 APScheduler 방식 유지
#
# Group B: Selenium 기반 (무거움, Queue 순차 처리)
# - hana, shinhan, ibk, nh, sc
# - AsyncIO Queue로 1개씩 순차 실행 (Semaphore 경합 제거)
# ─────────────────────────────────────────────────────────────
REQUEST_BASED_TASKS = [
    ("investing", investing.crawl_and_save_investing_exchange_rates, 4.9),
    ("kb", kb.crawl_and_save_kb_bank_exchange_rates, 7.9),
    ("woori", woori.crawl_and_save_woori_bank_exchange_rates, 23.3),
    ("bs", bs.crawl_and_save_bs_bank_exchange_rates, 27.7),
    ("citi", citi.crawl_and_save_citi_bank_exchange_rates, 28.5),
]

SELENIUM_BASED_TASKS = [
    ("hana", hana.crawl_and_save_hana_bank_exchange_rates, 7.3),
    ("shinhan", shinhan.crawl_and_save_shinhan_bank_exchange_rates, 31),
    ("ibk", ibk.crawl_and_save_ibk_bank_exchange_rates, 29.1),
    ("nh", nh.crawl_and_save_nh_bank_exchange_rates, 32.7),
    ("sc", sc.crawl_and_save_sc_bank_exchange_rates, 33.3),
]

# 현재 모드 상태 저장
current_mode = None


# ═════════════════════════════════════════════════════════════
# 타임아웃 래퍼 (느린 크롤러 격리)
# ═════════════════════════════════════════════════════════════
async def execute_with_timeout(job_func: Callable, bank_name: str) -> bool:
    """
    타임아웃 제어 래퍼 함수

    Args:
        job_func: 크롤러 함수
        bank_name: 은행 이름

    Returns:
        bool: 성공 시 True, 실패/타임아웃 시 False
    """
    timeout = SELENIUM_TIMEOUT_MAP.get(bank_name, 60)

    try:
        start_time = time.time()
        logger.info(f"⚡ [{bank_name}] Selenium 크롤링 시작 (타임아웃: {timeout}초)")

        # asyncio.wait_for로 타임아웃 제어
        await asyncio.wait_for(
            asyncio.to_thread(job_func),
            timeout=timeout
        )

        elapsed = time.time() - start_time
        logger.info(f"✅ [{bank_name}] 완료 ({elapsed:.2f}초)")
        return True

    except asyncio.TimeoutError:
        logger.warning(
            f"⏱️ [{bank_name}] 타임아웃 ({timeout}초 초과) - 다음 작업으로 진행",
            extra={"bank": bank_name, "timeout": timeout}
        )
        return False

    except Exception as e:
        logger.exception(f"❌ [{bank_name}] 실행 실패", extra={"error": str(e)})
        return False


# ═════════════════════════════════════════════════════════════
# AsyncIO PriorityQueue Worker (Selenium 크롤러 순차 실행)
# ═════════════════════════════════════════════════════════════
async def selenium_job_executor():
    """우선순위 Queue Worker (타임아웃 + 재시도 로직)"""
    global selenium_queue
    logger.info("🔧 Selenium Priority Queue Worker 시작")

    while True:
        try:
            # PriorityQueue에서 작업 가져오기 (blocking)
            # 튜플: (priority, timestamp, job_func, bank_name, is_retry)
            priority, timestamp, job_func, bank_name, is_retry = await selenium_queue.get()

            logger.info(
                f"🔄 [{bank_name}] Queue 처리 시작 (우선순위: {priority}, 재시도: {is_retry})"
            )

            # 타임아웃 포함 실행
            success = await execute_with_timeout(job_func, bank_name)

            # 실패 시 재시도 (최대 1회)
            if not success and not is_retry:
                retry_priority = priority + 1000  # 낮은 우선순위로 재시도
                await selenium_queue.put((
                    retry_priority,
                    time.time(),
                    job_func,
                    bank_name,
                    True  # 재시도 플래그
                ))
                logger.info(f"🔄 [{bank_name}] 재시도 Queue 추가 (우선순위: {retry_priority})")

            selenium_queue.task_done()

        except asyncio.CancelledError:
            logger.info("🛑 Selenium Priority Queue Worker 중단")
            break
        except Exception as e:
            logger.exception("Selenium Queue Worker 오류", extra={"error": str(e)})


def enqueue_selenium_job(job_func: Callable, bank_name: str):
    """
    Selenium 작업을 우선순위 Queue에 non-blocking 방식으로 추가

    Notes:
        - put_nowait() 사용으로 APScheduler event loop blocking 방지
        - Queue 포화 시 조용히 skip (다음 스케줄에서 재시도)
    """
    global selenium_queue

    priority = SELENIUM_PRIORITY_MAP.get(bank_name, 999)  # 기본값: 낮은 우선순위

    # PriorityQueue는 튜플의 첫 번째 요소로 정렬
    # (priority, timestamp, job_func, bank_name, is_retry)
    item = (
        priority,
        time.time(),  # 동일 우선순위 내에서 FIFO 보장
        job_func,
        bank_name,
        False  # 첫 실행
    )

    try:
        # Non-blocking put - Queue 가득 차면 즉시 예외 발생
        selenium_queue.put_nowait(item)
        logger.debug(
            f"📥 [{bank_name}] Priority Queue 추가 (우선순위: {priority}, 대기: {selenium_queue.qsize()}/50)"
        )
    except asyncio.QueueFull:
        # Queue 포화 시 조용히 skip
        # 다음 스케줄(7.3~33.3초 후)에서 자동으로 재시도됨
        logger.warning(
            f"⚠️ [{bank_name}] Queue 포화로 skip (size: {selenium_queue.qsize()}/50) - 다음 스케줄에서 재시도",
            extra={"bank": bank_name, "queue_size": selenium_queue.qsize()}
        )


def init_selenium_queue():
    """FastAPI 시작 시 Priority Queue 초기화"""
    global selenium_queue, selenium_worker_task

    # asyncio.Queue → asyncio.PriorityQueue 변경
    # maxsize: 재시도까지 고려 + Queue 포화 방지를 위해 50으로 증가
    # 최악 시나리오: 120초 동안 약 30개 추가 가능 → 여유를 두고 50 설정
    selenium_queue = asyncio.PriorityQueue(maxsize=50)

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

def make_selenium_job_wrapper(bank_name: str, job_func: Callable):
    """
    각 Selenium 크롤러마다 고유한 wrapper 함수 생성 (클로저 버그 해결)

    Args:
        bank_name: 은행 이름
        job_func: 크롤러 함수

    Returns:
        고유한 wrapper 함수 객체 (APScheduler가 구별 가능)

    Notes:
        - __name__, __qualname__ 설정으로 APScheduler가 각 job을 구별
        - for loop 안에서 직접 wrapper 정의 시 모든 wrapper가 같은 이름을 가짐
        - enqueue는 동기 non-blocking이므로 즉시 반환 (event loop blocking 방지)
    """
    def wrapper():
        enqueue_selenium_job(job_func, bank_name)

    # APScheduler가 함수를 구별할 수 있도록 고유 이름 설정
    wrapper.__name__ = f"selenium_wrapper_{bank_name}"
    wrapper.__qualname__ = wrapper.__name__
    return wrapper


def switch_jobs(mode: str):
    """모드에 따라 작업 재등록 (Request vs. Selenium 분리)"""
    # 기존 작업 제거
    for job in scheduler.get_jobs():
        if job.id.startswith("task_"):
            scheduler.remove_job(job.id)

    # Group A: Request 기반 크롤러 (기존 방식)
    for name, func, base_interval in REQUEST_BASED_TASKS:
        interval = base_interval if mode == "IN" else base_interval * 10
        scheduler.add_job(
            func,
            IntervalTrigger(seconds=interval, timezone=KST),
            id=f"task_{name}",
            max_instances=1,          # 중복 실행 방지
            misfire_grace_time=25     # 25초 이상 지연 시 건너뛰기
        )

    # Group B: Selenium 기반 크롤러 (Queue 방식)
    for name, func, base_interval in SELENIUM_BASED_TASKS:
        interval = base_interval if mode == "IN" else base_interval * 10

        scheduler.add_job(
            make_selenium_job_wrapper(name, func),  # Factory 함수로 고유 wrapper 생성
            IntervalTrigger(seconds=interval, timezone=KST),
            id=f"task_{name}",
            max_instances=1,          # 중복 실행 방지
            misfire_grace_time=25     # 25초 이상 지연 시 건너뛰기
        )

    logger.info(
        f"=== {mode} 모드로 전환됨 ===",
        extra={
            "mode": mode,
            "request_tasks": len(REQUEST_BASED_TASKS),
            "selenium_tasks": len(SELENIUM_BASED_TASKS),
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
    """좀비 Chrome 프로세스 정리 (3분 이상 실행된 프로세스 강제 종료)"""
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
    Selenium Priority Queue 상태 모니터링 (10초마다)

    Notes:
        - Queue 사용률 추적
        - 80% 이상 포화 시 경고
        - 성능 튜닝 및 문제 진단에 활용
    """
    global selenium_queue

    if selenium_queue is None:
        return

    try:
        size = selenium_queue.qsize()
        max_size = 50
        usage_percent = (size / max_size) * 100

        # 정상 상태: DEBUG 레벨
        if size < 40:  # 80% 미만
            logger.debug(
                f"📊 [Queue] size={size}/{max_size}, usage={usage_percent:.1f}%"
            )
        # 경고 상태: WARNING 레벨
        else:
            logger.warning(
                f"⚠️ [Queue] 포화 임박: {size}/{max_size} ({usage_percent:.1f}%)",
                extra={"queue_size": size, "usage_percent": usage_percent}
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


def start_scheduler():
    # 제어 작업: 1분마다 모드 확인
    scheduler.add_job(control_job, IntervalTrigger(minutes=1, timezone=KST), id="control_job")

    # Queue 상태 모니터링: 10초마다 (포화 감지)
    scheduler.add_job(
        report_queue_status,
        IntervalTrigger(seconds=10, timezone=KST),
        id="queue_status",
        coalesce=True,  # Misfire 시 밀린 실행을 1번으로 합치기
        max_instances=1  # 동시 실행 방지
    )

    # 좀비 Chrome 프로세스 정리: 1분마다 (메모리 누수 방지)
    from app.crawlers.constants import CHROME_CLEANUP_INTERVAL_MINUTES
    scheduler.add_job(
        cleanup_zombie_chrome_processes,
        IntervalTrigger(minutes=CHROME_CLEANUP_INTERVAL_MINUTES, timezone=KST),
        id="cleanup_zombie_chrome",
        coalesce=True,  # Misfire 시 밀린 실행을 1번으로 합치기
        max_instances=1,  # 동시 실행 방지
        misfire_grace_time=180  # Misfire 허용 시간: 3분 (놓쳐도 실행)
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
