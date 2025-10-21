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


def insert_bank_rates_into_db(db: Session, current_rates: dict, bank_name: str) -> int:
    """
    은행 환율 DB 저장

    Returns:
        변경된 레코드 개수 (0: 변경 없음, N: N개 변경됨)
    """
    logger.info(f"|                {bank_name} 환율                 |", extra={"bank": bank_name})
    new_records_count = 0
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

    if new_records_count > 0:
        db.commit()
        logger.info(f"🎉 총 {new_records_count}개 {bank_name}은행의 새로운 환율 데이터 저장 완료", extra={"count": new_records_count, "bank": bank_name})
    elif current_rates:
        logger.debug(f"✋ 모든 {bank_name}은행 환율 확인 완료 - 변경사항 없음", extra={"bank": bank_name})
    else:
        logger.warning(f"🈚️ {bank_name}은행 환율 데이터 없음", extra={"bank": bank_name, "source": "crud"})

    return new_records_count


def insert_investing_rates_into_db(db: Session, current_rates: dict) -> int:
    """
    Investing 환율 DB 저장

    Returns:
        변경된 레코드 개수 (0: 변경 없음, N: N개 변경됨)
    """
    logger.info("|                Investing 환율                 |", extra={"bank": "investing"})
    new_records_count = 0
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

    if new_records_count > 0:
        db.commit()
        logger.info(f"🎉 총 {new_records_count}개 Investing의 새로운 환율 데이터 저장 완료", extra={"count": new_records_count, "bank": "investing"})
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