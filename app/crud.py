# app/crud.py

# 표준 라이브러리
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

# 서드파티 라이브러리
from pytz import timezone
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.sql import func, and_

# 로컬 애플리케이션
from app import models

# 로거 설정
logger = logging.getLogger("exchange_rate.db")

# 알림 메시지용 한글 매핑
BANK_NAMES_KR = {
    "investing": "인베스팅",
    "kb": "국민은행",
    "hana": "하나은행",
    "shinhan": "신한은행",
    "woori": "우리은행",
    "ibk": "IBK기업은행",
    "nh": "NH농협",
    "sc": "SC제일은행",
    "bs": "부산은행",
    "citi": "씨티은행",
}

CURRENCY_NAMES_KR = {
    "usd-krw": "달러",
    "jpy-krw": "엔화",
    "eur-krw": "유로",
}


def format_threshold(value: float) -> str:
    """목표값 포맷: 소수점 이하 불필요한 0 제거 (1475.00 → 1475, 1475.50 → 1475.5)"""
    formatted = f"{value:.2f}"
    if '.' in formatted:
        formatted = formatted.rstrip('0').rstrip('.')
    return formatted


def insert_bank_rates_into_db(db: Session, current_rates: dict, bank_name: str) -> int:
    """
    은행 환율 DB 저장 + 알림 조건 체크

    Returns:
        변경된 레코드 개수 (0: 변경 없음, N: N개 변경됨)

    Notes:
        환율 변경 시 알림 조건을 체크하고 FCM 발송 (process_rate_alerts)
    """
    logger.info(f"|                {bank_name} 환율                 |", extra={"bank": bank_name})
    new_records_count = 0
    changed_rates = []  # 알림 처리용 변경 환율 수집

    for pair, current_rate in current_rates.items():
        if current_rate is None:
            continue

        last_record = (
            db.query(models.BankExchangeRate)
            .filter(and_(models.BankExchangeRate.bank == bank_name, models.BankExchangeRate.currency == pair))
            .order_by(models.BankExchangeRate.id.desc())
            .first()
        )

        should_save = False

        if last_record is None:
            should_save = True
            logger.info(f"⭐️ [신규] {pair}: {current_rate}", extra={"pair": pair, "rate": current_rate, "type": "new", "bank": bank_name})
        elif last_record.rate != current_rate:
            should_save = True
            logger.info(f"⚡️ [변경] {pair}: {last_record.rate} → {current_rate}", extra={"pair": pair, "old_rate": last_record.rate, "new_rate": current_rate, "change": current_rate - last_record.rate, "type": "change", "bank": bank_name})
        else:
            logger.debug(f"📼 [유지] {pair}: {current_rate}", extra={"pair": pair, "rate": current_rate, "type": "unchanged", "bank": bank_name})

        if should_save:
            new_entry = models.BankExchangeRate(
                bank = bank_name,
                currency = pair,
                rate = current_rate,
                timestamp = models.get_kst_now()
            )
            db.add(new_entry)
            new_records_count += 1
            logger.debug("✅ DB에 새 레코드 저장됨")

            # 알림 처리용 변경 정보 수집
            changed_rates.append({
                "bank": bank_name,
                "currency": pair,
                "rate": current_rate
            })

    if new_records_count > 0:
        db.commit()
        logger.info(f"🎉 총 {new_records_count}개 {bank_name}은행의 새로운 환율 데이터 저장 완료", extra={"count": new_records_count, "bank": bank_name})

        # 알림 조건 체크 및 FCM 발송 (실패해도 환율 저장에 영향 없음)
        if changed_rates:
            try:
                sent_count = process_rate_alerts(db, changed_rates)
                if sent_count > 0:
                    logger.info(f"🔔 {sent_count}건 알림 발송 완료", extra={"bank": bank_name, "sent": sent_count})
            except Exception as e:
                logger.exception("알림 처리 중 예외 발생", extra={"bank": bank_name, "error": str(e)})

    elif current_rates:
        logger.debug(f"✋ 모든 {bank_name}은행 환율 확인 완료 - 변경사항 없음", extra={"bank": bank_name})
    else:
        logger.warning(f"🈚️ {bank_name}은행 환율 데이터 없음", extra={"bank": bank_name, "source": "crud"})

    return new_records_count


def insert_investing_rates_into_db(db: Session, current_rates: dict) -> int:
    """
    Investing 환율 DB 저장 + 알림 조건 체크

    Returns:
        변경된 레코드 개수 (0: 변경 없음, N: N개 변경됨)

    Notes:
        환율 변경 시 알림 조건을 체크하고 FCM 발송 (process_rate_alerts)
        bank 값은 "investing"으로 통일
    """
    logger.info("|                Investing 환율                 |", extra={"bank": "investing"})
    new_records_count = 0
    changed_rates = []  # 알림 처리용 변경 환율 수집

    for pair, current_rate in current_rates.items():
        if current_rate is None:
            continue

        last_record = (
            db.query(models.InvestingExchangeRate)
            .filter(models.InvestingExchangeRate.currency == pair)
            .order_by(models.InvestingExchangeRate.id.desc())
            .first()
        )

        should_save = False

        if last_record is None:
            should_save = True
            logger.info(f"⭐️ [신규] {pair}: {current_rate:.2f}", extra={"pair": pair, "rate": current_rate, "type": "new", "bank": "investing"})
        elif last_record.rate != current_rate:
            should_save = True
            logger.info(f"⚡️ [변경] {pair}: {last_record.rate:.2f} → {current_rate:.2f}", extra={"pair": pair, "old_rate": last_record.rate, "new_rate": current_rate, "change": current_rate - last_record.rate, "type": "change", "bank": "investing"})
        else:
            logger.debug(f"📼 [유지] {pair}: {current_rate:.2f}", extra={"pair": pair, "rate": current_rate, "type": "unchanged", "bank": "investing"})

        if should_save:
            new_entry = models.InvestingExchangeRate(
                currency = pair,
                rate = current_rate,
                timestamp = models.get_kst_now()
            )
            db.add(new_entry)
            new_records_count += 1
            logger.debug("✅ DB에 새 레코드 저장됨")

            # 알림 처리용 변경 정보 수집 (bank="investing")
            changed_rates.append({
                "bank": "investing",
                "currency": pair,
                "rate": current_rate
            })

    if new_records_count > 0:
        db.commit()
        logger.info(f"🎉 총 {new_records_count}개 Investing의 새로운 환율 데이터 저장 완료", extra={"count": new_records_count, "bank": "investing"})

        # 알림 조건 체크 및 FCM 발송 (실패해도 환율 저장에 영향 없음)
        if changed_rates:
            try:
                sent_count = process_rate_alerts(db, changed_rates)
                if sent_count > 0:
                    logger.info(f"🔔 {sent_count}건 알림 발송 완료", extra={"bank": "investing", "sent": sent_count})
            except Exception as e:
                logger.exception("알림 처리 중 예외 발생", extra={"bank": "investing", "error": str(e)})

    elif current_rates:
        logger.debug("✋ 모든 Investing 환율 확인 완료 - 변경사항 없음", extra={"bank": "investing"})
    else:
        logger.warning("🈚️ Investing 환율 데이터 없음", extra={"bank": "investing", "source": "crud"})

    return new_records_count


def select_a_latest_investing_rate_from_db(db: Session, pair: str) -> Optional[Dict[str, Any]]:
    """
    특정 통화쌍의 Investing.com 최신 환율 조회
    
    Args:
        db: 데이터베이스 세션
        pair: 통화쌍 (예: 'usd-krw')
        
    Returns:
        환율 정보 딕셔너리 (데이터가 없으면 None 반환)
    """
    record = (
        db.query(models.InvestingExchangeRate)
        .filter(models.InvestingExchangeRate.currency == pair)
        .order_by(models.InvestingExchangeRate.id.desc())
        .first()
    )
    
    if record:
        return {
            "currency": record.currency,
            "bank": "investing",
            "rate": record.rate,
            "timestamp": record.timestamp.astimezone().isoformat()  # ISO 8601 형식
        }
    else:
        return None


def select_latest_bank_rates_from_db(db: Session, pair: str) -> List[Dict[str, Any]]:
    """
    특정 통화쌍의 모든 은행 최신 환율 조회 (환율순으로 정렬)
    
    Args:
        db: 데이터베이스 세션
        pair: 통화쌍 (예: 'usd-krw')
        
    Returns:
        각 은행의 최신 pair 환율 데이터 리스트 (환율 낮은 순으로 정렬)
    """
    
    # 각 은행별로 해당 통화쌍의 최신 레코드 ID를 찾는 서브쿼리
    subquery = (
        db.query(
            models.BankExchangeRate.bank,
            func.max(models.BankExchangeRate.id).label("max_id")
        )
        .filter(models.BankExchangeRate.currency == pair)
        .group_by(models.BankExchangeRate.bank)
        .subquery()
    )

    # 실제 환율 데이터를 조회
    records = (
        db.query(models.BankExchangeRate)
        .join(subquery, models.BankExchangeRate.id == subquery.c.max_id)
        # .order_by(models.BankExchangeRate.bank)  # 은행명으로 정렬
        .order_by(models.BankExchangeRate.rate)  # 환율순으로 정렬 (낮은 환율부터)
        .all()
    )

    return [
        {
            "currency": record.currency,
            "bank": record.bank,
            "rate": record.rate,
            "timestamp": record.timestamp.astimezone().isoformat()  # ISO 8601 형식
        }
        for record in records
    ]


def get_all_rates_flat(db: Session) -> List[Dict[str, Any]]:
    """
    모든 환율을 플랫 배열 구조로 반환하는 함수 (모바일/AJAX용)
    
    Args:
        db: 데이터베이스 세션
        
    Returns:
        모든 환율 데이터가 플랫 배열로 구성된 리스트 (ISO 8601 타임스탬프 포함)
    """
    all_rates = []
    
    # 사용 가능한 통화쌍 조회
    pairs = get_available_currency_pairs(db)
    if not pairs:
        pairs = ["usd-krw", "jpy-krw", "eur-krw"]
    
    for pair in pairs:
        # Investing 데이터 추가
        investing_data = select_a_latest_investing_rate_from_db(db=db, pair=pair)
        if investing_data:
            all_rates.append(investing_data)
        
        # 은행 데이터 추가
        bank_data = select_latest_bank_rates_from_db(db=db, pair=pair)
        all_rates.extend(bank_data)
    
    return all_rates


def get_rates_by_currency(db: Session, currency: str) -> List[Dict[str, Any]]:
    """
    특정 통화쌍의 모든 환율을 플랫 배열로 반환
    
    Args:
        db: 데이터베이스 세션
        currency: 통화쌍 (예: 'usd-krw')
        
    Returns:
        해당 통화쌍의 모든 환율 데이터 (ISO 8601 타임스탬프 포함)
    """
    rates = []
    
    # Investing 데이터
    investing_data = select_a_latest_investing_rate_from_db(db=db, pair=currency)
    if investing_data:
        rates.append(investing_data)
    
    # 은행 데이터
    bank_data = select_latest_bank_rates_from_db(db=db, pair=currency)
    rates.extend(bank_data)
    
    return rates


# 추가 유틸리티 함수들

def get_available_currency_pairs(db: Session) -> List[str]:
    """
    데이터베이스에 저장된 모든 통화쌍 조회
    
    Args:
        db: 데이터베이스 세션
        
    Returns:
        통화쌍 리스트
    """
    # Investing 테이블에서 통화쌍 조회
    investing_pairs = db.query(models.InvestingExchangeRate.currency).distinct().all()
    investing_pairs = [pair[0] for pair in investing_pairs]
    
    # 은행 테이블에서 통화쌍 조회
    bank_pairs = db.query(models.BankExchangeRate.currency).distinct().all()
    bank_pairs = [pair[0] for pair in bank_pairs]
    
    # 중복 제거 및 정렬
    all_pairs = list(set(investing_pairs + bank_pairs))
    all_pairs.sort()
    
    return all_pairs


def get_available_banks(db: Session) -> List[str]:
    """
    데이터베이스에 저장된 모든 은행명 조회

    Args:
        db: 데이터베이스 세션

    Returns:
        은행명 리스트
    """
    banks = db.query(models.BankExchangeRate.bank).distinct().all()
    return sorted([bank[0] for bank in banks])


def delete_old_bank_data(db: Session, days: int = 10) -> int:
    """
    지정된 일수 이상 지난 은행 환율 데이터 삭제

    Args:
        db: 데이터베이스 세션
        days: 보관할 일수 (기본값: 10일)

    Returns:
        삭제된 레코드 개수
    """
    KST = timezone('Asia/Seoul')

    cutoff_date = datetime.now(KST) - timedelta(days=days)

    deleted_count = db.query(models.BankExchangeRate).filter(
        models.BankExchangeRate.timestamp < cutoff_date
    ).delete()

    db.commit()

    # 1000개 이상 삭제 시 VACUUM 실행
    if deleted_count >= 1000:
        db.execute(text("VACUUM"))
        db.commit()

    return deleted_count


def has_changes_since(db: Session, since_time: Optional[datetime]) -> bool:
    """
    마지막 시간 이후 변경된 레코드가 있는지 확인 (초경량 쿼리)

    Args:
        db: 데이터베이스 세션
        since_time: 마지막 체크 시간 (None이면 항상 True 반환)

    Returns:
        변경사항 있으면 True, 없으면 False
    """
    if since_time is None:
        return True

    # 은행 환율 변경 체크
    bank_count = db.query(models.BankExchangeRate).filter(
        models.BankExchangeRate.timestamp > since_time
    ).count()

    # 인베스팅 환율 변경 체크
    investing_count = db.query(models.InvestingExchangeRate).filter(
        models.InvestingExchangeRate.timestamp > since_time
    ).count()

    return (bank_count + investing_count) > 0





# SELECT * FROM investing_exchange_rates;

# 모든 데이터 삭제 (SQLite, AUTOINCREMENT 초기화 X)
# DELETE FROM table_name;
# UPDATE SQLITE_SEQUENCE SET seq = 0 WHERE name = 'table_name';

# 모든 데이터 삭제 (빠르고 효율적) - 사이즈클때
# TRUNCATE TABLE table_name;


# 테이블 구조, 데이터 모두 삭제
# DROP TABLE table_name;
# ⬇️
# 재 생성
# CREATE TABLE IF NOT EXISTS investing_exchange_rates (
#     id INTEGER PRIMARY KEY AUTOINCREMENT,
#     currency TEXT NOT NULL,
#     rate REAL,
#     timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
# )               
# """);

# 대량의 테이블 데이터 삭제 후, DB 재 정렬
# VACUUM;


# ═════════════════════════════════════════════════════════════
# 크롤러 설정 관리 (Phase 1.8)
# ═════════════════════════════════════════════════════════════

def init_crawler_config(db: Session) -> None:
    """
    crawler_config 테이블 초기화 (서버 시작 시 1회 실행)

    Notes:
        - 모든 크롤러를 enabled=True로 초기화
        - 이미 존재하는 경우 skip (멱등성 보장)
    """
    # 모든 크롤러 이름 정의
    CRAWLER_NAMES = [
        'investing', 'kb', 'hana', 'shinhan', 'woori',
        'ibk', 'nh', 'sc', 'bs', 'citi'
    ]

    for crawler_name in CRAWLER_NAMES:
        # 이미 존재하는지 확인
        existing = db.query(models.CrawlerConfig).filter(
            models.CrawlerConfig.crawler_name == crawler_name
        ).first()

        if not existing:
            # 신규 생성 (기본값: enabled=True)
            config = models.CrawlerConfig(
                crawler_name=crawler_name,
                enabled=True,
                updated_at=models.get_kst_now()
            )
            db.add(config)
            logger.info(f"✅ 크롤러 설정 초기화: {crawler_name} (enabled=True)")

    db.commit()
    logger.info("✅ crawler_config 테이블 초기화 완료")


def get_all_crawler_configs(db: Session) -> List[Dict[str, Any]]:
    """
    모든 크롤러 설정 조회 (관리자 API용)

    Returns:
        [
            {
                "crawler_name": "investing",
                "enabled": True,
                "updated_at": "2025-11-27T10:30:00+09:00"
            },
            ...
        ]
    """
    configs = db.query(models.CrawlerConfig).order_by(models.CrawlerConfig.crawler_name).all()

    return [
        {
            "crawler_name": config.crawler_name,
            "enabled": config.enabled,
            "updated_at": config.updated_at.astimezone().isoformat()
        }
        for config in configs
    ]


def update_crawler_config(db: Session, crawler_name: str, enabled: bool) -> bool:
    """
    크롤러 설정 업데이트 (토글 API용)

    Args:
        db: 데이터베이스 세션
        crawler_name: 크롤러 이름
        enabled: 활성화 상태

    Returns:
        성공 시 True, 실패 시 False

    Raises:
        ValueError: 존재하지 않는 크롤러 이름
    """
    config = db.query(models.CrawlerConfig).filter(
        models.CrawlerConfig.crawler_name == crawler_name
    ).first()

    if not config:
        raise ValueError(f"Invalid crawler name: {crawler_name}")

    config.enabled = enabled
    config.updated_at = models.get_kst_now()

    db.commit()

    action = "활성화" if enabled else "비활성화"
    logger.info(
        f"✅ 크롤러 설정 업데이트: {crawler_name} → {action}",
        extra={"crawler": crawler_name, "enabled": enabled}
    )

    return True


# ═════════════════════════════════════════════════════════════
# Phase 2: Firebase Auth + FCM 알림 CRUD
# ═════════════════════════════════════════════════════════════

def register_device(
    db: Session,
    user_id: str,
    device_token: str,
    platform: str
) -> models.UserDevice:
    """
    사용자 기기 등록 (FCM Device Token) - UPSERT 방식

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id
        device_token: FCM Device Token
        platform: 'ios' or 'android'

    Returns:
        UserDevice 객체

    Notes:
        - device_token은 전역 유니크 (한 토큰 = 한 사용자)
        - UPSERT: 토큰 존재 시 user_id/platform/updated_at 갱신
        - 다른 계정으로 로그인 시 자동으로 소유권 이전
    """
    # SQLite UPSERT용 import
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    try:
        # 소유권 이전 로깅용: 기존 소유자 확인
        existing = db.query(models.UserDevice).filter(
            models.UserDevice.device_token == device_token
        ).first()

        transferred_from = None
        if existing and existing.user_id != user_id:
            transferred_from = existing.user_id

        # UPSERT: INSERT OR UPDATE on device_token conflict
        now = models.get_kst_now()
        stmt = sqlite_insert(models.UserDevice).values(
            user_id=user_id,
            device_token=device_token,
            platform=platform,
            created_at=now,
            updated_at=now
        )

        stmt = stmt.on_conflict_do_update(
            index_elements=['device_token'],
            set_={
                'user_id': user_id,
                'platform': platform,
                'updated_at': now
            }
        )

        db.execute(stmt)
        db.commit()

        # 결과 조회
        device = db.query(models.UserDevice).filter(
            models.UserDevice.device_token == device_token
        ).first()

        # 로깅
        if transferred_from:
            logger.warning(
                "토큰 소유권 이전",
                extra={
                    "old_user": transferred_from[:8] + "...",
                    "new_user": user_id[:8] + "...",
                    "token": device_token[:20] + "..."
                }
            )
            logger.info(
                "기기 토큰 업데이트 (소유권 이전)",
                extra={"user_id": user_id[:8] + "...", "platform": platform}
            )
        elif existing:
            logger.info(
                "기기 토큰 업데이트",
                extra={"user_id": user_id[:8] + "...", "platform": platform}
            )
        else:
            logger.info(
                "새 기기 등록",
                extra={"user_id": user_id[:8] + "...", "platform": platform, "device_id": device.id}
            )

        return device

    except Exception as e:
        db.rollback()
        logger.error("기기 등록 실패", exc_info=True)
        raise


def delete_device(db: Session, user_id: str, device_token: str) -> bool:
    """
    사용자 기기 삭제

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id
        device_token: FCM Device Token

    Returns:
        삭제 성공 시 True, 해당 레코드 없으면 False
    """
    deleted = db.query(models.UserDevice).filter(
        models.UserDevice.user_id == user_id,
        models.UserDevice.device_token == device_token
    ).delete()

    db.commit()

    if deleted:
        logger.info(
            "기기 삭제",
            extra={"user_id": user_id[:8] + "...", "deleted": deleted}
        )
    return deleted > 0


def delete_devices_by_token(db: Session, device_token: str) -> int:
    """
    무효 토큰 삭제 (FCM 발송 실패 시 호출)

    Args:
        db: 데이터베이스 세션
        device_token: 무효화된 FCM Device Token

    Returns:
        삭제된 레코드 개수
    """
    deleted = db.query(models.UserDevice).filter(
        models.UserDevice.device_token == device_token
    ).delete()

    db.commit()

    if deleted:
        logger.warning(
            "무효 토큰 삭제",
            extra={"token": device_token[:20] + "...", "deleted": deleted}
        )
    return deleted


def get_devices_by_user(db: Session, user_id: str) -> List[models.UserDevice]:
    """
    사용자의 모든 기기 조회

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id

    Returns:
        UserDevice 객체 리스트
    """
    return db.query(models.UserDevice).filter(
        models.UserDevice.user_id == user_id
    ).all()


# ─────────────────────────────────────────────────────────────
# NotificationSetting CRUD
# ─────────────────────────────────────────────────────────────

def create_notification_setting(
    db: Session,
    user_id: str,
    bank: str,
    currency: str,
    condition: str,
    threshold: float
) -> models.NotificationSetting:
    """
    알림 설정 생성 (중복 방지)

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id
        bank: 은행 코드 (예: 'hana', 'kb')
        currency: 통화쌍 (예: 'usd-krw')
        condition: 'above' or 'below'
        threshold: 임계값

    Returns:
        NotificationSetting 객체

    Notes:
        - 같은 (user_id, bank, currency, condition, threshold) 조합이 있으면
          기존 설정을 활성화하고 반환 (중복 푸시 방지)
    """
    # 중복 체크: 같은 조건의 알림 설정이 있는지 확인
    existing = db.query(models.NotificationSetting).filter(
        models.NotificationSetting.user_id == user_id,
        models.NotificationSetting.bank == bank,
        models.NotificationSetting.currency == currency,
        models.NotificationSetting.condition == condition,
        models.NotificationSetting.threshold == threshold
    ).first()

    if existing:
        # 기존 설정 활성화 및 triggered 초기화 (재알림 가능하도록)
        existing.enabled = True
        existing.triggered = False
        existing.last_notified_at = None
        existing.last_notified_rate = None
        existing.updated_at = models.get_kst_now()
        db.commit()
        db.refresh(existing)
        logger.info(
            "알림 설정 재활성화 (중복)",
            extra={
                "user_id": user_id[:8] + "...",
                "setting_id": existing.id,
                "bank": bank,
                "currency": currency
            }
        )
        return existing

    # 새 설정 생성
    setting = models.NotificationSetting(
        user_id=user_id,
        bank=bank,
        currency=currency,
        condition=condition,
        threshold=threshold,
        enabled=True,
        triggered=False
    )
    db.add(setting)
    db.commit()
    db.refresh(setting)

    logger.info(
        "알림 설정 생성",
        extra={
            "user_id": user_id[:8] + "...",
            "bank": bank,
            "currency": currency,
            "condition": condition,
            "threshold": threshold,
            "setting_id": setting.id
        }
    )
    return setting


def get_notification_settings(db: Session, user_id: str) -> List[models.NotificationSetting]:
    """
    사용자의 모든 알림 설정 조회

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id

    Returns:
        NotificationSetting 객체 리스트
    """
    return db.query(models.NotificationSetting).filter(
        models.NotificationSetting.user_id == user_id
    ).order_by(models.NotificationSetting.created_at.desc()).all()


def get_notification_setting_by_id(
    db: Session,
    setting_id: int,
    user_id: str
) -> Optional[models.NotificationSetting]:
    """
    특정 알림 설정 조회 (소유권 검증 포함)

    Args:
        db: 데이터베이스 세션
        setting_id: NotificationSetting ID
        user_id: Firebase Auth user_id (소유권 검증용)

    Returns:
        NotificationSetting 객체 (없거나 권한 없으면 None)
    """
    return db.query(models.NotificationSetting).filter(
        models.NotificationSetting.id == setting_id,
        models.NotificationSetting.user_id == user_id
    ).first()


def update_notification_setting(
    db: Session,
    setting_id: int,
    user_id: str,
    bank: Optional[str] = None,
    condition: Optional[str] = None,
    threshold: Optional[float] = None,
    enabled: Optional[bool] = None
) -> Optional[models.NotificationSetting]:
    """
    알림 설정 수정 (PUT - 부분 업데이트)

    Args:
        db: 데이터베이스 세션
        setting_id: NotificationSetting ID
        user_id: Firebase Auth user_id (소유권 검증용)
        bank: 은행 코드 (선택)
        condition: 조건 (선택)
        threshold: 임계값 (선택)
        enabled: 활성화 상태 (선택)

    Returns:
        수정된 NotificationSetting 객체 (없거나 권한 없으면 None)

    Notes:
        - bank, condition, threshold 중 실제로 값이 변경되면 triggered 초기화
        - enabled: False→True 전환 시에도 triggered 초기화
        - is_enabled는 기존 값 유지 (자동 활성화 안 함)
    """
    setting = get_notification_setting_by_id(db, setting_id, user_id)
    if not setting:
        return None

    # 재알림 조건: 실제 값이 변경될 때만 triggered 초기화
    should_reset_triggered = False

    # bank 변경 감지 및 적용
    if bank is not None:
        if bank != setting.bank:
            should_reset_triggered = True
            logger.debug(f"bank 변경: {setting.bank} → {bank}")
        setting.bank = bank

    # condition 변경 감지 및 적용
    if condition is not None:
        if condition != setting.condition:
            should_reset_triggered = True
            logger.debug(f"condition 변경: {setting.condition} → {condition}")
        setting.condition = condition

    # threshold 변경 감지 및 적용
    if threshold is not None:
        if threshold != setting.threshold:
            should_reset_triggered = True
            logger.debug(f"threshold 변경: {setting.threshold} → {threshold}")
        setting.threshold = threshold

    # enabled 변경 (토글)
    if enabled is not None:
        # False→True 전환 시 재알림 가능하도록
        if enabled and not setting.enabled:
            should_reset_triggered = True
        setting.enabled = enabled

    # triggered 초기화 (재알림 가능) - last_notified_* 도 함께 초기화
    if should_reset_triggered:
        setting.triggered = False
        setting.last_notified_at = None
        setting.last_notified_rate = None
        logger.info(
            "알림 설정 재활성화 (조건 변경)",
            extra={"setting_id": setting_id, "triggered_reset": True}
        )

    setting.updated_at = models.get_kst_now()
    db.commit()
    db.refresh(setting)

    logger.info(
        "알림 설정 수정",
        extra={
            "setting_id": setting_id,
            "bank": setting.bank,
            "condition": setting.condition,
            "threshold": setting.threshold,
            "enabled": setting.enabled,
            "triggered": setting.triggered
        }
    )
    return setting


def delete_notification_setting(db: Session, setting_id: int, user_id: str) -> bool:
    """
    알림 설정 삭제

    Args:
        db: 데이터베이스 세션
        setting_id: NotificationSetting ID
        user_id: Firebase Auth user_id (소유권 검증용)

    Returns:
        삭제 성공 시 True, 해당 레코드 없으면 False
    """
    deleted = db.query(models.NotificationSetting).filter(
        models.NotificationSetting.id == setting_id,
        models.NotificationSetting.user_id == user_id
    ).delete()

    db.commit()

    if deleted:
        logger.info(
            "알림 설정 삭제",
            extra={"setting_id": setting_id, "user_id": user_id[:8] + "..."}
        )
    return deleted > 0


# ─────────────────────────────────────────────────────────────
# FCM 알림 발송용 쿼리
# ─────────────────────────────────────────────────────────────

def get_triggered_settings_for_rate(
    db: Session,
    bank: str,
    currency: str,
    rate: float
) -> List[Dict[str, Any]]:
    """
    특정 환율에 대해 알림 조건이 충족된 설정 목록 조회

    Args:
        db: 데이터베이스 세션
        bank: 은행 코드
        currency: 통화쌍
        rate: 현재 환율

    Returns:
        [
            {
                "setting": NotificationSetting,
                "devices": [UserDevice, ...],
                "user_id": str
            },
            ...
        ]

    Notes:
        - N+1 쿼리 최적화: 2개 쿼리로 모든 데이터 조회
          1. 조건 충족 settings 조회
          2. 해당 user_ids의 모든 devices 일괄 조회
    """
    # 1. 활성화된 알림 설정 조회
    settings = db.query(models.NotificationSetting).filter(
        models.NotificationSetting.bank == bank,
        models.NotificationSetting.currency == currency,
        models.NotificationSetting.enabled == True,
        models.NotificationSetting.triggered == False  # 아직 발송 안 된 것만
    ).all()

    # 2. 조건 충족된 설정 필터링 + user_id 수집
    matched_settings = []
    user_ids = set()

    for setting in settings:
        condition_met = False
        if setting.condition == "above" and rate >= setting.threshold:
            condition_met = True
        elif setting.condition == "below" and rate <= setting.threshold:
            condition_met = True

        if condition_met:
            matched_settings.append(setting)
            user_ids.add(setting.user_id)

    if not matched_settings:
        return []

    # 3. 모든 user_ids의 devices 일괄 조회 (N+1 → 2 쿼리로 최적화)
    all_devices = db.query(models.UserDevice).filter(
        models.UserDevice.user_id.in_(user_ids)
    ).all()

    # 4. user_id → devices 매핑 생성
    devices_by_user: Dict[str, List[models.UserDevice]] = {}
    for device in all_devices:
        if device.user_id not in devices_by_user:
            devices_by_user[device.user_id] = []
        devices_by_user[device.user_id].append(device)

    # 5. 결과 생성 (devices 있는 것만)
    results = []
    for setting in matched_settings:
        devices = devices_by_user.get(setting.user_id, [])
        if devices:
            results.append({
                "setting": setting,
                "devices": devices,
                "user_id": setting.user_id
            })

    return results


def mark_setting_triggered(
    db: Session,
    setting_id: int,
    rate: float
) -> None:
    """
    알림 설정을 '발송됨'으로 표시 (멱등성 보장)

    Args:
        db: 데이터베이스 세션
        setting_id: NotificationSetting ID
        rate: 발송 시점의 환율
    """
    setting = db.query(models.NotificationSetting).filter(
        models.NotificationSetting.id == setting_id
    ).first()

    if setting:
        setting.triggered = True
        setting.enabled = False  # 알림 발송 후 자동 비활성화 (1회성 알림)
        setting.last_notified_at = models.get_kst_now()
        setting.last_notified_rate = rate
        db.commit()

        logger.info(
            "알림 발송 완료 (자동 비활성화)",
            extra={"setting_id": setting_id, "rate": rate, "enabled": False}
        )


def create_notification_log(
    db: Session,
    user_id: str,
    setting_id: Optional[int],
    bank: str,
    currency: str,
    rate: float,
    success: bool,
    error_message: Optional[str] = None
) -> models.NotificationLog:
    """
    알림 발송 히스토리 기록

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id
        setting_id: NotificationSetting ID (선택)
        bank: 은행 코드
        currency: 통화쌍
        rate: 발송 시점의 환율
        success: 발송 성공 여부
        error_message: 에러 메시지 (선택)

    Returns:
        NotificationLog 객체
    """
    log = models.NotificationLog(
        user_id=user_id,
        setting_id=setting_id,
        bank=bank,
        currency=currency,
        rate=rate,
        success=success,
        error_message=error_message
    )
    db.add(log)
    db.commit()

    return log


# ═══════════════════════════════════════════════════════════════════════════════
# 환율 변경 시 알림 처리 (크롤러에서 호출)
# ═══════════════════════════════════════════════════════════════════════════════

def process_rate_alerts(
    db: Session,
    changed_rates: List[Dict[str, Any]]
) -> int:
    """
    변경된 환율에 대해 알림 조건 체크 및 FCM 발송

    크롤러의 환율 저장 함수에서 호출됨.
    알림 발송 실패가 환율 저장에 영향을 주지 않도록 예외 처리.

    Args:
        db: 데이터베이스 세션
        changed_rates: 변경된 환율 목록
            [{"bank": "kb", "currency": "usd-krw", "rate": 1400.0}, ...]

    Returns:
        발송된 알림 수
    """
    # 순환 참조 방지를 위해 함수 내부에서 import
    from app.notifications.fcm import send_fcm_multicast_sync, is_firebase_initialized

    if not changed_rates:
        return 0

    # Firebase 초기화 안 됐으면 스킵
    if not is_firebase_initialized():
        logger.debug("Firebase 미초기화 - 알림 스킵")
        return 0

    sent_count = 0
    all_failed_tokens = []  # 일괄 삭제용 무효 토큰 수집

    for rate_info in changed_rates:
        bank = rate_info["bank"]
        currency = rate_info["currency"]
        rate = rate_info["rate"]

        try:
            # 조건 충족 설정 조회 (이미 N+1 최적화됨)
            triggered_items = get_triggered_settings_for_rate(db, bank, currency, rate)

            if not triggered_items:
                continue

            for item in triggered_items:
                setting = item["setting"]
                devices = item["devices"]
                user_id = item["user_id"]

                # 알림 메시지 생성
                bank_kr = BANK_NAMES_KR.get(bank, bank.upper())
                currency_kr = CURRENCY_NAMES_KR.get(currency, currency.upper())
                icon = "📈" if setting.condition == "above" else "📉"

                title = f"{icon}  {bank_kr}  {currency_kr}"

                condition_arrow = "↑" if setting.condition == "above" else "↓"
                condition_text = "이상" if setting.condition == "above" else "이하"
                threshold_str = format_threshold(setting.threshold)
                rate_str = f"{rate:.2f}"

                body = f"[ {threshold_str} {condition_arrow}{condition_text} 도달 ]   {rate_str}"

                # data payload (앱에서 처리용)
                data = {
                    "type": "rate_alert",
                    "bank": bank,
                    "currency": currency,
                    "rate": str(rate),
                    "threshold": str(setting.threshold),
                    "condition": setting.condition,
                    "setting_id": str(setting.id),
                }

                # FCM 발송
                tokens = [d.device_token for d in devices]
                result = send_fcm_multicast_sync(tokens, title, body, data)

                # 발송 결과 처리
                if result["success_count"] > 0:
                    # triggered 플래그 설정
                    mark_setting_triggered(db, setting.id, rate)
                    sent_count += 1

                    # 발송 로그 기록
                    create_notification_log(
                        db=db,
                        user_id=user_id,
                        setting_id=setting.id,
                        bank=bank,
                        currency=currency,
                        rate=rate,
                        success=True
                    )

                    logger.info(
                        "🔔 환율 알림 발송",
                        extra={
                            "event": "rate_alert_sent",
                            "bank": bank,
                            "currency": currency,
                            "rate": rate,
                            "threshold": setting.threshold,
                            "condition": setting.condition,
                            "user_id": user_id[:8] + "...",
                            "devices": len(devices),
                            "success": result["success_count"],
                        }
                    )

                # 무효 토큰 수집 (나중에 일괄 삭제)
                if result["failed_tokens"]:
                    all_failed_tokens.extend(result["failed_tokens"])

        except Exception as e:
            # 알림 처리 실패가 환율 저장에 영향 주지 않도록
            logger.exception(
                "알림 처리 실패",
                extra={
                    "bank": bank,
                    "currency": currency,
                    "rate": rate,
                    "error": str(e)
                }
            )
            continue

    # 무효 토큰 일괄 삭제 (N번 commit → 1번 commit으로 최적화)
    if all_failed_tokens:
        try:
            deleted_count = db.query(models.UserDevice).filter(
                models.UserDevice.device_token.in_(all_failed_tokens)
            ).delete(synchronize_session=False)
            db.commit()
            logger.info(
                "무효 토큰 일괄 삭제",
                extra={"count": deleted_count, "tokens": len(all_failed_tokens)}
            )
        except Exception as e:
            logger.exception("무효 토큰 삭제 실패", extra={"error": str(e)})

    return sent_count