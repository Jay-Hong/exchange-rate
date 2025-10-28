# app/scheduler.py

# 표준 라이브러리
import logging
from datetime import datetime

# 서드파티 라이브러리
from apscheduler.schedulers.background import BackgroundScheduler
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
from app import crud
from app.database import SessionLocal

# 로거 설정
logger = logging.getLogger("exchange_rate.scheduler")

# 한국 시간대 스케줄러 인스턴스 생성
KST = timezone('Asia/Seoul')
scheduler = BackgroundScheduler(timezone=KST)

# === 은행별 기본 주기(초) 정의 ===
# 순서: investing → kb → hana → shinhan → woori → ibk → nh → sc → bs → citi
# (index.html DEFAULT_BANK_ORDER와 일관성 유지)
BANK_TASKS = [
    ("investing", investing.crawl_and_save_investing_exchange_rates, 4.9),
    ("kb", kb.crawl_and_save_kb_bank_exchange_rates, 7.9),
    ("hana", hana.crawl_and_save_hana_bank_exchange_rates, 7.3),
    ("shinhan", shinhan.crawl_and_save_shinhan_bank_exchange_rates, 31),
    ("woori", woori.crawl_and_save_woori_bank_exchange_rates, 23.3),
    ("ibk", ibk.crawl_and_save_ibk_bank_exchange_rates, 29.1),
    ("nh", nh.crawl_and_save_nh_bank_exchange_rates, 32.7),
    ("sc", sc.crawl_and_save_sc_bank_exchange_rates, 33.3),
    ("bs", bs.crawl_and_save_bs_bank_exchange_rates, 27.7),
    ("citi", citi.crawl_and_save_citi_bank_exchange_rates, 28.5),
]

# 현재 모드 상태 저장
current_mode = None

def is_in_time(now: datetime) -> bool:
    """월요일 04:00 ~ 토요일 07:59 구간인지 판별"""
    weekday = now.weekday()  # 월=0, 화=1 ... 일=6
    hour = now.hour

    return (
        (weekday == 0 and hour >= 4) or
        (weekday in [1, 2, 3, 4]) or
        (weekday == 5 and hour < 8)
    )

def switch_jobs(mode: str):
    """모드에 따라 작업 재등록"""
    # 기존 작업 제거
    for job in scheduler.get_jobs():
        if job.id.startswith("task_"):
            scheduler.remove_job(job.id)

    # 새 작업 등록
    for name, func, base_interval in BANK_TASKS:
        interval = base_interval if mode == "IN" else base_interval * 10
        scheduler.add_job(
            func,
            IntervalTrigger(seconds=interval, timezone=KST),
            id=f"task_{name}",
            max_instances=1,          # 중복 실행 방지 (명시적 표시)
            misfire_grace_time=70     # 70초 이상 지연 시 건너뛰기 (2회 주기 여유)
        )
    logger.info(f"=== {mode} 모드로 전환됨 ===", extra={"mode": mode, "jobs_count": len(scheduler.get_jobs())})

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


def start_scheduler():
    # 제어 작업: 1분마다 모드 확인
    scheduler.add_job(control_job, IntervalTrigger(minutes=1, timezone=KST), id="control_job")

    # 로그 파일 정리: 매일 새벽 4시
    scheduler.add_job(cleanup_old_log_files, CronTrigger(hour=4, minute=0, timezone=KST), id="cleanup_old_log_files")

    # 은행 데이터 정리: 매일 새벽 3시
    scheduler.add_job(cleanup_old_bank_data, CronTrigger(hour=3, minute=0, timezone=KST), id="cleanup_old_bank_data")

    # 시작 시 즉시 모드 판별 및 등록
    control_job()

    # 스케줄러 시작
    scheduler.start()
