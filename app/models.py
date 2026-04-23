# app/models.py

# 표준 라이브러리
from datetime import datetime, timezone as dt_timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Index

# 로컬 애플리케이션
from app.database import Base


def get_utc_now():
    """UTC 기준 현재 시각 (naive datetime)."""
    return datetime.now(dt_timezone.utc).replace(tzinfo=None)


class InvestingExchangeRate(Base):
    __tablename__ = 'investing_exchange_rates'

    id = Column(Integer, primary_key=True)
    currency = Column(String, index=True)
    rate = Column(Float)
    timestamp = Column(DateTime, default=get_utc_now)


class BankExchangeRate(Base):
    __tablename__ = 'bank_exchange_rates'

    id = Column(Integer, primary_key=True)
    bank = Column(String, index=True)
    currency = Column(String, index=True)
    rate = Column(Float)
    timestamp = Column(DateTime, default=get_utc_now)

    __table_args__ = (
        Index('ix_bank_currency_timestamp', 'bank', 'currency', 'timestamp'),
    )


class MarketIndexRate(Base):
    """시장 지수 데이터 (DXY 등)"""
    __tablename__ = "market_index_rates"

    id = Column(Integer, primary_key=True)
    instrument = Column(String, nullable=False)  # 'dxy' (향후 다른 지수 확장 가능)
    source = Column(String, nullable=False)  # 'investing' | 'yahoo'
    rate = Column(Float, nullable=False)
    timestamp = Column(DateTime, nullable=False, default=get_utc_now)
    granularity = Column(String, nullable=False, default="realtime")  # 'realtime' | 'hourly' | 'daily'

    __table_args__ = (
        Index('ix_market_index_instrument_ts', 'instrument', 'timestamp'),
        Index('ix_market_index_granularity', 'instrument', 'granularity', 'timestamp'),
        Index('uq_market_index', 'instrument', 'source', 'timestamp', 'granularity', unique=True),
    )


class CrawlerConfig(Base):
    """크롤러 활성화/비활성화 설정 (Phase 1.8)"""
    __tablename__ = "crawler_config"

    id = Column(Integer, primary_key=True)
    crawler_name = Column(String, unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime, default=get_utc_now, onupdate=get_utc_now)


# ============================================================
# Phase 2: Firebase Auth + FCM 알림 테이블
# ============================================================

class UserDevice(Base):
    """사용자 기기 정보 (FCM Device Token)"""
    __tablename__ = "user_devices"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)  # Firebase Auth user_id
    device_token = Column(String, nullable=False, unique=True)  # FCM Device Token (전역 유니크)
    platform = Column(String, nullable=False)  # 'ios' or 'android'
    created_at = Column(DateTime, default=get_utc_now)
    updated_at = Column(DateTime, default=get_utc_now, onupdate=get_utc_now)

    # Note: device_token이 전역 유니크이므로 복합 인덱스는 조회 최적화용으로만 유지


class NotificationSetting(Base):
    """알림 설정"""
    __tablename__ = "notification_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    bank = Column(String, nullable=False)  # 'hana', 'kb', etc.
    currency = Column(String, nullable=False)  # 'usd-krw', etc.
    condition = Column(String, nullable=False)  # 'above', 'below'
    threshold = Column(Float, nullable=False)
    enabled = Column(Boolean, default=True)
    # 멱등성 필드 (중복 알림 방지)
    triggered = Column(Boolean, default=False)
    last_notified_at = Column(DateTime, nullable=True)
    last_notified_rate = Column(Float, nullable=True)
    created_at = Column(DateTime, default=get_utc_now)
    updated_at = Column(DateTime, default=get_utc_now, onupdate=get_utc_now)

    __table_args__ = (
        Index('ix_notification_settings_bank_currency', 'bank', 'currency'),
    )


class NotificationLog(Base):
    """알림 발송 히스토리"""
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    setting_id = Column(Integer, nullable=True)  # NotificationSetting.id 참조
    bank = Column(String, nullable=False)
    currency = Column(String, nullable=False)
    rate = Column(Float, nullable=False)
    success = Column(Boolean, default=True)
    error_message = Column(String, nullable=True)
    sent_at = Column(DateTime, default=get_utc_now)


# ============================================================
# USDT Phase 1: Source 기반 새 데이터 모델
# ============================================================
# - 기존 bank/investing 세계와 분리
# - DB 내부: source + asset
# - 레거시 API 응답 시에만 bank + currency로 변환 (어댑터 레이어)

class SourceRate(Base):
    """Source 기반 실시간 시세 (크립토 거래소 등).

    Phase 1: 업비트/빗썸/코인원/고팍스/코빗 USDT/KRW
    Phase 2 예정: KRX 미국달러선물
    """
    __tablename__ = "source_rates"

    id = Column(Integer, primary_key=True)
    source = Column(String, nullable=False)   # 'upbit', 'bithumb', etc.
    asset = Column(String, nullable=False)    # 'usdt-krw', 'usd-krw-futures'
    rate = Column(Float, nullable=False)
    timestamp = Column(DateTime, nullable=False, default=get_utc_now)

    __table_args__ = (
        Index('ix_source_rates_source_asset_ts', 'source', 'asset', 'timestamp'),
    )


class SourceNotificationSetting(Base):
    """Source 기반 단일 소스 알림 설정.

    기존 NotificationSetting(bank + currency)과 별도로 유지한다.
    """
    __tablename__ = "source_notification_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False)
    asset = Column(String, nullable=False)
    condition = Column(String, nullable=False)   # 'above', 'below'
    threshold = Column(Float, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    triggered = Column(Boolean, default=False, nullable=False)
    last_notified_at = Column(DateTime, nullable=True)
    last_notified_rate = Column(Float, nullable=True)
    created_at = Column(DateTime, default=get_utc_now, nullable=False)
    updated_at = Column(DateTime, default=get_utc_now, onupdate=get_utc_now, nullable=False)

    __table_args__ = (
        Index('ix_source_notification_settings_source_asset', 'source', 'asset'),
    )


class SourceNotificationLog(Base):
    """Source 기반 알림 발송 히스토리 (운영 추적용)."""
    __tablename__ = "source_notification_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    setting_id = Column(Integer, nullable=True)   # SourceNotificationSetting.id (설정 삭제 후에도 로그 유지)
    source = Column(String, nullable=False)
    asset = Column(String, nullable=False)
    condition = Column(String, nullable=False)
    threshold = Column(Float, nullable=False)
    triggered_rate = Column(Float, nullable=False)
    success = Column(Boolean, default=True, nullable=False)
    error_message = Column(String, nullable=True)
    sent_at = Column(DateTime, default=get_utc_now, nullable=False)
