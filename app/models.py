# app/models.py

# 표준 라이브러리
from datetime import datetime

# 서드파티 라이브러리
from pytz import timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Index

# 로컬 애플리케이션
from app.database import Base


def get_kst_now():
    kst = timezone('Asia/Seoul')
    return datetime.now(kst)


class InvestingExchangeRate(Base):
    __tablename__ = 'investing_exchange_rates'

    id = Column(Integer, primary_key=True, index=True)
    currency = Column(String, index=True)
    rate = Column(Float)
    timestamp = Column(DateTime, default=get_kst_now)


class BankExchangeRate(Base):
    __tablename__ = 'bank_exchange_rates'

    id = Column(Integer, primary_key=True, index=True)
    bank = Column(String, index=True)
    currency = Column(String, index=True)
    rate = Column(Float)
    timestamp = Column(DateTime, default=get_kst_now)

    __table_args__ = (
        Index('ix_bank_currency_timestamp', 'bank', 'currency', 'timestamp'),
    )


class CrawlerConfig(Base):
    """크롤러 활성화/비활성화 설정 (Phase 1.8)"""
    __tablename__ = "crawler_config"

    id = Column(Integer, primary_key=True, index=True)
    crawler_name = Column(String, unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime, default=get_kst_now, onupdate=get_kst_now)


# ============================================================
# Phase 2: Firebase Auth + FCM 알림 테이블
# ============================================================

class UserDevice(Base):
    """사용자 기기 정보 (FCM Device Token)"""
    __tablename__ = "user_devices"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, nullable=False, index=True)  # Firebase Auth user_id
    device_token = Column(String, nullable=False, unique=True)  # FCM Device Token (전역 유니크)
    platform = Column(String, nullable=False)  # 'ios' or 'android'
    created_at = Column(DateTime, default=get_kst_now)
    updated_at = Column(DateTime, default=get_kst_now, onupdate=get_kst_now)

    # Note: device_token이 전역 유니크이므로 복합 인덱스는 조회 최적화용으로만 유지


class NotificationSetting(Base):
    """알림 설정"""
    __tablename__ = "notification_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, nullable=False, index=True)
    bank = Column(String, nullable=False)  # 'hana', 'kb', etc.
    currency = Column(String, nullable=False)  # 'usd-krw', etc.
    condition = Column(String, nullable=False)  # 'greater_than', 'less_than'
    threshold = Column(Float, nullable=False)
    enabled = Column(Boolean, default=True)
    # 멱등성 필드 (중복 알림 방지)
    triggered = Column(Boolean, default=False)
    last_notified_at = Column(DateTime, nullable=True)
    last_notified_rate = Column(Float, nullable=True)
    created_at = Column(DateTime, default=get_kst_now)
    updated_at = Column(DateTime, default=get_kst_now, onupdate=get_kst_now)

    __table_args__ = (
        Index('ix_notification_settings_bank_currency', 'bank', 'currency'),
    )


class NotificationLog(Base):
    """알림 발송 히스토리"""
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, nullable=False, index=True)
    setting_id = Column(Integer, nullable=True)  # NotificationSetting.id 참조
    bank = Column(String, nullable=False)
    currency = Column(String, nullable=False)
    rate = Column(Float, nullable=False)
    success = Column(Boolean, default=True)
    error_message = Column(String, nullable=True)
    sent_at = Column(DateTime, default=get_kst_now)
